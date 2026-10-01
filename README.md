# tcg-restock

Async restock alert monitor for Pokémon / TCG products. Pings your phone (ntfy) or Discord the instant something flips to in-stock, and can optionally check out for you on sites that allow it.

## Setup
```
pip install -r requirements.txt
cp config.example.yaml config.yaml   # add products + alert target
python monitor.py --test-alert       # confirm your phone buzzes
python monitor.py --check            # hit each product once: IN/out, price, latency
python monitor.py                    # run (-v for per-check logs)
```

## Dashboard
```
python monitor.py --dashboard        # opens http://127.0.0.1:8787
```
A control panel that runs inside the bot:
- Every product as a card with live status, price, last check, response time, and the last checkout result (with a screenshot link).
- Add, edit, pause, and delete products without touching YAML. Products you add there are saved to `tasks.yaml`; ones in `config.yaml` show up too but are edited in the file.
- **Check now** and **Test checkout** (a dry run that stops before paying) on any card.
- Auto-checkout on/off and **Dry run / Live** switches in the header. Live asks you to confirm and shows a warning banner while it's on. These switches last until you restart; set `checkout.dry_run` in config to make it permanent.
- Running totals (restocks caught, orders, average checkout time) and a live activity feed.

It only listens on your own machine (127.0.0.1), and every request needs a per-run key built into the page, so other websites in your browser can't control it. You can start with an empty config and add everything from the dashboard.

## Finding the right thing to watch
Fastest signal is usually the JSON endpoint the product page calls for availability: open DevTools → Network → filter "Fetch/XHR", reload, look for a response with a stock field. Use `mode: json` with that URL. Otherwise use `mode: text` with regexes for the button text.

## Winning the manual checkout
- `auto_open: true` opens the buy page in your browser the instant stock flips (run it on the computer you'll buy from).
- ntfy pushes are urgent priority with an **Open** button, so one tap from the lock screen lands you on the page.
- Before a drop: be logged in, have address + card saved, and turn on any express checkout the retailer offers. That's the difference between 10 seconds and 60.
- `--check` after editing config so a broken regex doesn't silently miss a drop; health alerts catch pages that change later.

## Auto-checkout (optional)
Only turn this on for retailers whose terms allow automated purchasing. Many big ones (Pokémon Center, Target, Walmart) don't, and they cancel bot orders or flag accounts.

```
pip install playwright && playwright install chromium
python monitor.py --login https://store.example.com/account   # sign in, save address + card, close window
python monitor.py --checkout-test "Surging Sparks"            # dry run: goes all the way to Pay, doesn't click it
```
Then set `checkout.enabled: true` and, once the dry run passes, `dry_run: false`.

Two ways to tell it how to check out, per product:
- **`checkout: auto`** works on most stores with no setup. On Shopify it grabs the first in-stock variant and uses a cart link straight to checkout. Elsewhere it clicks Add to cart, then Checkout (cart drawer or `/cart`), clicks through Continue buttons, reads the order total, and clicks Place order / Pay now. It never clicks PayPal, Shop Pay, Apple Pay, or other wallet buttons. Needs a `max_total` (per product or under `checkout:`) and a `buy_url` if `url` is a JSON endpoint. Dry-run output lists every button it clicked, so you can see exactly what it would do.
- **Explicit `steps:`** for a store where auto mode picks the wrong button. Exactly one step is marked `final: true`: the click that places the order.
- It uses a real browser profile you signed into, so your saved card pays. The bot never sees card details. Don't commit `browser-profile/`.
- Browser launches at startup, so a restock goes straight to checkout; alerts still fire in parallel.
- Guardrails: `max_price` before starting, `max_total` / `check_price` on the real checkout total (auto mode refuses to buy if it can't read one), `max_orders_per_product` (tracked in `orders.json`), one checkout at a time.
- If Place order stays greyed out (missing saved card, CVV, address), it stops and tells you to save that info.
- If it hits a CAPTCHA, queue, or bot wall, it stops, leaves the window on that page, and sends an urgent alert so you can finish by hand. It doesn't try to solve or get around them.
- Every attempt saves a screenshot to `checkout-shots/`. "Order maybe placed" means it clicked Pay but didn't see the confirmation, so check your email before retrying.

## Speed
Measured against a local test shop with realistic delays (120ms network, images, tracker scripts, 1.5s payment processing):

| | Before | Now |
|---|---|---|
| Shopify: checkout only | 2.1s | **0.5s** |
| Generic store: checkout only | 4.0s | **0.85s** |
| Shopify: restock to order submitted (avg of 6) | 8.5s | **2.3s** |

What makes it fast:
- **`mode: shopify`** polls the product's tiny `.js` endpoint every 3s and passes the in-stock variant straight to checkout: no product page load.
- **Warm tabs:** the checkout browser parks a tab on each store at startup, so connections and cookies are ready when a restock hits.
- **No images, fonts, or trackers** in the checkout browser (they're turned back on if it stops for you to finish by hand).
- **No fixed sleeps:** it watches for the next button every 150ms and moves on the moment the page changes.

Limits it keeps on purpose: 3s minimum for JSON/Shopify endpoints, 10s for full HTML pages, robots.txt `Crawl-delay` honored, backoff on 403/429, one IP, one order. Real sites add their own page-load and payment time, so expect a few seconds more than the table.

## Monitor internals
- Python asyncio + httpx: the bottleneck is network round-trip, not language, so a compiled language buys ~nothing here; one process handles dozens of products at near-zero CPU. uvloop is used automatically if installed.
- HTTP/2 + keep-alive connection pooling (no repeated TLS handshakes).
- Conditional GETs (ETag / If-Modified-Since) → unchanged pages cost a tiny 304.
- JSON endpoints skip downloading full HTML.
- Edge-triggered alerts, parallel fan-out to all notifiers.
- State persisted to `state.json` so restarts don't re-alert.

## Being a good client
Minimum 10s interval per product, a per-site request gap, jitter, and exponential backoff on 403/429. It does not rotate proxies, spoof fingerprints, or solve CAPTCHAs — if a site blocks you, it backs off. Run it on an always-on box (a cheap VPS or a Raspberry Pi) for 24/7 coverage.
