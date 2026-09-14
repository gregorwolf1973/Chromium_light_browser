#!/usr/bin/env python3
"""Add-on options: defaults, validation, and the domains to log out of."""
import json
from urllib.parse import urlsplit

OPTIONS_FILE = "/data/options.json"

DEFAULTS = {
    "idle_minutes": 15,
    "sites": [],
    "logout_domains": [],
    "save_passwords": False,
    "low_memory_mode": True,
    "renderer_process_limit": 4,
    "language": "de",
}


def load(path=OPTIONS_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    return normalise(raw if isinstance(raw, dict) else {})


def _int(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def normalise(raw):
    opts = dict(DEFAULTS)
    opts["idle_minutes"] = _int(raw.get("idle_minutes"), DEFAULTS["idle_minutes"], 0, 1440)
    opts["renderer_process_limit"] = _int(raw.get("renderer_process_limit"), 4, 1, 16)
    opts["save_passwords"] = bool(raw.get("save_passwords", False))
    opts["low_memory_mode"] = bool(raw.get("low_memory_mode", True))
    lang = str(raw.get("language") or "de").strip()
    opts["language"] = lang if lang.replace("-", "").isalnum() and len(lang) <= 10 else "de"
    sites = []
    for s in raw.get("sites") or []:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "").strip()
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname:
            continue
        sites.append({
            "name": str(s.get("name") or urlsplit(url).hostname)[:40],
            "url": url,
            "logout_on_sleep": bool(s.get("logout_on_sleep", False)),
        })
    opts["sites"] = sites
    opts["logout_domains"] = [d for d in (normalise_domain(x) for x in raw.get("logout_domains") or []) if d]
    return opts


def normalise_domain(value):
    """'https://www.Sparkasse.de/x' / '.sparkasse.de' / 'sparkasse.de' -> 'sparkasse.de'."""
    v = str(value or "").strip().lower()
    if "://" in v:
        v = urlsplit(v).hostname or ""
    v = v.split("/")[0].split(":")[0].strip(".")
    if v.startswith("www."):
        v = v[4:]
    return v if "." in v else ""


def logout_domains(opts):
    """Domains whose cookies and site data are wiped when the browser sleeps."""
    out = {normalise_domain(s["url"]) for s in opts["sites"] if s.get("logout_on_sleep")}
    out.update(opts["logout_domains"])
    return sorted(d for d in out if d)


def host_matches(host, domains):
    """True for the domain itself and every subdomain: banking.sparkasse.de
    matches sparkasse.de, notsparkasse.de does not."""
    h = str(host or "").lower().strip(".")
    return any(h == d or h.endswith("." + d) for d in domains)
