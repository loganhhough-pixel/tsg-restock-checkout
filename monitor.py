"""
tcg-restock: async restock monitor for trading card products.

Watches product pages / JSON endpoints and alerts you (Discord, ntfy, console)
the moment an item flips from out-of-stock to in-stock. Optionally checks out
automatically on sites that allow it (see checkout.py); otherwise you check out.

Design choices:
  - asyncio + httpx with HTTP/2 and a shared connection pool: one process
    watches dozens of products with near-zero CPU.
  - Conditional GETs (ETag / Last-Modified): unchanged pages cost a 304.
  - Per-host rate limiting + jitter so you stay a polite, low-footprint client.
  - Edge-triggered alerts: you get pinged on OUT -> IN transitions only.
  - Backs off hard on 403/429 instead of trying to evade blocks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from checkout import CheckoutRunner, CheckoutSettings, validate_spec
from checkout import login as checkout_login

log = logging.getLogger("tcg-restock")
STATE_FILE = Path("state.json")

# Lowest allowed poll interval per mode. JSON/Shopify endpoints are a few hundred
# bytes (and usually a 304), so polling them every 3s is a light load. Full HTML
# pages stay at 10s. robots.txt Crawl-delay raises these further for that site.
MIN_INTERVAL = {"text": 10.0, "json": 3.0, "shopify": 3.0}


# ---------- config ----------

@dataclass
class Product:
    name: str
    url: str
    # "text": regex search on the page body; "json": dotted path into JSON;
    # "shopify": product page URL, polls its tiny <product>.js and reads variant stock
    mode: str = "text"
    in_stock: str | None = None       # regex (text) or expected value (json)
    out_of_stock: str | None = None   # regex; takes priority if it matches
    json_path: str | None = None
    interval: float = 30.0            # seconds between checks
    max_price: float | None = None
    price_regex: str | None = None    # first capture group = price
    buy_url: str | None = None        # page to land on when alerted (defaults to url)
    checkout: dict | str | None = None  # `auto`, or explicit steps; see config.example.yaml
    variant: int | str | None = None  # shopify mode: watch one specific variant ID

    @property
    def landing(self) -> str:
        return self.buy_url or self.url

    @property
    def fetch_url(self) -> str:
        """What the monitor actually requests."""
        if self.mode != "shopify":
            return self.url
        base = self.url.split("#")[0].split("?")[0].rstrip("/")
        return base if base.endswith((".js", ".json")) else base + ".js"


@dataclass
class Config:
    products: list[Product]
    discord_webhook: str | None = None
    discord_mention: str | None = None  # e.g. "<@&ROLE_ID>" or "@everyone"
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    auto_open: bool = False           # open buy page in your browser on alert
    health_alert_after: int = 10      # alert if a product fails this many times in a row
    min_host_gap: float = 2.0         # min seconds between hits to one host
    user_agent: str = "tcg-restock/1.0 (personal stock alert)"
    checkout: CheckoutSettings = field(default_factory=CheckoutSettings)


def load_config(path: str) -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    prods = [Product(**p) for p in raw.pop("products")]
    checkout = CheckoutSettings(**(raw.pop("checkout", None) or {}))
    for p in prods:
        if p.mode not in MIN_INTERVAL:
            raise SystemExit(f"{p.name!r}: mode must be one of {', '.join(MIN_INTERVAL)}")
        floor = MIN_INTERVAL[p.mode]
        if p.interval < floor:
            log.warning("%s: interval raised to the %ss minimum for %s mode", p.name, floor, p.mode)
            p.interval = floor
        if p.checkout:
            try:
                validate_spec(p.url, p.checkout, checkout)
            except ValueError as e:
                raise SystemExit(f"config error in checkout for {p.name!r}: {e}")
    return Config(products=prods, checkout=checkout, **raw)


# ---------- rate limiting ----------

class HostLimiter:
    """Guarantees a minimum gap between requests to the same host."""

    def __init__(self, gap: float):
        self.gap = gap
        self.next_ok: dict[str, float] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.penalty: dict[str, float] = {}
        self.host_gap: dict[str, float] = {}   # e.g. from robots.txt Crawl-delay

    async def wait(self, host: str) -> None:
        lock = self.locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            ready = self.next_ok.get(host, 0.0)
            if ready > now:
                await asyncio.sleep(ready - now)
            gap = max(self.gap, self.host_gap.get(host, 0.0))
            self.next_ok[host] = time.monotonic() + gap + self.penalty.get(host, 0.0)

    def punish(self, host: str) -> float:
        cur = self.penalty.get(host, 0.0)
        new = min(max(cur * 2, 60.0), 1800.0)  # 1 min -> 30 min cap
        self.penalty[host] = new
        return new

    def forgive(self, host: str) -> None:
        self.penalty.pop(host, None)


# ---------- stock detection ----------

@dataclass
class CheckResult:
    in_stock: bool
    price: float | None = None
    variant: int | None = None      # shopify mode: the in-stock variant to buy


def dig(obj, path: str):
    for key in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(key)]
        else:
            obj = obj[key]
    return obj


def evaluate(p: Product, body: str) -> CheckResult:
    price = None
    if p.price_regex and (m := re.search(p.price_regex, body)):
        try:
            price = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    if p.mode == "shopify":
        data = json.loads(body)
        data = data.get("product", data)          # products/x.json wraps it; x.js doesn't
        vs = data.get("variants") or []
        if p.variant is not None:
            vs = [v for v in vs if str(v.get("id")) == str(p.variant)]
        v = next((v for v in vs if v.get("available", True)), None)
        if v is None:
            return CheckResult(False, None)
        raw = v.get("price")
        if isinstance(raw, int):
            price = raw / 100                       # .js prices are in cents
        elif raw is not None:
            price = float(str(raw).replace(",", ""))
        return CheckResult(True, price, v.get("id"))

    if p.mode == "json":
        val = dig(json.loads(body), p.json_path or "")
        if p.in_stock is None:
            ok = bool(val)
        else:
            ok = str(val).lower() == str(p.in_stock).lower()
        return CheckResult(ok, price)

    if p.out_of_stock and re.search(p.out_of_stock, body, re.I):
        return CheckResult(False, price)
    if p.in_stock:
        return CheckResult(bool(re.search(p.in_stock, body, re.I)), price)
    return CheckResult(False, price)


# ---------- alerts ----------

class Notifier:
    def __init__(self, cfg: Config, client: httpx.AsyncClient):
        self.cfg, self.client = cfg, client

    async def send(self, title: str, msg: str, url: str, urgent: bool = True,
                   open_url: bool = True) -> None:
        log.info("ALERT %s | %s", title, url)
        if urgent:
            print("\a", end="", flush=True)  # terminal bell
            if self.cfg.auto_open and open_url:
                # run in a thread so a slow browser launch never delays the pushes
                asyncio.get_running_loop().run_in_executor(None, webbrowser.open, url)
        tasks = []
        if self.cfg.discord_webhook:
            payload = {"embeds": [{"title": title, "description": f"{msg}\n[**Open**]({url})",
                                   "url": url, "color": 0x2ECC71 if urgent else 0xE67E22}]}
            if urgent and self.cfg.discord_mention:
                payload["content"] = self.cfg.discord_mention
                payload["allowed_mentions"] = {"parse": ["everyone", "roles", "users"]}
            tasks.append(self.client.post(self.cfg.discord_webhook, json=payload))
        if self.cfg.ntfy_topic:
            tasks.append(self.client.post(
                f"{self.cfg.ntfy_server.rstrip('/')}/{self.cfg.ntfy_topic}",
                content=msg.encode(),
                headers={"Title": title, "Click": url,
                         "Priority": "urgent" if urgent else "default",
                         "Tags": "rotating_light" if urgent else "warning",
                         "Actions": f"view, Open, {url}, clear=true"}))
        for r in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(r, Exception):
                log.error("notify failed: %s", r)
            elif r.status_code >= 400:
                log.error("notify failed: HTTP %s %s", r.status_code, r.text[:200])


# ---------- watcher ----------

@dataclass
class Watch:
    product: Product
    etag: str | None = None
    last_mod: str | None = None
    last_body: str | None = None
    in_stock: bool | None = None
    errors: int = 0
    checks: int = 0
    latencies: list[float] = field(default_factory=list)


class Monitor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.limiter = HostLimiter(cfg.min_host_gap)
        self.state: dict[str, bool] = (
            json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {})
        self.checkout: CheckoutRunner | None = None
        self._tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def save(self) -> None:
        STATE_FILE.write_text(json.dumps(self.state, indent=2))

    async def check(self, client: httpx.AsyncClient, w: Watch) -> CheckResult | None:
        p = w.product
        host = urlparse(p.fetch_url).netloc
        await self.limiter.wait(host)

        headers = {}
        if w.etag:
            headers["If-None-Match"] = w.etag
        if w.last_mod:
            headers["If-Modified-Since"] = w.last_mod

        t0 = time.perf_counter()
        r = await client.get(p.fetch_url, headers=headers)
        w.latencies = (w.latencies + [time.perf_counter() - t0])[-50:]
        w.checks += 1

        if r.status_code in (403, 429):
            wait = self.limiter.punish(host)
            retry = r.headers.get("Retry-After")
            log.warning("%s: HTTP %s from %s, backing off %.0fs",
                        p.name, r.status_code, host, max(wait, float(retry or 0)))
            return None
        self.limiter.forgive(host)

        if r.status_code == 304 and w.last_body is not None:
            body = w.last_body
        else:
            r.raise_for_status()
            body = r.text
            w.last_body = body
            w.etag = r.headers.get("ETag")
            w.last_mod = r.headers.get("Last-Modified")
        return evaluate(p, body)

    async def run_one(self, client: httpx.AsyncClient, notifier: Notifier, w: Watch):
        p = w.product
        w.in_stock = self.state.get(p.url)
        health_alerted = False
        await asyncio.sleep(random.uniform(0, min(p.interval, 5)))  # stagger start
        while True:
            try:
                res = await self.check(client, w)
                w.errors = 0
                if health_alerted:
                    health_alerted = False
                    log.info("%s recovered", p.name)
                if res is not None:
                    if res.in_stock and w.in_stock is not True:
                        if p.max_price is None or res.price is None or res.price <= p.max_price:
                            price = f" at ${res.price:.2f}" if res.price else ""
                            auto = self.checkout is not None and bool(p.checkout)
                            if auto:  # start checkout before anything else
                                self.spawn(self.checkout.run(p, variant=res.variant))
                            await notifier.send(
                                f"IN STOCK: {p.name}",
                                f"{p.name} is available{price}"
                                + (" (auto-checkout running)" if auto else ""),
                                p.landing, open_url=not auto)
                        else:
                            log.info("%s in stock but $%.2f > max $%.2f, skipped",
                                     p.name, res.price, p.max_price)
                    elif not res.in_stock and w.in_stock:
                        log.info("%s went out of stock", p.name)
                    if res.in_stock != w.in_stock:
                        w.in_stock = res.in_stock
                        self.state[p.url] = res.in_stock
                        self.save()
                    log.debug("%s: %s", p.name, "IN" if res.in_stock else "out")
            except Exception as e:  # network blips, parse failures
                w.errors += 1
                log.warning("%s: %s (%d in a row)", p.name, e, w.errors)
                if w.errors >= self.cfg.health_alert_after and not health_alerted:
                    health_alerted = True
                    await notifier.send(f"Monitor failing: {p.name}",
                                        f"{w.errors} failed checks in a row. Last error: {e}",
                                        p.url, urgent=False)
            backoff = min(2 ** min(w.errors, 6), 300) if w.errors else 0
            await asyncio.sleep(p.interval * random.uniform(0.85, 1.15) + backoff)

    async def stats_loop(self, watches: list[Watch]):
        while True:
            await asyncio.sleep(300)
            for w in watches:
                lat = sorted(w.latencies)
                p50 = lat[len(lat) // 2] * 1000 if lat else 0
                log.info("stats %-30s checks=%d p50=%.0fms stock=%s",
                         w.product.name[:30], w.checks, p50, w.in_stock)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            http2=_has_h2(),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20),
            follow_redirects=True, timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"User-Agent": self.cfg.user_agent,
                     "Accept-Encoding": "gzip, deflate, br"})

    async def apply_robots(self, client: httpx.AsyncClient) -> None:
        """Honor each site's robots.txt Crawl-delay as a floor on how often we poll it."""
        async def one(origin: str) -> tuple[str, float | None]:
            try:
                r = await client.get(origin + "/robots.txt", timeout=5)
                return origin, (crawl_delay(r.text, self.cfg.user_agent) if r.status_code == 200 else None)
            except Exception:
                return origin, None
        origins = {f"{urlparse(p.fetch_url).scheme}://{urlparse(p.fetch_url).netloc}"
                   for p in self.cfg.products}
        for origin, delay in await asyncio.gather(*(one(o) for o in origins)):
            if not delay:
                continue
            host = urlparse(origin).netloc
            self.limiter.host_gap[host] = delay
            for p in self.cfg.products:
                if urlparse(p.fetch_url).netloc == host and p.interval < delay:
                    p.interval = delay
            log.info("%s asks for Crawl-delay %ss; polling it no faster than that", host, delay)

    async def run(self) -> None:
        async with self.client() as client:
            notifier = Notifier(self.cfg, client)
            await self.apply_robots(client)
            watches = [Watch(p) for p in self.cfg.products]
            if self.cfg.checkout.enabled and any(p.checkout for p in self.cfg.products):
                self.checkout = CheckoutRunner(self.cfg.checkout, notifier)
                await self.checkout.start()
                self.spawn(self.checkout.warm(
                    f"{urlparse(p.landing).scheme}://{urlparse(p.landing).netloc}"
                    for p in self.cfg.products if p.checkout))
                self.spawn(self.checkout.keep_warm())
                if self.cfg.checkout.dry_run:
                    log.warning("auto-checkout is in DRY RUN: it stops before placing orders. "
                                "Set checkout.dry_run: false when --checkout-test passes.")
            log.info("watching %d products (auto_open=%s, auto_checkout=%s)", len(watches),
                     self.cfg.auto_open, sum(bool(p.checkout) for p in self.cfg.products)
                     if self.checkout else 0)
            try:
                await asyncio.gather(self.stats_loop(watches),
                                     *(self.run_one(client, notifier, w) for w in watches))
            finally:
                if self.checkout:
                    await self.checkout.close()

    async def check_all_once(self) -> None:
        """Hit every product once and print what the detector sees. For tuning config."""
        async with self.client() as client:
            async def one(p: Product):
                w = Watch(p)
                try:
                    res = await self.check(client, w)
                    ms = w.latencies[-1] * 1000
                    if res is None:
                        return f"BLOCKED  {p.name}  (403/429)"
                    price = f"${res.price:.2f}" if res.price is not None else "no price"
                    size = len(w.last_body or "")
                    return (f"{'IN ' if res.in_stock else 'out'}      {p.name}  "
                            f"{price}  {ms:.0f}ms  {size/1024:.1f}KB")
                except Exception as e:
                    return f"ERROR    {p.name}  {type(e).__name__}: {e}"
            for line in await asyncio.gather(*(one(p) for p in self.cfg.products)):
                print(line)


def crawl_delay(robots: str, agent: str) -> float | None:
    """Crawl-delay for our user agent (or *) from a robots.txt body."""
    me = agent.split("/")[0].lower()
    groups: list[tuple[list[str], float | None]] = []
    agents: list[str] = []
    delay = None
    last_was_agent = False
    for line in robots.splitlines():
        line = line.split("#")[0].strip()
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if not last_was_agent and agents:
                groups.append((agents, delay))
                agents, delay = [], None
            agents.append(v.lower())
            last_was_agent = True
        else:
            last_was_agent = False
            if k == "crawl-delay":
                try:
                    delay = float(v)
                except ValueError:
                    pass
    if agents:
        groups.append((agents, delay))
    mine = [d for a, d in groups if any(x != "*" and x in me for x in a)]
    star = [d for a, d in groups if "*" in a]
    for d in (mine or star):
        if d:
            return min(d, 60.0)
    return None


def _has_h2() -> bool:
    try:
        import h2  # noqa: F401
        return True
    except ImportError:
        return False


async def checkout_test(cfg: Config, name: str) -> None:
    """Dry-run one product's checkout right now, regardless of stock."""
    matches = [p for p in cfg.products if name.lower() in p.name.lower()]
    if len(matches) != 1:
        raise SystemExit(f"--checkout-test {name!r} matched {len(matches)} products; be more specific")
    p = matches[0]
    if not p.checkout:
        raise SystemExit(f"{p.name!r} has no checkout: block in config")
    runner = CheckoutRunner(cfg.checkout)
    try:
        out = await runner.run(p, dry_run=True, notify=False)
    finally:
        await runner.close()
    print(f"{out.status.upper()}: {out.detail}")
    if out.screenshot:
        print(f"screenshot: {out.screenshot}")


async def test_alert(cfg: Config) -> None:
    async with httpx.AsyncClient(timeout=10) as c:
        await Notifier(cfg, c).send("tcg-restock test", "Alerts are working.",
                                    "https://example.com")


def main() -> None:
    ap = argparse.ArgumentParser(description="Pokémon/TCG restock alert monitor")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--test-alert", action="store_true", help="send a test notification")
    ap.add_argument("--check", action="store_true",
                    help="check every product once, print results, exit (for tuning config)")
    ap.add_argument("--login", metavar="URL",
                    help="open the checkout browser profile to sign in and save address + card")
    ap.add_argument("--checkout-test", metavar="NAME",
                    help="dry-run one product's checkout now (stops before placing the order)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "hpack", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config(args.config)
    try:
        import uvloop  # faster event loop on Linux/macOS if installed
        uvloop.install()
    except ImportError:
        pass
    if args.login:
        job = checkout_login(cfg.checkout, args.login)
    elif args.checkout_test:
        job = checkout_test(cfg, args.checkout_test)
    elif args.test_alert:
        job = test_alert(cfg)
    elif args.check:
        job = Monitor(cfg).check_all_once()
    else:
        job = Monitor(cfg).run()
    try:
        asyncio.run(job)
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()
