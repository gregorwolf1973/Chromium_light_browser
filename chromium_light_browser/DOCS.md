# Chromium Light Browser

A browser in the Home Assistant sidebar that only uses memory while you use it.
Chromium, a virtual display and a VNC server start when you open the panel and
are stopped after `idle_minutes` without activity. The picture reaches your
browser through noVNC over ingress - no port is published.

## Using it

- **Sleeping**: the panel shows "Browser schläft" and your site tiles. Click
  *Browser starten* or a tile. The virtual screen takes the size of the panel.
- **Bar**: site tiles (🔒 = logged out when the browser sleeps), open tabs
  (click to switch, ✕ to close), clipboard, fullscreen, **💤 Schlafen** to free
  the memory right away. The bar shows when the browser will fall asleep.
- **Clipboard**: text copied inside the browser appears in the 📋 panel; text
  pasted there goes into the browser with *In den Browser*.
- **Downloads** go to `/share/browser`, uploads can be picked from there too.
- **Logins** (cookies, WhatsApp Web pairing) are kept in `/data/profile` and
  survive sleeping, restarts and updates - except for sites with
  `logout_on_sleep`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `idle_minutes` | `15` | Minutes without activity before the browser sleeps. `0` = never. |
| `sites` | WhatsApp, Sparkasse | Tiles: `name`, `url`, `logout_on_sleep`. |
| `logout_domains` | `[]` | More domains to log out of when sleeping (subdomains included). |
| `save_passwords` | `false` | Let Chromium offer to save passwords. |
| `low_memory_mode` | `true` | Chromium's low-end device mode. |
| `renderer_process_limit` | `4` | Max. processes for web pages. |
| `language` | `de` | UI and page language. |

Example:

```yaml
sites:
  - name: WhatsApp
    url: https://web.whatsapp.com
    logout_on_sleep: false
  - name: Sparkasse
    url: https://www.sparkasse-musterstadt.de
    logout_on_sleep: true
  - name: Router
    url: http://192.168.178.1
logout_domains:
  - s-banking.de
```

## Activity and sleeping

Activity means: opening the panel, clicking a tile, and any mouse, keyboard or
touch input in the panel. Watching without touching anything does not count, so
a forgotten tab still falls asleep. With `idle_minutes: 0` it never does.

While the browser sleeps nothing is loaded: **WhatsApp Web receives no
messages** until you open it again (it then syncs). If you want it always
connected, set `idle_minutes: 0` - it then permanently uses about as much
memory as while you look at it.

## Online banking

For sites with `logout_on_sleep: true` and every `logout_domains` entry the
add-on, before Chromium stops:

1. closes all tabs of that domain (and its subdomains),
2. deletes its cookies,
3. clears its site data (local/session storage, IndexedDB, caches, service workers).

Please also keep in mind:

- The browser runs on your Home Assistant host. Anyone with access to your
  Home Assistant sidebar can open it - including a banking session that has not
  fallen asleep yet. Use *💤 Schlafen* when you are done.
- Do not save banking passwords (`save_passwords: false`).
- If your bank redirects to a different domain for the login, add it to
  `logout_domains`. The log shows which domains were logged out.

## Memory

Typical values: sleeping ~30 MB (the controller only), one page on your
network ~250-300 MB, WhatsApp Web plus banking ~550-700 MB. WhatsApp Web itself
is the heaviest part in any browser.

## Limits

- No sound (no WhatsApp calls, voice messages do not play).
- Videos and animations are choppy - this is a remote picture.
- The screen size is fixed at start. After resizing the panel a lot, put the
  browser to sleep and start it again for a sharp picture.

## Security notes

- No port is published; the controller only answers the Supervisor's ingress
  proxy. Web pages inside the browser can reach neither the VNC stream nor the
  API nor Chromium's DevTools.
- Chromium runs as an unprivileged user with `--no-sandbox` (the container has
  no user namespaces for Chromium's own sandbox).
