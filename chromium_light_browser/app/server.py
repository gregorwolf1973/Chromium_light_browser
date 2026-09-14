#!/usr/bin/env python3
"""Chromium Light Browser - controller and web UI (Home Assistant ingress).

The add-on publishes no port. The sidebar page, its API and the VNC stream only
answer the Supervisor's ingress proxy, i.e. a signed-in Home Assistant user.

The browser inside the container reaches this server on 127.0.0.1 as well. A
web page open in it must not be able to watch or steer the browser (think of an
open banking session), so from 127.0.0.1 there is exactly:

- /start - the start page with the site tiles (every new tab shows it);
- /api/local/* - sleep, status and "open the clipboard panel", and only with
  the per-run secret that is embedded in /start. Other pages cannot read /start
  (different origin, no CORS), so they never learn the secret, and a custom
  header rules out simple cross-site form posts.
"""
import asyncio
import hmac
import json
import os
import secrets
import sys
from html import escape

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


def asset_version():
    """Short content hash of app.js: part of its URL, so a new version is a new URL."""
    import hashlib
    try:
        with open(os.path.join(STATIC, "app.js"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "0"


def create_app(opts, session=None, allowed_ips=None, http=None, local_token=None):
    app = web.Application(client_max_size=64 * 1024)
    state = {
        "session": session or sess.Session(opts, start_url=f"http://127.0.0.1:{PORT}/start"),
        "allowed": set(allowed_ips if allowed_ips is not None else INGRESS_PROXY_IPS),
        "http": http,
        "token": local_token or secrets.token_urlsafe(32),
        "ui": [],                     # requests from the start page for the sidebar page
        "watcher": None,
    }
    app["state"] = state

    @web.middleware
    async def guard(request, handler):
        path, remote = request.path, request.remote
        if path == "/start":
            if remote not in LOCAL_IPS | state["allowed"]:
                raise web.HTTPForbidden()
        elif path.startswith("/api/local/"):
            if remote not in LOCAL_IPS or not hmac.compare_digest(
                    request.headers.get("X-CLB-Local", ""), state["token"]):
                raise web.HTTPForbidden()
        elif remote not in state["allowed"]:
            raise web.HTTPForbidden(text="Nur ueber die Home-Assistant-Seitenleiste erreichbar.")
        elif request.method == "POST" and request.headers.get("X-CLB") != "1":
            raise web.HTTPBadRequest(text="bad request")
        resp = await handler(request)
        if path.startswith("/static/"):
            # always revalidate: a cached app.js from an older version next to a
            # new index.html broke the page after an update
            resp.headers["Cache-Control"] = "no-cache"
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    app.middlewares.append(guard)

    async def http_session():
        if state["http"] is None:
            state["http"] = aiohttp.ClientSession()
        return state["http"]

    def s():
        return state["session"]

    def public_config():
        return {"sites": [{"name": x["name"], "url": x["url"], "logout": x["logout_on_sleep"]} for x in opts["sites"]],
                "idle_minutes": opts["idle_minutes"]}

    # ── pages ───────────────────────────────────────────────────────────────
    async def index(request):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
            html = f.read()
        html = html.replace("/*__CONFIG__*/null", json.dumps(public_config()).replace("</", "<\\/"))
        # a new file name per version: a reverse proxy with asset caching (Nginx
        # Proxy Manager "Cache Assets") ignores query strings and kept serving
        # the app.js of an older release for hours
        html = html.replace('src="static/app.js"', f'src="assets/app-{asset_version()}.js"')
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def start_page(request):
        with open(os.path.join(STATIC, "start.html"), encoding="utf-8") as f:
            html = f.read()
        tiles = "".join(
            f'<a class="tile" href="{escape(x["url"], quote=True)}">'
            f'<span class="ico">{escape(x["name"][:1].upper())}</span>'
            f'<span class="nm">{escape(x["name"])}{" 🔒" if x["logout_on_sleep"] else ""}</span>'
            f'<span class="u">{escape(x["url"])}</span></a>'
            for x in opts["sites"]) or ('<p class="empty">Noch keine Seiten eingerichtet - in der Addon-Konfiguration '
                                         'unter „sites“ eintragen.</p>')
        html = html.replace("<!--TILES-->", tiles).replace("__LOCAL_TOKEN__", state["token"])
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def versioned_app_js(request):
        if request.match_info["v"] != asset_version():
            raise web.HTTPNotFound()
        with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="application/javascript",
                                headers={"Cache-Control": "public, max-age=31536000, immutable"})

    async def body(request):
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            data = {}
        return data if isinstance(data, dict) else {}

    # ── sidebar API (ingress) ───────────────────────────────────────────────
    async def status(request):
        return web.json_response(s().status())

    async def wake(request):
        data = await body(request)
        st = await s().wake(data.get("w", 1280), data.get("h", 800))
        return web.json_response(st, status=200 if st["state"] == sess.RUNNING else 503)

    async def sleep(request):
        return web.json_response(await s().sleep("manuell"))

    async def activity(request):
        s().touch()
        return web.json_response({"ok": True})

    async def open_site(request):
        data = await body(request)
        if "site" in data:
            try:
                url = opts["sites"][int(data["site"])]["url"]
            except (ValueError, TypeError, IndexError):
                raise web.HTTPBadRequest(text="unknown site")
        else:
            url = str(data.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise web.HTTPBadRequest(text="url must be http(s)")
        if s().state != sess.RUNNING:
            st = await s().wake(data.get("w", 1280), data.get("h", 800))
            if st["state"] != sess.RUNNING:
                return web.json_response(st, status=503)
        s().touch()
        try:
            res = await cdp.open_url(await http_session(), url)
        except Exception as e:
            return web.json_response({"error": f"{type(e).__name__}: {e}"}, status=502)
        return web.json_response({"ok": True, **res})

    async def ui_requests(request):
        """The sidebar page polls this while the picture is live."""
        pending, state["ui"] = state["ui"], []
        return web.json_response({"requests": pending, "state": s().state})

    # ── start page API (inside the browser, with the secret) ───────────────
    async def local_status(request):
        return web.json_response(s().status())

    async def local_sleep(request):
        # answer first: the page asking is about to disappear with the browser
        asyncio.ensure_future(s().sleep("Startseite"))
        return web.json_response({"ok": True})

    async def local_clipboard(request):
        if "clipboard" not in state["ui"]:
            state["ui"].append("clipboard")
        s().touch()
        return web.json_response({"ok": True})

    # ── VNC stream ──────────────────────────────────────────────────────────
    async def websockify(request):
        if s().state != sess.RUNNING:
            raise web.HTTPServiceUnavailable(text="browser sleeping")
        ws = web.WebSocketResponse(protocols=("binary",), max_msg_size=0, heartbeat=30)
        await ws.prepare(request)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", sess.VNC_PORT)
        except OSError:
            await ws.close()
            return ws
        session_ = s()
        session_.viewer_joined()

        async def vnc_to_ws():
            while True:
                chunk = await reader.read(64 * 1024)
                if not chunk:
                    break
                await ws.send_bytes(chunk)
            await ws.close()

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
            session_.viewer_left()
            await ws.close()
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/start", start_page)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/wake", wake)
    app.router.add_post("/api/sleep", sleep)
    app.router.add_post("/api/activity", activity)
    app.router.add_post("/api/open", open_site)
    app.router.add_get("/api/ui", ui_requests)
    app.router.add_get("/api/local/status", local_status)
    app.router.add_post("/api/local/sleep", local_sleep)
    app.router.add_post("/api/local/clipboard", local_clipboard)
    app.router.add_get("/websockify", websockify)
    app.router.add_get("/assets/app-{v:[0-9a-f]{12}}.js", versioned_app_js)
    app.router.add_static("/static/", STATIC)
    if os.path.isdir(NOVNC):
        app.router.add_static("/novnc/", NOVNC)

    async def on_start(app_):
        state["watcher"] = asyncio.ensure_future(s().run_watcher())

    async def on_cleanup(app_):
        if state["watcher"]:
            state["watcher"].cancel()
        await s().sleep("Addon wird beendet")
        if state["http"] is not None:
            await state["http"].close()

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    opts = options.load()
    domains = options.logout_domains(opts)
    sess.log(f"Seiten: {', '.join(x['name'] for x in opts['sites']) or 'keine'} · Schlafen nach "
             f"{opts['idle_minutes'] or 'nie'} min · Abmelden beim Schlafen: {', '.join(domains) or 'nichts'}")
    web.run_app(create_app(opts), host="0.0.0.0", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
