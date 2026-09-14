#!/usr/bin/env python3
"""Talking to Chromium over the DevTools protocol on 127.0.0.1.

Only the controller uses this port. Web pages inside the browser cannot: the
HTTP endpoints check the Host header and Chromium refuses WebSocket
connections that carry a page Origin (no --remote-allow-origins is set).
"""
import itertools
import json
from urllib.parse import quote, urlsplit

import aiohttp

import options

CDP = "http://127.0.0.1:9222"


class CdpError(Exception):
    pass


async def _get_json(session, path, method="GET"):
    async with session.request(method, CDP + path, timeout=aiohttp.ClientTimeout(total=5)) as r:
        if r.status >= 400:
            raise CdpError(f"{path}: HTTP {r.status}")
        text = await r.text()
        return json.loads(text) if text.strip().startswith(("{", "[")) else text


async def pages(session):
    data = await _get_json(session, "/json/list")
    return [{"id": t["id"], "title": t.get("title") or "", "url": t.get("url") or ""}
            for t in data if t.get("type") == "page"]


async def open_url(session, url, reuse=True):
    """Bring an existing tab of the same host to the front, else open a new one."""
    host = (urlsplit(url).hostname or "").lower()
    if reuse and host:
        for p in await pages(session):
            if (urlsplit(p["url"]).hostname or "").lower() == host:
                await _get_json(session, f"/json/activate/{p['id']}")
                return {"id": p["id"], "reused": True}
    t = await _get_json(session, "/json/new?" + quote(url, safe=":/?&=#%"), method="PUT")
    return {"id": t.get("id") if isinstance(t, dict) else None, "reused": False}


async def activate(session, target_id):
    await _get_json(session, f"/json/activate/{quote(target_id)}")


async def close(session, target_id):
    await _get_json(session, f"/json/close/{quote(target_id)}")


class BrowserConnection:
    """Browser-level DevTools WebSocket with request/response matching."""

    def __init__(self, ws):
        self.ws = ws
        self._ids = itertools.count(1)

    async def call(self, method, params=None):
        mid = next(self._ids)
        await self.ws.send_json({"id": mid, "method": method, "params": params or {}})
        while True:
            msg = await self.ws.receive(timeout=10)
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise CdpError(f"{method}: connection closed")
            data = json.loads(msg.data)
            if data.get("id") != mid:
                continue                    # an event, not our answer
            if "error" in data:
                raise CdpError(f"{method}: {data['error'].get('message')}")
            return data.get("result") or {}


def expired_cookie(c):
    """A CookieParam that overwrites cookie c with an expiry in the past -
    Chromium deletes it on the spot."""
    out = {"name": c["name"], "value": "", "domain": c["domain"], "path": c.get("path") or "/",
           "secure": bool(c.get("secure")), "httpOnly": bool(c.get("httpOnly")), "expires": 1}
    if c.get("sameSite"):
        out["sameSite"] = c["sameSite"]
    if c.get("partitionKey"):
        out["partitionKey"] = c["partitionKey"]
    return out


async def logout(session, domains, connect=None):
    """Close the tabs of these domains and wipe their cookies and site data.

    Returns a summary dict. Every step is best effort: a browser that is
    already half gone must not keep the add-on from going to sleep.
    """
    summary = {"tabs_closed": 0, "cookies_deleted": 0, "origins_cleared": 0}
    if not domains:
        return summary
    for p in await pages(session):
        if options.host_matches(urlsplit(p["url"]).hostname, domains):
            try:
                await close(session, p["id"])
                summary["tabs_closed"] += 1
            except CdpError:
                pass
    version = await _get_json(session, "/json/version")
    ws_url = version.get("webSocketDebuggerUrl") if isinstance(version, dict) else None
    if not ws_url:
        raise CdpError("no browser WebSocket")
    ws = await (connect or session.ws_connect)(ws_url, max_msg_size=64 * 1024 * 1024)
    try:
        conn = BrowserConnection(ws)
        cookies = (await conn.call("Storage.getCookies")).get("cookies") or []
        doomed = [c for c in cookies if options.host_matches(c.get("domain"), domains)]
        if doomed:
            await conn.call("Storage.setCookies", {"cookies": [expired_cookie(c) for c in doomed]})
            summary["cookies_deleted"] = len(doomed)
        hosts = {c["domain"].lstrip(".") for c in doomed} | set(domains) | {"www." + d for d in domains}
        for h in sorted(hosts):
            try:
                await conn.call("Storage.clearDataForOrigin", {
                    "origin": f"https://{h}",
                    "storageTypes": "cookies,local_storage,session_storage,indexeddb,cache_storage,service_workers,websql",
                })
                summary["origins_cleared"] += 1
            except CdpError:
                pass
    finally:
        await ws.close()
    return summary


async def close_browser(session, connect=None):
    """Ask Chromium to quit cleanly, so the profile (logins) is flushed to disk."""
    version = await _get_json(session, "/json/version")
    ws = await (connect or session.ws_connect)(version["webSocketDebuggerUrl"])
    try:
        await ws.send_json({"id": 1, "method": "Browser.close", "params": {}})
    finally:
        await ws.close()
