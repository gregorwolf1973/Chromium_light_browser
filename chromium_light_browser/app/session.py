#!/usr/bin/env python3
"""The browser session: virtual display, Chromium and VNC server, on demand.

Nothing runs while the browser sleeps - that is the whole point compared with
a desktop-in-a-container that keeps Firefox in memory around the clock.
wake() starts Xvfb at the size of the viewer's window, Chromium in it and
x11vnc on 127.0.0.1:5900; sleep() logs out of the configured sites, lets
Chromium close cleanly (so logins are written to the profile) and stops all
three. An idle watcher calls sleep() after `idle_minutes` without activity.
"""
import asyncio
import json
import os
import shutil
import signal
import time

import aiohttp

import cdp
import options

DISPLAY = ":99"
X_SOCKET = "/tmp/.X11-unix/X99"
VNC_PORT = 5900
CDP_PORT = 9222
PROFILE = "/data/profile"
DOWNLOADS = "/share/browser"
RUN_AS = "browser"
MIN_W, MIN_H, MAX_W, MAX_H = 1024, 640, 2560, 1600

SLEEPING, STARTING, RUNNING, STOPPING = "sleeping", "starting", "running", "stopping"


def log(msg):
    print(f"[browser] {msg}", flush=True)


def clamp_size(w, h):
    try:
        w, h = int(w), int(h)
    except (TypeError, ValueError):
        w, h = 1280, 800
    w = max(MIN_W, min(MAX_W, w)) // 2 * 2          # even sizes keep VNC encoders happy
    h = max(MIN_H, min(MAX_H, h)) // 2 * 2
    return w, h


def chromium_binary():
    return shutil.which("chromium-browser") or shutil.which("chromium") or "chromium-browser"


def chromium_args(opts, w, h, start_url):
    args = [
        "--no-sandbox",              # container without user namespaces; runs as an unprivileged user
        "--test-type",               # hides the "unsupported flag" warning bar that --no-sandbox brings
        f"--user-data-dir={PROFILE}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-dev-shm-usage", "--disable-gpu",
        f"--renderer-process-limit={opts['renderer_process_limit']}",
        "--disk-cache-size=67108864",
        "--disable-features=Translate,MediaRouter,OptimizationHints,AutofillServerCommunication",
        "--password-store=basic",
        f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
        "--window-position=0,0", f"--window-size={w},{h}", "--start-maximized",
        f"--lang={opts['language']}",
        "--hide-crash-restore-bubble",
        "--mute-audio", "--autoplay-policy=user-gesture-required",
    ]
    if opts["low_memory_mode"]:
        args.append("--enable-low-end-device-mode")
    return [chromium_binary(), *args, start_url]


def prepare_profile(opts, profile=PROFILE, downloads=DOWNLOADS):
    """Write the preferences Chromium reads at start: download folder, no
    password saving (unless allowed), and a clean exit flag so no "restore
    pages?" bubble covers the page after a hard stop."""
    default = os.path.join(profile, "Default")
    os.makedirs(default, exist_ok=True)
    os.makedirs(downloads, exist_ok=True)
    path = os.path.join(default, "Preferences")
    try:
        with open(path, encoding="utf-8") as f:
            prefs = json.load(f)
        if not isinstance(prefs, dict):
            prefs = {}
    except (OSError, ValueError):
        prefs = {}
    prefs.setdefault("download", {}).update({"default_directory": downloads, "prompt_for_download": False,
                                              "directory_upgrade": True})
    prefs.setdefault("savefile", {})["default_directory"] = downloads
    prefs["credentials_enable_service"] = bool(opts["save_passwords"])
    prefs.setdefault("profile", {})["password_manager_enabled"] = bool(opts["save_passwords"])
    prefs["profile"]["exit_type"] = "Normal"
    prefs["profile"]["exited_cleanly"] = True
    prefs.setdefault("intl", {})["accept_languages"] = f"{opts['language']},en"
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(prefs, f)
    os.replace(tmp, path)
    return prefs


def _chown_tree(path, user=RUN_AS):
    try:
        import pwd
        pw = pwd.getpwnam(user)
    except (ImportError, KeyError):
        return
    for root, dirs, files in os.walk(path):
        for name in [root] + [os.path.join(root, n) for n in dirs + files]:
            try:
                os.lchown(name, pw.pw_uid, pw.pw_gid)
            except OSError:
                pass


async def _port_open(port, host="127.0.0.1"):
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 1)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


class Session:
    def __init__(self, opts, start_url="http://127.0.0.1:8099/start", spawn=None, clock=time.monotonic,
                 http=None):
        self.opts = opts
        self.start_url = start_url
        self.state = SLEEPING
        self.size = None
        self.error = ""
        self.viewers = 0
        self.last_activity = clock()
        self.started_at = None
        self._clock = clock
        self._spawn = spawn or self._spawn_process
        self._http = http
        self._procs = {}
        self._lock = asyncio.Lock()
        self._watch = None
        self._restarts = []

    # ── activity ────────────────────────────────────────────────────────────
    def touch(self):
        self.last_activity = self._clock()

    def viewer_joined(self):
        self.viewers += 1
        self.touch()

    def viewer_left(self):
        self.viewers = max(0, self.viewers - 1)
        self.touch()

    def idle_seconds(self):
        return self._clock() - self.last_activity

    def should_sleep(self):
        minutes = self.opts["idle_minutes"]
        return self.state == RUNNING and minutes > 0 and self.idle_seconds() >= minutes * 60

    def status(self):
        minutes = self.opts["idle_minutes"]
        left = None
        if self.state == RUNNING and minutes > 0:
            left = max(0, int(minutes * 60 - self.idle_seconds()))
        return {"state": self.state, "size": self.size, "viewers": self.viewers, "error": self.error,
                "idle_minutes": minutes, "sleep_in_seconds": left}

    # ── processes ───────────────────────────────────────────────────────────
    async def _spawn_process(self, name, argv, env=None):
        full_env = dict(os.environ, **(env or {}))
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            argv = ["su-exec", RUN_AS, *argv]
            full_env["HOME"] = "/home/browser"
        return await asyncio.create_subprocess_exec(*argv, env=full_env, stdin=asyncio.subprocess.DEVNULL,
                                                    start_new_session=True)

    async def _wait_for(self, check, seconds, what):
        end = self._clock() + seconds
        while self._clock() < end:
            if await check():
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"{what} startet nicht")

    async def _start_chromium(self):
        w, h = self.size
        self._procs["chromium"] = await self._spawn(
            "chromium", chromium_args(self.opts, w, h, self.start_url), {"DISPLAY": DISPLAY})

    async def wake(self, w=1280, h=800):
        async with self._lock:
            if self.state in (RUNNING, STARTING):
                self.touch()
                return self.status()
            self.state, self.error = STARTING, ""
            self.size = clamp_size(w, h)
            self.touch()
            try:
                for stale in ("/tmp/.X99-lock", X_SOCKET):
                    try:
                        os.unlink(stale)
                    except OSError:
                        pass
                try:                                # Xvfb runs unprivileged and needs this sticky dir
                    os.makedirs(os.path.dirname(X_SOCKET), exist_ok=True)
                    os.chmod(os.path.dirname(X_SOCKET), 0o1777)
                except OSError:
                    pass
                prepare_profile(self.opts, PROFILE, DOWNLOADS)
                _chown_tree(PROFILE)
                _chown_tree(DOWNLOADS)
                w, h = self.size
                self._procs["xvfb"] = await self._spawn(
                    "xvfb", ["Xvfb", DISPLAY, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp", "-dpi", "96"])
                await self._wait_for(self._x_ready, 10, "Xvfb")
                await self._start_chromium()
                self._procs["x11vnc"] = await self._spawn(
                    "x11vnc", ["x11vnc", "-display", DISPLAY, "-rfbport", str(VNC_PORT), "-localhost", "-nopw",
                               "-forever", "-shared", "-noxdamage", "-xkb", "-ncache", "0", "-quiet"])
                await self._wait_for(lambda: _port_open(VNC_PORT), 15, "x11vnc")
                await self._wait_for(lambda: _port_open(CDP_PORT), 30, "Chromium")
            except Exception as e:                  # any failure: leave nothing half running
                self.error = str(e)
                log(f"Start fehlgeschlagen: {e}")
                await self._kill_all()
                self.state = SLEEPING
                return self.status()
            self.state = RUNNING
            self.started_at = self._clock()
            self._restarts.clear()
            log(f"Browser gestartet ({w}x{h})")
            return self.status()

    async def _x_ready(self):
        return os.path.exists(X_SOCKET)

    async def sleep(self, reason="manuell"):
        async with self._lock:
            if self.state not in (RUNNING, STARTING):
                return self.status()
            self.state = STOPPING
            log(f"Browser schlaeft ein ({reason})")
            domains = options.logout_domains(self.opts)
            session = self._http or aiohttp.ClientSession()
            try:
                if domains:
                    try:
                        s = await cdp.logout(session, domains)
                        log(f"Abgemeldet von {', '.join(domains)}: {s['tabs_closed']} Tabs geschlossen, "
                            f"{s['cookies_deleted']} Cookies geloescht")
                    except Exception as e:
                        log(f"Abmelden unvollstaendig: {type(e).__name__}: {e}")
                try:
                    await cdp.close_browser(session)
                except Exception:
                    pass
            finally:
                if self._http is None:
                    await session.close()
            await self._kill_all()
            self.state = SLEEPING
            self.size = None
            return self.status()

    async def _kill_all(self):
        for name in ("chromium", "x11vnc", "xvfb"):
            p = self._procs.pop(name, None)
            if p is None or p.returncode is not None:
                continue
            try:
                await asyncio.wait_for(p.wait(), 8 if name == "chromium" else 0.1)
                continue
            except asyncio.TimeoutError:
                pass
            for sig, wait in ((signal.SIGTERM, 5), (getattr(signal, "SIGKILL", signal.SIGTERM), 2)):
                try:
                    p.send_signal(sig)
                    await asyncio.wait_for(p.wait(), wait)
                    break
                except (ProcessLookupError, asyncio.TimeoutError):
                    continue

    # ── watchers ────────────────────────────────────────────────────────────
    async def tick(self):
        """One watcher round: idle sleep, and bring Chromium back if its last
        window was closed (the session stays, the start page returns)."""
        if self.should_sleep():
            await self.sleep(f"{self.opts['idle_minutes']} min ohne Nutzung")
            return
        if self.state != RUNNING:
            return
        p = self._procs.get("chromium")
        if p is not None and p.returncode is not None:
            now = self._clock()
            self._restarts = [t for t in self._restarts if now - t < 120] + [now]
            if len(self._restarts) > 3:
                log("Chromium beendet sich wiederholt - Browser schlaeft ein")
                await self.sleep("Chromium stuerzt ab")
                return
            log("Chromium wurde beendet - starte mit der Startseite neu")
            async with self._lock:
                await self._start_chromium()
        for name in ("xvfb", "x11vnc"):
            q = self._procs.get(name)
            if q is not None and q.returncode is not None:
                log(f"{name} beendet - Browser schlaeft ein")
                await self.sleep(f"{name} beendet")
                return

    async def run_watcher(self, interval=10):
        while True:
            try:
                await self.tick()
            except Exception as e:
                log(f"Watcher: {type(e).__name__}: {e}")
            await asyncio.sleep(interval)
