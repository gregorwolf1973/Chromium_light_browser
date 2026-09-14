#!/usr/bin/env python3
"""Chromium Light Browser: options, DevTools logout, session lifecycle, web API guard."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "chromium_light_browser", "app"))

import cdp  # noqa: E402
import options  # noqa: E402
import server  # noqa: E402
import session as sess  # noqa: E402

RAW = {"idle_minutes": 15, "sites": [
    {"name": "WhatsApp", "url": "https://web.whatsapp.com", "logout_on_sleep": False},
    {"name": "Sparkasse", "url": "https://www.sparkasse.de", "logout_on_sleep": True},
    {"name": "kaputt", "url": "javascript:alert(1)"},
], "logout_domains": ["https://banking.example.org/login", "keinpunkt"], "renderer_process_limit": 99}


class OptionsTest(unittest.TestCase):
    def test_normalise(self):
        o = options.normalise(RAW)
        self.assertEqual([s["name"] for s in o["sites"]], ["WhatsApp", "Sparkasse"], "nur http(s)")
        self.assertEqual(o["renderer_process_limit"], 16)
        self.assertEqual(o["logout_domains"], ["banking.example.org"])
        self.assertEqual(options.logout_domains(o), ["banking.example.org", "sparkasse.de"])
        self.assertEqual(options.normalise({})["idle_minutes"], 15)
        self.assertEqual(options.normalise({"idle_minutes": 0})["idle_minutes"], 0)

    def test_host_matching(self):
        d = ["sparkasse.de"]
        for host, want in (("sparkasse.de", True), (".sparkasse.de", True), ("banking.sparkasse.de", True),
                           ("notsparkasse.de", False), ("sparkasse.de.evil.com", False), ("", False)):
            self.assertEqual(options.host_matches(host, d), want, host)

    def test_chromium_args_and_size(self):
        o = options.normalise(RAW)
        args = sess.chromium_args(o, 1600, 900, "http://127.0.0.1:8099/start")
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--renderer-process-limit=16", args)
        self.assertIn("--enable-low-end-device-mode", args)
        self.assertEqual(args[-1], "http://127.0.0.1:8099/start")
        self.assertFalse(any(a.startswith("--remote-allow-origins") for a in args), "sonst koennten Webseiten an DevTools")
        ext = [a.split("=", 1)[1] for a in args if a.startswith("--load-extension=")]
        self.assertEqual(len(ext), 1)
        manifest = json.load(open(os.path.join(ext[0], "manifest.json"), encoding="utf-8"))
        self.assertEqual(manifest["chrome_url_overrides"]["newtab"], "newtab.html")
        self.assertTrue(os.path.exists(os.path.join(ext[0], "newtab.js")))
        self.assertIn("127.0.0.1:8099/start", open(os.path.join(ext[0], "newtab.js"), encoding="utf-8").read())
        self.assertIn("--disable-background-networking", args)
        self.assertEqual(sess.clamp_size(100, 99999), (1024, 1600))
        self.assertEqual(sess.clamp_size("x", None), (1280, 800))
        self.assertEqual(sess.clamp_size(1333, 777), (1332, 776))

    def test_prepare_profile(self):
        tmp = tempfile.mkdtemp()
        try:
            prof, dl = os.path.join(tmp, "profile"), os.path.join(tmp, "dl")
            os.makedirs(os.path.join(prof, "Default"))
            with open(os.path.join(prof, "Default", "Preferences"), "w") as f:
                json.dump({"keep": 1, "profile": {"exit_type": "Crashed", "name": "x"}}, f)
            sess.prepare_profile(options.normalise(RAW), prof, dl)
            prefs = json.load(open(os.path.join(prof, "Default", "Preferences")))
            self.assertEqual(prefs["keep"], 1)
            self.assertEqual(prefs["profile"]["name"], "x")
            self.assertEqual(prefs["profile"]["exit_type"], "Normal")
            self.assertFalse(prefs["credentials_enable_service"])
            self.assertFalse(prefs["translate"]["enabled"])
            self.assertEqual(prefs["download"]["default_directory"], dl)
            self.assertTrue(os.path.isdir(dl))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FakeChromium:
    """/json endpoints and a browser WebSocket like Chromium's DevTools server."""

    def __init__(self):
        self.pages = [{"id": "T1", "type": "page", "title": "Sparkasse", "url": "https://banking.sparkasse.de/konto"},
                      {"id": "T2", "type": "page", "title": "WhatsApp", "url": "https://web.whatsapp.com/"},
                      {"id": "W1", "type": "service_worker", "url": "https://web.whatsapp.com/sw.js"}]
        self.cookies = [{"name": "SID", "domain": ".sparkasse.de", "path": "/", "secure": True, "httpOnly": True},
                        {"name": "wa", "domain": "web.whatsapp.com", "path": "/"}]
        self.calls, self.closed, self.activated, self.new = [], [], [], []

    def app(self):
        a = web.Application()

        async def lst(r):
            return web.json_response(self.pages)

        async def new(r):
            self.new.append((r.method, r.query_string))
            return web.json_response({"id": "T9", "type": "page"})

        async def close(r):
            self.closed.append(r.match_info["id"])
            self.pages = [p for p in self.pages if p["id"] != r.match_info["id"]]
            return web.Response(text="Target is closing")

        async def activate(r):
            self.activated.append(r.match_info["id"])
            return web.Response(text="Target activated")

        async def version(r):
            return web.json_response({"webSocketDebuggerUrl": f"ws://{r.host}/devtools/browser/abc"})

        async def ws(r):
            w = web.WebSocketResponse()
            await w.prepare(r)
            async for msg in w:
                m = json.loads(msg.data)
                self.calls.append((m["method"], m["params"]))
                await w.send_json({"method": "Target.targetInfoChanged", "params": {}})   # an event first
                result = {}
                if m["method"] == "Storage.getCookies":
                    result = {"cookies": self.cookies}
                elif m["method"] == "Storage.setCookies":
                    names = {c["name"] for c in m["params"]["cookies"] if c["expires"] == 1}
                    self.cookies = [c for c in self.cookies if c["name"] not in names]
                await w.send_json({"id": m["id"], "result": result})
            return w

        a.router.add_get("/json/list", lst)
        a.router.add_put("/json/new", new)
        a.router.add_get("/json/close/{id}", close)
        a.router.add_get("/json/activate/{id}", activate)
        a.router.add_get("/json/version", version)
        a.router.add_get("/devtools/browser/abc", ws)
        return a


class CdpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeChromium()
        self.srv = TestServer(self.fake.app())
        self.client = TestClient(self.srv)
        await self.client.start_server()
        self._saved = cdp.CDP
        cdp.CDP = str(self.srv.make_url("")).rstrip("/")

    async def asyncTearDown(self):
        cdp.CDP = self._saved
        await self.client.close()

    async def test_pages_and_open(self):
        s = self.client.session
        self.assertEqual([p["id"] for p in await cdp.pages(s)], ["T1", "T2"])
        self.assertEqual(await cdp.open_url(s, "https://web.whatsapp.com/"), {"id": "T2", "reused": True})
        self.assertEqual(self.fake.activated, ["T2"])
        res = await cdp.open_url(s, "http://192.168.178.1/")
        self.assertEqual(res, {"id": "T9", "reused": False})
        self.assertEqual(self.fake.new[0][0], "PUT", "Chromium verlangt PUT fuer /json/new")

    async def test_logout_only_hits_the_bank(self):
        s = self.client.session
        summary = await cdp.logout(s, ["sparkasse.de"])
        self.assertEqual(summary["tabs_closed"], 1)
        self.assertEqual(summary["cookies_deleted"], 1)
        self.assertEqual(self.fake.closed, ["T1"])
        self.assertEqual([c["name"] for c in self.fake.cookies], ["wa"], "WhatsApp bleibt angemeldet")
        origins = [p["origin"] for m, p in self.fake.calls if m == "Storage.clearDataForOrigin"]
        self.assertIn("https://sparkasse.de", origins)
        self.assertIn("https://www.sparkasse.de", origins)
        self.assertFalse(any("whatsapp" in o for o in origins))
        self.assertEqual(await cdp.logout(s, []), {"tabs_closed": 0, "cookies_deleted": 0, "origins_cleared": 0})


class FakeProc:
    def __init__(self, name):
        self.name, self.returncode, self.signals = name, None, []

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = -sig


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SessionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = (sess.PROFILE, sess.DOWNLOADS, sess._port_open, sess.X_SOCKET, cdp.logout, cdp.close_browser)
        sess.PROFILE, sess.DOWNLOADS = os.path.join(self.tmp, "p"), os.path.join(self.tmp, "d")
        sess.X_SOCKET = os.path.join(self.tmp, "X99")

        async def port_open(port, host="127.0.0.1"):
            return True
        sess._port_open = port_open
        self.logouts = []

        async def fake_logout(session, domains, connect=None):
            self.logouts.append(domains)
            return {"tabs_closed": 1, "cookies_deleted": 2, "origins_cleared": 2}

        async def fake_close(session, connect=None):
            for p in self.procs.values():
                if p.name == "chromium":
                    p.returncode = 0
        cdp.logout, cdp.close_browser = fake_logout, fake_close
        self.procs = {}

        async def spawn(name, argv, env=None):
            p = FakeProc(name)
            self.procs[f"{name}{len(self.procs)}"] = p
            if name == "xvfb":
                open(sess.X_SOCKET, "w").close()
            return p
        self.clock = Clock()
        self.s = sess.Session(options.normalise(RAW), spawn=spawn, clock=self.clock, http=object())

    async def asyncTearDown(self):
        (sess.PROFILE, sess.DOWNLOADS, sess._port_open, sess.X_SOCKET, cdp.logout, cdp.close_browser) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_wake_sleep_and_idle(self):
        st = await self.s.wake(1600, 900)
        self.assertEqual(st["state"], "running")
        self.assertEqual(sorted(p.name for p in self.procs.values()), ["chromium", "x11vnc", "xvfb"])
        self.assertEqual(st["sleep_in_seconds"], 900)
        self.assertEqual((await self.s.wake())["state"], "running", "zweites Wecken startet nichts neu")
        self.assertEqual(len(self.procs), 3)

        self.clock.t += 14 * 60
        await self.s.tick()
        self.assertEqual(self.s.state, "running")
        self.s.touch()                                   # user did something
        self.clock.t += 14 * 60
        await self.s.tick()
        self.assertEqual(self.s.state, "running")
        self.clock.t += 61
        await self.s.tick()
        self.assertEqual(self.s.state, "sleeping")
        self.assertEqual(self.logouts, [["banking.example.org", "sparkasse.de"]])
        self.assertTrue(all(p.returncode is not None for p in self.procs.values()), "nichts laeuft mehr")

    async def test_never_sleep_with_zero(self):
        self.s.opts["idle_minutes"] = 0
        await self.s.wake()
        self.clock.t += 7 * 24 * 3600
        await self.s.tick()
        self.assertEqual(self.s.state, "running")
        self.assertIsNone(self.s.status()["sleep_in_seconds"])

    async def test_closed_chromium_comes_back_but_not_forever(self):
        await self.s.wake()
        for i in range(3):
            [p for p in self.procs.values() if p.name == "chromium"][-1].returncode = 0
            await self.s.tick()
            self.assertEqual(self.s.state, "running", i)
        self.assertEqual(sum(p.name == "chromium" for p in self.procs.values()), 4)
        [p for p in self.procs.values() if p.name == "chromium"][-1].returncode = 0
        await self.s.tick()
        self.assertEqual(self.s.state, "sleeping", "Absturzschleife")

    async def test_failed_start_leaves_nothing_running(self):
        async def never(port, host="127.0.0.1"):
            return False
        sess._port_open = never
        real_sleep = asyncio.sleep

        async def fast_sleep(s):
            self.clock.t += 5
            await real_sleep(0)
        sess.asyncio.sleep = fast_sleep
        try:
            st = await self.s.wake()
        finally:
            sess.asyncio.sleep = real_sleep
        self.assertEqual(st["state"], "sleeping")
        self.assertIn("startet nicht", st["error"])
        self.assertTrue(all(p.returncode is not None for p in self.procs.values()))


class FakeSession:
    def __init__(self):
        self.state = "sleeping"
        self.touched = 0
        self.viewers = 0

    def status(self):
        return {"state": self.state, "sleep_in_seconds": None, "error": ""}

    async def wake(self, w, h):
        self.state = "running"
        self.size = (w, h)
        return self.status()

    async def sleep(self, reason=""):
        self.state = "sleeping"
        return self.status()

    def touch(self):
        self.touched += 1

    def viewer_joined(self):
        self.viewers += 1

    def viewer_left(self):
        self.viewers -= 1

    async def run_watcher(self):
        await asyncio.sleep(3600)


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def make(self, allowed):
        self.fs = FakeSession()
        app = server.create_app(options.normalise(RAW), session=self.fs, allowed_ips=allowed,
                                local_token="SECRET-TOKEN")
        c = TestClient(TestServer(app))
        await c.start_server()
        self.addAsyncCleanup(c.close)
        return c

    async def test_only_ingress_may_use_the_api(self):
        c = await self.make({"172.30.32.2"})          # the test client comes from 127.0.0.1, like Chromium would
        for path in ("/", "/api/status", "/api/ui", "/websockify"):
            self.assertEqual((await c.get(path)).status, 403, path)
        r = await c.post("/api/wake", json={}, headers={"X-CLB": "1"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.fs.state, "sleeping")
        r = await c.get("/start")
        self.assertEqual(r.status, 200, "die Startseite ist fuer Chromium selbst")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        html = await r.text()
        self.assertIn("web.whatsapp.com", html)
        self.assertIn("Sparkasse 🔒", html)
        self.assertNotIn("javascript:", html)
        self.assertIn("SECRET-TOKEN", html)

    async def test_start_page_api_needs_the_secret(self):
        c = await self.make({"172.30.32.2"})
        self.fs.state = "running"
        # a web page in the browser comes from 127.0.0.1 too, but does not know the secret
        for headers in ({}, {"X-CLB-Local": "falsch"}, {"X-CLB": "1"}):
            self.assertEqual((await c.get("/api/local/status", headers=headers)).status, 403, headers)
            self.assertEqual((await c.post("/api/local/sleep", headers=headers)).status, 403, headers)
        self.assertEqual(self.fs.state, "running")
        ok = {"X-CLB-Local": "SECRET-TOKEN"}
        self.assertEqual((await (await c.get("/api/local/status", headers=ok)).json())["state"], "running")
        self.assertEqual((await c.post("/api/local/clipboard", headers=ok)).status, 200)
        self.assertEqual((await c.post("/api/local/sleep", headers=ok)).status, 200)
        for _ in range(50):
            if self.fs.state == "sleeping":
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.fs.state, "sleeping")

    async def test_clipboard_request_reaches_the_sidebar_once(self):
        fs = FakeSession()
        fs.state = "running"
        app = server.create_app(options.normalise(RAW), session=fs, allowed_ips={"127.0.0.1"},
                                local_token="SECRET-TOKEN")
        c = TestClient(TestServer(app))
        await c.start_server()
        self.addAsyncCleanup(c.close)
        await c.post("/api/local/clipboard", headers={"X-CLB-Local": "SECRET-TOKEN"})
        await c.post("/api/local/clipboard", headers={"X-CLB-Local": "SECRET-TOKEN"})
        self.assertEqual((await (await c.get("/api/ui")).json())["requests"], ["clipboard"])
        self.assertEqual((await (await c.get("/api/ui")).json())["requests"], [])

    async def test_api_through_ingress(self):
        c = await self.make({"127.0.0.1"})
        html = await (await c.get("/")).text()
        self.assertIn('"name": "Sparkasse"', html)
        self.assertNotIn("/*__CONFIG__*/null", html)
        v = server.asset_version()
        self.assertIn(f'src="assets/app-{v}.js"', html, "neuer Dateiname je Version gegen Proxy-Caches")
        r = await c.get(f"/assets/app-{v}.js")
        self.assertEqual(r.status, 200)
        self.assertIn("clip-close", await r.text())
        self.assertEqual(r.content_type, "application/javascript")
        self.assertEqual((await c.get("/assets/app-000000000000.js")).status, 404, "alte Version gibt es nicht mehr")
        r = await c.get("/static/app.js")
        self.assertEqual((r.status, r.headers["Cache-Control"]), (200, "no-cache"))
        self.assertEqual((await c.post("/api/wake", json={"w": 1400, "h": 800})).status, 400, "ohne X-CLB")
        r = await c.post("/api/wake", json={"w": 1400, "h": 800}, headers={"X-CLB": "1"})
        self.assertEqual((r.status, (await r.json())["state"]), (200, "running"))
        self.assertEqual(self.fs.size, (1400, 800))
        await c.post("/api/activity", json={}, headers={"X-CLB": "1"})
        self.assertEqual(self.fs.touched, 1)
        r = await c.post("/api/open", json={"url": "file:///etc/passwd"}, headers={"X-CLB": "1"})
        self.assertEqual(r.status, 400)
        r = await c.post("/api/open", json={"site": 7}, headers={"X-CLB": "1"})
        self.assertEqual(r.status, 400)
        r = await c.post("/api/sleep", json={}, headers={"X-CLB": "1"})
        self.assertEqual((await r.json())["state"], "sleeping")
        self.assertEqual((await c.get("/websockify")).status, 503, "schlafend kein Bild")

    async def test_websocket_proxies_to_vnc(self):
        received = []

        async def vnc(reader, writer):
            writer.write(b"RFB 003.008\n")
            await writer.drain()
            received.append(await reader.read(12))
            writer.close()
        vnc_srv = await asyncio.start_server(vnc, "127.0.0.1", 0)
        self.addAsyncCleanup(vnc_srv.wait_closed)
        self.addCleanup(vnc_srv.close)
        saved = sess.VNC_PORT
        sess.VNC_PORT = vnc_srv.sockets[0].getsockname()[1]
        self.addCleanup(setattr, sess, "VNC_PORT", saved)
        c = await self.make({"127.0.0.1"})
        self.fs.state = "running"
        ws = await c.ws_connect("/websockify", protocols=("binary",))
        msg = await ws.receive(timeout=5)
        self.assertEqual(msg.data, b"RFB 003.008\n")
        self.assertEqual(self.fs.viewers, 1)
        await ws.send_bytes(b"RFB 003.008\n")
        for _ in range(100):
            if received:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(received, [b"RFB 003.008\n"])
        await ws.close()
        for _ in range(100):
            if self.fs.viewers == 0:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(self.fs.viewers, 0)


if __name__ == "__main__":
    unittest.main()
