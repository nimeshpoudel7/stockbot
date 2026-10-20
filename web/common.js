// Stockbot — helpers shared by both pages.
const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $ = id => document.getElementById(id);
const QS = new URLSearchParams(location.search);
const DASH_TOKEN = QS.get('token') || '';
const withToken = u => DASH_TOKEN ? u + (u.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(DASH_TOKEN) : u;

async function api(method, url, body) {
  const r = await fetch(withToken(url), {method, headers: {'Content-Type': 'application/json'},
    body: body ? JSON.stringify(body) : undefined});
  let j = null;
  try { j = await r.json(); } catch (e) { j = null; }
  if (!r.ok) throw new Error((j && j.detail) || ('Request failed (' + r.status + ')'));
  return j;
}

function fmtP(p) {
  if (p == null || isNaN(p)) return '—';
  if (p >= 1) return '$' + p.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  if (p >= 0.01) return '$' + p.toFixed(4);
  return '$' + p.toFixed(6);
}
function fmtPct(v, digits = 1) {
  if (v == null || isNaN(v)) return '—';
  return (v >= 0 ? '+' : '−') + Math.abs(v).toFixed(digits) + '%';
}
function stockUrl(s) { return withToken('/stock?symbol=' + encodeURIComponent(s)); }
function homeUrl() { return withToken('/'); }
function etTime(iso, opts) {
  return new Date(iso).toLocaleString('en-US', Object.assign({timeZone: 'America/New_York'}, opts || {}));
}
function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return 'just now';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  return etTime(iso, {month: 'short', day: 'numeric'});
}

// Is the US market open right now (Mon-Fri 9:30-16:00 ET)? Holidays are not
// known here; the scanner itself skips them.
function marketOpenNow() {
  const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', weekday: 'short',
    hour: 'numeric', minute: 'numeric', hour12: false}).formatToParts(new Date()).map(x => [x.type, x.value]));
  if (p.weekday === 'Sat' || p.weekday === 'Sun') return false;
  const m = (+p.hour % 24) * 60 + (+p.minute);
  return m >= 570 && m < 960;
}

// ---- rating meter (x.x / 10 as ten segments) ------------------------------
function meter(rating, side) {
  const v = parseFloat(rating);
  const n = isNaN(v) ? 0 : Math.round(Math.max(0, Math.min(10, v)));
  const cls = side === 'SELL' ? 'sell' : 'buy';
  let bars = '';
  for (let i = 0; i < 10; i++) bars += `<i class="${i < n ? 'on' : ''}"></i>`;
  return `<span class="meter ${cls}" title="Rating ${isNaN(v) ? '—' : v.toFixed(1)} out of 10">
    <span class="meter-val">${isNaN(v) ? '—' : v.toFixed(1)}</span><span class="meter-bars" aria-hidden="true">${bars}</span></span>`;
}

// ---- side badge ---------------------------------------------------------
const PLAYBOOK = {dip: 'dip buy', momentum: 'momentum'};
function sideBadge(side, playbook) {
  const s = side === 'SELL' ? 'sell' : 'buy';
  const pb = side === 'BUY' && PLAYBOOK[playbook] ? `<span class="pb">· ${PLAYBOOK[playbook]}</span>` : '';
  return `<span class="side ${s}">${side === 'SELL' ? 'Sell' : 'Buy'}${pb}</span>`;
}

// ---- risk ladder: stop · price · target ------------------------------------
function ladder(price, stop, target) {
  if (price == null || stop == null || target == null) return '';
  const loss = Math.abs(price - stop), gain = Math.abs(target - price), span = loss + gain;
  if (!span) return '';
  const lp = loss / span * 100;
  const stopPct = (stop - price) / price * 100, tgtPct = (target - price) / price * 100;
  return `<div class="ladder" aria-label="Stop ${fmtP(stop)}, target ${fmtP(target)}">
    <span class="lv stop"><b>${fmtP(stop)}</b><span class="mute">stop ${fmtPct(stopPct)}</span></span>
    <span class="track" aria-hidden="true"><span class="seg-loss" style="width:${lp}%"></span><span class="seg-gain" style="width:${100 - lp}%"></span><span class="now" style="left:${lp}%"></span></span>
    <span class="lv tgt"><b>${fmtP(target)}</b><span class="mute">target ${fmtPct(tgtPct)}</span></span>
  </div>`;
}

// ---- reasons -> short human chips -------------------------------------------
function cleanReasons(reasons) {
  return (reasons || []).filter(r => r && !/^stop \$/.test(r) && !/^(dip buy|momentum)$/.test(r));
}

// ---- theme --------------------------------------------------------------
function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}
function initTheme(btnId, onChange) {
  try { const t = localStorage.getItem('sb-theme'); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
  const btn = $(btnId);
  if (!btn) return;
  const paint = () => {
    const dark = currentTheme() === 'dark';
    btn.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
    btn.innerHTML = dark
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>';
  };
  btn.onclick = () => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('sb-theme', next); } catch (e) {}
    paint(); if (onChange) onChange();
  };
  paint();
}

// ---- toast --------------------------------------------------------------
let _toastT = null;
function toast(msg, isErr) {
  let t = $('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; t.className = 'toast'; t.setAttribute('role', 'status'); document.body.appendChild(t); }
  t.textContent = msg; t.classList.toggle('err', !!isErr); t.classList.add('show');
  clearTimeout(_toastT); _toastT = setTimeout(() => t.classList.remove('show'), 2600);
}

// ---- candlestick chart themed from CSS variables ---------------------------
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
function makeChart(el) {
  const c = LightweightCharts.createChart(el, {autoSize: true, layout: {}, grid: {}, timeScale: {timeVisible: true, borderVisible: false},
    rightPriceScale: {borderVisible: false}, crosshair: {mode: 0}});
  const s = c.addCandlestickSeries({borderVisible: false});
  const theme = () => {
    c.applyOptions({layout: {background: {color: 'transparent'}, textColor: cssVar('--mute'), fontFamily: "'IBM Plex Sans', system-ui, sans-serif"},
      grid: {vertLines: {color: cssVar('--chart-grid')}, horzLines: {color: cssVar('--chart-grid')}}});
    s.applyOptions({upColor: cssVar('--buy'), downColor: cssVar('--sell'), wickUpColor: cssVar('--buy'), wickDownColor: cssVar('--sell')});
  };
  theme();
  return {chart: c, series: s, theme};
}
