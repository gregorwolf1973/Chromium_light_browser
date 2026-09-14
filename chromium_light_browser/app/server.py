#!/usr/bin/env python3
"""Chromium Light Browser - controller and web UI (Home Assistant ingress).

The add-on publishes no port. Everything arrives through the Supervisor's
ingress proxy, i.e. from a signed-in Home Assistant user. Requests from any
other address are refused - that matters because the browser inside the
container can reach this server on 127.0.0.1: a web page open in it must not
be able to connect to the VNC stream or the API and watch or steer the
browser (think of an open banking session).
"""
import asyncio
import json
import os
import sys

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import cdp        # noqa: E402
import options    # noqa: E402
import session as sess  # noqa: E402

PORT = 8099
INGRESS_PROXY_IPS = {"172.30.32.2"}
LOCAL_IPS = {"127.0.0.1", "::1"}
STATIC = os.path.join(HERE, "static")
NOVNC = "/usr/share/novnc"


def create_app(opts, session=None, allowed_ips=None, http=None):
    app = web.Application(client_max_size=64 * 1024)
    app["opts"] = opts
    app["session"] = session or sess.Session(opts, start_url=f"http://127.0.0.1:{PORT}/start")
    app["allowed"] = set(allowed_ips if allowed_ips is not None else INGRESS_PROXY_IPS)
    app["http"] = http

    @web.middleware
    async def guard(request, handler):
        if request.path == "/start":
            if request.remote not in LOCAL_IPS | app["allowed"]:
                raise web.HTTPForbidden()
        elif request.remote not in app["allowed"]:
            raise web.HTTPForbidden(text="Nur ueber die Home-Assistant-Seitenleiste erreichbar.")
        if request.method == "POST" and request.headers.get("X-CLB") != "1":
            raise web.HTTPBadRequest(text="bad request")
        resp = await handler(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    app.middlewares.append(guard)

    async def http_session():
        if app["http"] is None:
            app["http"] = aiohttp.ClientSession()
        return app["http"]

    # ── pages ───────────────────────────────────────────────────────────────
    async def index(request):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            html = f.read()
        public = {"sites": [{"name": s["name"], "url": s["url"], "logout": s["logout_on_sleep"]}
                            for s in opts["sites"]],
                  "idle_minutes": opts["idle_minutes"]}
        html = html.replace("/*__CONFIG__*/null", json.dumps(public).replace("</", "<\\/"))
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def start_page(request):
        with open(os.path.join(STATIC, "start.html"), encoding="utf-8") as f:
            html = f.read()
        from html import escape
        tiles = "".join(
            f'<a class="tile" href="{escape(s["url"], quote=True)}"><span class="ico">{escape(s["name"][:1].upper())}</span>'
            f'<span class="nm">{escape(s["name"])}</span><span class="u">{escape(s["url"])}</span></a>'
            for s in opts["sites"]) or '<p class="empty">Noch keine Seiten eingerichtet - in der Addon-Konfiguration unter "sites" eintragen.</p>'
        return web.Response(text=html.replace("<!--TILES-->", tiles), content_type="text/html")

    # ── API ─────────────────────────────────────────────────────────────────
    async def status(request):
        return web.json_response(app["session"].status())

    async def body(request):
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            data = {}
        return data if isinstance(data, dict) else {}

    async def wake(request):
        data = await body(request)
        st = await app["session"].wake(data.get("w", 1280), data.get("h", 800))
        return web.json_response(st, status=200 if st["state"] == sess.RUNNING else 503)

    async def sleep(request):
        return web.json_response(await app["session"].sleep("manuell"))

    async def activity(request):
        app["session"].touch()
        return web.json_response({"ok": True})

    async def open_site(request):
        data = await body(request)
        s = app["session"]
        if "site" in data:
            try:
                url = opts["sites"][int(data["site"])]["url"]
            except (ValueError, TypeError, IndexError):
                raise web.HTTPBadRequest(text="unknown site")
        else:
            url = str(data.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise web.HTTPBadRequest(text="url must be http(s)")
        if s.state != sess.RUNNING:
            st = await s.wake(data.get("w", 1280), data.get("h", 800))
            if st["state"] != sess.RUNNING:
                return web.json_response(st, status=503)
        s.touch()
        try:
            res = await cdp.open_url(await http_session(), url)
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)
        return web.json_response({"ok": True, **res})

    async def tabs(request):
        s = app["session"]
        if s.state != sess.RUNNING:
            return web.json_response({"tabs": []})
        try:
            return web.json_response({"tabs": await cdp.pages(await http_session())})
        except Exception as e:
            return web.json_response({"tabs": [], "error": str(e)})

    async def tab_action(request):
        data = await body(request)
        tid = str(data.get("id") or "")
        if not tid or app["session"].state != sess.RUNNING:
            raise web.HTTPBadRequest(text="no tab")
        app["session"].touch()
        fn = cdp.close if request.match_info["action"] == "close" else cdp.activate
        try:
            await fn(await http_session(), tid)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
        return web.json_response({"ok": True})

    # ── VNC stream ──────────────────────────────────────────────────────────
    async def websockify(request):
        s = app["session"]
        if s.state != sess.RUNNING:
            raise web.HTTPServiceUnavailable(text="browser sleeping")
        ws = web.WebSocketResponse(protocols=("binary",), max_msg_size=0, heartbeat=30)
        await ws.prepare(request)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", sess.VNC_PORT)
        except OSError:
            await ws.close()
            return ws
        s.viewer_joined()

        async def vnc_to_ws():
            while True:
                chunk = await reader.read(64 * 1024)
                if not chunk:
                    break
                await ws.send_bytes(chunk)

        pump = asyncio.ensure_future(vnc_to_ws())
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            pump.cancel()
            writer.close()
            s.viewer_left()
            await ws.close()
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/start", start_page)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/wake", wake)
    app.router.add_post("/api/sleep", sleep)
    app.router.add_post("/api/activity", activity)
    app.router.add_post("/api/open", open_site)
    app.router.add_get("/api/tabs", tabs)
    app.router.add_post("/api/tabs/{action:close|activate}", tab_action)
    app.router.add_get("/websockify", websockify)
    app.router.add_static("/static/", STATIC)
    if os.path.isdir(NOVNC):
        app.router.add_static("/novnc/", NOVNC)

    async def on_start(app_):
        app_["watcher"] = asyncio.ensure_future(app_["session"].run_watcher())

    async def on_cleanup(app_):
        w = app_.get("watcher")
        if w:
            w.cancel()
        await app_["session"].sleep("Addon wird beendet")
        if app_["http"] is not None:
            await app_["http"].close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    opts = options.load()
    domains = options.logout_domains(opts)
    sess.log(f"Seiten: {', '.join(s['name'] for s in opts['sites']) or 'keine'} · Schlafen nach "
             f"{opts['idle_minutes'] or 'nie'} min · Abmelden beim Schlafen: {', '.join(domains) or 'nichts'}")
    web.run_app(create_app(opts), host="0.0.0.0", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
