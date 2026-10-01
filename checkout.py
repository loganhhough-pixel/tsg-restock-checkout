"""
Auto-checkout for tcg-restock.

Drives a real browser (Playwright) with a persistent profile you log into once
(`python monitor.py --login URL`). Your saved address and card on the retailer's
site are what pay; this code never sees or stores payment details.

Each product lists its checkout as a short series of steps in config.yaml
(goto / click / fill / select / wait_for / check_price / expect). The step that
actually places the order is marked `final: true`; dry-run mode runs everything
up to that step and stops.

Speed: the browser starts with the monitor, keeps a warm tab parked on each
store (open connections, fresh cookies), skips images/fonts/trackers, and polls
for the next button every ~150ms instead of sleeping fixed amounts. On Shopify
the monitor hands over the in-stock variant ID, so checkout is one cart-link jump.

Only turn this on for retailers whose terms allow automated purchasing. It does
not solve CAPTCHAs, skip queues, or evade bot protection. If it sees any of that,
it stops, leaves the browser on that page, and alerts you to finish by hand.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from monitor import Notifier, Product

log = logging.getLogger("tcg-restock.checkout")
ORDERS_FILE = Path("orders.json")

ACTIONS = ("goto", "click", "fill", "select", "wait_for", "check_price", "expect")

# Visible challenge widgets / queue pages. Invisible, not-yet-triggered widgets
# are ignored because the check requires the element to be visible.
BLOCK_SELECTORS = (
    'iframe[src*="recaptcha/api2/bframe"]',
    'iframe[src*="recaptcha/enterprise/bframe"]',
    'iframe[src*="hcaptcha"][src*="challenge"]',
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="captcha-delivery.com"]',   # DataDome
    '#px-captcha',                           # PerimeterX / HUMAN
    '.g-recaptcha',
    '.h-captcha',
)
BLOCK_TEXT = re.compile(
    r"verify (that )?you('re| are) (a )?human|are you a robot|press (and|&) hold"
    r"|you('re| are) (now )?in (the )?(line|queue)|waiting room"
    r"|access denied|unusual (activity|traffic)",
    re.I,
)
BLOCK_HOSTS = ("queue-it.net",)

POLL_MS = 150   # how often to look for the next button

# Loaded by nothing a checkout needs: blocked in the main page to cut load time.
# Cross-origin iframes (card fields, CAPTCHAs) are separate processes and are untouched.
BLOCKED_URLS = [
    "*.png*", "*.jpg*", "*.jpeg*", "*.gif*", "*.webp*", "*.avif*", "*.svg*", "*.ico*",
    "*.mp4*", "*.webm*", "*.woff*", "*.ttf*", "*.otf*",
    "*google-analytics.com*", "*googletagmanager.com*", "*doubleclick.net*",
    "*connect.facebook.net*", "*hotjar.com*", "*clarity.ms*", "*analytics.tiktok.com*",
    "*bat.bing.com*", "*static.klaviyo.com*", "*cdn.attn.tv*", "*attentivemobile.com*",
    "*privy.com*", "*cdn.segment.com*", "*snap.licdn.com*", "*sc-static.net*",
]

# One round trip: visible challenge widget? else the page text to scan.
BLOCK_JS = """(sels) => {
  for (const s of sels) for (const el of document.querySelectorAll(s)) {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    if (r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none')
      return {hit: true, text: ''};
  }
  return {hit: false, text: document.body ? document.body.innerText.slice(0, 5000) : ''};
}"""

# Visible, empty card-number field (in the page or a payment iframe) = no saved card.
CARD_FIELD = ('input[autocomplete="cc-number"], input[name="number"], input[name="cardnumber"], '
              'input[name="cardNumber"], input[placeholder*="card number" i]')

# Auto mode: how buttons are recognized (matched against the button's visible name).
ADD_RE = re.compile(r"^\s*(add to (cart|bag|basket)|add\b)", re.I)
ADD_SKIP = re.compile(r"wish|registry|list|notify|compare|gift", re.I)
CHECKOUT_RE = re.compile(r"^\s*((proceed|continue|go) to |secure )?check\s?out\b", re.I)
CONTINUE_RE = re.compile(r"^\s*(continue|next|review order|save and continue)\b", re.I)
CONTINUE_SKIP = re.compile(r"shopping|browsing|guest|with (paypal|google|apple|amazon|shop)", re.I)
PLACE_RE = re.compile(r"^\s*(place (your |my )?order|pay now|complete (order|purchase|payment)"
                      r"|submit order|confirm (and pay|order|purchase)|buy now|pay \$)", re.I)
# Express/wallet buttons open popups or third-party flows; never click them.
EXPRESS_SKIP = re.compile(r"paypal|shop ?pay|apple ?pay|google ?pay|g ?pay|amazon|klarna|afterpay"
                          r"|affirm|sezzle|venmo|link\b|guest", re.I)
CONFIRM_URL_RE = re.compile(r"thank|confirm|order-received|order-complete|/orders?/\w", re.I)
CONFIRM_TEXT_RE = re.compile(r"thank you for (your )?(order|purchase)|order (is |has been )?"
                             r"(confirmed|placed|received)|your order number|order (number|#|no\.)"
                             r"|confirmation (number|#)", re.I)

# First available variant of the Shopify product on this page.
SHOPIFY_VARIANT_JS = """async () => {
  const m = location.pathname.match(/\\/products\\/[^/?#]+/);
  if (!m) return null;
  const r = await fetch(m[0] + '.js', {credentials: 'same-origin'});
  if (!r.ok) return null;
  const j = await r.json();
  const want = new URLSearchParams(location.search).get('variant');
  const v = j.variants.find(v => v.available && (!want || String(v.id) === want))
         || j.variants.find(v => v.available);
  return v ? v.id : null;
}"""

# Largest money amount next to a "Total" label (not subtotal/savings). Taking the
# largest candidate keeps the max_total check conservative.
TOTAL_JS = """() => {
  const money = /[$\u00a3\u20ac]\\s?\\d[\\d,]*(?:\\.\\d{2})?/g;
  const label = /^\\s*(order |estimated |grand )?total\\b/i;
  const bad = /sub\\s?total|saving|discount|items?\\b/i;
  let best = null;
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 3 || !el.offsetParent) continue;
    const own = (el.innerText || '').trim();
    if (!label.test(own) || bad.test(own.slice(0, 40))) continue;
    let node = el, found = null;
    for (let i = 0; i < 4 && node && !found; i++, node = node.parentElement) {
      const vals = (node.innerText || '').match(money);
      if (vals && node.innerText.length < 300) found = vals[vals.length - 1];
    }
    if (found) {
      const v = parseFloat(found.replace(/[^\\d.]/g, ''));
      if (!isNaN(v) && (best === null || v > best)) best = v;
    }
  }
  return best;
}"""


@dataclass
class CheckoutSettings:
    enabled: bool = False
    dry_run: bool = True              # stop before the `final: true` step
    profile_dir: str = "browser-profile"
    channel: str | None = None        # "chrome" to use your installed Chrome instead of bundled Chromium
    headless: bool = False            # keep False so you can take over if it stops
    step_timeout: float = 20.0        # seconds per step (override per step with `timeout:`)
    max_orders_per_product: int = 1   # lifetime cap per product URL; edit orders.json to reset
    max_total: float | None = None    # default checkout-total cap for `checkout: auto` products
    screenshots_dir: str = "checkout-shots"
    block_assets: bool = True         # skip images/fonts/trackers in the checkout browser
    keep_warm: float = 240            # seconds between keep-alive pings on parked tabs (0 = off)


class Blocked(Exception):
    pass


class StepFailed(Exception):
    pass


@dataclass
class _Run:
    t0: float
    placed: bool = False
    where: str = ""
    trail: list = field(default_factory=list)

    def secs(self) -> float:
        return time.perf_counter() - self.t0


@dataclass
class Outcome:
    status: str          # ordered | unconfirmed | dry_run_ok | blocked | failed | skipped
    detail: str
    url: str = ""
    screenshot: str | None = None
    secs: float | None = None


# ---------- config ----------

def normalize_spec(spec) -> dict:
    """`checkout: auto` / `checkout: true` -> {"auto": True}."""
    if spec is True or (isinstance(spec, str) and spec.strip().lower() == "auto"):
        return {"auto": True}
    if isinstance(spec, dict):
        return spec
    raise ValueError("checkout must be `auto` or a block with `auto: true` / `steps`")


def validate_spec(product_url: str, spec, settings: CheckoutSettings) -> None:
    spec = normalize_spec(spec)
    if spec.get("auto"):
        if spec.get("max_total", settings.max_total) is None:
            raise ValueError("auto mode needs a max_total (on the product or under checkout:) "
                             "so it never buys without checking the price")
    else:
        build_steps(product_url, spec)


def build_steps(product_url: str, spec: dict) -> list[dict]:
    """Expand a product's `checkout:` block into a validated list of steps."""
    steps: list[dict] = []
    if variant := spec.get("shopify_variant"):
        # Shopify cart permalink: /cart/<variant>:<qty> adds to cart and jumps to checkout.
        u = urlparse(spec.get("store") or product_url)
        qty = int(spec.get("quantity", 1))
        steps.append({"goto": f"{u.scheme}://{u.netloc}/cart/{variant}:{qty}"})
    steps += [dict(s) for s in (spec.get("steps") or [])]

    if not steps:
        raise ValueError("checkout needs `steps` (and/or `shopify_variant`)")
    for i, s in enumerate(steps, 1):
        acts = [a for a in ACTIONS if a in s]
        if len(acts) != 1:
            raise ValueError(f"step {i}: needs exactly one of {', '.join(ACTIONS)}, got {sorted(s)}")
        if acts[0] in ("fill", "select") and "value" not in s:
            raise ValueError(f"step {i}: `{acts[0]}` needs a `value`")
        if acts[0] == "check_price" and "max" not in s:
            raise ValueError(f"step {i}: `check_price` needs a `max`")
    finals = [i for i, s in enumerate(steps) if s.get("final")]
    if len(finals) != 1:
        raise ValueError("mark exactly one step (the one that places the order) with `final: true`")
    return steps


def describe(s: dict) -> str:
    act = next(a for a in ACTIONS if a in s)
    return f"{act} {s[act]!r}"


def _origin(url: str) -> str:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}"


def _pick_variant(data: dict, want=None) -> dict | None:
    """First available Shopify variant (or the wanted one if it's available)."""
    vs = data.get("variants") or (data.get("product") or {}).get("variants") or []
    if want is not None:
        vs = [v for v in vs if str(v.get("id")) == str(want)]
    return next((v for v in vs if v.get("available", True)), None)


def _parse_money(text: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    return float(m.group()) if m else None


# ---------- runner ----------

class CheckoutRunner:
    def __init__(self, settings: CheckoutSettings, notifier: "Notifier | None" = None):
        self.s = settings
        self.notifier = notifier
        self.orders: dict[str, int] = (
            json.loads(ORDERS_FILE.read_text()) if ORDERS_FILE.exists() else {})
        self._lock = asyncio.Lock()   # one checkout at a time in the shared browser
        self._pw = None
        self._ctx = None
        self._parked: dict[str, object] = {}   # origin -> warm Page
        self._warming = False
        self._bg: set[asyncio.Task] = set()

    # browser lifecycle: launched once at startup so a restock doesn't wait on it
    async def start(self) -> None:
        if self._ctx is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise SystemExit("auto-checkout needs Playwright: pip install playwright && "
                             "playwright install chromium") from e
        self._pw = await async_playwright().start()
        self._ctx = await self._pw.chromium.launch_persistent_context(
            self.s.profile_dir, headless=self.s.headless, channel=self.s.channel)
        log.info("checkout browser ready (profile=%s, dry_run=%s)", self.s.profile_dir, self.s.dry_run)

    async def _new_page(self):
        page = await self._ctx.new_page()
        if self.s.block_assets:
            try:  # in-browser URL blocking: no per-request round trip to Python
                cdp = await self._ctx.new_cdp_session(page)
                await cdp.send("Network.enable")
                await cdp.send("Network.setBlockedURLs", {"urls": BLOCKED_URLS})
                page._tcg_cdp = cdp
            except Exception as e:
                log.debug("asset blocking unavailable: %s", e)
        return page

    async def _unblock(self, page) -> None:
        """Let a page load everything again (e.g. before handing it to you)."""
        cdp = getattr(page, "_tcg_cdp", None)
        if cdp is not None:
            try:
                await cdp.send("Network.setBlockedURLs", {"urls": []})
            except Exception:
                pass

    async def _park(self, origin: str) -> None:
        """Keep a tab open on the store so connections and cookies are warm."""
        old = self._parked.get(origin)
        if old is not None and not old.is_closed():
            return
        page = await self._new_page()
        self._parked[origin] = page
        try:
            await page.goto(origin + "/", wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            log.debug("warm-up of %s failed: %s", origin, e)

    async def warm(self, origins) -> None:
        self._warming = True
        await self.start()
        await asyncio.gather(*(self._park(o) for o in set(origins)), return_exceptions=True)
        log.info("checkout warm on %d store(s)", len(self._parked))

    async def keep_warm(self) -> None:
        """Tiny same-origin request now and then so idle connections don't close."""
        while self.s.keep_warm > 0:
            await asyncio.sleep(self.s.keep_warm)
            if self._lock.locked():
                continue
            for origin, page in list(self._parked.items()):
                try:
                    await page.evaluate("fetch('/robots.txt', {cache: 'no-store'})"
                                        ".then(r => r.status).catch(() => 0)")
                except Exception:
                    self._parked.pop(origin, None)
                    self._spawn(self._park(origin))

    async def _take_page(self, origin: str):
        page = self._parked.pop(origin, None)
        if page is None or page.is_closed():
            page = await self._new_page()
        return page

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def close(self) -> None:
        for t in list(self._bg):
            t.cancel()
        if self._ctx is not None:
            await self._ctx.close()
        if self._pw is not None:
            await self._pw.stop()
        self._ctx = self._pw = None

    def _ordered(self, key: str) -> int:
        return self.orders.get(key, 0)

    def _record_order(self, key: str) -> None:
        self.orders[key] = self._ordered(key) + 1
        self._save_orders()

    def _save_orders(self) -> None:
        ORDERS_FILE.write_text(json.dumps(self.orders, indent=2))

    async def run(self, p: "Product", dry_run: bool | None = None, notify: bool = True,
                  variant=None) -> Outcome:
        dry = self.s.dry_run if dry_run is None else dry_run
        key = p.url
        async with self._lock:
            if not dry and self._ordered(key) >= self.s.max_orders_per_product:
                out = Outcome("skipped", f"already ordered {self._ordered(key)}x (cap "
                              f"{self.s.max_orders_per_product}); edit {ORDERS_FILE} to reset")
                log.info("%s: checkout %s", p.name, out.detail)
                return out
            await self.start()
            out = await self._attempt(p, dry, variant)
        if self._warming:
            self._spawn(self._park(_origin(p.landing)))   # re-park for next time
        log.info("%s: checkout %s: %s", p.name, out.status, out.detail)
        if notify and self.notifier:
            self._spawn(self._notify(p, out))   # don't hold up the caller on the push
        return out

    async def _attempt(self, p: "Product", dry: bool, variant=None) -> Outcome:
        spec = normalize_spec(p.checkout)
        run = _Run(time.perf_counter())
        page = await self._take_page(_origin(p.landing))
        try:
            if spec.get("auto"):
                out = await self._generic(page, p, spec, dry, run, variant)
            else:
                out = await self._scripted(page, p, spec, dry, run)
            out.secs = round(run.secs(), 2)
            return await self._finish(page, p, out, close=True)
        except Exception as e:
            try:
                await self._check_block(page)   # a stuck step is often a challenge that popped up
                blocked = None
            except Blocked as b:
                blocked = b
            trail = f" [{' > '.join(run.trail)}]" if run.trail else ""
            if run.placed:
                out = Outcome("unconfirmed", "clicked the order button but couldn't confirm "
                              f"({e}). Check your email before doing anything else.", page.url)
            elif blocked or isinstance(e, Blocked):
                out = Outcome("blocked", f"{blocked or e} at {run.where}. Browser left on that page; "
                              "finish by hand.", page.url)
            else:
                out = Outcome("failed", f"{e}{trail}", page.url)
            if not self.s.headless:
                await self._unblock(page)   # you may be finishing this page by hand
            return await self._finish(page, p, out, close=self.s.headless)

    # ----- scripted mode: explicit steps from config -----

    async def _scripted(self, page, p: "Product", spec: dict, dry: bool, run: "_Run") -> Outcome:
        for i, s in enumerate(build_steps(p.url, spec), 1):
            run.where = f"step {i} ({describe(s)})"
            timeout_ms = float(s.get("timeout", self.s.step_timeout)) * 1000
            await self._check_block(page)
            if s.get("final") and dry:
                # Prove the order button is really there, then stop.
                if "click" in s:
                    await self._run_step(page, {"wait_for": s["click"]}, timeout_ms, run.where)
                return Outcome("dry_run_ok", f"reached the order step in {run.secs():.1f}s, "
                               "stopped before placing it", page.url)
            await self._run_step(page, s, timeout_ms, run.where)
            if s.get("final"):
                run.placed = True
                self._record_order(p.url)   # count it now so a failed confirm can't cause a rebuy
        await self._check_block(page)
        return Outcome("ordered", f"order placed in {run.secs():.1f}s", page.url)

    # ----- auto mode: works out the buttons on any site -----

    async def _generic(self, page, p: "Product", spec: dict, dry: bool, run: "_Run",
                       variant=None) -> Outcome:
        timeout = self.s.step_timeout
        max_total = spec.get("max_total", self.s.max_total)
        qty = int(spec.get("quantity", 1))
        variant = variant or spec.get("shopify_variant")

        if variant is None and getattr(p, "mode", None) == "shopify":
            run.where = "Shopify product JSON"
            variant = await self._shopify_variant(p)

        if variant is not None:
            await self._shopify_cart(page, _origin(p.landing), variant, qty, run)
        else:
            run.where = "product page"
            await page.goto(p.landing, wait_until="domcontentloaded", timeout=timeout * 1000)
            await self._check_block(page)
            if await page.evaluate("!!window.Shopify"):
                variant = await page.evaluate(SHOPIFY_VARIANT_JS)
                if variant is None:
                    raise StepFailed("Shopify product has no available variant (sold out again?)")
                await self._shopify_cart(page, _origin(page.url), variant, qty, run)
            else:
                await self._cart_to_checkout(page, p, qty, run)

        # Checkout: click through Continue buttons until a place-order button shows up.
        run.where = "checkout"
        place, place_name = None, None
        deadline = time.monotonic() + timeout * 3
        disabled_seen = None
        while time.monotonic() < deadline:
            await self._check_block(page)
            place, place_name, disabled = await self._find(page, PLACE_RE, EXPRESS_SKIP)
            disabled_seen = disabled_seen or disabled
            if place is not None:
                break
            cont, cont_name, _ = await self._find(page, CONTINUE_RE, CONTINUE_SKIP)
            if cont is not None:
                before = page.url
                handle = await cont.element_handle()
                await cont.click(timeout=timeout * 1000)
                run.trail.append(f"clicked {cont_name!r}")
                await self._settle(page, handle, cont_name, before)
            else:
                await page.wait_for_timeout(POLL_MS)
        if place is None:
            if disabled_seen:
                raise StepFailed(f"{disabled_seen!r} stayed disabled; the site probably needs info "
                                 "that isn't saved (address, card, CVV). Run --login and save it")
            raise StepFailed("couldn't find a place-order button on the checkout page")

        run.where = "order total"
        total = await page.evaluate(TOTAL_JS)
        if total is None:
            raise StepFailed("couldn't read the order total; not buying without checking it")
        if total > float(max_total):
            raise StepFailed(f"total ${total:.2f} is over max_total ${float(max_total):.2f}")
        run.trail.append(f"total ${total:.2f} <= ${float(max_total):.2f}")

        run.where = "payment info"
        if await self._card_field_empty(page):
            raise StepFailed("card number field is empty: no saved card for this store. "
                             "Run --login, save a card (or sign in to Shop Pay), and retry")

        if dry:
            run.trail.append(f"found {place_name!r} (not clicked)")
            return Outcome("dry_run_ok", f"{' > '.join(run.trail)} in {run.secs():.1f}s", page.url)

        run.where = f"placing order ({place_name!r})"
        before = page.url
        await place.click(timeout=timeout * 1000)
        run.placed = True
        self._record_order(p.url)
        run.trail.append(f"clicked {place_name!r}")

        run.where = "order confirmation"
        deadline = time.monotonic() + max(45, timeout)
        while time.monotonic() < deadline:
            if await self._confirmed(page, before):
                return Outcome("ordered", f"{' > '.join(run.trail)} in {run.secs():.1f}s", page.url)
            await self._check_block(page)
            await page.wait_for_timeout(200)
        raise StepFailed("no confirmation page within 45s")

    async def _shopify_variant(self, p: "Product"):
        url = getattr(p, "fetch_url", None) or p.url
        r = await self._ctx.request.get(url, timeout=self.s.step_timeout * 1000)
        if not r.ok:
            raise StepFailed(f"Shopify product JSON returned HTTP {r.status}")
        v = _pick_variant(await r.json(), getattr(p, "variant", None))
        if v is None:
            raise StepFailed("Shopify product has no available variant (sold out again?)")
        return v["id"]

    async def _shopify_cart(self, page, origin: str, variant, qty: int, run: "_Run") -> None:
        # Cart permalink: /cart/<variant>:<qty> adds to cart and redirects into checkout.
        run.trail.append(f"Shopify variant {variant} x{qty}")
        run.where = "Shopify cart link"
        await page.goto(f"{origin}/cart/{variant}:{qty}", wait_until="commit",
                        timeout=self.s.step_timeout * 1000)

    async def _cart_to_checkout(self, page, p: "Product", qty: int, run: "_Run") -> None:
        timeout = self.s.step_timeout
        if qty != 1:
            log.warning("%s: auto mode sets quantity only on Shopify; buying 1", p.name)
        run.where = "add to cart"
        name = await self._click_first(page, ADD_RE, ADD_SKIP, timeout, "an Add to cart button")
        run.trail.append(f"clicked {name!r}")

        # Watch for a cart-drawer Checkout button, or for Add to cart having gone
        # straight to checkout. Fall back to /cart if neither shows up.
        run.where = "go to checkout"
        deadline = time.monotonic() + 2
        while True:
            await self._check_block(page)
            place, _, place_disabled = await self._find(page, PLACE_RE, EXPRESS_SKIP)
            if place is not None or place_disabled:
                return
            el, name, _ = await self._find(page, CHECKOUT_RE, EXPRESS_SKIP)
            if el is not None:
                await el.click(timeout=timeout * 1000)
                run.trail.append(f"clicked {name!r}")
                return
            if time.monotonic() >= deadline:
                break
            await page.wait_for_timeout(POLL_MS)
        await page.goto(_origin(page.url) + "/cart", wait_until="domcontentloaded",
                        timeout=timeout * 1000)
        name = await self._click_first(page, CHECKOUT_RE, EXPRESS_SKIP, timeout,
                                       "a Checkout button (drawer or /cart)")
        run.trail.append(f"clicked {name!r}")

    async def _settle(self, page, handle, name: str, before_url: str, limit: float = 8.0) -> None:
        """After a Continue click: return as soon as the page moved on (new URL, or the
        button vanished / changed), instead of sleeping a fixed amount."""
        end = time.monotonic() + limit
        while time.monotonic() < end:
            await page.wait_for_timeout(POLL_MS)
            if page.url != before_url:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=limit * 1000)
                except Exception:
                    pass
                return
            try:
                if handle is None or not await handle.is_visible():
                    return
                if " ".join((await handle.inner_text()).split()) != name:
                    return
            except Exception:
                return   # detached

    async def _card_field_empty(self, page) -> bool:
        for frame in page.frames:
            try:
                loc = frame.locator(CARD_FIELD)
                for i in range(min(await loc.count(), 3)):
                    el = loc.nth(i)
                    if await el.is_visible() and not (await el.input_value()).strip():
                        return True
            except Exception:
                continue
        return False

    async def _find(self, page, pattern: re.Pattern, skip: re.Pattern | None):
        """First visible, enabled button/link whose name matches. Returns (el, name, disabled_name)."""
        disabled = None
        for role in ("button", "link"):
            loc = page.get_by_role(role, name=pattern)
            try:
                n = min(await loc.count(), 15)
            except Exception:
                continue
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not await el.is_visible():
                        continue
                    name = " ".join(((await el.inner_text()) or (await el.get_attribute("value"))
                                     or (await el.get_attribute("aria-label")) or "").split())
                    if skip and skip.search(name):
                        continue
                    if not await el.is_enabled():
                        disabled = disabled or name
                        continue
                    return el, name, disabled
                except Exception:
                    continue
        return None, None, disabled

    async def _click_first(self, page, pattern, skip, timeout: float, what: str | None):
        """Poll for a matching button and click it. Returns its name, or None if `what` is None."""
        deadline = time.monotonic() + timeout
        while True:
            await self._check_block(page)
            el, name, disabled = await self._find(page, pattern, skip)
            if el is not None:
                await el.click(timeout=timeout * 1000)
                return name
            if time.monotonic() >= deadline:
                if what is None:
                    return None
                extra = f" ({disabled!r} was there but disabled)" if disabled else ""
                raise StepFailed(f"couldn't find {what}{extra}")
            await page.wait_for_timeout(POLL_MS)

    async def _confirmed(self, page, before: str) -> bool:
        if page.url != before and CONFIRM_URL_RE.search(page.url):
            return True
        try:
            text = await page.evaluate("document.body ? document.body.innerText.slice(0, 4000) : ''")
        except Exception:
            return False
        return bool(CONFIRM_TEXT_RE.search(text or ""))

    async def _run_step(self, page, s: dict, timeout_ms: float, where: str) -> None:
        try:
            if "goto" in s:
                await page.goto(s["goto"], wait_until="domcontentloaded", timeout=timeout_ms)
            elif "click" in s:
                await page.locator(s["click"]).first.click(timeout=timeout_ms)
            elif "fill" in s:
                await page.locator(s["fill"]).first.fill(str(s["value"]), timeout=timeout_ms)
            elif "select" in s:
                await page.locator(s["select"]).first.select_option(str(s["value"]), timeout=timeout_ms)
            elif "wait_for" in s or "expect" in s:
                sel = s.get("wait_for") or s.get("expect")
                await page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
            elif "check_price" in s:
                text = await page.locator(s["check_price"]).first.inner_text(timeout=timeout_ms)
                total = _parse_money(text)
                if total is None:
                    raise StepFailed(f"couldn't read a price from {text!r}")
                if total > float(s["max"]):
                    raise StepFailed(f"total ${total:.2f} is over max ${float(s['max']):.2f}")
                log.info("checkout total $%.2f (max $%.2f)", total, float(s["max"]))
        except StepFailed:
            raise
        except Exception as e:
            msg = str(e).splitlines()[0][:200]
            raise StepFailed(f"{where} failed: {msg}") from e

    async def _check_block(self, page) -> None:
        if any(h in (urlparse(page.url).netloc or "") for h in BLOCK_HOSTS):
            raise Blocked("queue page")
        try:
            r = await page.evaluate(BLOCK_JS, list(BLOCK_SELECTORS))
        except Exception:
            return   # mid-navigation; next poll will look again
        if r.get("hit"):
            raise Blocked("CAPTCHA / bot challenge")
        if m := BLOCK_TEXT.search(r.get("text") or ""):
            raise Blocked(f"challenge/queue page ({m.group(0)!r})")

    async def _finish(self, page, p: "Product", out: Outcome, close: bool) -> Outcome:
        try:
            shots = Path(self.s.screenshots_dir)
            shots.mkdir(exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", p.name.lower()).strip("-")[:40]
            path = shots / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}-{out.status}.png"
            await page.screenshot(path=str(path), full_page=True)
            out.screenshot = str(path)
        except Exception as e:
            log.debug("screenshot failed: %s", e)
        if close:
            await page.close()
        return out

    async def _notify(self, p: "Product", out: Outcome) -> None:
        titles = {
            "ordered": f"ORDERED: {p.name}",
            "unconfirmed": f"Order maybe placed: {p.name}",
            "blocked": f"Checkout needs you: {p.name}",
            "failed": f"Checkout failed: {p.name}",
            "dry_run_ok": f"Dry run OK: {p.name}",
        }
        if out.status not in titles:
            return
        urgent = out.status != "dry_run_ok"
        await self.notifier.send(titles[out.status], out.detail, out.url or p.landing,
                                 urgent=urgent, open_url=False)


async def login(settings: CheckoutSettings, url: str) -> None:
    """Open the checkout browser profile so you can sign in and save address + card."""
    from playwright.async_api import async_playwright
    Path(settings.profile_dir).mkdir(exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            settings.profile_dir, headless=False, channel=settings.channel)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)
        print("Sign in, save your shipping address and card, then close the browser window.")
        closed = asyncio.Event()
        ctx.on("close", lambda *_: closed.set())
        await closed.wait()
    print(f"Saved to {settings.profile_dir}/")
