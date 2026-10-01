"""
Local dashboard for tcg-restock.

Serves a single page on http://127.0.0.1:<port> from inside the monitor process:
live status for every product, add / edit / pause / delete, Check now, dry-run
checkout, the Live / Dry run switch, and the activity log.

It only listens on 127.0.0.1. Every API call needs a per-run token that is
embedded in the page, and the Host header must be localhost, so other websites
open in your browser can't drive the bot.
"""
from __future__ import annotations

import logging
import secrets
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import web

if TYPE_CHECKING:
    from monitor import DashboardSettings, Monitor, Watch

log = logging.getLogger("tcg-restock.dashboard")
PAGE = Path(__file__).with_name("dashboard.html")


class Dashboard:
    def __init__(self, mon: "Monitor", settings: "DashboardSettings"):
        self.mon = mon
        self.s = settings
        self.token = secrets.token_urlsafe(24)
        self.runner: web.AppRunner | None = None
        self.allowed_hosts = {f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"}

    # ----- lifecycle -----

    async def start(self) -> None:
        app = web.Application(middlewares=[self.guard], client_max_size=64 * 1024)
        r = app.router
        r.add_get("/", self.index)
        r.add_get("/api/state", self.state)
        r.add_post("/api/tasks", self.create)
        r.add_put("/api/tasks/{id}", self.update)
        r.add_delete("/api/tasks/{id}", self.delete)
        r.add_post("/api/tasks/{id}/{action}", self.action)
        r.add_post("/api/settings", self.settings)
        r.add_get("/api/shot/{id}", self.screenshot)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.s.port).start()
        url = f"http://127.0.0.1:{self.s.port}/"
        log.info("dashboard running at %s", url)
        if self.s.open_browser:
            webbrowser.open(url)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    @web.middleware
    async def guard(self, request: web.Request, handler):
        if request.host not in self.allowed_hosts:
            return web.Response(status=403, text="dashboard only answers on localhost")
        if request.path.startswith("/api/"):
            token = request.headers.get("X-Token") or request.query.get("token")
            if not token or not secrets.compare_digest(token, self.token):
                return web.json_response({"error": "bad token; reload the page"}, status=403)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except (ValueError, TypeError) as e:
            return web.json_response({"error": str(e)}, status=400)

    # ----- pages -----

    async def index(self, request: web.Request) -> web.Response:
        html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", self.token)
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store",
                                     "X-Frame-Options": "DENY",
                                     "Referrer-Policy": "no-referrer"})

    async def screenshot(self, request: web.Request) -> web.StreamResponse:
        w = self._watch(request)
        shot = (w.checkout or {}).get("screenshot")
        if not shot or not Path(shot).is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(shot)

    # ----- api -----

    async def state(self, request: web.Request) -> web.Response:
        m = self.mon
        after = int(request.query.get("after", 0))
        secs = m.checkout_secs
        return web.json_response({
            "now": time.time(),
            "started": m.started,
            "settings": {
                "auto_checkout": m.auto_checkout,
                "dry_run": m.cfg.checkout.dry_run,
                "alerts": bool(m.cfg.ntfy_topic or m.cfg.discord_webhook),
            },
            "totals": {
                **m.totals,
                "checks": sum(w.checks for w in m.watches.values()),
                "avg_checkout": round(sum(secs) / len(secs), 2) if secs else None,
            },
            "tasks": [self._task(w) for w in m.watches.values()],
            "events": m.events.since(after),
            "seq": m.events.seq,
        })

    def _task(self, w: "Watch") -> dict:
        from monitor import product_to_dict
        p = w.product
        return {
            "id": w.id, "source": w.source, "name": p.name, "url": p.url,
            "landing": p.landing, "host": urlparse(p.url).netloc, "mode": p.mode,
            "interval": p.interval, "max_price": p.max_price,
            "checkout": bool(p.checkout), "paused": w.paused, "status": w.status,
            "in_stock": w.in_stock, "last_check": w.last_check, "price": w.price,
            "p50": round(w.p50_ms()) if w.p50_ms() is not None else None,
            "checks": w.checks, "errors": w.errors, "last_error": w.last_error,
            "restocks": w.restocks, "last_checkout": w.checkout,
            "ordered": (self.mon.checkout.orders.get(p.url, 0) if self.mon.checkout else 0),
            "product": product_to_dict(p),
        }

    def _watch(self, request: web.Request) -> "Watch":
        w = self.mon.watches.get(request.match_info["id"])
        if w is None:
            raise web.HTTPNotFound(text="no such task")
        return w

    def _product(self, data: dict):
        from monitor import prepare_product, product_from_dict
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        clean = {k: v for k, v in data.items() if v not in (None, "")}
        for k in ("interval", "max_price"):
            if k in clean:
                clean[k] = float(clean[k])
        if "variant" in clean:
            clean["variant"] = str(clean["variant"]).strip()
        p = product_from_dict(clean)
        prepare_product(p, self.mon.cfg.checkout)
        return p

    async def create(self, request: web.Request) -> web.Response:
        p = self._product(await request.json())
        if any(w.product.url == p.url for w in self.mon.watches.values()):
            raise ValueError("you're already watching that URL")
        w = self.mon.add(p, source="dashboard")
        self.mon.save_dashboard_tasks()
        log.info("added %s", p.name)
        return web.json_response(self._task(w))

    async def update(self, request: web.Request) -> web.Response:
        w = self._watch(request)
        if w.source != "dashboard":
            raise ValueError("this product is defined in config.yaml; edit it there")
        p = self._product(await request.json())
        w = await self.mon.replace(w.id, p)
        self.mon.save_dashboard_tasks()
        log.info("updated %s", p.name)
        return web.json_response(self._task(w))

    async def delete(self, request: web.Request) -> web.Response:
        w = self._watch(request)
        if w.source != "dashboard":
            raise ValueError("this product is defined in config.yaml; remove it there")
        await self.mon.remove(w.id)
        self.mon.save_dashboard_tasks()
        log.info("deleted %s", w.product.name)
        return web.json_response({"ok": True})

    async def action(self, request: web.Request) -> web.Response:
        w = self._watch(request)
        act = request.match_info["action"]
        name = w.product.name
        if act == "pause":
            w.paused = True
            w.status = "paused"
            w.wake.set()
            log.info("paused %s", name)
        elif act == "resume":
            w.paused = False
            w.status = "starting"
            w.wake.set()
            log.info("resumed %s", name)
        elif act == "check":
            if w.paused:
                raise ValueError("resume it first")
            w.wake.set()
        elif act == "dryrun":
            if not w.product.checkout:
                raise ValueError("turn on auto-checkout for this product first")
            if w.status == "checking out":
                raise ValueError("a checkout is already running")
            log.info("dry-run checkout for %s", name)
            self.mon.spawn(self.mon.run_checkout(w, dry_run=True))
        elif act == "reset":
            if self.mon.checkout:
                self.mon.checkout.orders.pop(w.product.url, None)
                self.mon.checkout._save_orders()
            log.info("reset order count for %s", name)
        else:
            raise web.HTTPNotFound(text="unknown action")
        return web.json_response(self._task(w))

    async def settings(self, request: web.Request) -> web.Response:
        data = await request.json()
        m = self.mon
        if "auto_checkout" in data:
            m.auto_checkout = bool(data["auto_checkout"])
            if m.auto_checkout:
                await m.ensure_checkout()
                from monitor import _origin_of
                m.spawn(m.checkout.warm({_origin_of(w.product.landing)
                                         for w in m.watches.values() if w.product.checkout}))
            log.info("auto-checkout turned %s", "on" if m.auto_checkout else "off")
        if "dry_run" in data:
            m.cfg.checkout.dry_run = bool(data["dry_run"])
            log.warning("checkout mode: %s", "DRY RUN (stops before paying)" if m.cfg.checkout.dry_run
                        else "LIVE (will place real orders)")
        return web.json_response({"auto_checkout": m.auto_checkout,
                                  "dry_run": m.cfg.checkout.dry_run})
