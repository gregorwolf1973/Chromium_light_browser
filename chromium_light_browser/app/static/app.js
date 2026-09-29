// Chromium Light Browser - sidebar page. No toolbar: the picture fills the
// panel, everything else lives on the start page inside the browser.
// All URLs are relative: under ingress the page lives below
// /api/hassio_ingress/<token>/, so nothing may start with "/".
import RFB from '../novnc/core/rfb.js';

const CFG = window.CLB_CONFIG || {sites: [], idle_minutes: 15};
const $ = id => document.getElementById(id);
const XK_Control_L = 0xffe3, XK_v = 0x0076;
let rfb = null;
let state = 'sleeping';
let wanted = false;          // the user wants a live picture
let lastActivityPost = 0;
let hiddenTimer = null;
let uiTimer = null;
let lastRemoteClip = '';

function esc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]); }

async function api(path, body) {
  const opts = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CLB': '1'},
                                          body: JSON.stringify(body)};
  const res = await fetch('api/' + path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok && !data.state) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function toast(text) {
  const t = $('toast');
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 2200);
}

function viewSize() {
  return {w: Math.round(window.innerWidth), h: Math.round(window.innerHeight)};
}

// ── overlay (only while the browser is not running) ────────────────────────
function showOverlay(kind, msg) {
  const card = $('card');
  if (kind === 'starting') {
    card.innerHTML = '<div class="spinner"></div><h1>Browser startet …</h1><p>Das dauert ein paar Sekunden.</p>';
  } else if (kind === 'stopping') {
    card.innerHTML = '<div class="spinner"></div><h1>Browser schläft ein …</h1><p>Abmelden und Speicher freigeben.</p>';
  } else {
    const idle = CFG.idle_minutes ? `Nach ${CFG.idle_minutes} Minuten ohne Nutzung schläft er wieder ein.` : 'Er bleibt an, bis du ihn schlafen legst.';
    card.innerHTML = `<h1>💤 Browser schläft</h1><p>Er belegt gerade keinen Arbeitsspeicher. ${idle}</p>
      <button class="primary" id="wake-btn">Browser starten</button>
      <div class="grid">${CFG.sites.map((s, i) => `<button data-site="${i}">${esc(s.name)}${s.logout ? ' 🔒' : ''}</button>`).join('')}</div>
      ${msg ? `<div class="err">${esc(msg)}</div>` : ''}`;
    $('wake-btn').onclick = () => wake();
    card.querySelectorAll('[data-site]').forEach(b => b.onclick = () => openSite(+b.dataset.site));
  }
  $('overlay').hidden = false;
}

function hideOverlay() { $('overlay').hidden = true; }

// ── session ────────────────────────────────────────────────────────────────
async function wake() {
  wanted = true;
  showOverlay('starting');
  try {
    const st = await api('wake', viewSize());
    applyStatus(st);
    if (st.state !== 'running') showOverlay('sleeping', st.error || 'Start fehlgeschlagen');
  } catch (e) {
    showOverlay('sleeping', e.message);
  }
}

async function openSite(i) {
  wanted = true;
  showOverlay('starting');
  try {
    const res = await api('open', {site: i, ...viewSize()});
    if (res.state && res.state !== 'running') { showOverlay('sleeping', res.error || 'Start fehlgeschlagen'); return; }
    await refreshStatus();
  } catch (e) {
    showOverlay('sleeping', e.message);
  }
}

function applyStatus(st) {
  state = st.state;
  if (st.state === 'running') {
    if (wanted && !document.hidden) connect();
  } else {
    disconnect();
    if (st.state === 'starting' || st.state === 'stopping') showOverlay(st.state);
    else showOverlay('sleeping', st.error);
  }
}

async function refreshStatus() {
  try { applyStatus(await api('status')); } catch (e) {}
}

// ── VNC ────────────────────────────────────────────────────────────────────
function wsUrl() {
  const base = location.pathname.replace(/[^/]*$/, '');
  return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + base + 'websockify';
}

function connect() {
  if (rfb) return;
  hideOverlay();
  rfb = new RFB($('vnc'), wsUrl(), {wsProtocols: ['binary']});
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.focusOnClick = true;
  rfb.background = '#000';
  rfb.addEventListener('connect', () => rfb && rfb.focus());
  rfb.addEventListener('disconnect', () => {
    rfb = null;
    // the start page's "Schlafen" ends the stream: look at the state instead of reconnecting blindly
    setTimeout(refreshStatus, 800);
  });
  rfb.addEventListener('clipboard', e => remoteCopied(e.detail.text));
  $('kbd-btn').hidden = false;
  startUiPolling();
}

function disconnect() {
  stopUiPolling();
  $('kbd-btn').hidden = true;
  if (!rfb) return;
  const r = rfb;
  rfb = null;
  try { r.disconnect(); } catch (e) {}
}

// ── requests from the start page (clipboard panel) ─────────────────────────
function startUiPolling() {
  stopUiPolling();
  uiTimer = setInterval(async () => {
    try {
      const d = await api('ui');
      if ((d.requests || []).includes('clipboard')) openClipPanel();
      if (d.state !== 'running') refreshStatus();
    } catch (e) {}
  }, 1500);
}

function stopUiPolling() {
  clearInterval(uiTimer);
  uiTimer = null;
}

// ── clipboard ──────────────────────────────────────────────────────────────
// Browser -> device: try to write it straight into the device clipboard; if the
// page is not allowed to, it waits in the panel.
async function remoteCopied(text) {
  lastRemoteClip = text;
  $('clip-text').value = text;
  try {
    await navigator.clipboard.writeText(text);
    toast('Kopiert – auch auf diesem Gerät');
  } catch (e) {}
}

// Device -> browser: Ctrl+V. The key press is held back from noVNC, the
// browser's own paste event hands over the text (no permission prompt), it is
// put into the remote clipboard, and only then Ctrl+V is sent - so the paste in
// Chromium gets the new text, not the old one.
let pasteArmed = false;
window.addEventListener('keydown', e => {
  if (!rfb || $('clip-panel').hidden === false) return;
  if ((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'v' || e.key === 'V')) {
    e.stopImmediatePropagation();          // noVNC must not see it; default action -> paste event
    pasteArmed = true;
    setTimeout(() => { if (pasteArmed) { pasteArmed = false; sendCtrlV(); } }, 300);   // no paste event came
  }
}, true);

document.addEventListener('paste', e => {
  if (!rfb || !pasteArmed) return;
  pasteArmed = false;
  e.preventDefault();
  const text = (e.clipboardData && e.clipboardData.getData('text/plain')) || '';
  if (text) rfb.clipboardPasteFrom(text);
  setTimeout(sendCtrlV, 60);
}, true);

function sendCtrlV() {
  if (!rfb) return;
  rfb.sendKey(XK_Control_L, 'ControlLeft', true);
  rfb.sendKey(XK_v, 'KeyV', true);
  rfb.sendKey(XK_v, 'KeyV', false);
  rfb.sendKey(XK_Control_L, 'ControlLeft', false);
}

function openClipPanel() {
  $('clip-text').value = lastRemoteClip;
  $('clip-panel').hidden = false;
  setTimeout(() => $('clip-text').select(), 0);
}
function closeClipPanel() {
  $('clip-panel').hidden = true;
  if (rfb) rfb.focus();
}
$('clip-close').onclick = closeClipPanel;
$('clip-send').onclick = () => {
  if (rfb) rfb.clipboardPasteFrom($('clip-text').value);
  closeClipPanel();
  toast('Im Browser mit Strg+V einfügen');
};
$('clip-copy').onclick = async () => {
  try { await navigator.clipboard.writeText($('clip-text').value); toast('Kopiert'); }
  catch (e) { $('clip-text').select(); document.execCommand('copy'); toast('Kopiert'); }
};

// ── on-screen keyboard (tablets, phones) ───────────────────────────────────
// The picture is a canvas, so tapping into a text field in Chromium never
// brings up the device keyboard. The ⌨ button focuses a hidden text field
// instead; what the device keyboard types there is sent on as key presses.
// The field is kept filled with padding so Backspace always has something to
// delete (Android sends no key codes, only the changed text), and autocorrect
// replacing a word shows up as "n characters removed, new text added".
const XK_BackSpace = 0xff08, XK_Return = 0xff0d, XK_Tab = 0xff09, XK_Escape = 0xff1b,
      XK_Left = 0xff51, XK_Up = 0xff52, XK_Right = 0xff53, XK_Down = 0xff54, XK_Delete = 0xffff;
const SPECIAL = {Backspace: XK_BackSpace, Enter: XK_Return, Tab: XK_Tab, Escape: XK_Escape, Delete: XK_Delete,
                 ArrowLeft: XK_Left, ArrowUp: XK_Up, ArrowRight: XK_Right, ArrowDown: XK_Down};
const PAD = '_'.repeat(64);
const kbd = $('kbd-input');
let kbdLast = PAD;

function keysymFor(ch) {
  if (ch === '\n') return XK_Return;
  const cp = ch.codePointAt(0);
  return (cp >= 0x20 && cp <= 0xff) ? cp : 0x01000000 + cp;      // Latin-1 as is, the rest as Unicode keysym
}
function tap(keysym) {
  if (!rfb) return;
  rfb.sendKey(keysym, null, true);
  rfb.sendKey(keysym, null, false);
}
function kbdReset() {
  kbd.value = PAD;
  kbdLast = PAD;
  try { kbd.setSelectionRange(PAD.length, PAD.length); } catch (e) {}
}
kbd.addEventListener('input', () => {
  const now = kbd.value;
  let same = 0;
  while (same < kbdLast.length && same < now.length && kbdLast[same] === now[same]) same++;
  for (let i = same; i < kbdLast.length; i++) tap(XK_BackSpace);
  for (const ch of now.slice(same)) tap(keysymFor(ch));
  kbdLast = now;
  if (now.length < 16 || now.length > 200) kbdReset();
});
kbd.addEventListener('keydown', e => {
  const ks = SPECIAL[e.key];
  if (!ks) return;                         // letters come through 'input'
  e.preventDefault();
  e.stopPropagation();
  tap(ks);
});
kbd.addEventListener('blur', () => $('kbd-btn').classList.remove('on'));
$('kbd-btn').addEventListener('pointerdown', e => e.preventDefault());   // keep the focus where it is
$('kbd-btn').onclick = () => {
  if (document.activeElement === kbd) { kbd.blur(); if (rfb) rfb.focus(); return; }
  kbdReset();
  kbd.focus();
  $('kbd-btn').classList.add('on');
};

// ── activity, visibility ───────────────────────────────────────────────────
function activity() {
  const now = Date.now();
  if (state === 'running' && now - lastActivityPost > 30000) {
    lastActivityPost = now;
    api('activity', {}).catch(() => {});
  }
}
['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(ev => window.addEventListener(ev, activity, {capture: true, passive: true}));

document.addEventListener('visibilitychange', () => {
  clearTimeout(hiddenTimer);
  if (document.hidden) {
    // no picture needed while the tab is in the background; the idle timer keeps counting
    hiddenTimer = setTimeout(disconnect, 60000);
  } else {
    refreshStatus();
  }
});

// ── start ──────────────────────────────────────────────────────────────────
(async () => {
  const st = await api('status').catch(() => ({state: 'sleeping'}));
  wanted = st.state === 'running';
  applyStatus(st);
})();
setInterval(() => { if (!rfb) refreshStatus(); }, 15000);
