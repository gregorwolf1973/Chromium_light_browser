// Chromium Light Browser - sidebar UI.
// All URLs are relative: under ingress the page lives below
// /api/hassio_ingress/<token>/, so nothing may start with "/".
import RFB from '../novnc/core/rfb.js';

const CFG = window.CLB_CONFIG || {sites: [], idle_minutes: 15};
const $ = id => document.getElementById(id);
let rfb = null;
let state = 'sleeping';
let wanted = false;          // the user wants a live picture
let lastActivityPost = 0;
let hiddenTimer = null;

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

function viewSize() {
  const r = $('screen').getBoundingClientRect();
  return {w: Math.round(r.width), h: Math.round(r.height)};
}

// ── overlay ────────────────────────────────────────────────────────────────
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
      <div class="grid">${CFG.sites.map((s, i) => `<button data-site="${i}">${esc(s.name)}</button>`).join('')}</div>
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
  if (state !== 'running') showOverlay('starting');
  try {
    const res = await api('open', {site: i, ...viewSize()});
    if (res.state && res.state !== 'running') { showOverlay('sleeping', res.error || 'Start fehlgeschlagen'); return; }
    await refreshStatus();
    refreshTabs();
  } catch (e) {
    showOverlay('sleeping', e.message);
  }
}

async function goToSleep() {
  wanted = false;
  disconnect();
  showOverlay('stopping');
  try { applyStatus(await api('sleep', {})); } catch (e) {}
  showOverlay('sleeping');
}

function applyStatus(st) {
  state = st.state;
  $('dot').className = 'dot ' + st.state;
  const info = $('info');
  if (st.state === 'running' && st.sleep_in_seconds !== null && st.sleep_in_seconds !== undefined) {
    info.textContent = st.sleep_in_seconds < 120 ? `schläft in ${st.sleep_in_seconds} s` : `schläft in ${Math.ceil(st.sleep_in_seconds / 60)} min`;
  } else {
    info.textContent = st.state === 'running' ? 'bleibt an' : '';
  }
  $('sleep-btn').disabled = st.state !== 'running';
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
  rfb.addEventListener('connect', () => { rfb.focus(); refreshTabs(); });
  rfb.addEventListener('disconnect', () => {
    rfb = null;
    if (wanted && !document.hidden) setTimeout(refreshStatus, 1500);
  });
  rfb.addEventListener('clipboard', e => { $('clip-text').value = e.detail.text; });
}

function disconnect() {
  if (!rfb) return;
  const r = rfb;
  rfb = null;
  try { r.disconnect(); } catch (e) {}
}

// ── tabs & sites ───────────────────────────────────────────────────────────
function renderSites() {
  $('sites').innerHTML = CFG.sites.map((s, i) =>
    `<button data-site="${i}" title="${esc(s.url)}${s.logout ? ' – wird beim Einschlafen abgemeldet' : ''}">${esc(s.name)}${s.logout ? ' 🔒' : ''}</button>`).join('');
  $('sites').querySelectorAll('[data-site]').forEach(b => b.onclick = () => openSite(+b.dataset.site));
}

async function refreshTabs() {
  if (state !== 'running') { $('tabs').innerHTML = ''; return; }
  let data;
  try { data = await api('tabs'); } catch (e) { return; }
  $('tabs').innerHTML = (data.tabs || []).map(t =>
    `<div class="tab" title="${esc(t.url)}"><span data-act="${esc(t.id)}">${esc(t.title || t.url)}</span><button data-close="${esc(t.id)}" title="Tab schließen">✕</button></div>`).join('');
  $('tabs').querySelectorAll('[data-act]').forEach(el => el.onclick = () => api('tabs/activate', {id: el.dataset.act}).then(refreshTabs));
  $('tabs').querySelectorAll('[data-close]').forEach(el => el.onclick = () => api('tabs/close', {id: el.dataset.close}).then(refreshTabs));
}

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

// ── clipboard, fullscreen ──────────────────────────────────────────────────
$('clip-btn').onclick = () => { $('clip-panel').hidden = !$('clip-panel').hidden; };
$('clip-send').onclick = () => {
  if (rfb) { rfb.clipboardPasteFrom($('clip-text').value); rfb.focus(); }
  $('clip-panel').hidden = true;
};
$('clip-copy').onclick = async () => {
  try { await navigator.clipboard.writeText($('clip-text').value); } catch (e) { $('clip-text').select(); document.execCommand('copy'); }
};
$('full-btn').onclick = () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen().catch(() => {});
};
$('sleep-btn').onclick = goToSleep;

// ── start ──────────────────────────────────────────────────────────────────
renderSites();
(async () => {
  const st = await api('status').catch(() => ({state: 'sleeping'}));
  wanted = st.state === 'running';
  applyStatus(st);
})();
setInterval(refreshStatus, 15000);
setInterval(refreshTabs, 10000);
