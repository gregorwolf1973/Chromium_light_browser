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
  startUiPolling();
}

function disconnect() {
  stopUiPolling();
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
