// Sianna Financials — frontend app.
// Vanilla JS, no framework. Fetches the FastAPI backend and renders the
// dashboard and ticker-detail views into a single HTML shell.

'use strict';

// Must match `SCREENER_EARNINGS_WINDOW_DAYS` in `app/analyst/constants.py` (Screener card).
const SCREENER_EARNINGS_WINDOW_DAYS = 7;

// ---------- Time helpers (all user-facing times are US Eastern) -----------

const EST_TZ = 'America/New_York';

function fmtEST(date = new Date(), opts = {}) {
  const defaults = {
    timeZone: EST_TZ,
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
  };
  return new Intl.DateTimeFormat('en-US', { ...defaults, ...opts }).format(date);
}

function fmtESTDateTime(date = new Date()) {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: EST_TZ,
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
    timeZoneName: 'short',
  }).format(date);
}

function fmtESTCompact(iso) {
  // Accepts either an ISO string or a Date. Shows: Apr 22 14:35:02 EST
  const d = iso ? new Date(iso) : new Date();
  if (Number.isNaN(d.getTime())) return iso || '—';
  return new Intl.DateTimeFormat('en-US', {
    timeZone: EST_TZ,
    month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
    hour12: false,
    timeZoneName: 'short',
  }).format(d);
}

// ---------- Tiny DOM helpers -----------------------------------------------

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === 'data') {
      for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    } else {
      el.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// SVG equivalent (createElement doesn't work for SVG without NS)
function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// ---------- Formatters ------------------------------------------------------

const fmtUSD = (x) => {
  if (x == null) return '∞';
  const abs = Math.abs(x);
  const opts = abs >= 1000 ? { maximumFractionDigits: 0 } : { maximumFractionDigits: 2, minimumFractionDigits: 2 };
  return (x < 0 ? '-$' : '$') + abs.toLocaleString('en-US', opts);
};
const fmtPct = (x, signed = true) => {
  const s = signed && x > 0 ? '+' : '';
  return `${s}${x.toFixed(2)}%`;
};
const fmtScore = (x) => (x >= 0 ? '+' : '') + x.toFixed(2);

// ---------- API -------------------------------------------------------------

// Browser fetch() has no default timeout. Dashboard calls can be slow on a cold
// cache; cap waits so the UI can show a failure instead of hanging forever.
const API_FETCH_TIMEOUT_MS = 6 * 60 * 1000; // 6 minutes

async function apiJson(path, init = {}) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), API_FETCH_TIMEOUT_MS);
  try {
    const r = await fetch(path, { ...init, signal: controller.signal });
    if (!r.ok) {
      let detail = '';
      try {
        const errData = await r.json();
        if (errData && typeof errData === 'object' && errData.detail != null) {
          detail = String(errData.detail);
        }
      } catch (_) {
        detail = '';
      }
      throw new Error(`${path}: ${r.status}${detail ? ` - ${detail}` : ''}`);
    }
    return r.json();
  } catch (e) {
    if (e && e.name === 'AbortError') {
      throw new Error(
        `Request timed out after ${API_FETCH_TIMEOUT_MS / 60000}m — ${path} (try refresh)`,
      );
    }
    throw e;
  } finally {
    clearTimeout(t);
  }
}

async function api(path) {
  return apiJson(path);
}

// ---------- Application state ----------------------------------------------

const state = {
  view: 'dashboard',
  filter: 'all',
  activeSymbol: null,
  universe: [], // all symbols (for the detail chip row)
  details: {}, // "SYMBOL|range" -> cached detail payload
  /** Ticker detail price chart: range matches API `?range=`, view is client-only. */
  detailChart: { range: '1d', view: 'mountain' },
  /** 0–1 session ratio window for 1D (6am–6pm ET in full view); scroll to zoom, drag to pan. */
  detailChartSessionZoom: { start: 0, end: 1 },
  /** Analyst / Search embed charts use the same API + controls; session zoom is isolated per tab. */
  analystChart: { range: '1d', view: 'mountain' },
  analystChartSessionZoom: { start: 0, end: 1 },
  stocksChart: { range: '1d', view: 'line', overlayMode: 'simple' },
  stocksChartSessionZoom: { start: 0, end: 1 },
  searchChart: { range: '1d', view: 'mountain' },
  searchChartSessionZoom: { start: 0, end: 1 },
  /** Which surface opened the enlarged chart modal (`openDetailChartModal`); drives View toolbar routing. */
  chartModalSource: null,
};

const SESSION_1D_MIN_ZOOM = 0.0015; // ≈1 min in a 12h range — zoom in to “minute” scale
const TICKER_DETAIL_CACHE_TTL_MS = 90_000;

function _readFreshTickerDetail(cache, key) {
  const d = cache[key];
  if (!d) return null;
  const fetchedAt = Number(d.__clientFetchedAtMs || 0);
  if (!Number.isFinite(fetchedAt) || Date.now() - fetchedAt > TICKER_DETAIL_CACHE_TTL_MS) {
    delete cache[key];
    return null;
  }
  return d;
}

function _writeTickerDetailCache(cache, key, payload) {
  if (payload && typeof payload === 'object') {
    payload.__clientFetchedAtMs = Date.now();
  }
  cache[key] = payload;
  return payload;
}

function stocksChartIsSimpleMode() {
  return state.stocksChart.overlayMode !== 'advanced';
}

function detailCacheKey(symbol) {
  return `${symbol}|${state.detailChart.range}`;
}

function reapplySessionDetailChart() {
  const sym = state.activeSymbol;
  if (!sym || state.view !== 'detail') return;
  const d = state.details[detailCacheKey(sym)];
  if (d) {
    renderPriceChart(d);
    const backdrop = $('#chart-enlarge-backdrop');
    if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'detail') {
      openDetailChartModal(d, 'detail');
    }
  }
}

function stocksTickerCacheKey(symbol) {
  return `${String(symbol || '').toUpperCase()}|${state.stocksChart.range}`;
}

function reapplyStocksSessionChart() {
  if (state.view !== 'stocks') return;
  const sym = stocks.activeSymbol;
  if (!sym) return;
  const d = stocks.tickerDetails[stocksTickerCacheKey(sym)];
  if (d) {
    renderPriceChart(d, 'stocks');
    const backdrop = $('#chart-enlarge-backdrop');
    if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'stocks') {
      openDetailChartModal(d, 'stocks');
    }
  }
}

function analystTickerCacheKey(symbol) {
  return `${String(symbol || '').toUpperCase()}|${state.analystChart.range}`;
}

function searchTickerCacheKey(symbol) {
  return `${String(symbol || '').toUpperCase()}|${state.searchChart.range}`;
}

function reapplyAnalystSessionChart() {
  if (state.view !== 'analyst') return;
  const sym = analyst.activeSymbol;
  if (!sym) return;
  const d = analyst.tickerDetails[analystTickerCacheKey(sym)];
  if (d) {
    renderPriceChart(d, 'analyst');
    const backdrop = $('#chart-enlarge-backdrop');
    if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'analyst') {
      openDetailChartModal(d, 'analyst');
    }
  }
}

function reapplySearchSessionChart() {
  if (state.view !== 'search') return;
  const d = searchUI.lastTickerDetail;
  if (d) {
    renderPriceChart(d, 'search');
    const backdrop = $('#chart-enlarge-backdrop');
    if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'search') {
      openDetailChartModal(d, 'search');
    }
  }
}

/** Live refresh for Ticker detail chart (Yahoo; not tick-by-tick). Align with data._CACHE_TTL_1D_INTRADAY. */
let detailChartPollTimer = null;
const DETAIL_CHART_POLL_MS = 2 * 60_000; // 2 min — pair with 1D intraday disk TTL; use 60_000 for 1 min + env TTL

function clearDetailChartPoll() {
  if (detailChartPollTimer) {
    clearInterval(detailChartPollTimer);
    detailChartPollTimer = null;
  }
  const live = $('#detail-chart-live');
  if (live) {
    live.classList.add('hidden');
    live.textContent = '';
  }
}

function scheduleDetailChartPoll() {
  clearDetailChartPoll();
  const live = $('#detail-chart-live');
  if (live) {
    live.classList.remove('hidden');
    {
      const min = Math.max(1, Math.round(DETAIL_CHART_POLL_MS / 60_000));
      live.textContent = min === 1
        ? ' · Live · every 1 min'
        : ` · Live · every ${min} min`;
    }
  }
  detailChartPollTimer = setInterval(async () => {
    if (state.view !== 'detail' || !state.activeSymbol) {
      clearDetailChartPoll();
      return;
    }
    if (document.visibilityState === 'hidden') return;
    const sym = state.activeSymbol;
    delete state.details[detailCacheKey(sym)];
    try {
      const r = state.detailChart.range;
      const detail = await api(
        `/api/ticker/${encodeURIComponent(sym)}?range=${encodeURIComponent(r)}&refresh=${Date.now()}`,
      );
      state.details[detailCacheKey(sym)] = detail;
      if (state.activeSymbol !== sym || state.view !== 'detail') return;
      renderDetail(detail);
      const backdrop = $('#chart-enlarge-backdrop');
      if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'detail') {
        openDetailChartModal(detail, 'detail');
      }
    } catch (e) {
      /* best-effort; user can use Refresh in header */
    }
  }, DETAIL_CHART_POLL_MS);
}

// ---------- Router / nav ----------------------------------------------------

function switchView(view, opts = {}) {
  if (view !== 'detail') {
    clearDetailChartPoll();
  }
  state.view = view;
  $$('.tab').forEach((t) => {
    const active = t.dataset.view === view;
    t.classList.toggle('is-active', active);
    t.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  $$('.view').forEach((v) => v.classList.toggle('hidden', v.dataset.view !== view));
  if (view === 'detail' && opts.symbol) {
    openTicker(opts.symbol);
  }
}

function initTabs() {
  $$('.tab').forEach((t) => {
    t.addEventListener('click', () => {
      const target = t.dataset.view;
      if (target === 'detail' && !state.activeSymbol && state.universe.length) {
        switchView('detail', { symbol: state.universe[0] });
      } else if (target === 'analyst') {
        switchView('analyst');
        initAnalystOnce();
      } else if (target === 'stocks') {
        switchView('stocks');
        initStocksOnce();
      } else if (target === 'search') {
        switchView('search');
        initSearchOnce();
      } else if (target === 'screener') {
        switchView('screener');
        loadScreener();
      } else if (target === 'etf') {
        switchView('etf');
        initEtfOnce();
      } else {
        switchView(target);
      }
    });
  });
  const back = $('#back-to-dashboard');
  if (back) {
    back.addEventListener('click', () => switchView('dashboard'));
  }
}

// ---------- Dashboard: regime + stats --------------------------------------

async function loadDashboard() {
  // Do NOT await /api/regime before the other fetches. Regime is heavy (full
  // overview + breadth); if it runs first, every card stayed on "Loading…"
  // until it finished. Parallelize so pulse, news, crypto, and the screener
  // can render as soon as their endpoints respond.
  const regimeP = api('/api/regime')
    .then((env) => {
      renderRegime(env);
    })
    .catch((e) => {
      const label = $('#regime-label');
      const detail = $('#regime-detail');
      if (label) label.textContent = 'Failed to load market regime';
      if (detail) detail.textContent = String(e);
    });

  const tasks = [
    regimeP,
    loadMarketPulse(),
    loadOptionsHighlights(),
    loadCrypto(),
    loadNews(),
    loadScreen(state.filter),
  ];
  await Promise.allSettled(tasks);
}

function renderRegime(env) {
  const r = env.regime;
  const c = env.counts;
  const set = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };
  set('last-scan', 'scan ' + fmtESTCompact(env.last_scan_iso));
  set('regime-label', `Market regime: ${r.label}`);

  // Regime banner "pills" — populated into structured spans so each term
  // can carry its own hover tooltip.
  const vix = $('#rp-vix');
  const vixChg = $('#rp-vix-chg');
  const bd = $('#rp-breadth');
  const pc = $('#rp-pc');
  const ts = $('#rp-trend');
  if (vix) vix.textContent = r.vix.toFixed(1);
  if (vixChg) {
    vixChg.textContent = ` (${fmtPct(r.vix_change)})`;
    vixChg.classList.toggle('pos', r.vix_change > 0);
    vixChg.classList.toggle('neg', r.vix_change < 0);
  }
  if (bd) bd.textContent = `${r.breadth_pct_above_50d.toFixed(0)}%`;
  if (pc) pc.textContent = r.put_call_ratio.toFixed(2);
  if (ts) ts.textContent = r.trend_score.toFixed(2);

  set('stat-universe', String(c.universe_size));
  set('stat-longs', String(c.longs));
  set('stat-shorts', String(c.shorts));
  set('stat-holds', String(c.holds));
  set('stat-vix', r.vix.toFixed(1));
}

// ---------- Dashboard: market pulse ---------------------------------------

async function loadMarketPulse() {
  const gainers = $('#movers-gainers');
  const losers = $('#movers-losers');
  const heat = $('#sector-heat');
  const trail = $('#pulse-trail');
  const breadthEl = $('#stat-breadth');
  try {
    const p = await api('/api/market/pulse');
    renderMovers(gainers, p.top_gainers, 'pos');
    renderMovers(losers, p.top_losers, 'neg');
    renderSectorHeat(heat, p.sector_heatmap);
    if (trail) trail.textContent =
      `${p.tickers_covered} tickers · ${p.breadth_pct_up.toFixed(0)}% up`;
    if (breadthEl) breadthEl.textContent = `${p.breadth_pct_up.toFixed(0)}%`;
  } catch (e) {
    gainers && (gainers.innerHTML = `<li class="loading">Failed: ${escapeHtml(String(e))}</li>`);
    losers && (losers.innerHTML = '');
    heat && (heat.innerHTML = '');
  }
}

function renderMovers(ul, rows, side) {
  if (!ul) return;
  ul.innerHTML = '';
  if (!rows || !rows.length) {
    ul.append(h('li', { class: 'loading' }, 'No movers.'));
    return;
  }
  for (const m of rows) {
    const li = h(
      'li',
      {
        class: 'mover',
        onClick: () => switchView('detail', { symbol: m.symbol }),
      },
      h('span', { class: 'mover-sym' }, m.symbol),
      h('span', { class: 'mover-name muted' }, m.name),
      h(
        'span',
        { class: `mover-pct mono ${side}` },
        (m.change_pct >= 0 ? '+' : '') + m.change_pct.toFixed(2) + '%'
      ),
      h('span', { class: 'mover-last mono muted' }, fmtUSD(m.last))
    );
    ul.append(li);
  }
}

function renderSectorHeat(container, rows) {
  if (!container) return;
  container.innerHTML = '';
  if (!rows || !rows.length) {
    container.append(h('div', { class: 'loading' }, 'No sectors.'));
    return;
  }
  // Scale intensity by absolute change vs max.
  const maxAbs = Math.max(1, ...rows.map((r) => Math.abs(r.avg_change_pct)));
  for (const s of rows) {
    const intensity = Math.min(1, Math.abs(s.avg_change_pct) / maxAbs);
    const tone = s.avg_change_pct >= 0 ? 'pos' : 'neg';
    const bgAlpha = (0.12 + intensity * 0.55).toFixed(2);
    const tile = h(
      'div',
      {
        class: `sector-tile ${tone}`,
        style: `--heat:${bgAlpha};`,
        title: `${s.tickers.join(', ')} · bull ${s.bullish} / bear ${s.bearish} / neutral ${s.neutral}`,
      },
      h('div', { class: 'sector-tile-name' }, s.sector),
      h(
        'div',
        { class: 'sector-tile-val mono' },
        (s.avg_change_pct >= 0 ? '+' : '') + s.avg_change_pct.toFixed(2) + '%'
      ),
      h('div', { class: 'sector-tile-meta muted mono' }, `${s.count} · B${s.bullish}/S${s.bearish}`)
    );
    container.append(tile);
  }
}

// ---------- Dashboard: options highlights ----------------------------------

async function loadOptionsHighlights() {
  const calls = $('#opts-calls');
  const puts = $('#opts-puts');
  try {
    const h_ = await api('/api/options/highlights');
    renderOptionCards(calls, h_.top_calls, 'call');
    renderOptionCards(puts, h_.top_puts, 'put');
  } catch (e) {
    calls && (calls.innerHTML = `<div class="loading">Failed: ${escapeHtml(String(e))}</div>`);
    puts && (puts.innerHTML = '');
  }
  // Wire the "See all →" link to jump to the Analyst tab.
  $$('[data-jump-analyst]').forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      switchView('analyst');
      if (typeof initAnalystOnce === 'function') initAnalystOnce();
    };
  });
}

function renderOptionCards(container, rows, kind) {
  if (!container) return;
  container.innerHTML = '';
  if (!rows || !rows.length) {
    container.append(h('div', { class: 'loading' }, `No ${kind} ideas today.`));
    return;
  }
  for (const o of rows) {
    const side = kind === 'call' ? 'pos' : 'neg';
    const tag = kind === 'call' ? 'Long call' : 'Long put';
    const strike = o.strike != null ? '$' + Math.round(o.strike) : '—';
    const rr = o.risk_reward != null ? o.risk_reward.toFixed(2) + 'x' : '—';
    const exp = o.expiry_date || (o.expiry_dte ? `${o.expiry_dte}d` : '—');
    const be = o.break_even != null ? fmtUSD(o.break_even) : '—';
    const cost = o.cost_per_contract != null ? fmtUSD(o.cost_per_contract) : '—';
    const card = h(
      'div',
      {
        class: `opt-card opt-${side}`,
        onClick: () => switchView('detail', { symbol: o.symbol }),
      },
      h(
        'div',
        { class: 'opt-card-head' },
        h('span', { class: 'opt-sym' }, o.symbol),
        h('span', { class: `opt-tag ${side}` }, tag)
      ),
      h('div', { class: 'opt-name muted' }, o.name),
      h(
        'div',
        { class: 'opt-grid' },
        h('div', { class: 'opt-cell' },
          h('div', { class: 'opt-cell-l muted' }, 'Strike'),
          h('div', { class: 'opt-cell-v mono' }, strike)),
        h('div', { class: 'opt-cell' },
          h('div', { class: 'opt-cell-l muted' }, 'Expiry'),
          h('div', { class: 'opt-cell-v mono' }, exp)),
        h('div', { class: 'opt-cell' },
          h('div', { class: 'opt-cell-l muted' }, 'Cost'),
          h('div', { class: 'opt-cell-v mono' }, cost)),
        h('div', { class: 'opt-cell' },
          h('div', { class: 'opt-cell-l muted' }, 'Break-even'),
          h('div', { class: 'opt-cell-v mono' }, be)),
        h('div', { class: 'opt-cell' },
          h('div', { class: 'opt-cell-l muted' }, 'R / R'),
          h('div', { class: 'opt-cell-v mono' }, rr)),
        h('div', { class: 'opt-cell' },
          h('div', { class: 'opt-cell-l muted' }, 'Conf.'),
          h('div', { class: 'opt-cell-v mono' }, Math.round((o.conviction || 0) * 100) + '%'))
      )
    );
    container.append(card);
  }
}

// ---------- Dashboard: crypto snapshot -------------------------------------

async function loadCrypto() {
  const grid = $('#crypto-grid');
  const srcEl = $('#crypto-source');
  if (!grid) return;
  try {
    const d = await api('/api/crypto/snapshot');
    if (srcEl) srcEl.textContent = d.source;
    grid.innerHTML = '';
    if (!d.rows.length) {
      grid.append(h('div', { class: 'loading' }, 'Crypto data unavailable.'));
      return;
    }
    for (const c of d.rows) {
      const side24 = c.change_pct_24h >= 0 ? 'pos' : 'neg';
      const tile = h(
        'div',
        { class: `crypto-tile ${side24}` },
        h('div', { class: 'crypto-head' },
          h('div', { class: 'crypto-name' },
            h('span', { class: 'crypto-sym' }, c.symbol.replace('-USD', '')),
            h('span', { class: 'crypto-full muted' }, c.name)
          ),
          h('div', { class: `crypto-pct mono ${side24}` },
            (c.change_pct_24h >= 0 ? '+' : '') + c.change_pct_24h.toFixed(2) + '%')
        ),
        h('div', { class: 'crypto-body' },
          h('div', { class: 'crypto-price mono' }, fmtUSD(c.last)),
          cryptoSpark(c.spark, side24)
        ),
        h('div', { class: 'crypto-foot muted mono' },
          `24h ${c.change_pct_24h >= 0 ? '+' : ''}${c.change_pct_24h.toFixed(2)}%`)
      );
      grid.append(tile);
    }
  } catch (e) {
    grid.innerHTML = `<div class="loading">Failed: ${escapeHtml(String(e))}</div>`;
  }
}

function cryptoSpark(points, side) {
  if (!points || points.length < 2) return h('div');
  const w = 110, hgt = 36;
  const min = Math.min(...points), max = Math.max(...points);
  const range = max - min || 1;
  const step = w / (points.length - 1);
  const pts = points.map((v, i) =>
    `${(i * step).toFixed(1)},${(hgt - ((v - min) / range) * hgt).toFixed(1)}`
  ).join(' ');
  const stroke = side === 'pos' ? '#25c281' : '#ff6b6b';
  return svg('svg', {
    class: 'crypto-spark',
    viewBox: `0 0 ${w} ${hgt}`,
    width: w, height: hgt,
    preserveAspectRatio: 'none',
  },
    svg('polyline', {
      fill: 'none', stroke, 'stroke-width': 1.6,
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      points: pts,
    })
  );
}

// ---------- Dashboard: news feed -------------------------------------------

function fmtNewsSource(src) {
  const s = String(src || '');
  if (s.includes('+')) {
    const parts = s.split('+').map((x) => fmtNewsSource(x));
    return parts.join(' + ');
  }
  const m = {
    'cnbc-rss': 'CNBC',
    'marketwatch-rss': 'MarketWatch',
    'internal-snapshot': 'Sianna (snapshot)',
  };
  return m[s] != null ? m[s] : s;
}

function fitNewsListViewport(ul, visibleCount = 8) {
  if (!ul) return;
  const items = ul.querySelectorAll('.news-item');
  if (!items.length) {
    ul.style.maxHeight = '';
    return;
  }
  if (items.length <= visibleCount) {
    ul.style.maxHeight = '';
    return;
  }
  const limit = Math.min(visibleCount, items.length);
  let h = 0;
  for (let i = 0; i < limit; i++) h += items[i].offsetHeight;
  const cs = window.getComputedStyle(ul);
  const gap = parseFloat(cs.rowGap || cs.gap || '0') || 0;
  h += gap * Math.max(0, limit - 1);
  ul.style.maxHeight = `${Math.ceil(h)}px`;
}

async function loadNews() {
  const ul = $('#news-list');
  const srcEl = $('#news-source');
  if (!ul) return;
  try {
    const d = await api('/api/news?limit=14');
    if (srcEl) srcEl.textContent = fmtNewsSource(d.source);
    ul.innerHTML = '';
    if (!d.items.length) {
      ul.append(h('li', { class: 'loading' }, 'No headlines available.'));
      return;
    }
    for (const n of d.items) {
      const when = n.published_at ? fmtNewsWhen(n.published_at) : '';
      const li = h(
        'li',
        { class: 'news-item' },
        h('div', { class: 'news-head' },
          h('span', { class: 'news-sym mono' }, n.related.replace('-USD', '')),
          h('span', { class: 'news-pub muted' }, n.publisher),
          when ? h('span', { class: 'news-time muted mono' }, when) : ''
        ),
        n.url
          ? h('a', { class: 'news-title', href: n.url, target: '_blank', rel: 'noopener' }, n.title)
          : h('div', { class: 'news-title' }, n.title),
        n.summary ? h('div', { class: 'news-sum muted' }, shortText(n.summary, 180)) : ''
      );
      ul.append(li);
    }
    fitNewsListViewport(ul, 8);
  } catch (e) {
    ul.innerHTML = `<li class="loading">Failed: ${escapeHtml(String(e))}</li>`;
    ul.style.maxHeight = '';
  }
}

function fmtNewsWhen(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const diff = Date.now() - d.getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return fmtESTCompact(iso);
}

function shortText(s, n = 160) {
  if (!s) return '';
  const t = String(s).replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1) + '…' : t;
}

// ---------- Dashboard: screener table --------------------------------------

function initFilters() {
  $$('.filter-pills .pill').forEach((p) => {
    p.addEventListener('click', () => {
      state.filter = p.dataset.filter;
      $$('.filter-pills .pill').forEach((x) => x.classList.toggle('is-active', x === p));
      loadScreen(state.filter);
    });
  });
}

async function loadScreen(filter) {
  const tbody = $('#screen-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="10" class="loading">Loading signals…</td></tr>';
  try {
    const rows = await api(`/api/screen?filter=${encodeURIComponent(filter)}`);
    state.universe = rows.map((r) => r.symbol);
    renderScreen(rows);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="loading">Failed: ${e}</td></tr>`;
  }
}

function signalBadge(sig) {
  const cls = sig === 'Buy' ? 'success' : sig === 'Sell' ? 'danger' : 'neutral';
  return h('span', { class: `pill-badge ${cls}` }, sig);
}

function renderScreen(rows) {
  const tbody = $('#screen-tbody');
  tbody.innerHTML = '';
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="loading">No rows match this filter.</td></tr>';
    return;
  }
  for (const r of rows) {
    const rowClass =
      r.signal === 'Buy' ? 'row-buy' : r.signal === 'Sell' ? 'row-sell' : '';
    const tr = h(
      'tr',
      {
        class: rowClass,
        onClick: () => switchView('detail', { symbol: r.symbol }),
      },
      h('td', { class: 'sym' }, r.symbol),
      h('td', { class: 'name' }, r.name),
      h('td', { class: 'num' }, fmtUSD(r.price)),
      h('td', { class: `num ${r.change_pct >= 0 ? 'pos' : 'neg'}` }, fmtPct(r.change_pct)),
      h('td', {}, signalBadge(r.signal)),
      h('td', { class: 'num' }, fmtScore(r.composite_score)),
      h('td', { class: 'num' }, `${Math.round(r.confidence * 100)}%`),
      h('td', { class: 'num' }, r.iv_rank.toFixed(0)),
      h('td', { class: 'num' }, r.rsi.toFixed(0)),
      h(
        'td',
        { class: 'num' },
        h(
          'button',
          {
            class: 'btn-ghost',
            onClick: (e) => {
              e.stopPropagation();
              switchView('detail', { symbol: r.symbol });
            },
          },
          'Open →'
        )
      )
    );
    tbody.append(tr);
  }
}

// ---------- Ticker detail ---------------------------------------------------

async function openTicker(symbol) {
  clearDetailChartPoll();
  const prevSym = state.activeSymbol;
  state.activeSymbol = symbol;
  if (prevSym != null && prevSym !== symbol) {
    state.detailChartSessionZoom = { start: 0, end: 1 };
  }
  renderChipRow();

  const ck = detailCacheKey(symbol);
  let detail = state.details[ck];
  if (!detail) {
    $('#detail-hero').innerHTML = '<div class="hero-loading mono">Loading ' + symbol + '…</div>';
    try {
      const r = state.detailChart.range;
      detail = await api(
        `/api/ticker/${encodeURIComponent(symbol)}?range=${encodeURIComponent(r)}`,
      );
      state.details[ck] = detail;
    } catch (e) {
      $('#detail-hero').innerHTML = `<div class="hero-loading mono">Failed: ${e}</div>`;
      return;
    }
  }
  if (detail.chart && detail.chart.range_key) {
    state.detailChart.range = detail.chart.range_key;
  }
  syncDetailChartToolbar();
  renderDetail(detail);
  if (state.view === 'detail') {
    scheduleDetailChartPoll();
  }
}

function syncDetailChartToolbar() {
  const r = state.detailChart.range;
  const v = state.detailChart.view;
  document.querySelectorAll('#detail-chart-toolbar [data-detail-range]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-detail-range') === r);
  });
  document.querySelectorAll('#detail-chart-toolbar [data-detail-view]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-detail-view') === v);
  });
}

function syncAnalystChartToolbar() {
  const r = state.analystChart.range;
  const v = state.analystChart.view;
  document.querySelectorAll('#analyst-chart-toolbar [data-analyst-range]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-analyst-range') === r);
  });
  document.querySelectorAll('#analyst-chart-toolbar [data-analyst-view]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-analyst-view') === v);
  });
}

function syncSearchChartToolbar() {
  const r = state.searchChart.range;
  const v = state.searchChart.view;
  document.querySelectorAll('#search-chart-toolbar [data-search-range]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-search-range') === r);
  });
  document.querySelectorAll('#search-chart-toolbar [data-search-view]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-search-view') === v);
  });
}

/** Range + chart type: delegate from document so the same controls work in the enlarge modal. */
function initDetailChartToolbar() {
  document.addEventListener('click', (e) => {
    const tr = e.target.closest('#detail-chart-toolbar [data-detail-range]');
    const tvModal = e.target.closest('#chart-modal-view-toolbar [data-detail-view]');
    const toModal = e.target.closest('#chart-modal-overlays-toolbar [data-chart-overlays]');
    const tvDetail = e.target.closest('#detail-chart-toolbar [data-detail-view]');
    if (tr) {
      e.preventDefault();
      const nr = tr.getAttribute('data-detail-range');
      if (nr && nr !== state.detailChart.range && state.activeSymbol) {
        state.detailChart.range = nr;
        state.detailChartSessionZoom = { start: 0, end: 1 };
        delete state.details[detailCacheKey(state.activeSymbol)];
        void openTicker(state.activeSymbol);
      }
      return;
    }
    if (tvModal) {
      e.preventDefault();
      const nv = tvModal.getAttribute('data-detail-view');
      const src = state.chartModalSource;
      if (!nv || !src) return;
      if (src === 'analyst') {
        if (nv === state.analystChart.view) return;
        state.analystChart.view = nv;
        const sym = analyst.activeSymbol;
        const d = sym ? analyst.tickerDetails[analystTickerCacheKey(sym)] : null;
        if (d) {
          renderPriceChart(d, 'analyst');
          openDetailChartModal(d, 'analyst');
        }
        syncAnalystChartToolbar();
        syncChartModalViewToolbar();
        return;
      }
      if (src === 'search') {
        if (nv === state.searchChart.view) return;
        state.searchChart.view = nv;
        const d = searchUI.lastTickerDetail;
        if (d) {
          renderPriceChart(d, 'search');
          openDetailChartModal(d, 'search');
        }
        syncSearchChartToolbar();
        syncChartModalViewToolbar();
        return;
      }
      if (src === 'stocks') {
        if (nv === state.stocksChart.view) return;
        state.stocksChart.view = nv;
        const sym = stocks.activeSymbol;
        const d = sym ? stocks.tickerDetails[stocksTickerCacheKey(sym)] : null;
        if (d) {
          renderPriceChart(d, 'stocks');
          openDetailChartModal(d, 'stocks');
        }
        syncStocksChartToolbar();
        syncChartModalViewToolbar();
        return;
      }
      if (src === 'detail') {
        if (nv === state.detailChart.view) return;
        state.detailChart.view = nv;
        const d = state.activeSymbol ? state.details[detailCacheKey(state.activeSymbol)] : null;
        if (d) {
          renderPriceChart(d, 'detail');
          openDetailChartModal(d, 'detail');
        }
        syncDetailChartToolbar();
        syncChartModalViewToolbar();
        return;
      }
      return;
    }
    if (toModal) {
      e.preventDefault();
      const src = state.chartModalSource;
      if (src !== 'stocks') return;
      const no = toModal.getAttribute('data-chart-overlays');
      if (!no || no === state.stocksChart.overlayMode) return;
      state.stocksChart.overlayMode = no;
      const sym = stocks.activeSymbol;
      const d = sym ? stocks.tickerDetails[stocksTickerCacheKey(sym)] : null;
      if (d) {
        renderPriceChart(d, 'stocks');
        openDetailChartModal(d, 'stocks');
      }
      syncStocksChartToolbar();
      return;
    }
    if (tvDetail) {
      e.preventDefault();
      const nv = tvDetail.getAttribute('data-detail-view');
      if (nv && nv !== state.detailChart.view) {
        state.detailChart.view = nv;
        const d = state.activeSymbol
          ? state.details[detailCacheKey(state.activeSymbol)]
          : null;
        if (d) {
          renderPriceChart(d, 'detail');
          const backdrop = $('#chart-enlarge-backdrop');
          if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'detail') {
            openDetailChartModal(d, 'detail');
          }
        }
        syncDetailChartToolbar();
        syncChartModalViewToolbar();
      }
    }
  });
}

async function loadAnalystTickerDetail(symbol) {
  const r = state.analystChart.range;
  const key = `${String(symbol).toUpperCase()}|${r}`;
  const cached = _readFreshTickerDetail(analyst.tickerDetails, key);
  if (cached) return cached;
  const d = await api(
    `/api/ticker/${encodeURIComponent(symbol)}?range=${encodeURIComponent(r)}`
  );
  return _writeTickerDetailCache(analyst.tickerDetails, key, d);
}

function initAnalystChartToolbar() {
  document.addEventListener('click', (e) => {
    const tr = e.target.closest('#analyst-chart-toolbar [data-analyst-range]');
    const tv = e.target.closest('#analyst-chart-toolbar [data-analyst-view]');
    if (!tr && !tv) return;
    const sym = analyst.activeSymbol;
    if (!sym) return;
    e.preventDefault();
    if (tr) {
      const nr = tr.getAttribute('data-analyst-range');
      if (!nr || nr === state.analystChart.range) return;
      const oldR = state.analystChart.range;
      const oldKey = `${sym.toUpperCase()}|${oldR}`;
      state.analystChart.range = nr;
      state.analystChartSessionZoom = { start: 0, end: 1 };
      delete analyst.tickerDetails[oldKey];
      void (async () => {
        try {
          const d = await loadAnalystTickerDetail(sym);
          if (d && analyst.activeSymbol === sym) {
            const r = analyst.reports[`${sym}|${analyst.timeframe}`];
            if (r) renderAnalystReport(r, d);
            else renderPriceChart(d, 'analyst');
            syncAnalystChartToolbar();
          }
        } catch (err) {
          const el = $('#analyst-price-chart');
          if (el) {
            el.innerHTML = '';
            el.append(h('div', { class: 'callout callout-warning' },
              h('div', { class: 'callout-title' }, 'Price chart'),
              `Could not load chart: ${err}`));
          }
        }
      })();
      return;
    }
    if (tv) {
      const nv = tv.getAttribute('data-analyst-view');
      if (nv && nv !== state.analystChart.view) {
        state.analystChart.view = nv;
        const d = analyst.tickerDetails[analystTickerCacheKey(sym)];
        if (d) {
          renderPriceChart(d, 'analyst');
          const backdrop = $('#chart-enlarge-backdrop');
          if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'analyst') {
            openDetailChartModal(d, 'analyst');
          }
        }
        syncAnalystChartToolbar();
        syncChartModalViewToolbar();
      }
    }
  });
}

function initStocksChartToolbar() {
  document.addEventListener('click', (e) => {
    const tr = e.target.closest('#stocks-chart-toolbar [data-stocks-range]');
    const tv = e.target.closest('#stocks-chart-toolbar [data-stocks-view]');
    const to = e.target.closest('#stocks-chart-toolbar [data-stocks-overlay]');
    if (!tr && !tv && !to) return;
    const sym = stocks.activeSymbol;
    if (!sym) return;
    e.preventDefault();
    if (tr) {
      const nr = tr.getAttribute('data-stocks-range');
      if (!nr || nr === state.stocksChart.range) return;
      const oldR = state.stocksChart.range;
      const oldKey = `${sym.toUpperCase()}|${oldR}`;
      state.stocksChart.range = nr;
      state.stocksChartSessionZoom = { start: 0, end: 1 };
      delete stocks.tickerDetails[oldKey];
      void (async () => {
        try {
          const d = await loadStocksTickerDetail(sym);
          if (d && stocks.activeSymbol === sym) {
            const r = stocks.reports[`${sym}|${stocks.timeframe}`];
            if (r) renderStockReport(r, d);
            else renderPriceChart(d, 'stocks');
            syncStocksChartToolbar();
          }
        } catch (err) {
          const el = $('#stocks-price-chart');
          if (el) {
            el.innerHTML = '';
            el.append(h('div', { class: 'callout callout-warning' },
              h('div', { class: 'callout-title' }, 'Price chart'),
              `Could not load chart: ${err}`));
          }
        }
      })();
      return;
    }
    if (to) {
      const no = to.getAttribute('data-stocks-overlay');
      if (no && no !== state.stocksChart.overlayMode) {
        state.stocksChart.overlayMode = no;
        const d = stocks.tickerDetails[stocksTickerCacheKey(sym)];
        if (d) {
          renderPriceChart(d, 'stocks');
          const backdrop = $('#chart-enlarge-backdrop');
          if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'stocks') {
            openDetailChartModal(d, 'stocks');
          }
        }
        syncStocksChartToolbar();
      }
      return;
    }
    if (tv) {
      const nv = tv.getAttribute('data-stocks-view');
      if (nv && nv !== state.stocksChart.view) {
        state.stocksChart.view = nv;
        const d = stocks.tickerDetails[stocksTickerCacheKey(sym)];
        if (d) {
          renderPriceChart(d, 'stocks');
          const backdrop = $('#chart-enlarge-backdrop');
          if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'stocks') {
            openDetailChartModal(d, 'stocks');
          }
        }
        syncStocksChartToolbar();
        syncChartModalViewToolbar();
      }
    }
  });
}

async function loadStocksTickerDetail(symbol) {
  const r = state.stocksChart.range;
  const key = `${String(symbol).toUpperCase()}|${r}`;
  const cached = _readFreshTickerDetail(stocks.tickerDetails, key);
  if (cached) return cached;
  const d = await api(
    `/api/ticker/${encodeURIComponent(symbol)}?range=${encodeURIComponent(r)}`
  );
  return _writeTickerDetailCache(stocks.tickerDetails, key, d);
}

function initSearchChartToolbar() {
  document.addEventListener('click', (e) => {
    const tr = e.target.closest('#search-chart-toolbar [data-search-range]');
    const tv = e.target.closest('#search-chart-toolbar [data-search-view]');
    if (!tr && !tv) return;
    const sym = searchUI.lastQueryResolvedSymbol;
    if (!sym) return;
    e.preventDefault();
    if (tr) {
      const nr = tr.getAttribute('data-search-range');
      if (!nr || nr === state.searchChart.range) return;
      const oldR = state.searchChart.range;
      const oldKey = `${sym.toUpperCase()}|${oldR}`;
      state.searchChart.range = nr;
      state.searchChartSessionZoom = { start: 0, end: 1 };
      delete searchUI.tickerDetails[oldKey];
      void (async () => {
        try {
          const d = await loadSearchTickerDetail(sym);
          searchUI.lastTickerDetail = d;
          const host = $('#search-result');
          if (host && host.isConnected) {
            const r = searchUI.lastSearchPayload;
            if (r) renderSearchResult(r, d);
            else renderPriceChart(d, 'search');
            syncSearchChartToolbar();
          }
        } catch (err) {
          const el = $('#search-price-chart');
          if (el) {
            el.innerHTML = '';
            el.append(h('div', { class: 'callout callout-warning' },
              h('div', { class: 'callout-title' }, 'Price chart'),
              `Could not load chart: ${err}`));
          }
        }
      })();
      return;
    }
    if (tv) {
      const nv = tv.getAttribute('data-search-view');
      if (nv && nv !== state.searchChart.view) {
        state.searchChart.view = nv;
        const d = searchUI.tickerDetails[searchTickerCacheKey(sym)];
        if (d) {
          renderPriceChart(d, 'search');
          const backdrop = $('#chart-enlarge-backdrop');
          if (backdrop && !backdrop.classList.contains('hidden') && state.chartModalSource === 'search') {
            openDetailChartModal(d, 'search');
          }
        }
        syncSearchChartToolbar();
        syncChartModalViewToolbar();
      }
    }
  });
}

async function loadSearchTickerDetail(symbol) {
  const r = state.searchChart.range;
  const key = `${String(symbol).toUpperCase()}|${r}`;
  const cached = _readFreshTickerDetail(searchUI.tickerDetails, key);
  if (cached) return cached;
  const d = await api(
    `/api/ticker/${encodeURIComponent(symbol)}?range=${encodeURIComponent(r)}`
  );
  return _writeTickerDetailCache(searchUI.tickerDetails, key, d);
}

function renderChipRow() {
  const host = $('#symbol-chips');
  host.innerHTML = '';
  for (const sym of state.universe) {
    host.append(
      h(
        'span',
        {
          class: 'chip' + (sym === state.activeSymbol ? ' is-active' : ''),
          onClick: () => openTicker(sym),
        },
        sym
      )
    );
  }
}

function renderDetail(d) {
  renderHero(d);
  renderPriceChart(d);
  renderFactorChart(d);
  renderFactorTable(d);
  renderDetailSignalTech(d);
  renderRecommender(d);
}

function renderDetailSignalTech(d) {
  const sig = $('#detail-signal-body');
  if (sig) {
    sig.textContent = d.signal_detail || d.chart_context?.headline || '—';
  }
  const tech = $('#detail-tech-body');
  if (tech) {
    tech.innerHTML = '';
    if (d.narrative_summary) {
      tech.append(
        h('p', { class: 'detail-narrative' }, d.narrative_summary),
      );
    }
    if (d.technical_bullets && d.technical_bullets.length) {
      const ul = h('ul', { class: 'detail-tech-list' });
      for (const b of d.technical_bullets) {
        ul.append(h('li', {}, b));
      }
      tech.append(ul);
    } else if (!d.narrative_summary) {
      tech.append(h('div', { class: 'muted' }, '—'));
    }
  }
  const news = $('#detail-news-body');
  if (news) {
    news.innerHTML = '';
    if (!d.news || !d.news.length) {
      news.append(
        h('div', { class: 'muted' }, 'No symbol-specific headlines in the feed right now.'),
      );
    } else {
      for (const it of d.news) {
        const url = it.url && String(it.url).trim() ? it.url : '#';
        const when = it.published_at ? fmtESTCompact(it.published_at) : '';
        const meta = [it.publisher, when].filter(Boolean).join(' · ');
        news.append(
          h('div', { class: 'detail-news-item' },
            h('a', {
              class: 'detail-news-title',
              href: url,
              target: '_blank',
              rel: 'noopener noreferrer',
            }, String(it.title || '')),
            h('div', { class: 'detail-news-meta muted' }, meta),
          ),
        );
      }
    }
  }
}

function renderHero(d) {
  const r = d.row;
  const em = d.expected_move;
  const hero = $('#detail-hero');
  hero.innerHTML = '';

  hero.append(
    h(
      'div',
      { class: 'hero-left' },
      h('div', { class: 'hero-symbol' }, r.symbol),
      h('div', { class: 'hero-name' }, `${r.name} · ${r.sector}`)
    ),
    signalBadge(r.signal),
    h('div', { class: 'hero-price' }, fmtUSD(r.price)),
    h(
      'div',
      { class: 'hero-change ' + (r.change_pct >= 0 ? 'pos' : 'neg') },
      fmtPct(r.change_pct)
    ),
    h(
      'div',
      { class: 'hero-stats' },
      heroStat(fmtScore(r.composite_score), 'Composite score', {
        tone: r.composite_score > 0.3 ? 'pos' : r.composite_score < -0.3 ? 'neg' : '',
      }),
      heroStat(`${Math.round(r.confidence * 100)}%`, 'Confidence'),
      heroStat(r.iv_rank.toFixed(0), 'IV rank', {
        tone: r.iv_rank > 60 ? 'warning' : '',
      }),
      heroStat(`±${fmtUSD(em.one_sigma_usd)}`, `${r.dte_pref}d expected move (1σ)`)
    )
  );

  $('#price-change-trail').textContent =
    `${fmtPct(r.change_pct)}  ·  RSI ${r.rsi.toFixed(0)}`;
}

function heroStat(value, label, opts = {}) {
  return h(
    'div',
    { class: 'hero-stat ' + (opts.tone || '') },
    h('div', { class: 'hs-value' }, value),
    h('div', { class: 'hs-label' }, label)
  );
}

// ---------- Fancy price chart (SMA overlays, Bollinger, crosshair) --------

/** @param {string} target "detail" | "analyst" | "search" | "stocks" */
function renderPriceChart(d, target = 'detail') {
  const host =
    target === 'analyst'
      ? $('#analyst-price-chart')
      : target === 'search'
        ? $('#search-price-chart')
        : target === 'stocks'
          ? $('#stocks-price-chart')
          : $('#price-chart');
  const btn =
    target === 'analyst'
      ? $('#analyst-btn-enlarge-chart')
      : target === 'search'
        ? $('#search-btn-enlarge-chart')
        : target === 'stocks'
          ? $('#stocks-btn-enlarge-chart')
          : $('#btn-enlarge-chart');
  const titleEl =
    target === 'analyst'
      ? $('#analyst-price-card-title')
      : target === 'search'
        ? $('#search-price-card-title')
        : target === 'stocks'
          ? $('#stocks-price-card-title')
          : $('#price-chart-card-title');
  if (!host) return;
  host.innerHTML = '';
  const ch = d.chart;
  const bars = ch && Array.isArray(ch.bars) && ch.bars.length >= 2 ? ch.bars : null;
  const simpleMode = target === 'stocks' ? stocksChartIsSimpleMode() : false;
  const view =
    target === 'analyst'
      ? state.analystChart.view || 'mountain'
      : target === 'search'
        ? state.searchChart.view || 'mountain'
        : target === 'stocks'
          ? state.stocksChart.view || 'mountain'
          : state.detailChart.view || 'mountain';
  if (bars) {
    const vk = (ch.range_key || '1d').toUpperCase();
    const g =
      ch.x_granularity === 'session'
        ? '1D session (ET · 4am–8pm)'
        : ch.x_granularity === 'hour'
          ? 'Hourly'
          : 'Daily';
    if (titleEl) {
      titleEl.textContent = `Price · ${g} · ${bars.length} pts · range ${vk}`;
    }
    if (btn) {
      btn.hidden = false;
      btn.onclick = () => openDetailChartModal(d, target);
    }
    const xg = ch.x_granularity || 'day';
    const pClose = d.row && d.row.last != null && d.row.change_pct != null
      ? d.row.last / (1 + d.row.change_pct / 100)
      : null;
    const o = {
      yahooLayout: true,
      width: 1000,
      height: 300,
      xGranularity: xg,
      previousClose: pClose,
      simpleMode,
    };
    if (xg === 'session') {
      o.sessionZoom =
        target === 'analyst'
          ? state.analystChartSessionZoom
          : target === 'search'
            ? state.searchChartSessionZoom
            : target === 'stocks'
              ? state.stocksChartSessionZoom
              : state.detailChartSessionZoom;
      o.onSessionReapply =
        target === 'analyst'
          ? reapplyAnalystSessionChart
          : target === 'search'
            ? reapplySearchSessionChart
            : target === 'stocks'
              ? reapplyStocksSessionChart
              : reapplySessionDetailChart;
    }
    host.append(tickerOhlcChart(bars, view, d.row.direction, o));
    return;
  }
  const series =
    d.price_series && d.price_series.length >= 2 ? d.price_series : d.price_30d || [];
  if (series.length < 2) {
    if (btn) btn.hidden = true;
    if (titleEl) titleEl.textContent = 'Price';
    host.append(h('div', { class: 'chart-placeholder' }, 'No data'));
    return;
  }
  if (titleEl) {
    titleEl.textContent = `Price · ${series.length} sessions (SMA / EMA / Bollinger)`;
  }
  if (btn) {
    btn.hidden = false;
    btn.onclick = () => openDetailChartModal(d, target);
  }
  host.append(
    fancyPriceChart(series, d.row.direction, {
      yahooLayout: true,
      width: 1000,
      height: 300,
    }),
  );
}

function openDetailChartModal(d, source = 'detail') {
  state.chartModalSource = source;
  const backdrop = $('#chart-enlarge-backdrop');
  const host = $('#chart-modal-chart-host');
  const sig = $('#chart-modal-signals');
  const title = $('#chart-modal-title');
  if (!backdrop || !host || !sig) return;
  const ch = d.chart;
  const bars = ch && Array.isArray(ch.bars) && ch.bars.length >= 2 ? ch.bars : null;
  host.innerHTML = '';
  const simpleMode = source === 'stocks' ? stocksChartIsSimpleMode() : false;
  const view =
    source === 'analyst'
      ? state.analystChart.view || 'mountain'
      : source === 'search'
        ? state.searchChart.view || 'mountain'
        : source === 'stocks'
          ? state.stocksChart.view || 'mountain'
          : state.detailChart.view || 'mountain';
  if (bars) {
    const xg = ch.x_granularity || 'day';
    const pClose = d.row && d.row.last != null && d.row.change_pct != null
      ? d.row.last / (1 + d.row.change_pct / 100)
      : null;
    const o = {
      yahooLayout: true,
      width: 1400,
      height: 420,
      xGranularity: xg,
      enlarged: true,
      previousClose: pClose,
      simpleMode,
    };
    if (xg === 'session') {
      o.sessionZoom =
        source === 'analyst'
          ? state.analystChartSessionZoom
          : source === 'search'
            ? state.searchChartSessionZoom
            : source === 'stocks'
              ? state.stocksChartSessionZoom
              : state.detailChartSessionZoom;
      o.onSessionReapply =
        source === 'analyst'
          ? reapplyAnalystSessionChart
          : source === 'search'
            ? reapplySearchSessionChart
            : source === 'stocks'
              ? reapplyStocksSessionChart
              : reapplySessionDetailChart;
    }
    host.append(tickerOhlcChart(bars, view, d.row.direction, o));
  } else {
    const series =
      d.price_series && d.price_series.length >= 2 ? d.price_series : d.price_30d || [];
    if (series.length < 2) return;
    host.append(
      fancyPriceChart(series, d.row.direction, {
        yahooLayout: true,
        width: 1400,
        height: 420,
        enlarged: true,
      }),
    );
  }
  const ctx = d.chart_context;
  if (title) {
    title.textContent = ctx && ctx.headline
      ? `${d.row.symbol} · ${ctx.headline}`
      : `${d.row.symbol} · price & indicators`;
  }
  const overlaysBar = $('#chart-modal-overlays-toolbar');
  if (overlaysBar) {
    overlaysBar.hidden = source !== 'stocks';
    if (source === 'stocks') {
      const mode = state.stocksChart.overlayMode;
      overlaysBar.querySelectorAll('[data-chart-overlays]').forEach((b) => {
        b.classList.toggle('is-active', b.getAttribute('data-chart-overlays') === mode);
      });
    }
  }
  sig.innerHTML = '';
  if (ctx && Array.isArray(ctx.lines) && ctx.lines.length) {
    for (const line of ctx.lines) {
      sig.append(
        h('div', { class: 'chart-signal-line' },
          h('div', { class: 'chart-signal-label' }, line.label),
          h('div', { class: 'chart-signal-detail' }, line.detail)
        )
      );
    }
  } else {
    sig.append(
      h('div', { class: 'chart-signal-detail muted' },
        'Indicator notes are unavailable — refresh the page or re-open this ticker after upgrading the app.'
      )
    );
  }
  if (source === 'detail') syncDetailChartToolbar();
  if (source === 'analyst') syncAnalystChartToolbar();
  if (source === 'search') syncSearchChartToolbar();
  if (source === 'stocks') syncStocksChartToolbar();
  syncChartModalViewToolbar();
  backdrop.classList.remove('hidden');
  document.body.classList.add('chart-enlarge-open');
}

function syncStocksChartToolbar() {
  const r = state.stocksChart.range;
  const v = state.stocksChart.view;
  const o = state.stocksChart.overlayMode;
  document.querySelectorAll('#stocks-chart-toolbar [data-stocks-range]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-stocks-range') === r);
  });
  document.querySelectorAll('#stocks-chart-toolbar [data-stocks-view]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-stocks-view') === v);
  });
  document.querySelectorAll('#stocks-chart-toolbar [data-stocks-overlay]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-stocks-overlay') === o);
  });
}

function syncChartModalViewToolbar() {
  const v =
    state.chartModalSource === 'analyst'
      ? state.analystChart.view
      : state.chartModalSource === 'search'
        ? state.searchChart.view
        : state.chartModalSource === 'stocks'
          ? state.stocksChart.view
          : state.detailChart.view;
  document.querySelectorAll('#chart-modal-view-toolbar [data-detail-view]').forEach((b) => {
    b.classList.toggle('is-active', b.getAttribute('data-detail-view') === v);
  });
}

function closeDetailChartModal() {
  state.chartModalSource = null;
  const backdrop = $('#chart-enlarge-backdrop');
  const host = $('#chart-modal-chart-host');
  if (backdrop) backdrop.classList.add('hidden');
  document.body.classList.remove('chart-enlarge-open');
  if (host) host.innerHTML = '';
}

function initChartEnlargeModal() {
  const closeBtn = $('#chart-modal-close');
  const backdrop = $('#chart-enlarge-backdrop');
  if (closeBtn) {
    closeBtn.addEventListener('click', (e) => {
      e.preventDefault();
      closeDetailChartModal();
    });
  }
  if (backdrop) {
    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) closeDetailChartModal();
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!backdrop || backdrop.classList.contains('hidden')) return;
    closeDetailChartModal();
  });
}

function _sma(series, n) {
  const out = new Array(series.length).fill(null);
  let sum = 0;
  for (let i = 0; i < series.length; i++) {
    sum += series[i];
    if (i >= n) sum -= series[i - n];
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

function _ema(series, n) {
  const out = new Array(series.length).fill(null);
  const k = 2 / (n + 1);
  let prev = null;
  for (let i = 0; i < series.length; i++) {
    const v = series[i];
    if (i === n - 1) {
      let sum = 0;
      for (let j = 0; j < n; j++) sum += series[j];
      prev = sum / n;
      out[i] = prev;
    } else if (i >= n) {
      prev = v * k + prev * (1 - k);
      out[i] = prev;
    }
  }
  return out;
}

function _bollinger(series, n, mult) {
  const mid = _sma(series, n);
  const upper = new Array(series.length).fill(null);
  const lower = new Array(series.length).fill(null);
  for (let i = n - 1; i < series.length; i++) {
    let sum2 = 0;
    for (let j = i - n + 1; j <= i; j++) {
      const diff = series[j] - mid[i];
      sum2 += diff * diff;
    }
    const sd = Math.sqrt(sum2 / n);
    upper[i] = mid[i] + mult * sd;
    lower[i] = mid[i] - mult * sd;
  }
  return { mid, upper, lower };
}

function fancyPriceChart(series, direction, opts = {}) {
  const w = opts.width != null ? opts.width : 960;
  const h0 = opts.height != null ? opts.height : 280;
  const yahooLayout = opts.yahooLayout !== false;
  const pad = yahooLayout
    ? { top: 16, right: 54, bottom: 34, left: 10 }
    : { top: 18, right: 18, bottom: 26, left: 58 };
  const innerW = w - pad.left - pad.right;
  const innerH = h0 - pad.top - pad.bottom;
  const n = series.length;

  const simpleMode = !!opts.simpleMode;
  // Overlays — compute from closes only (no extra API payload required).
  const sma5 = simpleMode ? [] : _sma(series, Math.min(5, n - 1));
  const sma10 = simpleMode ? [] : _sma(series, Math.min(10, n - 1));
  const sma20 = simpleMode ? [] : _sma(series, Math.min(20, n - 1));
  const ema9 = simpleMode ? [] : _ema(series, Math.min(9, n - 1));
  const bandWin = Math.min(20, n - 1);
  const bb = simpleMode ? { upper: [], lower: [] } : _bollinger(series, bandWin, 2);

  // Value range uses price + bands so the envelope fits.
  const all = [...series];
  for (const v of [...sma5, ...sma10, ...sma20, ...ema9, ...bb.upper, ...bb.lower]) {
    if (v != null) all.push(v);
  }
  const min = Math.min(...all);
  const max = Math.max(...all);
  const pad_y = (max - min) * 0.06 || 1;
  const lo = min - pad_y;
  const hi = max + pad_y;
  const range = hi - lo;
  const xStep = innerW / (n - 1);

  const xy = (v, i) => [
    pad.left + i * xStep,
    pad.top + innerH - ((v - lo) / range) * innerH,
  ];

  const toPath = (seriesLike) => {
    let path = '';
    let started = false;
    for (let i = 0; i < seriesLike.length; i++) {
      const v = seriesLike[i];
      if (v == null) { started = false; continue; }
      const [x, y] = xy(v, i);
      path += (started ? 'L' : 'M') + x.toFixed(2) + ',' + y.toFixed(2) + ' ';
      started = true;
    }
    return path.trim();
  };

  const color =
    direction === 'bull' ? 'var(--success)' :
    direction === 'bear' ? 'var(--danger)'  : 'var(--info)';

  const enlarged = !!opts.enlarged;
  const smA = yahooLayout ? 0.55 : 0.9;
  const root = svg('svg', {
    class: 'fancy-chart' + (yahooLayout ? ' fancy-chart--yahoo' : ''),
    viewBox: `0 0 ${w} ${h0}`,
    preserveAspectRatio: 'none',
    style: `height: ${h0}px; width: 100%; min-width: 0; display: block;`,
  });

  // Defs: gradient for area fill + clip path.
  const defs = svg('defs');
  const gradId = 'grad-' + Math.random().toString(36).slice(2, 8);
  const grad = svg('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 });
  const gOp0 = yahooLayout ? 0.48 : 0.35;
  const gOp1 = yahooLayout ? 0.04 : 0;
  grad.append(
    svg('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': gOp0 }),
    svg('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': gOp1 }),
  );
  defs.append(grad);
  root.append(defs);

  // Dark plot backdrop (Yahoo-style)
  if (yahooLayout) {
    root.append(
      svg('rect', {
        x: pad.left,
        y: pad.top,
        width: innerW,
        height: innerH,
        fill: 'rgba(8, 10, 14, 0.55)',
        stroke: 'var(--stroke-soft)',
        'stroke-width': 1,
        rx: 2,
      })
    );
  }

  // Horizontal gridlines + y-axis labels (right side when Yahoo layout)
  const yTxt = (xPos, anc, yVal, yPx) => svg('text', {
    x: xPos, y: yPx + 4, 'text-anchor': anc,
    fill: 'var(--text-dim)', 'font-size': 11, 'font-family': 'var(--mono)',
  }, '$' + yVal.toFixed(yVal > 100 ? 0 : 2));

  for (let g = 0; g <= 4; g++) {
    const v = lo + (range * g) / 4;
    const y = pad.top + innerH - ((v - lo) / range) * innerH;
    root.append(
      svg('line', {
        x1: pad.left, y1: y, x2: w - pad.right, y2: y,
        stroke: 'var(--stroke-soft)', 'stroke-dasharray': yahooLayout ? '1 5' : '2 4',
        'stroke-opacity': yahooLayout ? 0.5 : 1,
      }),
    );
    if (yahooLayout) {
      root.append(yTxt(w - 8, 'end', v, y));
    } else {
      root.append(
        svg('text', {
          x: pad.left - 8, y: y + 3, 'text-anchor': 'end',
          fill: 'var(--text-dim)', 'font-size': 10, 'font-family': 'var(--mono)',
        }, '$' + v.toFixed(v > 100 ? 0 : 2)),
      );
    }
  }

  // Reference: prior bar close (dashed) — "previous session" in window
  if (n >= 2) {
    const pClose = opts.previousClose != null ? opts.previousClose : series[n - 2];
    if (pClose != null && pClose >= lo && pClose <= hi) {
      const yP = pad.top + innerH - ((pClose - lo) / range) * innerH;
      root.append(
        svg('line', {
          x1: pad.left, y1: yP, x2: w - pad.right, y2: yP,
          stroke: 'var(--text-muted)', 'stroke-dasharray': '4 3', 'stroke-width': 1,
          'stroke-opacity': 0.75,
        }),
        svg('text', {
          x: pad.left + 4, y: yP - 3, 'text-anchor': 'start',
          fill: 'var(--text-muted)', 'font-size': 9, 'font-family': 'var(--mono)',
        }, `Prev bar ${fmtUSD(pClose)}`),
      );
    }
  }

  // X-axis ticks (oldest bar → most recent; index vs window start)
  const nTicks = Math.min(7, n);
  for (let t = 0; t < nTicks; t++) {
    const idx = Math.round(((n - 1) * t) / (nTicks - 1));
    const x = pad.left + idx * xStep;
    const rel = idx - (n - 1);
    const lab = t === 0 ? 'oldest' : t === nTicks - 1 ? 'now' : `d${rel}`;
    root.append(
      svg('text', {
        x, y: h0 - 6,
        'text-anchor': 'middle',
        fill: 'var(--text-dim)',
        'font-size': 10,
        'font-family': 'var(--mono)',
      }, lab),
    );
  }

  // Bollinger band area (upper then lower reversed = closed poly).
  let bandD = '';
  let startedBand = false;
  for (let i = 0; i < n; i++) {
    if (bb.upper[i] == null) { startedBand = false; continue; }
    const [x, y] = xy(bb.upper[i], i);
    bandD += (startedBand ? 'L' : 'M') + x.toFixed(2) + ',' + y.toFixed(2) + ' ';
    startedBand = true;
  }
  for (let i = n - 1; i >= 0; i--) {
    if (bb.lower[i] == null) continue;
    const [x, y] = xy(bb.lower[i], i);
    bandD += 'L' + x.toFixed(2) + ',' + y.toFixed(2) + ' ';
  }
  if (bandD) bandD += 'Z';
  if (!simpleMode && bandD) {
    root.append(svg('path', {
      d: bandD,
      fill: 'var(--info)',
      'fill-opacity': 0.07,
      stroke: 'none',
    }));
    root.append(svg('path', {
      d: toPath(bb.upper),
      fill: 'none',
      stroke: 'var(--info)',
      'stroke-opacity': 0.5,
      'stroke-width': 1,
      'stroke-dasharray': '3 3',
    }));
    root.append(svg('path', {
      d: toPath(bb.lower),
      fill: 'none',
      stroke: 'var(--info)',
      'stroke-opacity': 0.5,
      'stroke-width': 1,
      'stroke-dasharray': '3 3',
    }));
  }

  // Area fill under price
  const pricePts = series.map((v, i) => xy(v, i));
  const lineD = pricePts.map(([x, y], i) => (i === 0 ? 'M' : 'L') + x.toFixed(2) + ',' + y.toFixed(2)).join(' ');
  const areaD = lineD +
    ' L' + pricePts[n - 1][0].toFixed(2) + ',' + (pad.top + innerH) +
    ' L' + pricePts[0][0].toFixed(2) + ',' + (pad.top + innerH) + ' Z';
  root.append(svg('path', { d: areaD, fill: `url(#${gradId})`, stroke: 'none' }));

  // Indicator lines (lightest → heaviest so price is on top).
  const sw = yahooLayout ? 1.0 : 1.2;
  if (!simpleMode) {
    root.append(svg('path', {
      d: toPath(sma20),
      fill: 'none', stroke: '#8b9dd9', 'stroke-width': sw, 'stroke-opacity': 0.75 * smA,
    }));
    root.append(svg('path', {
      d: toPath(sma10),
      fill: 'none', stroke: '#d9a36b', 'stroke-width': sw, 'stroke-opacity': 0.8 * smA,
    }));
    root.append(svg('path', {
      d: toPath(ema9),
      fill: 'none', stroke: '#bf7af0', 'stroke-width': sw,
      'stroke-dasharray': '4 2', 'stroke-opacity': 0.8 * smA,
    }));
    root.append(svg('path', {
      d: toPath(sma5),
      fill: 'none', stroke: '#f2d47a', 'stroke-width': sw, 'stroke-opacity': 0.8 * smA,
    }));
  }

  // Price line on top (mountain edge)
  root.append(svg('path', {
    d: lineD, fill: 'none', stroke: color, 'stroke-width': yahooLayout ? 2.2 : 2,
    'stroke-linejoin': 'round', 'stroke-linecap': 'round',
  }));

  // Last point marker
  const [lx, ly] = pricePts[n - 1];
  root.append(
    svg('circle', { cx: lx, cy: ly, r: 5, fill: color, 'fill-opacity': 0.25 }),
    svg('circle', { cx: lx, cy: ly, r: 2.6, fill: color }),
  );

  // Crosshair + tooltip on hover.
  const crossG = svg('g', { class: 'crosshair', opacity: 0 });
  const crossV = svg('line', {
    x1: 0, y1: pad.top, x2: 0, y2: pad.top + innerH,
    stroke: 'var(--text-dim)', 'stroke-dasharray': '2 3',
  });
  const crossH = svg('line', {
    x1: pad.left, y1: 0, x2: w - pad.right, y2: 0,
    stroke: 'var(--text-dim)', 'stroke-dasharray': '2 3',
  });
  const dot = svg('circle', { r: 3, fill: color });
  crossG.append(crossV, crossH, dot);
  root.append(crossG);

  const tip = h('div', { class: 'chart-tooltip hidden' });
  const legendRow = h(
    'div',
    { class: 'price-chart-legend-row mono' },
    legendSwatch(color, yahooLayout ? 'Close (area)' : 'Close'),
    ...(simpleMode
      ? [h('span', { class: 'muted' }, 'Simple chart')]
      : [
        legendSwatch('#f2d47a', `SMA${Math.min(5, n - 1)}`),
        legendSwatch('#d9a36b', `SMA${Math.min(10, n - 1)}`),
        legendSwatch('#8b9dd9', `SMA${Math.min(20, n - 1)}`),
        legendSwatch('#bf7af0', `EMA${Math.min(9, n - 1)}`, true),
        legendSwatch('var(--info)', `Bollinger (${bandWin},2σ)`, false, true),
      ]),
  );
  const canvas = h('div', { class: 'price-chart-canvas' }, root, tip);
  const figure = h(
    'div',
    { class: 'price-chart-figure' + (enlarged ? ' price-chart-figure--wide' : '') },
    canvas,
    legendRow,
  );

  const hitArea = svg('rect', {
    x: pad.left, y: pad.top, width: innerW, height: innerH,
    fill: 'transparent', 'pointer-events': 'all',
  });
  root.append(hitArea);

  hitArea.addEventListener('mousemove', (e) => {
    const rect = root.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * w;
    const i = Math.max(0, Math.min(n - 1, Math.round((mx - pad.left) / xStep)));
    const [x, y] = pricePts[i];
    crossG.setAttribute('opacity', 1);
    crossV.setAttribute('x1', x); crossV.setAttribute('x2', x);
    crossH.setAttribute('y1', y); crossH.setAttribute('y2', y);
    dot.setAttribute('cx', x); dot.setAttribute('cy', y);
    const val = series[i];
    const ref = series[0];
    const pct = ((val - ref) / ref) * 100;
    tip.classList.remove('hidden');
    tip.innerHTML =
      `<div class="tt-row"><span>Close</span><b>${fmtUSD(val)}</b></div>` +
      `<div class="tt-row"><span>vs start</span>` +
        `<b class="${pct >= 0 ? 'pos' : 'neg'}">${fmtPct(pct)}</b></div>` +
      (sma20[i] != null ? `<div class="tt-row"><span>SMA20</span><b>${fmtUSD(sma20[i])}</b></div>` : '') +
      (bb.upper[i] != null ? `<div class="tt-row"><span>BB hi</span><b>${fmtUSD(bb.upper[i])}</b></div>` : '') +
      (bb.lower[i] != null ? `<div class="tt-row"><span>BB lo</span><b>${fmtUSD(bb.lower[i])}</b></div>` : '');
    const cRect = canvas.getBoundingClientRect();
    const tipW = 180;
    const lx = e.clientX - cRect.left;
    const ly = e.clientY - cRect.top;
    tip.style.left = `${Math.min(cRect.width - tipW - 6, Math.max(6, lx - tipW / 2))}px`;
    tip.style.top = `${Math.max(6, ly - 72)}px`;
  });
  hitArea.addEventListener('mouseleave', () => {
    crossG.setAttribute('opacity', 0);
    tip.classList.add('hidden');
  });

  return figure;
}

function _fmtDetailAxisTime(iso, xGran) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 16);
  if (xGran === 'hour') {
    return d.toLocaleTimeString('en-US', { hour: 'numeric', timeZone: 'America/New_York' });
  }
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

/** Minutes from midnight, America/New_York, for a UTC instant. */
function _etMinFromMidnight(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 9 * 60 + 30;
  const f = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    hour: 'numeric',
    minute: 'numeric',
    hour12: false,
  });
  const parts = f.formatToParts(d);
  let hh = 0;
  let mm = 0;
  for (const p of parts) {
    if (p.type === 'hour') hh = parseInt(p.value, 10);
    if (p.type === 'minute') mm = parseInt(p.value, 10);
  }
  // Intl.DateTimeFormat with hour12: false can return '24' for midnight in some Node versions, or '00'
  if (hh === 24) hh = 0;
  return hh * 60 + mm;
}

/** X position 0..1 for Yahoo-style 6:00am–6:00pm session strip (extended bars clamped). */
function _etSessionRatio6am6pm(iso) {
  const m = _etMinFromMidnight(iso);
  const start = 4 * 60; // 4:00am ET (pre-market open)
  const end = 20 * 60; // 8:00pm ET (after-hours close)
  return Math.max(0, Math.min(1, (m - start) / (end - start)));
}

/** e.g. "4/27 10:53 AM" (Eastern) for intraday tooltips */
function _fmtYahooSessionHoverET(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    month: 'numeric',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    hour12: true,
  })
    .format(d)
    .replace(', ', ' ');
}

/**
 * Regular-session open: `o` of the first bar at or after 9:30 AM America/New_York
 * (NYSE/Nasdaq RTH open; ET includes EST/EDT).
 */
function _rthOpenPrice930Et(bars) {
  const openMin = 9 * 60 + 30;
  for (const b of bars) {
    const m = _etMinFromMidnight(b.t);
    if (m >= openMin) {
      const o = b.o;
      if (o != null && Number.isFinite(Number(o))) return Number(o);
      return null;
    }
  }
  return null;
}

/**
 * 1D intraday: time-based x over a full 6am–6pm ET window, dense minute path, pre-market tint.
 * Mirrors Yahoo’s smooth “mountain” and candle 1D experience.
 */
function tickerOhlcSessionChart(bars, view, direction, opts = {}) {
  const w = opts.width != null ? opts.width : 1000;
  const h0 = opts.height != null ? opts.height : 300;
  const yahooLayout = opts.yahooLayout !== false;
  const enlarged = !!opts.enlarged;
  const pad = yahooLayout
    ? { top: 16, right: 54, bottom: 46, left: 10 }
    : { top: 18, right: 18, bottom: 40, left: 58 };
  const innerW = w - pad.left - pad.right;
  const innerH = h0 - pad.top - pad.bottom;

  const sorted = bars.slice().sort((a, b) => new Date(a.t) - new Date(b.t));
  const n = sorted.length;
  if (n < 2) {
    return h('div', { class: 'chart-placeholder' }, 'Not enough bars for this range');
  }

  const zWin = opts.sessionZoom != null ? opts.sessionZoom : state.detailChartSessionZoom;
  const reapplySess = typeof opts.onSessionReapply === 'function' ? opts.onSessionReapply : reapplySessionDetailChart;
  const zs = zWin.start;
  const ze = zWin.end;
  const zspan = Math.max(1e-9, ze - zs);
  const rToX = (r) => pad.left + ((r - zs) / zspan) * innerW;
  const ratios = sorted.map((b) => _etSessionRatio6am6pm(b.t));
  const xs = ratios.map((r) => rToX(r));
  const inViewBars = sorted.filter((b, i) => {
    const r = ratios[i];
    return r >= zs - 1e-5 && r <= ze + 1e-5;
  });
  const forRange = inViewBars.length >= 2 ? inViewBars : sorted;

  const series = sorted.map((b) => b.c);
  const firstC = series[0];
  const lastC = series[n - 1];
  const dayDown = lastC < firstC;
  const yahooC = dayDown ? 'var(--danger)' : 'var(--success)';
  const color = direction === 'bull' && !dayDown
    ? 'var(--success)'
    : direction === 'bear' && dayDown
      ? 'var(--danger)'
      : yahooC;

  let min = Infinity;
  let max = -Infinity;
  for (const b of forRange) {
    min = Math.min(min, b.l, b.h, b.o, b.c);
    max = Math.max(max, b.l, b.h, b.o, b.c);
  }

  const simpleMode = !!opts.simpleMode;
  const showOverlays = !simpleMode && (view === 'mountain' || view === 'line');
  const sma5 = showOverlays ? _sma(series, Math.min(5, n - 1)) : [];
  const sma10 = showOverlays ? _sma(series, Math.min(10, n - 1)) : [];
  const sma20 = showOverlays ? _sma(series, Math.min(20, n - 1)) : [];
  const ema9 = showOverlays ? _ema(series, Math.min(9, n - 1)) : [];
  const bandWin = Math.min(20, n - 1);
  const bb = showOverlays ? _bollinger(series, bandWin, 2) : { mid: [], upper: [], lower: [] };

  const all = [min, max];
  if (showOverlays) {
    for (const v of [...series, ...sma5, ...sma10, ...sma20, ...ema9, ...bb.upper, ...bb.lower]) {
      if (v != null) all.push(v);
    }
  } else {
    for (const b of sorted) {
      all.push(b.o, b.h, b.l, b.c);
    }
  }
  const dmin = Math.min(...all);
  const dmax = Math.max(...all);
  const pad_y = (dmax - dmin) * 0.05 || 1;
  const lo = dmin - pad_y;
  const hi = dmax + pad_y;
  const yrange = hi - lo;
  const yAt = (v) => pad.top + innerH - ((v - lo) / yrange) * innerH;

  const xyS = (v, i) => [xs[i], yAt(v)];

  const toPath = (ser) => {
    let path = '';
    let started = false;
    for (let i = 0; i < ser.length; i++) {
      const v = ser[i];
      if (v == null) { started = false; continue; }
      const [x, y0] = xyS(v, i);
      path += (started ? 'L' : 'M') + x.toFixed(2) + ',' + y0.toFixed(2) + ' ';
      started = true;
    }
    return path.trim();
  };

  const root = svg('svg', {
    class: 'fancy-chart' + (yahooLayout ? ' fancy-chart--yahoo' : '') + (enlarged ? ' fancy-chart--enlarged' : '') + ' fancy-chart--session',
    viewBox: `0 0 ${w} ${h0}`,
    preserveAspectRatio: 'xMidYMid meet',
    style: `height: ${h0}px; width: 100%; max-width: 100%; min-width: 0; display: block;`,
  });
  const defs = svg('defs');
  const gradId = 's-grad-' + Math.random().toString(36).slice(2, 8);
  const patId = 's-pre-' + Math.random().toString(36).slice(2, 8);
  const grad = svg('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(
    svg('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': 0.5 }),
    svg('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0.04 }),
  );
  const pat = svg('pattern', { id: patId, width: 5, height: 5, patternUnits: 'userSpaceOnUse' });
  pat.append(
    svg('path', { d: 'M0,5 l5,-5 M-1,1 l2,-2 M4,6 l2,-2', stroke: 'rgba(255,255,255,0.07)', 'stroke-width': 0.8 }),
  );
  const clipId = 'sess-clip-' + Math.random().toString(36).slice(2, 9);
  defs.append(
    grad,
    pat,
    svg('clipPath', { id: clipId },
      svg('rect', { x: pad.left, y: pad.top, width: innerW, height: innerH }),
    ),
  );
  const gPlot = svg('g', { 'clip-path': `url(#${clipId})` });
  root.append(defs);
  root.append(gPlot);

  if (yahooLayout) {
    gPlot.append(
      svg('rect', {
        x: pad.left,
        y: pad.top,
        width: innerW,
        height: innerH,
        fill: 'rgba(8, 10, 14, 0.55)',
        stroke: 'var(--stroke-soft)',
        'stroke-width': 1,
        rx: 2,
      }),
    );
    const rOpen = ((9 * 60 + 30) - 4 * 60) / (16 * 60);
    const xPre0 = rToX(0);
    const xPre1 = rToX(rOpen);
    const preW = Math.max(0, xPre1 - xPre0);
    if (preW > 0) {
      gPlot.append(
        svg('rect', {
          x: xPre0,
          y: pad.top,
          width: preW,
          height: innerH,
          fill: `url(#${patId})`,
          'fill-opacity': 0.45,
        }),
      );
    }
    const rClose = ((16 * 60) - 4 * 60) / (16 * 60);
    const xPost0 = rToX(rClose);
    const xPost1 = rToX(1);
    const postW = Math.max(0, xPost1 - xPost0);
    if (postW > 0) {
      gPlot.append(
        svg('rect', {
          x: xPost0,
          y: pad.top,
          width: postW,
          height: innerH,
          fill: `url(#${patId})`,
          'fill-opacity': 0.45,
        }),
      );
    }
  }

  const yTxt = (yVal, yPx) => svg('text', {
    x: w - 8, y: yPx + 4, 'text-anchor': 'end',
    fill: 'var(--text-dim)', 'font-size': 11, 'font-family': 'var(--mono)',
  }, '$' + yVal.toFixed(yVal > 1000 ? 0 : 2));

  for (let g = 0; g <= 4; g++) {
    const v = lo + (yrange * g) / 4;
    const y = yAt(v);
    gPlot.append(
      svg('line', {
        x1: pad.left, y1: y, x2: w - pad.right, y2: y,
        stroke: 'var(--stroke-soft)', 'stroke-dasharray': '1 5', 'stroke-opacity': 0.5,
      }),
    );
    if (yahooLayout) root.append(yTxt(v, y));
  }

  const rthOpen = _rthOpenPrice930Et(sorted);
  if (rthOpen != null) {
    const yRth = yAt(rthOpen);
    if (Number.isFinite(yRth)) {
      gPlot.append(
        svg('line', {
          x1: pad.left,
          y1: yRth,
          x2: w - pad.right,
          y2: yRth,
          stroke: 'rgba(210, 215, 225, 0.85)',
          'stroke-width': 1.35,
          'pointer-events': 'none',
        }),
      );
    }
  }

  // X-axis: 2h ticks when wide; 10/5/2 minute ticks when zoomed in
  {
    const span = ze - zs;
    const fullR = (m) => (m - 6 * 60) / (12 * 60);
    if (span > 0.18) {
      for (const hr of [6, 8, 10, 12, 14, 16, 18]) {
        const r = fullR(hr * 60);
        if (r < zs - 1e-6 || r > ze + 1e-6) continue;
        const x = rToX(r);
        if (hr < 18) {
          const lab = hr < 12
            ? `${hr}:00 AM`
            : hr === 12
              ? '12:00 PM'
              : `${hr - 12}:00 PM`;
          root.append(
            svg('text', { x, y: h0 - 6, 'text-anchor': 'middle', fill: 'var(--text-dim)', 'font-size': 10, 'font-family': 'var(--mono)' },
              lab),
          );
        } else {
          const d0 = new Date(sorted[n - 1].t);
          const dateStr = !Number.isNaN(d0.getTime())
            ? d0.toLocaleDateString('en-US', { month: 'numeric', day: 'numeric', timeZone: 'America/New_York' })
            : '';
          root.append(
            svg('text', { x, y: h0 - 18, 'text-anchor': 'end', fill: 'var(--text-dim)', 'font-size': 10, 'font-family': 'var(--mono)' },
              '6:00 PM'),
          );
          root.append(
            svg('text', { x, y: h0 - 4, 'text-anchor': 'end', fill: 'var(--text-dim)', 'font-size': 10, 'font-family': 'var(--mono)' },
              dateStr),
          );
        }
      }
    } else {
      const stepMin = span > 0.04 ? 10 : span > 0.015 ? 5 : 2;
      const m0 = Math.ceil((zs * 12 * 60 + 6 * 60) / stepMin) * stepMin;
      const m1 = ze * 12 * 60 + 6 * 60;
      for (let m = m0; m <= m1 + 0.1; m += stepMin) {
        if (m < 6 * 60 || m > 18 * 60) continue;
        const r = fullR(m);
        if (r < zs - 1e-6 || r > ze + 1e-6) continue;
        const hh = Math.floor(m / 60);
        const mm = m % 60;
        const am = hh < 12;
        const dh = am ? (hh % 12 === 0 ? 12 : (hh % 12)) : (hh === 12 ? 12 : hh - 12);
        const lab = `${dh}:${String(mm).padStart(2, '0')} ${am ? 'AM' : 'PM'}`;
        root.append(
          svg('text', { x: rToX(r), y: h0 - 6, 'text-anchor': 'middle', fill: 'var(--text-dim)', 'font-size': 9, 'font-family': 'var(--mono)' },
            lab),
        );
      }
    }
  }

  if (view === 'candle' || view === 'bar') {
    // ~1 minute across plot at full 1:1; scales when zoomed to session subset
    const bw = Math.max(0.4, (innerW / (720 * zspan)) * 1.0);
    for (let i = 0; i < n; i++) {
      const b = sorted[i];
      const cx = xs[i];
      const yH = yAt(b.h);
      const yL = yAt(b.l);
      const yO = yAt(b.o);
      const yC0 = yAt(b.c);
      const bull = b.c >= b.o;
      const cFill = bull ? 'var(--success)' : 'var(--danger)';
      if (view === 'candle') {
        gPlot.append(
          svg('line', { x1: cx, y1: yH, x2: cx, y2: yL, stroke: cFill, 'stroke-width': 1, 'stroke-opacity': 0.95 }),
        );
        const topB = Math.min(yO, yC0);
        const botB = Math.max(yO, yC0);
        const hB = Math.max(0.5, botB - topB);
        gPlot.append(
          svg('rect', { x: cx - bw / 2, y: topB, width: bw, height: hB, fill: cFill, 'fill-opacity': 0.8, stroke: cFill, rx: 0.4 }),
        );
      } else {
        const tick = bw * 0.55;
        gPlot.append(
          svg('line', { x1: cx, y1: yH, x2: cx, y2: yL, stroke: 'var(--text-muted)', 'stroke-width': 1.1 }),
        );
        gPlot.append(
          svg('line', { x1: cx - tick, y1: yO, x2: cx, y2: yO, stroke: cFill, 'stroke-width': 1.2 }),
        );
        gPlot.append(
          svg('line', { x1: cx, y1: yC0, x2: cx + tick, y2: yC0, stroke: cFill, 'stroke-width': 1.2 }),
        );
      }
    }
  } else {
    if (showOverlays) {
      let bandD = '';
      let startedBand = false;
      for (let i = 0; i < n; i++) {
        if (bb.upper[i] == null) { startedBand = false; continue; }
        const [x, yU] = xyS(bb.upper[i], i);
        bandD += (startedBand ? 'L' : 'M') + x.toFixed(2) + ',' + yU.toFixed(2) + ' ';
        startedBand = true;
      }
      for (let i = n - 1; i >= 0; i--) {
        if (bb.lower[i] == null) continue;
        const [x, y0] = xyS(bb.lower[i], i);
        bandD += 'L' + x.toFixed(2) + ',' + y0.toFixed(2) + ' ';
      }
      if (bandD) {
        bandD += 'Z';
        gPlot.append(svg('path', { d: bandD, fill: 'var(--info)', 'fill-opacity': 0.07, stroke: 'none' }));
        gPlot.append(svg('path', { d: toPath(bb.upper), fill: 'none', stroke: 'var(--info)', 'stroke-opacity': 0.45, 'stroke-width': 1, 'stroke-dasharray': '3 3' }));
        gPlot.append(svg('path', { d: toPath(bb.lower), fill: 'none', stroke: 'var(--info)', 'stroke-opacity': 0.45, 'stroke-width': 1, 'stroke-dasharray': '3 3' }));
      }
      const sw = 1.0;
      const smA = 0.55;
      gPlot.append(svg('path', { d: toPath(sma20), fill: 'none', stroke: '#8b9dd9', 'stroke-width': sw, 'stroke-opacity': 0.75 * smA }));
      gPlot.append(svg('path', { d: toPath(sma10), fill: 'none', stroke: '#d9a36b', 'stroke-width': sw, 'stroke-opacity': 0.8 * smA }));
      gPlot.append(svg('path', { d: toPath(ema9), fill: 'none', stroke: '#bf7af0', 'stroke-width': sw, 'stroke-dasharray': '4 2', 'stroke-opacity': 0.8 * smA }));
      gPlot.append(svg('path', { d: toPath(sma5), fill: 'none', stroke: '#f2d47a', 'stroke-width': sw, 'stroke-opacity': 0.8 * smA }));
    }
    let lineD2 = '';
    for (let i = 0; i < n; i++) {
      const [x, y0] = xyS(series[i], i);
      lineD2 += (i === 0 ? 'M' : 'L') + x.toFixed(2) + ',' + y0.toFixed(2) + ' ';
    }
    if (view === 'mountain') {
      const [x0, y0] = xyS(series[0], 0);
      const [x1, y1] = xyS(series[n - 1], n - 1);
      const areaD = lineD2.trim() +
        ' L' + x1.toFixed(2) + ',' + (pad.top + innerH) +
        ' L' + x0.toFixed(2) + ',' + (pad.top + innerH) + ' Z';
      gPlot.append(svg('path', { d: areaD, fill: `url(#${gradId})`, stroke: 'none' }));
    }
    gPlot.append(svg('path', {
      d: lineD2.trim(),
      fill: 'none',
      stroke: color,
      'stroke-width': 2.2,
      'stroke-linejoin': 'round',
      'stroke-linecap': 'round',
    }));
  }

  // Right margin X for last-price guide
  const xPlotRight = w - pad.right;
  const xLastPriceDot = xPlotRight - 5; // stay inside clip

  // Last price horizontal (Yahoo-style): full-width guide + markers on the right margin
  const yLast = yAt(lastC);
  gPlot.append(
    svg('line', {
      x1: pad.left, y1: yLast, x2: xPlotRight, y2: yLast,
      stroke: 'var(--info)',
      'stroke-width': 1.2,
      'stroke-dasharray': '4 3',
      'stroke-opacity': 0.85,
    }),
  );
  gPlot.append(
    svg('circle', { cx: xLastPriceDot, cy: yLast, r: 4.2, fill: color, 'fill-opacity': 0.35 }),
    svg('circle', { cx: xLastPriceDot, cy: yLast, r: 2.5, fill: color }),
  );
  const lastCloseStr = fmtUSD(lastC);
  const lastLabelW = Math.max(50, lastCloseStr.length * 6.6);
  const yLastText = Math.max(pad.top + 4, Math.min(pad.top + innerH - 1, yLast + 4));
  root.append(
    svg('rect', {
      x: w - lastLabelW - 10,
      y: yLastText - 11,
      width: lastLabelW + 4,
      height: 16,
      rx: 3,
      fill: 'rgba(8, 10, 14, 0.94)',
      stroke: 'var(--info)',
      'stroke-width': 1,
      'pointer-events': 'none',
    }),
    svg('text', {
      x: w - 10,
      y: yLastText,
      'text-anchor': 'end',
      fill: 'var(--info)',
      'font-size': 11,
      'font-family': 'var(--mono)',
      'font-weight': 600,
      'pointer-events': 'none',
    }, lastCloseStr),
  );

  // Crosshair + Yahoo-style O/H/L/C/V tooltip (ET time label on axis)
  const crossG = svg('g', { class: 'session-crosshair', opacity: 0, 'pointer-events': 'none' });
  const crossV = svg('line', {
    x1: 0, y1: pad.top, x2: 0, y2: pad.top + innerH,
    stroke: 'rgba(255, 255, 255, 0.55)', 'stroke-width': 1, 'stroke-dasharray': '4 3',
  });
  const crossH = svg('line', {
    x1: pad.left, y1: 0, x2: w - pad.right, y2: 0,
    stroke: 'rgba(255, 255, 255, 0.55)', 'stroke-width': 1, 'stroke-dasharray': '4 3',
  });
  const hoverDot = svg('circle', { r: 3.5, fill: 'rgba(255,255,255,0.95)', stroke: color, 'stroke-width': 1.2 });
  crossG.append(crossV, crossH, hoverDot);
  const xTimeG = svg('g', { class: 'session-xhover', opacity: 0, 'pointer-events': 'none' });
  const xTimeBg = svg('rect', { rx: 3, fill: 'rgba(0,0,0,0.9)' });
  const xTimeTx = svg('text', {
    y: h0 - 8, 'text-anchor': 'middle', fill: '#fff', 'font-size': 10, 'font-family': 'var(--mono)', 'font-weight': 500,
  });
  xTimeG.append(xTimeBg, xTimeTx);
  const hitSession = svg('rect', {
    x: pad.left, y: pad.top, width: innerW, height: innerH,
    fill: 'transparent', 'pointer-events': 'all', style: 'cursor: crosshair;',
  });
  root.append(crossG, xTimeG, hitSession);
  const tip = h('div', { class: 'chart-tooltip chart-tooltip--yahoosession hidden' });
  const findSessionBar = (mx) => {
    let j = 0;
    let best = Infinity;
    for (let k = 0; k < n; k++) {
      const d0 = Math.abs(xs[k] - mx);
      if (d0 < best) {
        best = d0;
        j = k;
      }
    }
    return j;
  };
  const fmtBarVol = (v) => {
    if (v == null || v === undefined) return '—';
    if (typeof v === 'number' && (!Number.isFinite(v) || v < 0)) return '—';
    return Math.round(Number(v)).toLocaleString('en-US');
  };
  const hideSessionHover = () => {
    crossG.setAttribute('opacity', 0);
    xTimeG.setAttribute('opacity', 0);
    tip.classList.add('hidden');
  };
  const onSessionMove = (e) => {
    const rect = root.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / Math.max(1e-9, rect.width)) * w;
    if (mx < pad.left - 0.5 || mx > w - pad.right + 0.5) {
      hideSessionHover();
      return;
    }
    const j = findSessionBar(mx);
    const bar = sorted[j];
    const xi = xs[j];
    const yC = yAt(bar.c);
    crossG.setAttribute('opacity', 1);
    crossV.setAttribute('x1', xi); crossV.setAttribute('x2', xi);
    crossH.setAttribute('y1', yC); crossH.setAttribute('y2', yC);
    hoverDot.setAttribute('cx', xi); hoverDot.setAttribute('cy', yC);
    const tStr = _fmtYahooSessionHoverET(bar.t);
    const tw = Math.max(52, tStr.length * 5.8);
    xTimeBg.setAttribute('x', xi - tw / 2 - 4);
    xTimeBg.setAttribute('y', h0 - 22);
    xTimeBg.setAttribute('width', tw + 8);
    xTimeBg.setAttribute('height', 16);
    xTimeTx.setAttribute('x', xi);
    xTimeTx.setAttribute('y', h0 - 9);
    xTimeTx.textContent = tStr;
    xTimeG.setAttribute('opacity', 1);
    const volStr = fmtBarVol(bar.v);
    tip.classList.remove('hidden');
    tip.innerHTML =
      `<div class="tt-row"><span>Date</span><b>${tStr}</b></div>` +
      '<div class="tt-sep"></div>' +
      `<div class="tt-row"><span>Close</span><b>${fmtUSD(bar.c)}</b></div>` +
      `<div class="tt-row"><span>Open</span><b>${fmtUSD(bar.o)}</b></div>` +
      `<div class="tt-row"><span>High</span><b>${fmtUSD(bar.h)}</b></div>` +
      `<div class="tt-row"><span>Low</span><b>${fmtUSD(bar.l)}</b></div>` +
      '<div class="tt-sep"></div>' +
      `<div class="tt-row"><span>Volume</span><b>${volStr}</b></div>`;
    const pane = root.parentElement;
    if (!pane) return;
    const pr = pane.getBoundingClientRect();
    const tipW = 220;
    const lx = e.clientX - pr.left;
    const ly = e.clientY - pr.top;
    tip.style.left = `${Math.min(pr.width - tipW - 8, Math.max(8, lx + 14))}px`;
    tip.style.top = `${Math.max(8, Math.min(pr.height - 120, ly - 110))}px`;
  };
  hitSession.addEventListener('mousemove', onSessionMove);
  hitSession.addEventListener('mouseleave', hideSessionHover);

  const showOverlaysLeg = showOverlays;
  const legendRow = h(
    'div',
    { class: 'price-chart-legend-row mono' },
    legendSwatch(color, `Close / ${String(view).charAt(0).toUpperCase() + String(view).slice(1)} (1D · ET session)`),
    ...(rthOpen != null
      ? [legendSwatch('rgba(210, 215, 225, 0.95)', '9:30 ET open (RTH)', false)]
      : []),
    ...(showOverlaysLeg
      ? [
        legendSwatch('#f2d47a', `SMA${Math.min(5, n - 1)}`),
        legendSwatch('#d9a36b', `SMA${Math.min(10, n - 1)}`),
        legendSwatch('#8b9dd9', `SMA${Math.min(20, n - 1)}`),
        legendSwatch('#bf7af0', `EMA${Math.min(9, n - 1)}`, true),
        legendSwatch('var(--info)', `Bollinger (${Math.min(20, n - 1)},2σ)`, false, true),
      ]
      : [h('span', { class: 'muted' }, simpleMode ? 'Simple chart (indicators hidden)' : 'Indicators on Mountain / Line')]),
    h('span', { class: 'muted' }, 'Scroll to zoom · drag to pan · double-click to reset'),
  );

  const sessionZoomPane = h('div', { class: 'price-chart-canvas price-chart-canvas--session' }, root, tip);
  {
    const padLocal = pad;
    const innerWLocal = innerW;
    const zs0 = zs;
    const zspan0 = zspan;
    const toRatioAtClientX = (clientX, el) => {
      if (!el || !el.isConnected) return (zs0 + zspan0) / 2;
      const rect = el.getBoundingClientRect();
      const scale = rect.width > 0 ? w / rect.width : 1;
      const xSvg = (clientX - rect.left) * scale;
      const t = (xSvg - padLocal.left) / innerWLocal;
      return zs0 + Math.max(0, Math.min(1, t)) * zspan0;
    };
    const onWheel = (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 0.9 : 1.1;
      let newSpan = zspan0 * factor;
      newSpan = Math.max(SESSION_1D_MIN_ZOOM, Math.min(1, newSpan));
      if (Math.abs(newSpan - zspan0) < 1e-12) return;
      const rF = toRatioAtClientX(e.clientX, e.currentTarget);
      const t0 = (rF - zs0) / zspan0;
      let nz = rF - t0 * newSpan;
      let nze = nz + newSpan;
      if (nz < 0) {
        nze -= nz;
        nz = 0;
      }
      if (nze > 1) {
        nz -= nze - 1;
        nze = 1;
      }
      if (nz < 0) nz = 0;
      zWin.start = nz;
      zWin.end = nze;
      reapplySess();
    };
    const clampWin = (a, b) => {
      let zsa = a;
      let zea = b;
      if (zsa < 0) {
        zea -= zsa;
        zsa = 0;
      }
      if (zea > 1) {
        zsa -= zea - 1;
        zea = 1;
      }
      if (zsa < 0) zsa = 0;
      return { start: zsa, end: zea };
    };
    const onDown = (e) => {
      if (e.button !== 0) return;
      const pane = e.currentTarget;
      const plotW = pane.getBoundingClientRect().width;
      pane.classList.add('session-chart--panning');
      let lastClientX = e.clientX;
      const onMove = (ev) => {
        if (plotW < 1) return;
        const dx = ev.clientX - lastClientX;
        lastClientX = ev.clientX;
        const dxU = (dx / plotW) * w;
        const w0 = zWin;
        const sp = Math.max(1e-9, w0.end - w0.start);
        const dr = (dxU / innerWLocal) * sp;
        const next = clampWin(w0.start - dr, w0.end - dr);
        zWin.start = next.start;
        zWin.end = next.end;
        reapplySess();
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        if (pane.isConnected) pane.classList.remove('session-chart--panning');
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    };
    const onDbl = (e) => {
      e.preventDefault();
      zWin.start = 0;
      zWin.end = 1;
      reapplySess();
    };
    sessionZoomPane.addEventListener('wheel', onWheel, { passive: false });
    sessionZoomPane.addEventListener('mousedown', onDown);
    sessionZoomPane.addEventListener('dblclick', onDbl);
  }
  return h('div', { class: 'price-chart-figure' + (enlarged ? ' price-chart-figure--wide' : '') },
    sessionZoomPane, legendRow);
}

/**
 * OHLC / close chart: mountain & line use overlays on close; candle & bar are raw OHLC.
 */
function tickerOhlcChart(bars, view, direction, opts = {}) {
  const xGran = opts.xGranularity || 'day';
  if (xGran === 'session' && Array.isArray(bars) && bars.length >= 2) {
    return tickerOhlcSessionChart(bars, view, direction, opts);
  }
  const w = opts.width != null ? opts.width : 1000;
  const h0 = opts.height != null ? opts.height : 300;
  const yahooLayout = opts.yahooLayout !== false;
  const enlarged = !!opts.enlarged;
  const pad = yahooLayout
    ? { top: 16, right: 54, bottom: xGran === 'hour' ? 44 : 36, left: 10 }
    : { top: 18, right: 18, bottom: 28, left: 58 };
  const innerW = w - pad.left - pad.right;
  const innerH = h0 - pad.top - pad.bottom;
  const n = bars.length;
  if (n < 2) {
    return h('div', { class: 'chart-placeholder' }, 'Not enough bars for this range');
  }

  const series = bars.map((b) => b.c);
  const slotW = innerW / n;
  const xC = (i) => pad.left + (i + 0.5) * slotW;

  let min = Infinity;
  let max = -Infinity;
  for (const b of bars) {
    min = Math.min(min, b.l, b.h, b.o, b.c);
    max = Math.max(max, b.l, b.h, b.o, b.c);
  }

  const simpleMode = !!opts.simpleMode;
  const showOverlays = !simpleMode && (view === 'mountain' || view === 'line');
  const sma5 = showOverlays ? _sma(series, Math.min(5, n - 1)) : [];
  const sma10 = showOverlays ? _sma(series, Math.min(10, n - 1)) : [];
  const sma20 = showOverlays ? _sma(series, Math.min(20, n - 1)) : [];
  const ema9 = showOverlays ? _ema(series, Math.min(9, n - 1)) : [];
  const bandWin = Math.min(20, n - 1);
  const bb = showOverlays ? _bollinger(series, bandWin, 2) : { mid: [], upper: [], lower: [] };

  const all = [min, max];
  if (showOverlays) {
    for (const v of [...series, ...sma5, ...sma10, ...sma20, ...ema9, ...bb.upper, ...bb.lower]) {
      if (v != null) all.push(v);
    }
  } else {
    for (const b of bars) {
      all.push(b.o, b.h, b.l, b.c);
    }
  }
  const dmin = Math.min(...all);
  const dmax = Math.max(...all);
  const pad_y = (dmax - dmin) * 0.06 || 1;
  const lo = dmin - pad_y;
  const hi = dmax + pad_y;
  const yrange = hi - lo;

  const yAt = (v) => pad.top + innerH - ((v - lo) / yrange) * innerH;
  const color =
    direction === 'bull' ? 'var(--success)' :
    direction === 'bear' ? 'var(--danger)'  : 'var(--info)';

  const xy = (v, i) => [xC(i), yAt(v)];

  const toPath = (ser) => {
    let path = '';
    let started = false;
    for (let i = 0; i < ser.length; i++) {
      const v = ser[i];
      if (v == null) { started = false; continue; }
      const [x, y0] = xy(v, i);
      path += (started ? 'L' : 'M') + x.toFixed(2) + ',' + y0.toFixed(2) + ' ';
      started = true;
    }
    return path.trim();
  };

  const root = svg('svg', {
    class: 'fancy-chart' + (yahooLayout ? ' fancy-chart--yahoo' : '') + (enlarged ? ' fancy-chart--enlarged' : ''),
    viewBox: `0 0 ${w} ${h0}`,
    preserveAspectRatio: 'xMidYMid meet',
    style: `height: ${h0}px; width: 100%; max-width: 100%; min-width: 0; display: block;`,
  });

  const defs = svg('defs');
  const gradId = 'ohlc-grad-' + Math.random().toString(36).slice(2, 8);
  const grad = svg('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 });
  const gOp0 = yahooLayout ? 0.48 : 0.35;
  const gOp1 = yahooLayout ? 0.04 : 0;
  grad.append(
    svg('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': gOp0 }),
    svg('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': gOp1 }),
  );
  defs.append(grad);
  root.append(defs);

  if (yahooLayout) {
    root.append(
      svg('rect', {
        x: pad.left,
        y: pad.top,
        width: innerW,
        height: innerH,
        fill: 'rgba(8, 10, 14, 0.55)',
        stroke: 'var(--stroke-soft)',
        'stroke-width': 1,
        rx: 2,
      }),
    );
  }

  const yTxt = (xPos, anc, yVal, yPx) => svg('text', {
    x: xPos, y: yPx + 4, 'text-anchor': anc,
    fill: 'var(--text-dim)', 'font-size': 11, 'font-family': 'var(--mono)',
  }, '$' + yVal.toFixed(yVal > 1000 ? 0 : 2));

  for (let g = 0; g <= 4; g++) {
    const v = lo + (yrange * g) / 4;
    const y = yAt(v);
    root.append(
      svg('line', {
        x1: pad.left, y1: y, x2: w - pad.right, y2: y,
        stroke: 'var(--stroke-soft)', 'stroke-dasharray': yahooLayout ? '1 5' : '2 4',
        'stroke-opacity': yahooLayout ? 0.5 : 1,
      }),
    );
    if (yahooLayout) root.append(yTxt(w - 8, 'end', v, y));
  }

  // 1D hourly: one label per bar (≤16); if more intraday bars, sample 16 ticks; daily ranges: up to 8
  const nTicks = xGran === 'hour' ? Math.min(16, n) : Math.min(8, n);
  for (let t = 0; t < nTicks; t++) {
    let idx;
    if (nTicks <= 1) idx = 0;
    else if (xGran === 'hour' && n <= 16) idx = t;
    else if (xGran === 'hour') idx = Math.round((t * (n - 1)) / (nTicks - 1));
    else idx = Math.round(((n - 1) * t) / (nTicks - 1));
    const x = xC(idx);
    const bar = bars[idx];
    const lab = _fmtDetailAxisTime(bar.t, xGran);
    root.append(
      svg('text', {
        x,
        y: h0 - 6,
        'text-anchor': 'middle',
        fill: 'var(--text-dim)',
        'font-size': 10,
        'font-family': 'var(--mono)',
      }, lab),
    );
  }

  if (view === 'candle' || view === 'bar') {
    const bw = Math.max(2, slotW * 0.55);
    for (let i = 0; i < n; i++) {
      const b = bars[i];
      const cx = xC(i);
      const yH = yAt(b.h);
      const yL = yAt(b.l);
      const yO = yAt(b.o);
      const yC0 = yAt(b.c);
      const bull = b.c >= b.o;
      const cFill = bull ? 'var(--success)' : 'var(--danger)';
      if (view === 'candle') {
        root.append(
          svg('line', {
            x1: cx, y1: yH, x2: cx, y2: yL,
            stroke: cFill, 'stroke-width': 1.2, 'stroke-opacity': 0.9,
          }),
        );
        const topB = Math.min(yO, yC0);
        const botB = Math.max(yO, yC0);
        const hB = Math.max(1, botB - topB);
        root.append(
          svg('rect', {
            x: cx - bw / 2,
            y: topB,
            width: bw,
            height: hB,
            fill: cFill,
            'fill-opacity': 0.75,
            stroke: cFill,
            'stroke-width': 1,
            rx: 1,
          }),
        );
      } else {
        // OHLC "bar" — vertical high–low, ticks for open (left) and close (right)
        root.append(
          svg('line', {
            x1: cx, y1: yH, x2: cx, y2: yL,
            stroke: 'var(--text-muted)', 'stroke-width': 1.2,
          }),
        );
        const tick = bw * 0.45;
        root.append(
          svg('line', { x1: cx - tick, y1: yO, x2: cx, y2: yO, stroke: cFill, 'stroke-width': 1.4 }),
        );
        root.append(
          svg('line', { x1: cx, y1: yC0, x2: cx + tick, y2: yC0, stroke: cFill, 'stroke-width': 1.4 }),
        );
      }
    }
  } else {
    if (showOverlays) {
      let bandD = '';
      let startedBand = false;
      for (let i = 0; i < n; i++) {
        if (bb.upper[i] == null) { startedBand = false; continue; }
        const [x, yU] = xy(bb.upper[i], i);
        bandD += (startedBand ? 'L' : 'M') + x.toFixed(2) + ',' + yU.toFixed(2) + ' ';
        startedBand = true;
      }
      for (let i = n - 1; i >= 0; i--) {
        if (bb.lower[i] == null) continue;
        const [x, y0] = xy(bb.lower[i], i);
        bandD += 'L' + x.toFixed(2) + ',' + y0.toFixed(2) + ' ';
      }
      if (bandD) {
        bandD += 'Z';
        root.append(svg('path', {
          d: bandD,
          fill: 'var(--info)',
          'fill-opacity': 0.07,
          stroke: 'none',
        }));
        root.append(svg('path', {
          d: toPath(bb.upper),
          fill: 'none',
          stroke: 'var(--info)',
          'stroke-opacity': 0.5,
          'stroke-width': 1,
          'stroke-dasharray': '3 3',
        }));
        root.append(svg('path', {
          d: toPath(bb.lower),
          fill: 'none',
          stroke: 'var(--info)',
          'stroke-opacity': 0.5,
          'stroke-width': 1,
          'stroke-dasharray': '3 3',
        }));
      }
      const smA = yahooLayout ? 0.55 : 0.9;
      const sw = yahooLayout ? 1.0 : 1.2;
      root.append(svg('path', { d: toPath(sma20), fill: 'none', stroke: '#8b9dd9', 'stroke-width': sw, 'stroke-opacity': 0.75 * smA }));
      root.append(svg('path', { d: toPath(sma10), fill: 'none', stroke: '#d9a36b', 'stroke-width': sw, 'stroke-opacity': 0.8 * smA }));
      root.append(svg('path', { d: toPath(ema9), fill: 'none', stroke: '#bf7af0', 'stroke-width': sw, 'stroke-dasharray': '4 2', 'stroke-opacity': 0.8 * smA }));
      root.append(svg('path', { d: toPath(sma5), fill: 'none', stroke: '#f2d47a', 'stroke-width': sw, 'stroke-opacity': 0.8 * smA }));
    }

    const lineD = series.map((v, i) => {
      const [x, y0] = xy(v, i);
      return (i === 0 ? 'M' : 'L') + x.toFixed(2) + ',' + y0.toFixed(2);
    }).join(' ');

    if (view === 'mountain') {
      const [x0, y0] = xy(series[0], 0);
      const [x1, y1] = xy(series[n - 1], n - 1);
      const areaD = lineD +
        ' L' + x1.toFixed(2) + ',' + (pad.top + innerH) +
        ' L' + x0.toFixed(2) + ',' + (pad.top + innerH) + ' Z';
      root.append(svg('path', { d: areaD, fill: `url(#${gradId})`, stroke: 'none' }));
    }

    root.append(svg('path', {
      d: lineD,
      fill: 'none',
      stroke: color,
      'stroke-width': yahooLayout ? 2.2 : 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
  }

  const lastX = xC(n - 1);
  const lastY = yAt(bars[n - 1].c);
  root.append(
    svg('circle', { cx: lastX, cy: lastY, r: 5, fill: color, 'fill-opacity': 0.25 }),
    svg('circle', { cx: lastX, cy: lastY, r: 2.6, fill: color }),
  );

  const legendRow = h(
    'div',
    { class: 'price-chart-legend-row mono' },
    legendSwatch(color, `Close / ${String(view).charAt(0).toUpperCase() + String(view).slice(1)}`),
    ...(showOverlays
      ? [
        legendSwatch('#f2d47a', `SMA${Math.min(5, n - 1)}`),
        legendSwatch('#d9a36b', `SMA${Math.min(10, n - 1)}`),
        legendSwatch('#8b9dd9', `SMA${Math.min(20, n - 1)}`),
        legendSwatch('#bf7af0', `EMA${Math.min(9, n - 1)}`, true),
        legendSwatch('var(--info)', `Bollinger (${Math.min(20, n - 1)},2σ)`, false, true),
      ]
      : [h('span', { class: 'muted' }, simpleMode ? 'Simple chart (indicators hidden)' : 'Indicators on Mountain / Line only')]),
  );

  const canvas = h('div', { class: 'price-chart-canvas' }, root);
  const figure = h(
    'div',
    { class: 'price-chart-figure' + (enlarged ? ' price-chart-figure--wide' : '') },
    canvas,
    legendRow,
  );

  return figure;
}

function legendSwatch(color, label, dashed = false, band = false) {
  return h(
    'span',
    { class: 'legend-item' },
    h('span', {
      class: 'legend-swatch' + (dashed ? ' dashed' : '') + (band ? ' band' : ''),
      style: `background:${band ? 'transparent' : color};` +
        (band ? `border:1px dashed ${color};` : '') +
        (dashed && !band ? `background:repeating-linear-gradient(90deg,${color} 0 4px,transparent 4px 7px);` : ''),
    }),
    label
  );
}

// ---------- Factor bar chart ----------------------------------------------

function renderFactorChart(d) {
  const host = $('#factor-chart');
  host.innerHTML = '';
  host.append(factorBarsHtml(d.factors));
}

/** Tight layout: name | bar track (zero at centre) | score — avoids stretched SVG and label overlap. */
function factorBarsHtml(factors) {
  const wrap = h('div', { class: 'factor-bars factor-bars--html' });
  const maxMag = Math.max(1, ...factors.map((f) => Math.abs(f.score)));
  for (const f of factors) {
    const color =
      f.score > 0.15 ? 'var(--success)' :
      f.score < -0.15 ? 'var(--danger)'  :
      'var(--text-dim)';
    const t = Math.abs(f.score) / maxMag;
    const pct = Math.max(0, t * 50);
    const isPos = f.score >= 0;
    const val = (f.score >= 0 ? '+' : '') + f.score.toFixed(2);
    wrap.append(
      h('div', { class: 'factor-bars__row' },
        h('div', { class: 'factor-bars__name' }, f.name),
        h('div', { class: 'factor-bars__track' },
          h('div', { class: 'factor-bars__zero' }),
          h('div', {
            class: 'factor-bars__fill ' + (isPos ? 'is-pos' : 'is-neg'),
            style:
              (isPos
                ? `left:50%;width:${pct}%;background:${color};`
                : `right:50%;width:${pct}%;background:${color};`),
          }),
        ),
        h('div', { class: 'factor-bars__value mono' }, val),
      ),
    );
  }
  return wrap;
}

// ---------- Factor table ---------------------------------------------------

function renderFactorTable(d) {
  const tbody = $('#factors-tbody');
  tbody.innerHTML = '';
  for (const f of d.factors) {
    const tone = f.score > 0.15 ? 'success' : f.score < -0.15 ? 'danger' : 'neutral';
    tbody.append(
      h(
        'tr',
        {},
        h('td', {}, f.name),
        h(
          'td',
          { class: 'num' },
          h('span', { class: `pill-badge ${tone}` }, fmtScore(f.score))
        ),
        h('td', { class: 'name' }, f.note)
      )
    );
  }
}

// ---------- Strategy cards -------------------------------------------------

function renderRecommender(d) {
  const r = d.row;
  $('#recommender-context').textContent =
    `Direction ${r.direction} · IV ${(r.iv * 100).toFixed(1)}% · ` +
    `IV rank ${r.iv_rank.toFixed(0)} · ${r.dte_pref}d horizon` +
    (r.earnings_in_window ? ' · earnings in window' : '');

  const host = $('#strategy-grid');
  host.innerHTML = '';
  if (!d.recommendations.length) {
    host.append(
      h(
        'div',
        { class: 'callout' },
        h(
          'div',
          { class: 'callout-title' },
          'No high-conviction options plays for this context'
        ),
        'Consider sitting out until the direction / IV regime improves.'
      )
    );
    return;
  }

  d.recommendations.forEach((rec, idx) => {
    host.append(strategyCard(rec, idx === 0));
  });
}

function strategyCard(rec, isBest) {
  const m = rec.metrics;
  const credit = m.net_debit < 0;
  const amt = Math.abs(m.net_debit);

  const breakevens =
    m.breakevens.length
      ? m.breakevens.map((b) => b.toFixed(2)).join(' / ')
      : '—';

  return h(
    'div',
    { class: 'strategy-card' + (isBest ? ' fit-best' : '') },
    h(
      'div',
      { class: 'strategy-head' },
      h('div', { class: 'strategy-name' }, rec.strategy.name),
      h(
        'div',
        {},
        h(
          'span',
          { class: 'pill-badge ' + (isBest ? 'success' : 'neutral') },
          isBest ? 'Best fit' : 'Alt'
        )
      )
    ),
    h('div', { class: 'legs' }, rec.strategy.legs.map(legLine)),
    h(
      'div',
      { class: 'strategy-metrics' },
      metric(m.max_gain == null ? '∞' : fmtUSD(m.max_gain), 'Max gain', 'pos'),
      metric(m.max_loss == null ? '∞' : fmtUSD(m.max_loss), 'Max loss', 'neg'),
      metric(`${Math.round(m.probability_of_profit * 100)}%`, 'POP')
    ),
    h(
      'div',
      { class: 'breakeven-line' },
      'Break-even(s): ',
      h('strong', {}, breakevens),
      ' · ',
      credit ? 'net credit ' : 'net debit ',
      h('strong', {}, fmtUSD(amt))
    ),
    h('div', { class: 'rationale' }, rec.rationale),
    h('div', { class: 'tag-row' }, rec.tags.map((t) => h('span', { class: 'tag' }, t))),
    h(
      'div',
      { class: 'fit-bar' },
      h('div', {
        class: 'fit-bar-fill',
        style: `width: ${Math.max(4, rec.fit_score * 100)}%`,
      })
    )
  );
}

function legLine(leg) {
  const side = leg.side === 'long' ? 'BUY ' : 'SELL';
  const qty = leg.kind === 'stock' ? `${leg.quantity} sh` : `${leg.quantity / 100}x`;
  const strike = leg.kind === 'stock' ? 'shares' : `${leg.strike} ${leg.kind}`;
  const prem = leg.kind === 'stock'
    ? ` @ $${leg.premium.toFixed(2)}`
    : ` @ $${leg.premium.toFixed(2)}`;
  return h(
    'div',
    { class: 'leg' },
    h('span', { class: `leg-side ${leg.side}` }, side),
    `${qty}  ${strike}${prem}`
  );
}

function metric(value, label, tone = '') {
  return h(
    'div',
    { class: `metric ${tone}` },
    h('div', { class: 'metric-value' }, value),
    h('div', { class: 'metric-label' }, label)
  );
}

// ===========================================================================
// Analyst view
// ===========================================================================

const analyst = {
  initialized: false,
  tickers: [],
  activeSymbol: null,
  timeframe: 'daily',
  overview: [],
  _overviewReqId: 0,
  reports: {}, // key = sym|tf
  /** "SYMBOL|range" (client chart range) -> GET /api/ticker payload */
  tickerDetails: {},
  /** For resetting session zoom when switching symbols. */
  _priceChartSymbol: null,
  llm: { enabled: false, model: '' },
  polishCache: {}, // key = sym|tf -> polished narrative
  briefCache: {},  // key = tf -> brief text
};

const stocks = {
  initialized: false,
  tickers: [],
  activeSymbol: null,
  timeframe: 'daily',
  overview: [],
  reports: {},
  tickerDetails: {},
  _priceChartSymbol: null,
};

const etf = {
  initialized: false,
  timeframe: 'daily',
  overview: [],
};

async function initAnalystOnce() {
  if (analyst.initialized) return;
  analyst.initialized = true;
  initAnalystChartToolbar();

  const analystTfHost = document.querySelector('.view-analyst .analyst-timeframes');
  const analystTfPills = analystTfHost
    ? [...analystTfHost.querySelectorAll('.pill[data-tf]')]
    : [];

  analystTfPills.forEach((p) => {
    p.addEventListener('click', () => {
      if (analyst.timeframe === p.dataset.tf) return;
      analyst.timeframe = p.dataset.tf;
      analystTfPills.forEach((x) =>
        x.classList.toggle('is-active', x === p)
      );
      $('#overview-timeframe').textContent = p.dataset.tf;
      analyst.overview = [];
      loadAnalystOverview();
      if (analyst.llm.enabled) loadDailyBrief({ force: false });
      if (analyst.activeSymbol) loadAnalystReport(analyst.activeSymbol);
    });
  });

  // Probe LLM availability — shows Claude UI elements only when enabled.
  try {
    const cfg = await api('/api/analyst/llm-config');
    analyst.llm = { enabled: !!cfg.enabled, model: cfg.model || '' };
    if (analyst.llm.enabled) {
      $('#llm-status-chip').classList.remove('hidden');
      $('#llm-model').textContent = analyst.llm.model;
      $('#brief-card').classList.remove('hidden');
      const refresh = $('#brief-refresh');
      if (refresh) refresh.addEventListener('click', () => loadDailyBrief({ force: true }));
    }
  } catch (e) {
    analyst.llm = { enabled: false, model: '' };
  }

  try {
    analyst.tickers = await api('/api/analyst/tickers');
  } catch (e) {
    $('#analyst-ticker-strip').innerHTML =
      `<span class="ticker-strip-loading muted">Failed to load tickers: ${e}</span>`;
    return;
  }
  renderTickerStrip();
  loadAnalystOverview();
  if (analyst.llm.enabled) loadDailyBrief({ force: false });

  const first = analyst.tickers[0];
  if (first) loadAnalystReport(first.symbol);
}

async function initStocksOnce() {
  if (stocks.initialized) return;
  stocks.initialized = true;
  initStocksChartToolbar();

  $$('.stocks-timeframes .pill').forEach((p) => {
    p.addEventListener('click', () => {
      const tf = p.getAttribute('data-stocks-tf');
      if (!tf || stocks.timeframe === tf) return;
      stocks.timeframe = tf;
      $$('.stocks-timeframes .pill').forEach((x) =>
        x.classList.toggle('is-active', x === p)
      );
      const ot = $('#stocks-overview-timeframe');
      if (ot) ot.textContent = tf;
      stocks.overview = [];
      stocks.reports = {};
      loadStocksOverview();
      if (stocks.activeSymbol) loadStocksReport(stocks.activeSymbol);
    });
  });

  try {
    stocks.tickers = await api('/api/analyst/tickers');
  } catch (e) {
    const ts = $('#stocks-ticker-strip');
    if (ts) ts.innerHTML = `<span class="ticker-strip-loading muted">Failed to load tickers: ${e}</span>`;
    return;
  }
  renderStocksTickerStrip();
  loadStocksOverview();
  const first = stocks.tickers[0];
  if (first) loadStocksReport(first.symbol);
}

function renderTickerStrip() {
  const host = $('#analyst-ticker-strip');
  host.innerHTML = '';
  for (const t of analyst.tickers) {
    const ov = analyst.overview.find((r) => r.symbol === t.symbol);
    const verdict = ov ? ov.verdict : null;
    const cls =
      verdict === 'BULLISH' ? 'verdict-bullish' :
      verdict === 'BEARISH' ? 'verdict-bearish' :
      verdict === 'NEUTRAL' ? 'verdict-neutral' : '';
    const chip = h(
      'span',
      {
        class:
          'ticker-chip ' + cls +
          (analyst.activeSymbol === t.symbol ? ' is-active' : ''),
        onClick: () => loadAnalystReport(t.symbol),
        title: t.name,
      },
      h('span', { class: 'chip-sym' }, t.symbol),
      verdict
        ? h('span', { class: 'chip-verdict' }, verdict.slice(0, 4))
        : null
    );
    host.append(chip);
  }
}

function _setAnalystOverviewLoading(msg, { keepTable = false } = {}) {
  const list = $('#overview-list');
  const allRec = $('#all-recs-tbody');
  if (list) {
    list.innerHTML = `<div class="muted" style="padding:10px 12px;">${msg}</div>`;
  }
  if (allRec && !keepTable) {
    allRec.innerHTML = `<tr><td colspan="9" class="loading">${msg}</td></tr>`;
  }
}

async function loadAnalystOverview(opts = {}) {
  const { silent = false, pollBackground = false } = opts;
  const list = $('#overview-list');
  const allRec = $('#all-recs-tbody');
  const reqId = ++analyst._overviewReqId;
  const tf = analyst.timeframe;
  const hadRows = analyst.overview.length > 0;

  if (!hadRows) {
    _setAnalystOverviewLoading('Computing all recommendations…');
  }

  const fetchOverview = (fresh = false) =>
    api(
      `/api/analyst/overview?timeframe=${encodeURIComponent(tf)}${
        fresh ? '&fresh=1' : ''
      }`,
    );

  try {
    let rows = await fetchOverview(false);
    if (!rows.length) {
      _setAnalystOverviewLoading('Overview still computing — retrying…');
      await new Promise((r) => setTimeout(r, 2500));
      if (reqId !== analyst._overviewReqId || tf !== analyst.timeframe) return;
      rows = await fetchOverview(false);
    }
    if (reqId !== analyst._overviewReqId || tf !== analyst.timeframe) return;

    analyst.overview = rows;
    renderOverviewList();
    renderAllRecsTable();
    renderTickerStrip();
    const srcEl = $('#analyst-source');
    if (srcEl) {
      srcEl.textContent = rows.find((r) => r.source)?.source || '—';
    }
    if (!rows.length) {
      _setAnalystOverviewLoading(
        'No overview data yet — server may still be warming up. Click Refresh or wait a moment.',
      );
    } else if (pollBackground && !silent) {
      // Server returns warm cache immediately; pick up background rebuild shortly.
      window.setTimeout(() => {
        if (reqId === analyst._overviewReqId && tf === analyst.timeframe) {
          loadAnalystOverview({ silent: true });
        }
      }, 35000);
    }
  } catch (e) {
    if (reqId !== analyst._overviewReqId || tf !== analyst.timeframe) return;
    const err = escapeHtml(String(e));
    if (list) list.innerHTML = `<div class="muted" style="padding:10px 12px;">Failed: ${err}</div>`;
    if (allRec) allRec.innerHTML = `<tr><td colspan="9" class="loading">Failed: ${err}</td></tr>`;
  }
}

function renderAllRecsTable() {
  const tbody = $('#all-recs-tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  if (!analyst.overview.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="loading">No data.</td></tr>';
    return;
  }

  // Sort: calls first, then puts, each by conviction desc.
  const sorted = analyst.overview.slice().sort((a, b) => {
    if (a.rec_contract_type !== b.rec_contract_type) {
      return a.rec_contract_type === 'call' ? -1 : 1;
    }
    return b.conviction - a.conviction;
  });

  for (const r of sorted) {
    const recCls = r.rec_contract_type === 'call' ? 'rec-call' : 'rec-put';
    const recLabel = r.rec_contract_type === 'call' ? 'CALL' : 'PUT';
    const vcls = r.verdict === 'BULLISH' ? 'success'
              : r.verdict === 'BEARISH' ? 'danger' : 'neutral';
    tbody.append(
      h('tr',
        {
          class: 'clickable',
          onClick: () => {
            loadAnalystReport(r.symbol);
            // Scroll the main report into view.
            const el = $('#analyst-report');
            if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
          },
        },
        h('td', { class: 'sym mono' }, r.symbol),
        h('td', {}, h('span', { class: `pill-badge ${vcls}` }, r.verdict)),
        h('td', {}, h('span', { class: `rec-pill ${recCls} mono` }, recLabel)),
        h('td', { class: 'num mono' }, `$${fmtStrike(r.rec_strike)}`),
        h('td', { class: 'num mono' }, fmtExpiry(r.rec_expiry_date, r.rec_expiry_dte)),
        h('td', { class: 'num mono' }, r.rec_cost_per_contract != null ? fmtUSD(r.rec_cost_per_contract) : '—'),
        h('td', { class: 'num mono' }, r.rec_break_even != null ? `$${r.rec_break_even.toFixed(2)}` : '—'),
        h('td', { class: 'num mono' }, r.rec_target != null ? `$${r.rec_target.toFixed(2)}` : '—'),
        h('td', { class: 'num mono' }, r.rec_risk_reward != null ? `${r.rec_risk_reward.toFixed(2)}×` : '—'),
      )
    );
  }
}

function renderOverviewList() {
  const list = $('#overview-list');
  list.innerHTML = '';
  const sorted = analyst.overview.slice().sort((a, b) => {
    const vOrd = { BULLISH: 0, BEARISH: 1, NEUTRAL: 2 };
    if (vOrd[a.verdict] !== vOrd[b.verdict]) return vOrd[a.verdict] - vOrd[b.verdict];
    return b.conviction - a.conviction;
  });
  for (const r of sorted) {
    const vcls =
      r.verdict === 'BULLISH' ? 'v-bullish' :
      r.verdict === 'BEARISH' ? 'v-bearish' : 'v-neutral';
    const recCls = r.rec_contract_type === 'call' ? 'rec-call' : 'rec-put';
    const recLabel = r.rec_contract_type === 'call' ? 'CALL' : 'PUT';
    list.append(
      h(
        'div',
        {
          class: 'overview-row ' + vcls +
            (r.symbol === analyst.activeSymbol ? ' is-active' : ''),
          onClick: () => loadAnalystReport(r.symbol),
        },
        h('div', { class: 'ov-top' },
          h('span', { class: 'sym' },
            h('span', { class: 'verdict-dot' }),
            r.symbol
          ),
          h('span', { class: `rec-pill ${recCls} mono` },
            `${recLabel} $${fmtStrike(r.rec_strike)}`
          ),
          h('span', { class: 'rsi mono' },
            r.rsi != null ? `rsi ${r.rsi.toFixed(0)}` : '—'
          )
        ),
        h('div', { class: 'ov-bot mono muted' },
          r.rec_cost_per_contract != null ? fmtUSD(r.rec_cost_per_contract) : '—',
          ' · BE ',
          r.rec_break_even != null ? `$${r.rec_break_even.toFixed(2)}` : '—',
          ' · RR ',
          r.rec_risk_reward != null ? `${r.rec_risk_reward.toFixed(2)}×` : '—'
        )
      )
    );
  }
}

function fmtStrike(k) {
  if (k == null) return '—';
  return k >= 100 || Math.abs(k - Math.round(k)) < 1e-6
    ? k.toFixed(0)
    : k.toFixed(2);
}

async function initEtfOnce() {
  if (etf.initialized) return;
  etf.initialized = true;

  $$('.etf-timeframes .pill').forEach((p) => {
    p.addEventListener('click', () => {
      if (etf.timeframe === p.dataset.tf) return;
      etf.timeframe = p.dataset.tf;
      $$('.etf-timeframes .pill').forEach((x) =>
        x.classList.toggle('is-active', x === p)
      );
      const lbl = $('#etf-timeframe-label');
      if (lbl) lbl.textContent = p.dataset.tf;
      etf.overview = [];
      loadEtfOverview();
    });
  });
  loadEtfOverview();
}

async function loadEtfOverview() {
  const tbody = $('#etf-signals-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="10" class="loading">Loading ETF signals…</td></tr>';
  try {
    const rows = await api(
      `/api/etf/signals?timeframe=${encodeURIComponent(etf.timeframe)}`
    );
    etf.overview = rows;
    renderEtfTable();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="10" class="loading">Failed: ${e}</td></tr>`;
  }
}

function renderEtfTable() {
  const tbody = $('#etf-signals-tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  if (!etf.overview.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="loading">No data.</td></tr>';
    return;
  }
  const sorted = etf.overview.slice().sort((a, b) => {
    const vOrd = { BULLISH: 0, BEARISH: 1, NEUTRAL: 2 };
    if (vOrd[a.verdict] !== vOrd[b.verdict]) return vOrd[a.verdict] - vOrd[b.verdict];
    return b.conviction - a.conviction;
  });
  for (const r of sorted) {
    const vcls =
      r.verdict === 'BULLISH' ? 'success' :
      r.verdict === 'BEARISH' ? 'danger' : 'neutral';
    const recCls = r.rec_contract_type === 'call' ? 'rec-call' : 'rec-put';
    const recLabel = r.rec_contract_type === 'call' ? 'CALL' : 'PUT';
    tbody.append(
      h(
        'tr',
        {
          class: 'clickable',
          onClick: () => switchView('detail', { symbol: r.symbol }),
        },
        h('td', { class: 'sym mono' }, r.symbol),
        h('td', { class: 'name' }, r.name),
        h('td', { class: 'num mono' }, fmtUSD(r.last)),
        h('td', { class: `num ${r.change_pct >= 0 ? 'pos' : 'neg'}` }, fmtPct(r.change_pct)),
        h('td', {}, h('span', { class: `pill-badge ${vcls}` }, r.verdict)),
        h('td', { class: 'num mono' }, fmtScore(r.composite_score)),
        h('td', { class: 'num mono' }, `${Math.round(r.conviction * 100)}%`),
        h('td', { class: 'num mono' }, r.rsi != null ? r.rsi.toFixed(0) : '—'),
        h('td', { class: 'mono muted' }, r.trend),
        h(
          'td',
          { class: 'mono' },
          h('span', { class: `rec-pill ${recCls}` },
            `${recLabel} $${fmtStrike(r.rec_strike)}`)
        )
      )
    );
  }
}

async function loadAnalystReport(symbol) {
  if (analyst._priceChartSymbol != null && analyst._priceChartSymbol !== symbol) {
    state.analystChartSessionZoom = { start: 0, end: 1 };
  }
  analyst._priceChartSymbol = symbol;
  analyst.activeSymbol = symbol;
  renderTickerStrip();
  renderOverviewList();

  const requestedTimeframe = analyst.timeframe;
  const key = `${symbol}|${requestedTimeframe}`;
  // Evict options-flow data after 5 min so chain data stays fresh between tickers
  const _rptCached = analyst.reports[key];
  if (_rptCached && _rptCached._cachedAt && (Date.now() - _rptCached._cachedAt) > 5 * 60_000) {
    delete analyst.reports[key];
  }
  let rpt = analyst.reports[key];
  if (!rpt) {
    $('#analyst-report').innerHTML =
      `<div class="callout"><div class="callout-title">Loading ${symbol} · ${requestedTimeframe}</div>` +
      `Computing indicators and composing forecast…</div>`;
    try {
      rpt = await api(
        `/api/analyst/report/${encodeURIComponent(symbol)}` +
        `?timeframe=${encodeURIComponent(requestedTimeframe)}`,
      );
      rpt._cachedAt = Date.now();
      analyst.reports[key] = rpt;
    } catch (e) {
      if (analyst.activeSymbol === symbol && analyst.timeframe === requestedTimeframe) {
        $('#analyst-report').innerHTML =
          `<div class="callout callout-warning"><div class="callout-title">Failed to load ${symbol}</div>${e}</div>`;
      }
      return;
    }
  }
  let tickerD = null;
  try {
    tickerD = await loadAnalystTickerDetail(symbol);
  } catch (e) {
    tickerD = null;
  }
  
  if (analyst.activeSymbol !== symbol || analyst.timeframe !== requestedTimeframe) return; // Prevent race conditions
  
  renderAnalystReport(rpt, tickerD);
  syncAnalystChartToolbar();
}

function renderStocksTickerStrip() {
  const host = $('#stocks-ticker-strip');
  if (!host) return;
  host.innerHTML = '';
  for (const t of stocks.tickers) {
    const ov = stocks.overview.find((r) => r.symbol === t.symbol);
    const verdict = ov ? ov.verdict : null;
    const cls =
      verdict === 'BULLISH' ? 'verdict-bullish' :
        verdict === 'BEARISH' ? 'verdict-bearish' :
          verdict === 'NEUTRAL' ? 'verdict-neutral' : '';
    host.append(
      h(
        'span',
        {
          class: 'ticker-chip ' + cls + (stocks.activeSymbol === t.symbol ? ' is-active' : ''),
          onClick: () => loadStocksReport(t.symbol),
          title: t.name,
        },
        h('span', { class: 'chip-sym' }, t.symbol),
        verdict ? h('span', { class: 'chip-verdict' }, verdict.slice(0, 4)) : null,
      ),
    );
  }
}

async function loadStocksOverview() {
  const list = $('#stocks-overview-list');
  if (!list) return;
  list.innerHTML = '<div class="muted" style="padding:10px 12px;">Loading…</div>';
  try {
    const rows = await api(
      `/api/analyst/overview?timeframe=${encodeURIComponent(stocks.timeframe)}`
    );
    stocks.overview = rows;
    renderStocksOverviewList();
    renderStocksTickerStrip();
    if (!stocks.activeSymbol) renderStocksAllPicksSummary();
    const srcEl = $('#stocks-source');
    if (srcEl) srcEl.textContent = rows.find((r) => r.source)?.source || '—';
  } catch (e) {
    list.innerHTML = `<div class="muted" style="padding:10px 12px;">Failed: ${e}</div>`;
  }
}

function renderStocksAllPicksSummary() {
  const host = $('#stocks-report');
  if (!host) return;
  const rows = stocks.overview || [];
  host.innerHTML = '';
  if (!rows.length) {
    host.append(
      h('div', { class: 'callout' },
        h('div', { class: 'callout-title' }, 'Loading stock picks…'),
        'Fetching latest stock swing ideas for this timeframe.'
      )
    );
    return;
  }

  const sorted = rows.slice().sort((a, b) => {
    const vOrd = { BULLISH: 0, BEARISH: 1, NEUTRAL: 2 };
    if (vOrd[a.verdict] !== vOrd[b.verdict]) return vOrd[a.verdict] - vOrd[b.verdict];
    return b.conviction - a.conviction;
  });
  const top = sorted.slice(0, 12);

  const tbody = h('tbody', {});
  for (const r of top) {
    const vcls =
      r.verdict === 'BULLISH' ? 'success' :
        r.verdict === 'BEARISH' ? 'danger' : 'neutral';
    const rowCls =
      r.verdict === 'BULLISH' ? 'row-buy' :
        r.verdict === 'BEARISH' ? 'row-sell' : '';
    const recType = r.rec_contract_type === 'put' ? 'PUT ref' : 'CALL ref';
    const tp = r.rec_target != null ? fmtUSD(r.rec_target) : '—';
    const st = r.rec_stop != null ? fmtUSD(r.rec_stop) : '—';
    const conv = `${Math.round((r.conviction || 0) * 100)}%`;
    tbody.append(
      h(
        'tr',
        {
          class: rowCls,
          onClick: () => loadStocksReport(r.symbol),
          style: 'cursor:pointer;',
          title: `Open ${r.symbol} swing report`,
        },
        h('td', { class: 'mono' }, r.symbol),
        h('td', {}, h('span', { class: `pill-badge ${vcls}` }, r.verdict)),
        h('td', { class: 'mono' }, recType),
        h('td', { class: 'num mono' }, tp),
        h('td', { class: 'num mono' }, st),
        h('td', { class: 'num' }, h('span', { class: `pill-badge ${vcls} mono` }, conv)),
      )
    );
  }

  host.append(
    h('div', { class: 'top-picks-header' },
      h('div', { class: 'top-picks-title' }, 'Stock Picks'),
      h('div', { class: 'top-picks-note' }, 'Pick any row to open the full swing stock report with chart, levels, and rationale.')
    ),
    h(
      'div',
      { class: 'table-wrap' },
      h(
        'table',
        { class: 'data-table' },
        h('thead', {},
          h('tr', {},
            h('th', {}, 'Symbol'),
            h('th', {}, 'Verdict'),
            h('th', {}, 'Setup'),
            h('th', { class: 'num' }, 'Target'),
            h('th', { class: 'num' }, 'Stop'),
            h('th', { class: 'num' }, 'Conviction'),
          )
        ),
        tbody
      )
    )
  );
}

function renderStocksOverviewList() {
  const list = $('#stocks-overview-list');
  if (!list) return;
  list.innerHTML = '';
  const sorted = stocks.overview.slice().sort((a, b) => {
    const vOrd = { BULLISH: 0, BEARISH: 1, NEUTRAL: 2 };
    if (vOrd[a.verdict] !== vOrd[b.verdict]) return vOrd[a.verdict] - vOrd[b.verdict];
    return b.conviction - a.conviction;
  });
  for (const r of sorted) {
    const vcls =
      r.verdict === 'BULLISH' ? 'v-bullish' :
        r.verdict === 'BEARISH' ? 'v-bearish' : 'v-neutral';
    const recCls = r.rec_contract_type === 'call' ? 'rec-call' : 'rec-put';
    const recLabel = r.rec_contract_type === 'call' ? 'CALL' : 'PUT';
    list.append(
      h(
        'div',
        {
          class: 'overview-row ' + vcls + (r.symbol === stocks.activeSymbol ? ' is-active' : ''),
          onClick: () => loadStocksReport(r.symbol),
        },
        h(
          'div',
          { class: 'ov-top' },
          h('span', { class: 'sym' }, h('span', { class: 'verdict-dot' }), r.symbol),
          h('span', { class: `rec-pill ${recCls} mono` }, `${recLabel} ref`),
          h('span', { class: 'rsi mono' }, r.rsi != null ? `rsi ${r.rsi.toFixed(0)}` : '—'),
        ),
        h(
          'div',
          { class: 'ov-bot mono muted' },
          'TP ',
          r.rec_target != null ? `$${r.rec_target.toFixed(2)}` : '—',
          ' · ST ',
          r.rec_stop != null ? `$${r.rec_stop.toFixed(2)}` : '—',
        ),
      ),
    );
  }
}

async function loadStocksReport(symbol) {
  if (stocks._priceChartSymbol != null && stocks._priceChartSymbol !== symbol) {
    state.stocksChartSessionZoom = { start: 0, end: 1 };
  }
  stocks._priceChartSymbol = symbol;
  stocks.activeSymbol = symbol;
  renderStocksTickerStrip();
  renderStocksOverviewList();

  const requestedTimeframe = stocks.timeframe;
  const key = `${symbol}|${requestedTimeframe}`;
  const _rptCached = stocks.reports[key];
  if (_rptCached && _rptCached._cachedAt && (Date.now() - _rptCached._cachedAt) > 5 * 60_000) {
    delete stocks.reports[key];
  }
  let rpt = stocks.reports[key];
  const sr = $('#stocks-report');
  if (!rpt) {
    if (sr) {
      sr.innerHTML =
        `<div class="callout"><div class="callout-title">Loading ${symbol} · ${requestedTimeframe}</div>` +
        `Computing indicators and swing plan…</div>`;
    }
    try {
      rpt = await api(
        `/api/analyst/report/${encodeURIComponent(symbol)}` +
          `?timeframe=${encodeURIComponent(requestedTimeframe)}&fresh_quotes=1`,
      );
      rpt._cachedAt = Date.now();
      stocks.reports[key] = rpt;
    } catch (e) {
      if (sr && stocks.activeSymbol === symbol && stocks.timeframe === requestedTimeframe) {
        sr.innerHTML =
          `<div class="callout callout-warning"><div class="callout-title">Failed to load ${symbol}</div>${e}</div>`;
      }
      return;
    }
  }
  let tickerD = null;
  try {
    tickerD = await loadStocksTickerDetail(symbol);
  } catch (e) {
    tickerD = null;
  }
  
  if (stocks.activeSymbol !== symbol || stocks.timeframe !== requestedTimeframe) return; // Prevent race conditions
  
  renderStockReport(rpt, tickerD);
  syncStocksChartToolbar();
}

function factorTone(score) {
  if (score >= 0.25) return 'pos';
  if (score <= -0.25) return 'neg';
  return '';
}

/** API `verdict_factors` — why the model leans bull/bear (ticker detail parity). */
function renderVerdictFactors(factors) {
  if (!factors || !factors.length) return null;
  return h(
    'div',
    { class: 'verdict-factors', role: 'region', 'aria-label': 'Factors behind the verdict' },
    h('div', { class: 'verdict-factors-title muted' }, 'What’s driving the bias'),
    h(
      'ul',
      { class: 'verdict-factors-list' },
      ...factors.map((f) =>
        h(
          'li',
          { class: 'verdict-factor' },
          h('span', { class: 'vf-name' }, f.name),
          h('span', { class: `vf-score mono ${factorTone(f.score)}` }, fmtScore(f.score)),
          h('span', { class: 'vf-note muted' }, f.note),
        )
      ),
    ),
  );
}

function renderAnalystSurface(r, tickerD, surface = 'analyst') {
  const root = surface === 'stocks' ? $('#stocks-report') : $('#analyst-report');
  root.innerHTML = '';
  const rangeKey = surface === 'stocks' ? 'data-stocks-range' : 'data-analyst-range';
  const viewKey = surface === 'stocks' ? 'data-stocks-view' : 'data-analyst-view';
  const chState = surface === 'stocks' ? state.stocksChart : state.analystChart;
  const chartTarget = surface === 'stocks' ? 'stocks' : 'analyst';

  // Hero
  root.append(
    h(
      'div',
      { class: 'report-hero' },
      h(
        'div',
        { class: 'report-hero-left' },
        h('div', { class: 'report-hero-symbol' }, r.symbol),
        h('div', { class: 'report-hero-name' }, `${r.name} · ${r.sector}`),
        h('div', { class: `verdict-badge ${r.verdict}` },
          r.verdict, h('span', { class: 'muted', style: 'font-weight:500;letter-spacing:normal;' },
            ` · conviction ${Math.round(r.conviction * 100)}%`)
        ),
        (() => {
          const bp = r.bull_pct;
          const brp = r.bear_pct;
          const has = bp != null && brp != null;
          return h(
            'div',
            {
              class: 'verdict-balance-highlight',
              role: 'group',
              'aria-label': has
                ? `Net directional balance: ${bp.toFixed(1)} percent bull, ${brp.toFixed(1)} percent bear`
                : 'Net directional balance',
            },
            h(
              'div',
              { class: 'verdict-balance-highlight-head' },
              h('span', { class: 'verdict-balance-highlight-icon', 'aria-hidden': 'true' }),
              h('span', { class: 'verdict-balance-label' }, 'Net directional balance'),
            ),
            has
              ? h(
                'div',
                { class: 'vb-meter-wrap' },
                h(
                  'div',
                  { class: 'vb-meter-track', 'aria-hidden': 'true' },
                  h('div', { class: 'vb-meter-bull', style: `width: ${bp}%` }),
                  h('div', { class: 'vb-meter-bear', style: `width: ${brp}%` }),
                ),
                h(
                  'div',
                  { class: 'vb-num-row mono' },
                  h(
                    'div',
                    { class: 'vb-num-stack' },
                    h('span', { class: 'vb-num vb-num-bull tabular-nums' }, `${bp.toFixed(1)}%`),
                    h('span', { class: 'vb-num-sub vb-num-sub-bull' }, 'bull'),
                  ),
                  h(
                    'div',
                    { class: 'vb-num-stack vb-num-stack-right' },
                    h('span', { class: 'vb-num vb-num-bear tabular-nums' }, `${brp.toFixed(1)}%`),
                    h('span', { class: 'vb-num-sub vb-num-sub-bear' }, 'bear'),
                  ),
                ),
              )
              : h('div', { class: 'vb-meter-fallback muted' }, '—'),
            h(
              'p',
              { class: 'verdict-balance-hint' },
              'Directional signal weight (composite score −1…+1). This is NOT the probability of being right — see Historical Hit Rate for that.',
            ),
          );
        })(),
        renderVerdictFactors(r.verdict_factors),
        h('div', { class: 'report-hero-headline' }, r.headline)
      ),
      h(
        'div',
        { class: 'report-hero-meta' },
        h(
          'div',
          { class: 'report-hero-meta-prices' },
          h(
            'div',
            { class: 'report-hero-meta-price-row' },
            heroMetaCard(fmtUSD(r.price_action.last), 'Last price'),
            heroMetaCard(fmtPct(r.price_action.change_pct), 'Change',
              r.price_action.change_pct >= 0 ? 'pos' : 'neg'),
          ),
          r.earnings_soon
            ? h(
              'div',
              {
                class: 'analyst-earnings-soon-banner',
                role: 'status',
                'aria-label': `Upcoming earnings in ${r.earnings_soon.days_until} days`,
              },
              h('span', { class: 'analyst-earnings-soon-icon', 'aria-hidden': 'true' }, '◆'),
              h(
                'span',
                { class: 'analyst-earnings-soon-body mono' },
                h('strong', {}, 'Earnings · '),
                formatEarningsDate(
                  r.earnings_soon.earnings_date,
                  r.earnings_soon.days_until,
                ),
              ),
            )
            : null,
        ),
        heroMetaCard(r.timeframe.toUpperCase(), 'Timeframe'),
        heroMetaCard(r.source, 'Source'),
      )
    )
  );

  // Signal warnings (earnings, overbought+Bollinger stretch, stale open bar)
  const rawWarn =
    surface === 'stocks'
      ? (Array.isArray(r.equity_signal_warnings) && r.equity_signal_warnings.length
          ? r.equity_signal_warnings
          : r.signal_warnings)
      : r.signal_warnings;
  const warnings = Array.isArray(rawWarn) ? rawWarn : [];
  if (warnings.length) {
    root.append(
      h(
        'div',
        { class: 'signal-warnings' },
        ...warnings.map((w) =>
          h(
            'div',
            { class: `signal-warning ${w.startsWith('⚠') ? 'signal-warning--danger' : 'signal-warning--caution'}` },
            h('span', { class: 'sw-icon' }, w.startsWith('⚠') ? '⚠' : 'ℹ'),
            h(
              'span',
              { class: 'sw-text' },
              h('span', { class: 'sw-kicker' }, 'Risk:'),
              ' ',
              w.replace(/^[⚠ℹ]\s*/, ''),
            ),
          ),
        ),
      ),
    );
  }

  // Signal quality row (Tier-1 enhancements)
  const hasQuality = r.signal_probability != null || r.mtf_confluence || r.regime_gate;
  if (hasQuality) {
    const scopeMap = {
      symbol: 'symbol sample',
      aggregate: 'market aggregate',
      config: 'config baseline',
    };
    const scopeTxt = scopeMap[r.signal_probability_scope] || 'historical sample';
    const probLabel = r.verdict === 'NEUTRAL' ? 'Pos. periods (context)' : 'Hist. hit rate';
    const probHint = r.verdict === 'NEUTRAL'
      ? `For neutral signals this shows share of positive next-window moves from ${scopeTxt}, not hold accuracy.`
      : `Historical hit rate from ${scopeTxt}.`;
    root.append(
      h(
        'div',
        { class: 'signal-quality-row' },
        r.signal_probability != null
          ? h(
            'div',
            { class: 'sq-chip', title: probHint },
            h('span', { class: 'sq-label muted' }, probLabel),
            h(
              'span',
              {
                class: `sq-value mono ${r.signal_probability >= 55 ? 'pos' : r.signal_probability < 50 ? 'neg' : ''}`,
              },
              `${r.signal_probability.toFixed(1)}%`,
            ),
            h('span', { class: 'sq-sub muted' }, scopeTxt),
          )
          : null,
        r.mtf_confluence
          ? h(
            'div',
            { class: 'sq-chip' },
            h('span', { class: 'sq-label muted' }, 'MTF'),
            h('span', { class: 'sq-value mono' }, r.mtf_confluence),
          )
          : null,
        r.regime_gate
          ? h(
            'div',
            { class: `sq-chip ${r.regime_gate.includes('dampened') ? 'sq-chip--warn' : ''}` },
            h('span', { class: 'sq-label muted' }, 'Regime'),
            h('span', { class: 'sq-value mono' }, r.regime_gate),
          )
          : null,
      ),
    );
  }

  // Indicator grid
  const rsi = r.rsi;
  const macd = r.macd;
  const atr = r.atr;
  const smaB = r.sma;
  const adxR = r.adx;
  const bbR = r.bollinger;
  const st = r.stochastic;
  root.append(
    h(
      'div',
      { class: 'indicator-grid' },
      indicatorCard(
        'RSI (14)',
        rsi.value != null ? rsi.value.toFixed(1) : '—',
        rsi.state.toUpperCase(),
        rsi.state === 'overbought' || rsi.state === 'bullish' ? 'pos' :
        rsi.state === 'oversold'  || rsi.state === 'bearish' ? 'neg' : ''
      ),
      indicatorCard(
        'Stochastic (14/3/3)',
        st.pct_k != null && st.pct_d != null
          ? `${st.pct_k.toFixed(1)} / ${st.pct_d.toFixed(1)}`
          : '—',
        st.state !== 'unknown'
          ? (st.state.toUpperCase() +
              (st.bullish_cross_recent ? ' · K>D' : '') +
              (st.bearish_cross_recent ? ' · K<D' : ''))
          : '—',
        st.state === 'oversold' || st.bullish_cross_recent ? 'pos' :
        st.state === 'overbought' || st.bearish_cross_recent ? 'neg' : ''
      ),
      indicatorCard(
        'MACD (12/26/9)',
        macd.macd != null ? macd.macd.toFixed(2) : '—',
        macd.signal != null
          ? `signal ${macd.signal.toFixed(2)} · hist ${macd.histogram?.toFixed(2) ?? '—'} (${macd.histogram_direction})`
          : '—',
        macd.bullish_cross_recent ? 'pos' : macd.bearish_cross_recent ? 'neg' : ''
      ),
      indicatorCard(
        'SMA 50 / 200',
        smaB.sma50 != null ? `$${smaB.sma50.toFixed(2)}` : '—',
        smaB.sma200 != null
          ? `200: $${smaB.sma200.toFixed(2)} · ` + (
              smaB.stacked_bullish ? 'stacked bull' :
              smaB.stacked_bearish ? 'stacked bear' : 'mixed'
            ) + (smaB.golden_cross_recent ? ' · golden cross' :
                 smaB.death_cross_recent  ? ' · death cross' : '')
          : 'SMA200 pending',
        smaB.stacked_bullish ? 'pos' : smaB.stacked_bearish ? 'neg' : ''
      ),
      indicatorCard(
        'ATR (14)',
        atr.value != null ? `$${atr.value.toFixed(2)}` : '—',
        atr.pct_of_price != null
          ? `${atr.pct_of_price.toFixed(2)}% of spot · ${atr.regime}`
          : '—',
        (atr.pct_of_price ?? 0) > 4 ? 'warning' : ''
      ),
      indicatorCard(
        'ADX (14)',
        adxR.value != null ? adxR.value.toFixed(1) : '—',
        adxR.plus_di != null && adxR.minus_di != null
          ? `+DI ${adxR.plus_di.toFixed(2)} · −DI ${adxR.minus_di.toFixed(2)} · ${adxR.trend_strength}`
          : (adxR.trend_strength || '—'),
        adxR.directional_bias === 'bullish' ? 'pos' :
        adxR.directional_bias === 'bearish' ? 'neg' : ''
      ),
      indicatorCard(
        'Bollinger (20, 2σ)',
        bbR.middle != null ? fmtUSD(bbR.middle) : '—',
        bbR.upper != null && bbR.lower != null
          ? `${fmtUSD(bbR.lower)} – ${fmtUSD(bbR.upper)} · %B ${bbR.pct_b != null ? bbR.pct_b.toFixed(2) : '—'} · ${bbR.position}`
          : '—',
        bbR.position === 'above_upper' ? 'warning' :
        bbR.position === 'below_lower' ? 'neg' : ''
      )
    )
  );

  // Levels
  root.append(
    h(
      'div',
      { class: 'levels-row' },
      levelCard('Support', r.price_action.supports, 'support'),
      levelCard('Resistance', r.price_action.resistances, 'resistance')
    )
  );

  // Tier-2 options intelligence (UOA · term structure · skew) — plain-English cards
  const of = r.options_flow;
  if (surface !== 'stocks' && of && of.source !== 'unavailable') {
    const uoaNet = of.uoa_bull - of.uoa_bear;

    // ---- UOA plain-English ------------------------------------------------
    let uoaLabel, uoaDetail, uoaTone;
    if (of.uoa_bull < 0.1 && of.uoa_bear < 0.1) {
      uoaLabel = 'Normal activity';
      uoaDetail = 'No unusual options volume right now. Activity looks normal.';
      uoaTone = '';
    } else if (uoaNet > 0.25) {
      uoaLabel = 'Bullish flow';
      uoaDetail = `More unusual call buying than put buying. This often means traders are leaning bullish. Calls ${(of.uoa_bull * 100).toFixed(0)}% vs puts ${(of.uoa_bear * 100).toFixed(0)}%.`;
      uoaTone = 'pos';
    } else if (uoaNet < -0.25) {
      uoaLabel = 'Bearish flow';
      uoaDetail = `More unusual put buying than call buying. This often means traders are leaning bearish. Puts ${(of.uoa_bear * 100).toFixed(0)}% vs calls ${(of.uoa_bull * 100).toFixed(0)}%.`;
      uoaTone = 'neg';
    } else {
      uoaLabel = 'Mixed flow';
      uoaDetail = `Both calls and puts have unusual volume, so direction is less clear. Calls ${(of.uoa_bull * 100).toFixed(0)}% and puts ${(of.uoa_bear * 100).toFixed(0)}%.`;
      uoaTone = '';
    }
    if (of.flow_score_adj !== 0) {
      uoaDetail += ` Signal ${of.flow_score_adj > 0 ? 'boosted' : 'dampened'} ${of.flow_score_adj > 0 ? '+' : ''}${(of.flow_score_adj * 100).toFixed(1)}%.`;
    }

    // ---- Term structure plain-English -------------------------------------
    let termLabel, termDetail, termTone;
    if (of.term_slope == null) {
      termLabel = 'Unavailable';
      termDetail = 'Not enough expiry data to judge near-term vs longer-term volatility.';
      termTone = '';
    } else if (of.term_slope > 1.08) {
      termLabel = 'Market stressed';
      termDetail = `Near-term volatility (${of.front_iv != null ? (of.front_iv * 100).toFixed(0) + '%' : '—'}) is much higher than longer-term (${of.back_iv != null ? (of.back_iv * 100).toFixed(0) + '%' : '—'}). This usually means event risk is high, so expect bigger swings.`;
      termTone = 'warning';
    } else if (of.term_slope > 1.04) {
      termLabel = 'Mildly stressed';
      termDetail = 'Near-term volatility is a bit higher than usual. There may be event risk coming up.';
      termTone = 'warning';
    } else if (of.term_slope < 0.94) {
      termLabel = 'Calm & normal';
      termDetail = 'Near-term volatility is lower than longer-term. This is usually a calmer setup.';
      termTone = 'pos';
    } else {
      termLabel = 'Flat (normal)';
      termDetail = 'Volatility is similar across expiries. No strong event signal from options pricing.';
      termTone = '';
    }

    // ---- Skew plain-English -----------------------------------------------
    let skewLabel, skewDetail, skewTone;
    if (of.skew == null) {
      skewLabel = 'Unavailable';
      skewDetail = 'Not enough options data to compare put vs call pricing.';
      skewTone = '';
    } else if (of.skew >= 1.25) {
      skewLabel = 'Bearish hedging';
      skewDetail = `Puts are much more expensive than calls (${of.skew.toFixed(2)}×). Traders are paying up for downside protection, which is usually a bearish sign.`;
      skewTone = 'neg';
    } else if (of.skew >= 1.10) {
      skewLabel = 'Mild put skew';
      skewDetail = `Puts are a bit more expensive than calls (${of.skew.toFixed(2)}×). This is mild caution, not extreme fear.`;
      skewTone = '';
    } else if (of.skew <= 0.85) {
      skewLabel = 'Bullish lean';
      skewDetail = `Calls are more expensive than puts (${of.skew.toFixed(2)}×). Traders are paying for upside, which leans bullish.`;
      skewTone = 'pos';
    } else {
      skewLabel = 'Balanced skew';
      skewDetail = `Calls and puts are priced similarly (${of.skew.toFixed(2)}×). No clear directional signal from skew.`;
      skewTone = '';
    }

    // Ticker-specific numeric badges so users can see data differs between symbols
    const uoaNum = `Calls ${(of.uoa_bull * 100).toFixed(0)}% · Puts ${(of.uoa_bear * 100).toFixed(0)}%`;
    const termNum = of.term_slope != null ? `${of.term_slope.toFixed(2)}×` : '—';
    const skewNum = of.skew != null ? `${of.skew.toFixed(2)}×` : '—';
    const ivNum = of.atm_iv != null ? `${(of.atm_iv * 100).toFixed(1)}%` : '—';
    const adjTxt = of.flow_score_adj !== 0
      ? `Signal ${of.flow_score_adj > 0 ? 'boosted' : 'dampened'} ${of.flow_score_adj > 0 ? '+' : ''}${(of.flow_score_adj * 100).toFixed(1)}%`
      : 'No signal adjustment';

    let ivLabel, ivDetail, ivTone;
    if (of.atm_iv == null) {
      ivLabel = 'IV context unavailable';
      ivDetail = 'We could not get enough options data to judge volatility right now.';
      ivTone = '';
    } else if ((of.iv_baseline_ratio ?? 1) >= 1.45) {
      ivLabel = 'Expensive premium regime';
      ivDetail = `Options are priced high vs normal (${(of.iv_baseline_ratio ?? 1).toFixed(2)}× usual), and the market expects about ±${of.implied_move_30d_pct != null ? of.implied_move_30d_pct.toFixed(1) : '—'}% move in 30 days. Direction can be right but option value can still drop if volatility cools.`;
      ivTone = 'warning';
    } else if ((of.iv_baseline_ratio ?? 1) <= 0.85) {
      ivLabel = 'Relatively cheap IV';
      ivDetail = `Options are cheaper than normal for this symbol (${(of.iv_baseline_ratio ?? 1).toFixed(2)}× usual).`;
      ivTone = 'pos';
    } else {
      ivLabel = 'Neutral IV regime';
      ivDetail = `Option pricing looks normal (${(of.iv_baseline_ratio ?? 1).toFixed(2)}× usual), with an expected ±${of.implied_move_30d_pct != null ? of.implied_move_30d_pct.toFixed(1) : '—'}% move over 30 days.`;
      ivTone = '';
    }

    root.append(
      h(
        'div',
        { class: 'options-flow-section' },
        h(
          'div',
          { class: 'of-header' },
          h('h3', { class: 'of-title' }, `${r.symbol} — Options intelligence`),
          h('span', { class: 'of-sub muted' }, 'Live options chain data (Yahoo Finance, ~15 min delayed) · refreshed per ticker'),
        ),
        h(
          'div',
          { class: 'of-cards' },
          // Card 1: UOA flow
          h(
            'div',
            { class: `of-card ${uoaTone ? 'of-card--' + uoaTone : ''}` },
            h('div', { class: 'of-card-icon' }, uoaTone === 'pos' ? '↑' : uoaTone === 'neg' ? '↓' : '↔'),
            h('div', { class: 'of-card-body' },
              h('div', { class: 'of-card-head-row' },
                h('div', { class: `of-card-label ${uoaTone}` }, uoaLabel),
                h('div', { class: 'of-card-num mono' }, uoaNum),
              ),
              h('div', { class: 'of-card-cat muted' }, 'Smart money flow (unusual volume/OI)'),
              h('p', { class: 'of-card-detail' }, uoaDetail),
              h('div', { class: 'of-card-adj muted mono' }, adjTxt),
            ),
          ),
          // Card 2: Term structure
          h(
            'div',
            { class: `of-card ${termTone ? 'of-card--' + termTone : ''}` },
            h('div', { class: 'of-card-icon' }, termTone === 'warning' ? '⚠' : termTone === 'pos' ? '✓' : '~'),
            h('div', { class: 'of-card-body' },
              h('div', { class: 'of-card-head-row' },
                h('div', { class: `of-card-label ${termTone}` }, termLabel),
                h('div', { class: `of-card-num mono ${termTone}` }, termNum),
              ),
              h('div', { class: 'of-card-cat muted' }, 'Near-term vs long-term volatility'),
              h('p', { class: 'of-card-detail' }, termDetail),
            ),
          ),
          // Card 3: Put-call skew
          h(
            'div',
            { class: `of-card ${skewTone ? 'of-card--' + skewTone : ''}` },
            h('div', { class: 'of-card-icon' }, skewTone === 'neg' ? '↓' : skewTone === 'pos' ? '↑' : '='),
            h('div', { class: 'of-card-body' },
              h('div', { class: 'of-card-head-row' },
                h('div', { class: `of-card-label ${skewTone}` }, skewLabel),
                h('div', { class: `of-card-num mono ${skewTone}` }, skewNum),
              ),
              h('div', { class: 'of-card-cat muted' }, 'Put vs call option pricing ratio'),
              h('p', { class: 'of-card-detail' }, skewDetail),
            ),
          ),
          // Card 4: IV context
          h(
            'div',
            { class: `of-card ${ivTone ? 'of-card--' + ivTone : ''}` },
            h('div', { class: 'of-card-icon' }, ivTone === 'warning' ? '⚡' : ivTone === 'pos' ? '✓' : '•'),
            h('div', { class: 'of-card-body' },
              h('div', { class: 'of-card-head-row' },
                h('div', { class: `of-card-label ${ivTone}` }, ivLabel),
                h('div', { class: `of-card-num mono ${ivTone}` }, ivNum),
              ),
              h('div', { class: 'of-card-cat muted' }, 'ATM IV vs baseline + implied move'),
              h('p', { class: 'of-card-detail' }, ivDetail),
            ),
          ),
        ),
      ),
    );
  }

  // Price chart (same Yahoo-style OHLC path as Ticker detail; /api/ticker bars)
  const priceCardTitleId = surface === 'stocks' ? 'stocks-price-card-title' : 'analyst-price-card-title';
  const priceCardTrailId = surface === 'stocks' ? 'stocks-price-card-trail' : 'analyst-price-card-trail';
  const enlargeBtnId = surface === 'stocks' ? 'stocks-btn-enlarge-chart' : 'analyst-btn-enlarge-chart';
  const chartToolbarId = surface === 'stocks' ? 'stocks-chart-toolbar' : 'analyst-chart-toolbar';
  const priceChartHostId = surface === 'stocks' ? 'stocks-price-chart' : 'analyst-price-chart';
  const rangePills = [
    ['1d', '1D'],
    ['5d', '5D'],
    ['1m', '1M'],
    ['6m', '6M'],
    ['1y', '1Y'],
    ['ytd', 'YTD'],
  ];
  const viewPills = [
    ['mountain', 'Mountain'],
    ['candle', 'Candle'],
    ['line', 'Line'],
    ['bar', 'Bar (OHLC)'],
  ];
  const overlayPills = [
    ['simple', 'Simple'],
    ['advanced', 'Advanced'],
  ];
  root.append(
    h(
      'div',
      { class: 'card chart-card card-lg' },
      h(
        'div',
        { class: 'card-header card-header--chart' },
        h(
          'div',
          { class: 'card-header-titles' },
          h('span', { class: 'card-title', id: priceCardTitleId }, 'Price'),
          h(
            'span',
            { class: 'card-trail mono', id: priceCardTrailId },
            `${r.chart.close.length} bars · ${r.timeframe}`,
          ),
        ),
        h(
          'button',
          {
            type: 'button',
            class: 'btn-ghost btn-enlarge-chart',
            id: enlargeBtnId,
            hidden: true,
          },
          'Enlarge chart',
        ),
      ),
      h(
        'div',
        { class: 'card-body card-body--ticker-chart' },
        h(
          'div',
          { class: 'detail-chart-toolbar', id: chartToolbarId },
          h(
            'div',
            { class: 'detail-chart-toolbar__row', role: 'group', 'aria-label': 'Chart range' },
            h('span', { class: 'detail-chart-toolbar__label muted' }, 'Range'),
            h(
              'div',
              { class: 'pill-row' },
              ...rangePills.map(([rk, lab]) => {
                const props = {
                  type: 'button',
                  class: 'pill' + (chState.range === rk ? ' is-active' : ''),
                };
                props[rangeKey] = rk;
                return h('button', props, lab);
              }),
            ),
          ),
          h(
            'div',
            { class: 'detail-chart-toolbar__row', role: 'group', 'aria-label': 'Chart style' },
            h('span', { class: 'detail-chart-toolbar__label muted' }, 'View'),
            h(
              'div',
              { class: 'pill-row' },
              ...viewPills.map(([vk, lab]) => {
                const props = {
                  type: 'button',
                  class: 'pill' + (chState.view === vk ? ' is-active' : ''),
                };
                props[viewKey] = vk;
                return h('button', props, lab);
              }),
            ),
          ),
          ...(surface === 'stocks'
            ? [
              h(
                'div',
                { class: 'detail-chart-toolbar__row', role: 'group', 'aria-label': 'Indicator overlays' },
                h('span', { class: 'detail-chart-toolbar__label muted' }, 'Overlays'),
                h(
                  'div',
                  { class: 'pill-row' },
                  ...overlayPills.map(([ok, lab]) =>
                    h(
                      'button',
                      {
                        type: 'button',
                        class: 'pill' + (state.stocksChart.overlayMode === ok ? ' is-active' : ''),
                        'data-stocks-overlay': ok,
                      },
                      lab,
                    ),
                  ),
                ),
              ),
            ]
            : []),
        ),
        h('div', { id: priceChartHostId },
          h('div', { class: 'chart-placeholder' }, tickerD ? '…' : 'Loading…')),
      ),
    ),
  );
  if (tickerD) {
    renderPriceChart(tickerD, chartTarget);
  } else {
    const ph = document.getElementById(priceChartHostId);
    if (ph) {
      ph.innerHTML = '';
      ph.append(
        h(
          'div',
          { class: 'chart-placeholder' },
          'Could not load price bars — check that this symbol is in the universe.',
        ),
      );
    }
  }

  // Narrative — deterministic by default; optionally polished by Claude.
  const narrativeHost = h(
    'div',
    {
      class: 'report-narrative',
      id: surface === 'stocks' ? 'stocks-narrative-host' : 'narrative-host',
    },
    ...r.narrative.split('\n\n').map((para) => h('p', {}, para))
  );
  const polishBtn =
    surface === 'analyst' && analyst.llm.enabled
      ? h(
        'button',
        {
          class: 'btn-ghost polish-btn',
          id: 'polish-btn',
          onClick: () => togglePolishNarrative(r),
        },
        'Polish with Claude ✨'
      )
      : null;
  root.append(
    h(
      'div',
      { class: 'report-section-head' },
      h('h2', {}, 'Technical analysis'),
      polishBtn
    ),
    narrativeHost
  );

  // Volume stats + patterns block
  root.append(
    h(
      'div',
      { class: 'indicator-grid' },
      indicatorCard(
        'Volume (latest)',
        Math.round(r.volume.latest).toLocaleString(),
        `20-bar avg ${Math.round(r.volume.avg_20).toLocaleString()} · ratio ${r.volume.ratio.toFixed(2)}×` +
          (r.volume.unusual ? ' · unusual' : ''),
        r.volume.unusual && r.volume.ratio > 1.5 && r.price_action.change_pct > 0 ? 'pos' :
        r.volume.unusual && r.volume.ratio > 1.5 && r.price_action.change_pct < 0 ? 'neg' : ''
      ),
      indicatorCard(
        'Trend',
        r.price_action.trend.toUpperCase(),
        `Period move ${fmtPct(r.price_action.change_pct_period)}`,
        r.price_action.trend === 'uptrend' ? 'pos' :
        r.price_action.trend === 'downtrend' ? 'neg' : ''
      ),
      indicatorCard(
        'Patterns',
        (r.price_action.patterns.length || '0') + ' detected',
        r.price_action.patterns.length
          ? r.price_action.patterns.join(' · ')
          : 'No notable chart patterns.',
        ''
      ),
      indicatorCard(
        'Composite score',
        fmtScore(r.composite_score),
        `Verdict ${r.verdict} · conviction ${Math.round(r.conviction * 100)}%`,
        r.composite_score > 0 ? 'pos' : r.composite_score < 0 ? 'neg' : ''
      )
    )
  );

  // Market context
  root.append(
    h(
      'div',
      { class: 'context-card' },
      h('h4', {}, 'Market & sector context'),
      r.market_context
    )
  );

  if (surface === 'stocks') {
    root.append(stockStrategyCard(r.stock_strategy));
  } else {
    root.append(
      tradeTicketCard(
        r.options.trade_plan,
        r.verdict,
        r.options.headline,
        r.timeframe,
        r.signal_warnings || [],
        r.source,
      ),
    );
  }

  // Optional: ask Claude about this specific report.
  if (analyst.llm.enabled) {
    root.append(explainBox(r));
  }
}

function stockStrategyCard(ss) {
  if (!ss) {
    return h(
      'div',
      { class: 'callout' },
      h('div', { class: 'callout-title' }, 'Stock strategy'),
      'Unavailable for this response.',
    );
  }
  const ad = ss.action_display || 'WAIT';
  const actTone =
    ad === 'BUY' ? 'ssc-bull' :
      ad === 'SHORT' ? 'ssc-bear' : 'ssc-neutral';
  const heroPrice =
    ad === 'BUY' && ss.buy_price != null ? ss.buy_price :
      ad === 'SHORT' && ss.short_entry_price != null ? ss.short_entry_price :
        ss.entry.price;
  const heroLabel =
    ad === 'BUY' ? 'Buy zone' :
      ad === 'SHORT' ? 'Short (sell) zone' :
        'Reference — no directional buy/sell';

  const heroTone =
    ad === 'BUY' ? 'ssc-hero-bull' :
      ad === 'SHORT' ? 'ssc-hero-bear' : 'ssc-hero-wait';

  const dirWhy =
    ad === 'BUY' ? 'Why bullish' :
      ad === 'SHORT' ? 'Why bearish' : 'Why neutral / wait';

  const reasonHighlight =
    ss.direction_summary
      ? h(
        'div',
        {
          class: `ssc-direction-reason ${
            ad === 'BUY' ? 'ssc-dir-bull' : ad === 'SHORT' ? 'ssc-dir-bear' : 'ssc-dir-neutral'
          }`,
        },
        h('div', { class: 'ssc-dir-ribbon' }, dirWhy),
        h('p', { class: 'ssc-dir-summary' }, ss.direction_summary),
        ss.direction_bullets && ss.direction_bullets.length
          ? h(
            'ul',
            { class: 'ssc-dir-bullets' },
            ...ss.direction_bullets.map((t) => h('li', {}, t)),
          )
          : null,
      )
      : null;

  const hero = h(
    'div',
    { class: `ssc-action-hero ${heroTone}` },
    h('div', { class: 'ssc-hero-top' },
      h('span', { class: 'ssc-action-badge' }, ad),
      h('span', { class: 'ssc-hero-label muted small' }, heroLabel)),
    h('div', { class: 'ssc-hero-price mono' }, fmtUSD(heroPrice)),
    ss.entry && ss.entry.note
      ? h('div', { class: 'ssc-hero-note muted small' }, ss.entry.note)
      : null,
  );

  const rows = [];
  if (ad === 'BUY' && ss.buy_price != null) {
    rows.push(
      h(
        'div',
        { class: 'ssc-row ssc-row-highlight ssc-hl-buy' },
        h('div', { class: 'ssc-label' }, 'Buy recommendation'),
        h('div', { class: 'ssc-val mono ssc-price-em' }, fmtUSD(ss.buy_price)),
        h('div', { class: 'ssc-hint muted' },
          ss.entry.mode === 'limit'
            ? 'Limit near this level (pullback / structure).'
            : 'Size near spot / reference; consider limits on a dip.'),
      ),
    );
  } else if (ad === 'SHORT' && ss.short_entry_price != null) {
    rows.push(
      h(
        'div',
        { class: 'ssc-row ssc-row-highlight ssc-hl-short' },
        h('div', { class: 'ssc-label' }, 'Sell / short entry'),
        h('div', { class: 'ssc-val mono ssc-price-em' }, fmtUSD(ss.short_entry_price)),
        h('div', { class: 'ssc-hint muted' },
          ss.entry.mode === 'limit' ? 'Initiate short on strength / rally.' : 'Reference entry for bearish swing.'),
      ),
    );
  } else {
    rows.push(
      h(
        'div',
        { class: 'ssc-row' },
        h('div', { class: 'ssc-label muted' }, `Reference (${ss.entry.mode})`),
        h('div', { class: 'ssc-val mono' }, fmtUSD(ss.entry.price)),
        h('div', { class: 'ssc-hint muted' }, ss.entry.note || ''),
      ),
    );
  }

  if (ss.sell_take_profit_price != null) {
    const lab = ad === 'SHORT' ? 'Cover for profit (buy)' : 'Take profit (sell)';
    rows.push(
      h(
        'div',
        { class: 'ssc-row ssc-row-highlight ssc-hl-profit' },
        h('div', { class: 'ssc-label' }, lab),
        h('div', { class: 'ssc-val mono ssc-price-em' }, fmtUSD(ss.sell_take_profit_price)),
        h('div', { class: 'ssc-hint muted' },
          ad === 'SHORT'
            ? 'Buy to cover lower — book swing profit.'
            : 'Sell shares here to realize gains (scale per plan).'),
      ),
    );
  }
  if (ss.sell_stop_price != null) {
    const lab2 = ad === 'SHORT' ? 'Stop — buy to cover' : 'Stop loss (sell)';
    rows.push(
      h(
        'div',
        { class: 'ssc-row ssc-row-highlight ssc-hl-stop' },
        h('div', { class: 'ssc-label' }, lab2),
        h('div', { class: 'ssc-val mono ssc-price-em' }, fmtUSD(ss.sell_stop_price)),
        h('div', { class: 'ssc-hint muted' },
          'Thesis protection — gaps can slip fills.'),
      ),
    );
  }

  if (ss.risk_reward != null) {
    rows.push(
      h(
        'div',
        { class: 'ssc-row' },
        h('div', { class: 'ssc-label muted' }, 'Risk / reward'),
        h('div', { class: 'ssc-val mono' }, `${ss.risk_reward.toFixed(2)}×`),
        h('div', { class: 'ssc-hint muted' }, 'Underlying swing geometry'),
      ),
    );
  }

  const patList = ss.chart_patterns && ss.chart_patterns.length
    ? h(
      'div',
      { class: 'ssc-patterns' },
      h('div', { class: 'ssc-patterns-title muted small' }, 'Structure / pattern cues'),
      h(
        'div',
        { class: 'ssc-pattern-chips' },
        ...ss.chart_patterns.map((txt) => {
          const low = String(txt).toLowerCase();
          let chipTone = 'ssc-chip-neutral';
          if (/\bbear\b|bear trap|bear flag|descending triangle|lower lows/.test(low)) {
            chipTone = 'ssc-chip-bear';
          } else if (
            /\bbull\b|bull trap|bull flag|ascending triangle|cup-and-handle|cup-with-handle|higher highs/.test(low)
          ) {
            chipTone = 'ssc-chip-bull';
          }
          return h('span', { class: `ssc-chip ${chipTone}`, title: txt }, txt);
        }),
      ),
    )
    : null;

  return h(
    'div',
    { class: `stock-strategy-card ${actTone}` },
    h(
      'div',
      { class: 'ssc-head' },
      h('h3', { class: 'ssc-title' }, 'Swing stock plan'),
      h('div', { class: 'ssc-headline' }, ss.headline),
    ),
    reasonHighlight,
    hero,
    h('div', { class: 'ssc-horizon muted' }, ss.hold_horizon),
    h('div', { class: 'ssc-grid' }, ...rows),
    patList,
    ss.range_note ? h('div', { class: 'ssc-range muted' }, ss.range_note) : null,
    h('p', { class: 'ssc-rationale' }, ss.rationale),
    h('p', { class: 'ssc-disclaimer muted small' }, ss.disclaimer),
  );
}

function renderAnalystReport(r, tickerD) {
  renderAnalystSurface(r, tickerD, 'analyst');
}

function renderStockReport(r, tickerD) {
  renderAnalystSurface(r, tickerD, 'stocks');
}

async function togglePolishNarrative(report) {
  const host = $('#narrative-host');
  const btn = $('#polish-btn');
  if (!host || !btn) return;

  // Toggle back to deterministic if currently polished.
  if (host.dataset.mode === 'polished') {
    host.dataset.mode = 'deterministic';
    host.innerHTML = '';
    report.narrative.split('\n\n').forEach((para) =>
      host.append(h('p', {}, para))
    );
    btn.textContent = 'Polish with Claude ✨';
    return;
  }

  const key = `${report.symbol}|${report.timeframe}`;
  const cached = analyst.polishCache[key];
  const prevText = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Polishing…';

  try {
    let text = cached;
    if (!text) {
      const resp = await api(
        `/api/analyst/polish/${encodeURIComponent(report.symbol)}` +
          `?timeframe=${encodeURIComponent(report.timeframe)}`
      );
      text = resp.text;
      analyst.polishCache[key] = text;
    }
    host.dataset.mode = 'polished';
    host.innerHTML = '';
    text.split(/\n\n+/).forEach((para) =>
      host.append(h('p', { class: 'polished-para' }, para))
    );
    host.append(
      h(
        'div',
        { class: 'polish-footer muted mono' },
        `Polished by Claude ${analyst.llm.model} · facts pinned from technicals`
      )
    );
    btn.textContent = 'Show raw analysis';
    initPoweredBy(); // success -> clear any stale "error" badge
  } catch (e) {
    btn.textContent = prevText;
    alert(`Polish failed: ${e}`);
    initPoweredBy(); // refresh footer tooltip with the new last-error
  } finally {
    btn.disabled = false;
  }
}

async function loadDailyBrief({ force = false } = {}) {
  const body = $('#brief-body');
  if (!body) return;
  const key = analyst.timeframe;

  if (!force && analyst.briefCache[key]) {
    renderBriefText(body, analyst.briefCache[key]);
    return;
  }

  body.innerHTML = '<span class="muted">Generating today&rsquo;s brief…</span>';
  try {
    const resp = await api(
      `/api/analyst/brief?timeframe=${encodeURIComponent(key)}`
    );
    analyst.briefCache[key] = resp.text;
    renderBriefText(body, resp.text);
    initPoweredBy(); // success -> clear any stale "error" badge
  } catch (e) {
    body.innerHTML = `<span class="muted">Brief unavailable: ${e}</span>`;
    initPoweredBy();
  }
}

function renderBriefText(host, text) {
  // Light markdown: '### header' lines + '- ' bullets. Everything else = <p>.
  host.innerHTML = '';
  const lines = text.split('\n');
  let currentList = null;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { currentList = null; continue; }
    if (line.startsWith('### ')) {
      currentList = null;
      host.append(h('h4', { class: 'brief-h' }, line.slice(4)));
    } else if (line.startsWith('- ') || line.startsWith('* ')) {
      if (!currentList) {
        currentList = h('ul', { class: 'brief-list' });
        host.append(currentList);
      }
      currentList.append(h('li', {}, line.slice(2)));
    } else {
      currentList = null;
      host.append(h('p', { class: 'brief-p' }, line));
    }
  }
}

function explainBox(report) {
  const key = `${report.symbol}|${report.timeframe}`;
  const box = h(
    'div',
    { class: 'explain-box' },
    h(
      'div',
      { class: 'explain-head' },
      h('h3', {}, `Ask Claude about ${report.symbol}`),
      h('span', { class: 'brief-tag' }, `Claude ${analyst.llm.model}`)
    ),
    h(
      'div',
      { class: 'explain-hint muted' },
      'Questions grounded in this report only. Examples: ',
      h('span', { class: 'mono' }, '"why this strike?"'),
      ', ',
      h('span', { class: 'mono' }, '"what invalidates the thesis?"'),
      ', ',
      h('span', { class: 'mono' }, '"how does the RSI compare to the MACD signal?"')
    ),
    h(
      'div',
      { class: 'explain-input-row' },
      h('input', {
        type: 'text',
        class: 'explain-input mono',
        id: 'explain-input',
        placeholder: `Ask about ${report.symbol}'s setup, strike, stop…`,
        onKeydown: (e) => {
          if (e.key === 'Enter') submitExplain(report);
        },
      }),
      h(
        'button',
        {
          class: 'btn-primary',
          id: 'explain-submit',
          onClick: () => submitExplain(report),
        },
        'Ask'
      )
    ),
    h('div', { class: 'explain-answer', id: 'explain-answer' })
  );
  return box;
}

async function submitExplain(report) {
  const input = $('#explain-input');
  const btn = $('#explain-submit');
  const ans = $('#explain-answer');
  if (!input || !ans) return;
  const q = input.value.trim();
  if (!q) return;

  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = 'Thinking…';
  ans.innerHTML = '<div class="muted">Claude is reasoning over the report…</div>';

  try {
    const r = await fetch(
      `/api/analyst/explain/${encodeURIComponent(report.symbol)}`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ question: q, timeframe: report.timeframe }),
      }
    );
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    const { text } = await r.json();
    ans.innerHTML = '';
    text.split(/\n\n+/).forEach((para) =>
      ans.append(h('p', { class: 'explain-para' }, para))
    );
    ans.append(
      h('div', { class: 'polish-footer muted mono' },
        `Grounded in ${report.symbol} · ${report.timeframe} · facts pinned`)
    );
    initPoweredBy(); // success -> clear any stale "error" badge
  } catch (e) {
    ans.innerHTML = `<div class="muted">Claude couldn't answer: ${e}</div>`;
    initPoweredBy();
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// ---------- Recommended Option Strategy ticket -----------------------------

function tradeTicketCard(tp, verdict, headline, timeframe, warnings, source = 'yfinance') {
  const noTrade = verdict === 'NEUTRAL' || /^NO TRADE/i.test(String(headline || '')) || source === 'synthetic';
  if (noTrade) {
    const why =
      source === 'synthetic'
        ? 'Live market data is unavailable, so this report uses simulated prices. Do not place a real options trade from this signal.'
        : 'This setup does not have enough directional edge yet. Wait for stronger confirmation before opening a call or put.';
    const simp = (msg) => String(msg || '').replace(/^[⚠ℹ]\s*/, '');
    return h(
      'div',
      { class: 'callout callout-warning' },
      h('div', { class: 'callout-title' }, 'No directional options trade right now'),
      h('div', {}, why),
      Array.isArray(warnings) && warnings.length
        ? h(
          'ul',
          {},
          ...warnings.slice(0, 3).map((w) =>
            h('li', {}, h('span', { class: 'sw-kicker' }, 'Risk:'), ' ', simp(w))
          ),
        )
        : null,
    );
  }
  const type = tp.contract_type; // always 'call' or 'put'
  const action = type === 'call' ? 'BUY CALL' : 'BUY PUT';
  const tone = type === 'call' ? 'pos' : 'neg';
  const expiryDate = fmtExpiry(tp.expiry_date, tp.expiry_dte);

  // Plain-English explanation of what "Bullish" means for the chosen timeframe
  const tfHorizon = { '1h': 'hours (intraday)', '4h': 'days (swing)', 'daily': 'days to weeks', 'weekly': 'weeks to months' };
  const horizonText = tfHorizon[timeframe] || 'the medium term';
  const signalScopeNote = h(
    'div',
    { class: 'ticket-scope-note' },
    h('span', { class: 'ticket-scope-icon' }, 'ℹ'),
    h('div', { class: 'ticket-scope-body' },
      h('span', {},
        `This ${(timeframe || '').toUpperCase()} signal reflects the trend over ${horizonText}. ` +
        'It is NOT a same-day trade recommendation. ' +
        'Options should only be opened when the signal aligns with your intended holding window ' +
        'AND no active warnings are shown above.',
      ),
      h(
        'span',
        { class: 'ticket-sizing-note' },
        '⚡ Position sizing: risk only 1–2% of your total account on any single options trade. ' +
        'Never risk more than you can afford to lose entirely on speculative options.',
      ),
    ),
  );

  // Warn if any active signal warnings exist
  const simplifyWarningText = (msg) => {
    let out = String(msg || '').replace(/^[⚠ℹ]\s*/, '');
    out = out.replaceAll('DTE', 'days to expiry');
    out = out.replaceAll('IV', 'implied volatility');
    out = out.replaceAll('ATM', 'at-the-money');
    out = out.replaceAll('OI', 'open interest');
    out = out.replaceAll('R:R', 'risk/reward');
    out = out.replaceAll('theta', 'time decay');
    return out;
  };

  const hasWarnings = Array.isArray(warnings) && warnings.length > 0;
  const warningBanner = hasWarnings
    ? h(
      'div',
      { class: 'ticket-warning-banner' },
      h('strong', {}, '⚠ Important risks to review first:'),
      h(
        'ul',
        {},
        ...warnings.map((w) =>
          h('li', {}, h('span', { class: 'sw-kicker' }, 'Risk:'), ' ', simplifyWarningText(w))
        ),
      ),
    )
    : null;

  const hero = h(
    'div',
    { class: 'ticket-hero' },
    h('div', { class: 'ticket-label' }, 'Recommended Option Strategy'),
    h(
      'div',
      { class: `ticket-action tone-${tone}` },
      `${action}  $${fmtStrike(tp.strike)}  ${expiryDate}`
    ),
    h('span', { class: `verdict-badge ${verdict}` }, verdict)
  );

  // Break-even as % move from spot
  const beMovePct = tp.break_even != null && tp.spot_at_entry > 0
    ? Math.abs((tp.break_even - tp.spot_at_entry) / tp.spot_at_entry * 100)
    : null;
  // Theta as % of premium per week
  const thetaWeeklyPct = tp.theta_per_day != null && tp.estimated_premium != null && tp.estimated_premium > 0
    ? Math.abs(tp.theta_per_day * 7 / tp.estimated_premium * 100)
    : null;

  const statRow = h(
    'div',
    { class: 'ticket-stats' },
    ticketStat(
      tp.estimated_premium != null ? `$${tp.estimated_premium.toFixed(2)}` : '—',
      'Est. premium / sh'
    ),
    ticketStat(
      tp.cost_per_contract != null ? fmtUSD(tp.cost_per_contract) : '—',
      'Max loss / contract'
    ),
    ticketStat(
      tp.break_even != null
        ? `$${tp.break_even.toFixed(2)}${beMovePct != null ? ` (+${beMovePct.toFixed(1)}%)` : ''}`
        : '—',
      'Break-even (% needed)'
    ),
    ticketStat(`$${tp.spot_at_entry.toFixed(2)}`, 'Spot at entry'),
    ticketStat(
      tp.one_sigma_move_usd != null ? `±$${tp.one_sigma_move_usd.toFixed(2)}` : '—',
      `1σ move (${tp.expiry_dte}D)`
    ),
    thetaWeeklyPct != null
      ? ticketStat(
        `${thetaWeeklyPct.toFixed(1)}%/week`,
        'Theta decay / week',
      )
      : null,
  );

  const planRow = h(
    'div',
    { class: 'ticket-plan' },
    planLeg(
      'Target (underlying)',
      tp.target_price != null ? `$${tp.target_price.toFixed(2)}` : '—',
      type === 'call' ? 'pos' : 'neg',
      tp.target_price != null && tp.spot_at_entry
        ? fmtPct(((tp.target_price / tp.spot_at_entry) - 1) * 100)
        : ''
    ),
    planLeg(
      'Stop / invalidation',
      tp.stop_loss != null ? `$${tp.stop_loss.toFixed(2)}` : '—',
      type === 'call' ? 'neg' : 'pos',
      tp.stop_loss != null && tp.spot_at_entry
        ? fmtPct(((tp.stop_loss / tp.spot_at_entry) - 1) * 100)
        : ''
    ),
    planLeg(
      'Risk / reward (underlying)',
      tp.risk_reward != null ? `${tp.risk_reward.toFixed(2)}×` : '—',
      tp.risk_reward != null && tp.risk_reward >= 1.5 ? 'pos' : '',
      tp.risk_reward != null && tp.risk_reward >= 1.5 ? 'favorable' :
      tp.risk_reward != null ? 'mediocre' : ''
    )
  );

  // Greeks row (delta · theta · vega)
  const hasGreeks = tp.delta != null || tp.theta_per_day != null || tp.vega_per_pct != null;
  const greeksRow = hasGreeks
    ? h(
      'div',
      { class: 'ticket-greeks' },
      h('div', { class: 'tg-title muted' }, 'Option Greeks (Black-Scholes estimate)'),
      h(
        'div',
        { class: 'tg-grid' },
        tp.delta != null
          ? h(
            'div',
            { class: 'tg-item' },
            h('div', { class: 'tg-val mono' }, tp.delta.toFixed(2)),
            h('div', { class: 'tg-label' }, 'Delta'),
            h('div', { class: 'tg-hint muted' }, `$${(tp.delta * tp.spot_at_entry).toFixed(2)} exposure per $1 move`),
          )
          : null,
        tp.theta_per_day != null
          ? h(
            'div',
            { class: 'tg-item' },
            h('div', { class: `tg-val mono ${tp.theta_per_day < 0 ? 'neg' : ''}` }, `$${tp.theta_per_day.toFixed(3)}/day`),
            h('div', { class: 'tg-label' }, 'Theta (daily)'),
            h('div', { class: 'tg-hint muted' }, 'Time decay per calendar day per share'),
          )
          : null,
        tp.vega_per_pct != null
          ? h(
            'div',
            { class: 'tg-item' },
            h('div', { class: 'tg-val mono' }, `$${tp.vega_per_pct.toFixed(3)}/1%IV`),
            h('div', { class: 'tg-label' }, 'Vega (per 1% IV)'),
            h('div', { class: 'tg-hint muted' }, 'Change in value per +1% implied volatility'),
          )
          : null,
      ),
    )
    : null;

  const rationale = h('div', { class: 'rationale ticket-rationale' }, tp.rationale);

  // P&L scenario table (BS re-pricing estimates)
  const hasScenarios = tp.scenario_at_target != null || tp.scenario_flat_14d != null || tp.scenario_at_stop != null;
  const scenarioRow = hasScenarios
    ? h(
      'div',
      { class: 'ticket-scenarios' },
      h('div', { class: 'ts-title muted' }, 'Approximate P&L scenarios per contract (Black-Scholes re-priced)'),
      h(
        'div',
        { class: 'ts-grid' },
        tp.scenario_at_target != null
          ? h('div', { class: `ts-item ${tp.scenario_at_target >= 0 ? 'pos' : 'neg'}` },
            h('div', { class: 'ts-val mono' },
              `${tp.scenario_at_target >= 0 ? '+' : ''}${fmtUSD(tp.scenario_at_target)}`),
            h('div', { class: 'ts-label' }, 'If stock hits target'),
            h('div', { class: 'ts-hint muted' }, `~DTE/3 days elapsed, stock at $${tp.target_price?.toFixed(2)}`),
          )
          : null,
        tp.scenario_flat_14d != null
          ? h('div', { class: `ts-item ${tp.scenario_flat_14d >= 0 ? 'pos' : 'neg'}` },
            h('div', { class: 'ts-val mono' },
              `${tp.scenario_flat_14d >= 0 ? '+' : ''}${fmtUSD(tp.scenario_flat_14d)}`),
            h('div', { class: 'ts-label' }, 'If flat after 14 days'),
            h('div', { class: 'ts-hint muted' }, 'Pure theta drag, stock at same price'),
          )
          : null,
        tp.scenario_at_stop != null
          ? h('div', { class: `ts-item ${tp.scenario_at_stop >= 0 ? 'pos' : 'neg'}` },
            h('div', { class: 'ts-val mono' },
              `${tp.scenario_at_stop >= 0 ? '+' : ''}${fmtUSD(tp.scenario_at_stop)}`),
            h('div', { class: 'ts-label' }, 'If stock hits stop'),
            h('div', { class: 'ts-hint muted' }, `Stock at $${tp.stop_loss?.toFixed(2)} (thesis invalidated)`),
          )
          : null,
      ),
      h('p', { class: 'ts-disclaimer muted' },
        'Estimates use Black-Scholes with constant IV — actual P&L will differ due to IV changes, slippage, and bid-ask spread. Cut losses before stop is hit if signal conditions change.'
      ),
    )
    : null;

  return h(
    'div',
    { class: 'ticket-card ticket-card-concrete' },
    hero,
    signalScopeNote,
    warningBanner,
    statRow,
    planRow,
    greeksRow,
    scenarioRow,
    rationale
  );
}

function ticketStat(value, label) {
  return h(
    'div',
    { class: 'ticket-stat' },
    h('div', { class: 'ticket-stat-v' }, value),
    h('div', { class: 'ticket-stat-l' }, label)
  );
}

function planLeg(label, value, tone, sub) {
  return h(
    'div',
    { class: 'plan-leg' },
    h('div', { class: 'plan-leg-l' }, label),
    h('div', { class: `plan-leg-v tone-${tone}` }, value),
    sub ? h('div', { class: 'plan-leg-sub' }, sub) : null
  );
}

function fmtExpiry(iso, dte) {
  // Accept "YYYY-MM-DD" and render "May 29, 2026 (35D)".
  try {
    const d = new Date(iso + 'T00:00:00');
    const m = d.toLocaleString('en-US', { month: 'short' });
    return `${m} ${d.getDate()}, ${d.getFullYear()}` + (dte != null ? ` (${dte}D)` : '');
  } catch {
    return iso;
  }
}

function heroMetaCard(value, label, tone = '') {
  return h(
    'div',
    { class: 'rh' },
    h(
      'div',
      { class: 'v ' + (tone === 'pos' ? 'pos' : tone === 'neg' ? 'neg' : '') },
      value
    ),
    h('div', { class: 'l' }, label)
  );
}

function indicatorCard(title, bigValue, sub, tone = '') {
  return h(
    'div',
    { class: 'indicator-card' },
    h('h4', {}, title),
    h('div', { class: 'big ' + tone }, bigValue),
    h('div', { class: 'sub' }, sub)
  );
}

function levelCard(title, levels, cls) {
  return h(
    'div',
    { class: 'level-card' },
    h('h4', {}, title),
    h(
      'div',
      { class: 'level-list' },
      levels.length
        ? levels.map((v) => h('span', { class: `level-pill ${cls}` }, `$${v.toFixed(2)}`))
        : h('span', { class: 'muted' }, 'none identified in current range')
    )
  );
}

// ---------- Price + SMA overlay chart --------------------------------------

function priceWithSmaChart(chart) {
  const w = 920;
  const h0 = 280;
  const pad = { top: 16, right: 60, bottom: 22, left: 60 };
  const innerW = w - pad.left - pad.right;
  const innerH = h0 - pad.top - pad.bottom;
  const n = chart.close.length;
  if (n < 2) {
    return h('div', { class: 'chart-placeholder' }, 'No data');
  }

  const all = [];
  for (const v of chart.close) all.push(v);
  for (const v of chart.sma50) if (v != null) all.push(v);
  for (const v of chart.sma200) if (v != null) all.push(v);
  const min = Math.min(...all);
  const max = Math.max(...all);
  const range = (max - min) || 1;
  const xStep = innerW / (n - 1);

  const xy = (v, i) => [
    pad.left + i * xStep,
    pad.top + innerH - ((v - min) / range) * innerH,
  ];

  const toPath = (series) => {
    let path = '';
    let started = false;
    series.forEach((v, i) => {
      if (v == null) { started = false; return; }
      const [x, y] = xy(v, i);
      path += (started ? ' L' : 'M') + x + ',' + y;
      started = true;
    });
    return path;
  };

  const root = svg('svg', {
    class: 'spark',
    viewBox: `0 0 ${w} ${h0}`,
    preserveAspectRatio: 'none',
    style: 'height: 280px;',
  });

  // Horizontal gridlines + y labels
  for (let g = 0; g <= 4; g++) {
    const v = min + (range * g) / 4;
    const y = pad.top + innerH - ((v - min) / range) * innerH;
    root.append(
      svg('line', {
        x1: pad.left, y1: y, x2: w - pad.right, y2: y,
        stroke: 'var(--stroke-soft)', 'stroke-dasharray': '2 3',
      }),
      svg('text', {
        x: pad.left - 8, y: y + 3,
        'text-anchor': 'end',
        fill: 'var(--text-dim)',
        'font-size': 10,
        'font-family': 'var(--mono)',
      }, '$' + v.toFixed(v > 50 ? 0 : 2))
    );
  }

  // SMA200 then SMA50 (so 50 draws on top)
  root.append(svg('path', {
    d: toPath(chart.sma200),
    fill: 'none',
    stroke: 'var(--warning)',
    'stroke-width': 1.2,
    'stroke-opacity': 0.8,
  }));
  root.append(svg('path', {
    d: toPath(chart.sma50),
    fill: 'none',
    stroke: 'var(--accent)',
    'stroke-width': 1.2,
    'stroke-opacity': 0.9,
  }));

  // Close line on top
  root.append(svg('path', {
    d: toPath(chart.close),
    fill: 'none',
    stroke: 'var(--text)',
    'stroke-width': 1.6,
  }));

  // Last point marker + label
  const last = chart.close[n - 1];
  const [lx, ly] = xy(last, n - 1);
  root.append(
    svg('circle', { cx: lx, cy: ly, r: 3.5, fill: 'var(--text)' }),
    svg('text', {
      x: w - pad.right + 6, y: ly + 3,
      fill: 'var(--text)',
      'font-size': 10,
      'font-family': 'var(--mono)',
    }, '$' + last.toFixed(2))
  );

  // Legend
  root.append(
    svg('g', { transform: `translate(${pad.left + 8}, ${pad.top + 8})` },
      svg('rect', { x: 0, y: 0, width: 190, height: 50, fill: 'var(--bg-raised)', 'fill-opacity': 0.85, rx: 4 }),
      legendItem(0, 10, 'var(--text)',    'Close'),
      legendItem(0, 26, 'var(--accent)',  'SMA 50'),
      legendItem(0, 42, 'var(--warning)', 'SMA 200'),
    )
  );

  return root;
}

function legendItem(x, y, color, label) {
  return svg('g', {},
    svg('line', { x1: x + 6, y1: y, x2: x + 26, y2: y, stroke: color, 'stroke-width': 2 }),
    svg('text', {
      x: x + 32, y: y + 3,
      fill: 'var(--text-muted)',
      'font-size': 11,
    }, label)
  );
}

// ---------- Clock (EST) ----------------------------------------------------

function startESTClock() {
  const headEl = $('#est-clock');
  const footEl = $('#footer-clock');
  const tick = () => {
    const now = new Date();
    if (headEl) headEl.textContent = fmtEST(now) + ' ET';
    if (footEl) footEl.textContent = fmtESTDateTime(now);
  };
  tick();
  setInterval(tick, 1000);
}

// ---------- Data refresh (manual + 5 min auto) ----------------------------

const REFRESH_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes

const refresher = {
  timer: null,
  nextAt: 0,
  inFlight: false,
  lastISO: null,
};

async function refreshAll({ reason = 'manual' } = {}) {
  if (refresher.inFlight) return;
  refresher.inFlight = true;
  const btn = $('#refresh-btn');
  if (btn) btn.classList.add('refreshing');

  // Invalidate cached detail payloads so Ticker view pulls fresh data too.
  state.details = {};
  analyst.tickerDetails = {};
  stocks.tickerDetails = {};
  searchUI.tickerDetails = {};
  analyst.reports = {};
  analyst.polishCache = {};
  analyst.briefCache = {};
  etf.overview = [];

  try {
    const analystRefreshP =
      state.view === 'analyst' || analyst.initialized
        ? (async () => {
            if (reason === 'manual') {
              analyst.activeSymbol = null;
              analyst._priceChartSymbol = null;
              const rpt = $('#analyst-report');
              if (rpt) rpt.innerHTML = '';
              renderTickerStrip();
            }
            await loadAnalystOverview({ pollBackground: reason === 'manual' });
            if (analyst.activeSymbol) await loadAnalystReport(analyst.activeSymbol);
            if (analyst.llm.enabled) await loadDailyBrief({ force: reason === 'manual' });
          })()
        : null;

    await Promise.allSettled([
      loadDashboard(),
      analystRefreshP,
    ]);

    if (state.view === 'detail' && state.activeSymbol) {
      await openTicker(state.activeSymbol);
    }
    if (state.view === 'search' && searchUI.lastQuery) {
      runSearch(searchUI.lastQuery);
    }
    if (state.view === 'screener') {
      loadScreener();
    }
    if (state.view === 'etf') {
      loadEtfOverview();
    }
    if (state.view === 'stocks' || stocks.initialized) {
      if (reason === 'manual') {
        stocks.activeSymbol = null;
        stocks._priceChartSymbol = null;
        const rpt = $('#stocks-report');
        if (rpt) rpt.innerHTML = '';
        renderStocksTickerStrip();
      }
      loadStocksOverview();
      if (stocks.activeSymbol) loadStocksReport(stocks.activeSymbol);
    }
    // Re-check LLM health on every refresh so the badge recovers from
    // transient errors (e.g. right after the user adds credits).
    initPoweredBy();
    refresher.lastISO = new Date().toISOString();
  } catch (e) {
    console.warn('refresh failed:', e);
  } finally {
    refresher.inFlight = false;
    if (btn) btn.classList.remove('refreshing');
    scheduleNextRefresh();
  }
}

function scheduleNextRefresh() {
  if (refresher.timer) clearTimeout(refresher.timer);
  refresher.nextAt = Date.now() + REFRESH_INTERVAL_MS;
  refresher.timer = setTimeout(() => refreshAll({ reason: 'auto' }), REFRESH_INTERVAL_MS);
}

function initRefresh() {
  const btn = $('#refresh-btn');
  if (btn) {
    btn.addEventListener('click', () => refreshAll({ reason: 'manual' }));
  }
  // Refresh when the tab becomes visible after being hidden for a while.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      const idle = Date.now() - (refresher.lastISO ? Date.parse(refresher.lastISO) : 0);
      if (idle > REFRESH_INTERVAL_MS) refreshAll({ reason: 'visibility' });
    }
  });
  scheduleNextRefresh();
}

// ---------- Powered-by label ----------------------------------------------

async function initPoweredBy() {
  const el = $('#powered-by-name');
  if (!el) return;

  const wrap = el.closest('.powered-by');
  const ttTitle = $('#pbt-title');
  const ttBody = $('#pbt-body');

  const setState = ({ state, model, detail }) => {
    el.classList.remove('powered-on', 'powered-off', 'powered-error');
    const pretty = prettyModelName(model || 'claude-sonnet-4-5');
    if (state === 'on') {
      el.textContent = pretty;
      el.classList.add('powered-on');
      if (wrap) wrap.dataset.state = 'on';
      if (ttTitle) ttTitle.textContent = `${pretty} — online`;
      if (ttBody) ttBody.innerHTML =
        'Live calls to the Anthropic API are working. The ' +
        '<b>Polish with Claude</b> button, the <b>Daily desk brief</b> ' +
        'card, and the <b>Ask about this trade</b> box are all enabled.';
    } else if (state === 'error') {
      el.textContent = `${pretty} (error)`;
      el.classList.add('powered-error');
      if (wrap) wrap.dataset.state = 'error';
      if (ttTitle) ttTitle.textContent = `${pretty} — call failing`;
      if (ttBody) ttBody.innerHTML =
        'The API key is accepted but the last request was rejected:' +
        `<br/><span class="tt-reason mono">${escapeHtml(detail || 'unknown error')}</span><br/>` +
        'The deterministic analysis is unaffected — only Claude-powered ' +
        'features (polish / brief / Q&amp;A) are paused until this clears.';
    } else {
      el.textContent = `${pretty} (offline)`;
      el.classList.add('powered-off');
      if (wrap) wrap.dataset.state = 'off';
      if (ttTitle) ttTitle.textContent = `${pretty} — offline`;
      if (ttBody) ttBody.innerHTML =
        '"Offline" means the app has no <code>ANTHROPIC_API_KEY</code> ' +
        'set, so it will never call Anthropic. Every deterministic ' +
        'feature (dashboard, signals, analyst report, charts, ' +
        'recommended option strategy) still works.<br/><br/>' +
        'To enable Claude:<br/>' +
        '<span class="tt-cmd mono">export ANTHROPIC_API_KEY=sk-ant-…</span><br/>' +
        '<span class="tt-cmd mono">./run.sh prod</span>';
    }
  };

  try {
    const cfg = await api('/api/analyst/llm-config');
    if (cfg.enabled && cfg.last_error) {
      setState({ state: 'error', model: cfg.model, detail: cfg.last_error });
    } else if (cfg.enabled) {
      setState({ state: 'on', model: cfg.model });
    } else {
      setState({ state: 'off', model: cfg.model });
    }
  } catch (e) {
    setState({ state: 'off', model: 'claude-sonnet-4-5' });
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function prettyModelName(slug) {
  if (!slug) return 'Claude Sonnet 4.5';
  // "claude-sonnet-4-5" -> "Claude Sonnet 4.5"
  const parts = slug.split('-');
  if (parts[0] !== 'claude') return slug;
  const family = parts[1] || '';
  const major = parts[2] || '';
  const minor = parts[3] || '';
  const ver = minor ? `${major}.${minor}` : major;
  return `Claude ${family.charAt(0).toUpperCase() + family.slice(1)} ${ver}`.trim();
}

// ---------- Screener view (jumps / dips / upcoming earnings) -------------

const screenerUI = {
  inFlight: false,
  lastLoadedAt: null,
  loadSeq: 0,
};

async function loadScreener() {
  if (screenerUI.inFlight) return;
  screenerUI.inFlight = true;
  const seq = ++screenerUI.loadSeq;
  const trail = $('#screener-trail');
  const scopePill = $('#screener-scope');
  const hint = $('#screener-movers-hint');
  const jumpsEl = $('#screener-jumps');
  const dipsEl = $('#screener-dips');
  const earnEl = $('#screener-earnings');
  const jumpsTrail = $('#screener-jumps-trail');
  const dipsTrail = $('#screener-dips-trail');
  const earnTrail = $('#screener-earnings-trail');

  const setHint = (on) => {
    if (hint) {
      if (on) hint.removeAttribute('hidden');
      else hint.setAttribute('hidden', '');
    }
  };

  try {
    // Phase 1: instant curated movers (tracked universe) + earnings in parallel.
    const [fastMovers, earn] = await Promise.all([
      api('/api/screener/movers?quick=1&limit=10'),
      api(
        `/api/screener/earnings?window_days=${SCREENER_EARNINGS_WINDOW_DAYS}&limit=50`
      ),
    ]);
    if (seq !== screenerUI.loadSeq) return;
    renderScreenerMovers(jumpsEl, fastMovers.jumps.rows, 'pos');
    renderScreenerMovers(dipsEl, fastMovers.dips.rows, 'neg');
    renderScreenerEarnings(earnEl, earn.rows);

    if (jumpsTrail) jumpsTrail.textContent = 'top gainers today · quick list';
    if (dipsTrail) dipsTrail.textContent = 'top losers today · quick list';
    if (earnTrail) {
      earnTrail.textContent = sourceLabel(
        earn.source,
        `next ${SCREENER_EARNINGS_WINDOW_DAYS} days`
      );
    }
    if (scopePill) {
      scopePill.textContent = 'Tracked';
      scopePill.classList.add('is-fallback');
    }
    if (trail) {
      trail.textContent = 'partial · ' + fmtESTCompact(new Date());
    }
    setHint(true);

    // Phase 2: full S&P 500 (or curated fallback) — single batched yfinance in backend.
    try {
      const fullMovers = await api('/api/screener/movers?limit=10');
      if (seq !== screenerUI.loadSeq) return;
      renderScreenerMovers(jumpsEl, fullMovers.jumps.rows, 'pos');
      renderScreenerMovers(dipsEl, fullMovers.dips.rows, 'neg');
      if (jumpsTrail) {
        jumpsTrail.textContent = sourceLabel(
          fullMovers.jumps.source,
          'top gainers today'
        );
      }
      if (dipsTrail) {
        dipsTrail.textContent = sourceLabel(
          fullMovers.dips.source,
          'top losers today'
        );
      }
      if (scopePill) {
        const overall = pickOverallSource([
          fullMovers.jumps.source,
          fullMovers.dips.source,
          earn.source,
        ]);
        scopePill.textContent =
          overall === 'sp500'
            ? 'S&P 500'
            : overall === 'curated'
            ? 'Tracked'
            : 'Limited';
        scopePill.classList.toggle('is-fallback', overall !== 'sp500');
      }
      screenerUI.lastLoadedAt = new Date();
      if (trail) {
        trail.textContent = 'updated ' + fmtESTCompact(screenerUI.lastLoadedAt);
      }
    } catch (e2) {
      if (seq === screenerUI.loadSeq) {
        if (trail) {
          trail.textContent =
            'partial (S&P 500 list unavailable) · ' +
            fmtESTCompact(new Date());
        }
      }
    } finally {
      if (seq === screenerUI.loadSeq) setHint(false);
    }
  } catch (e) {
    if (seq === screenerUI.loadSeq) {
      if (jumpsEl) {
        jumpsEl.innerHTML = `<li class="loading">Failed: ${escapeHtml(String(e))}</li>`;
      }
      if (dipsEl) dipsEl.innerHTML = '';
      if (earnEl) earnEl.innerHTML = '';
    }
  } finally {
    if (seq === screenerUI.loadSeq) {
      screenerUI.inFlight = false;
    }
  }
}

function sourceLabel(source, defaultText) {
  if (!source || source === 'sp500') return defaultText;
  if (source === 'curated') return defaultText + ' · curated fallback';
  if (source === 'unavailable') return 'data source unavailable';
  return defaultText;
}

function pickOverallSource(sources) {
  if (sources.every((s) => s === 'sp500')) return 'sp500';
  if (sources.includes('unavailable')) return 'unavailable';
  return 'curated';
}

function renderScreenerMovers(ul, rows, side) {
  if (!ul) return;
  ul.innerHTML = '';
  if (!rows || !rows.length) {
    ul.append(h('li', { class: 'loading' }, 'No movers in this direction today.'));
    return;
  }
  for (const r of rows) {
    const pct = (r.change_pct >= 0 ? '+' : '') + r.change_pct.toFixed(2) + '%';
    const li = h(
      'li',
      {
        class: 'screener-row',
        onClick: () => switchView('detail', { symbol: r.symbol }),
        title: `${r.symbol} · ${r.name} · ${r.verdict}`,
      },
      h('span', { class: 'screener-sym mono' }, r.symbol),
      h('span', { class: 'screener-name muted' }, r.name),
      h('span', { class: `screener-pct mono ${side}` }, pct),
      h('span', { class: 'screener-last mono muted' }, fmtUSD(r.last)),
      h(
        'span',
        { class: `screener-verdict mono ${verdictTone(r.verdict)}` },
        r.verdict || '—'
      )
    );
    ul.append(li);
  }
}

function renderScreenerEarnings(ul, rows) {
  if (!ul) return;
  ul.innerHTML = '';
  if (!rows || !rows.length) {
    ul.append(
      h(
        'li',
        { class: 'loading' },
        'No earnings dates available right now (yfinance offline or none in window).'
      )
    );
    return;
  }
  for (const r of rows) {
    const when = formatEarningsDate(r.earnings_date, r.days_until);
    const pctTone =
      r.change_pct == null ? 'muted' : r.change_pct >= 0 ? 'pos' : 'neg';
    const pctTxt =
      r.change_pct == null
        ? '—'
        : (r.change_pct >= 0 ? '+' : '') + r.change_pct.toFixed(2) + '%';
    const li = h(
      'li',
      {
        class: 'screener-row screener-row-earn',
        onClick: () => switchView('detail', { symbol: r.symbol }),
        title: `${r.symbol} · ${r.name} · reports ${r.earnings_date}`,
      },
      h('span', { class: 'screener-sym mono' }, r.symbol),
      h('span', { class: 'screener-name muted' }, r.name),
      h('span', { class: 'screener-when mono' }, when),
      h('span', { class: `screener-pct mono ${pctTone}` }, pctTxt),
      h(
        'span',
        { class: `screener-verdict mono ${verdictTone(r.verdict)}` },
        r.verdict || '—'
      )
    );
    ul.append(li);
  }
}

function verdictTone(verdict) {
  if (!verdict) return 'muted';
  const v = String(verdict).toUpperCase();
  if (v === 'BULLISH') return 'pos';
  if (v === 'BEARISH') return 'neg';
  return 'muted';
}

function formatEarningsDate(iso, daysUntil) {
  if (!iso) return '—';
  let dateLabel = iso;
  try {
    const d = new Date(iso + 'T00:00:00Z');
    if (!Number.isNaN(d.getTime())) {
      dateLabel = new Intl.DateTimeFormat('en-US', {
        timeZone: 'UTC',
        month: 'short',
        day: '2-digit',
        weekday: 'short',
      }).format(d);
    }
  } catch (_) {
    /* fall through to ISO */
  }
  if (daysUntil == null) return dateLabel;
  if (daysUntil <= 0) return `${dateLabel} · today`;
  if (daysUntil === 1) return `${dateLabel} · tomorrow`;
  return `${dateLabel} · in ${daysUntil}d`;
}

// ---------- Search view (free-form stock lookup) --------------------------

const searchUI = {
  initialized: false,
  timeframe: 'daily',
  lastQuery: '',
  suggestTimer: null,
  suggestIndex: -1,
  suggestions: [],
  inFlight: false,
  lastTickerDetail: null,
  tickerDetails: {},
  lastQueryResolvedSymbol: null,
  lastSearchPayload: null,
  _lastSearchChartSymbol: null,
};

function initSearchOnce() {
  if (searchUI.initialized) return;
  searchUI.initialized = true;
  initSearchChartToolbar();

  const input = $('#search-input');
  const suggest = $('#search-suggest');
  const go = $('#search-go');

  $$('.search-tf .pill').forEach((p) => {
    p.addEventListener('click', () => {
      searchUI.timeframe = p.dataset.sfTf;
      $$('.search-tf .pill').forEach((x) => x.classList.toggle('is-active', x === p));
      if (searchUI.lastQuery) runSearch(searchUI.lastQuery);
    });
  });

  $$('.chip-ex').forEach((c) => {
    c.addEventListener('click', () => {
      input.value = c.dataset.ex;
      runSearch(c.dataset.ex);
    });
  });

  input.addEventListener('input', () => {
    const q = input.value.trim();
    clearTimeout(searchUI.suggestTimer);
    searchUI.suggestIndex = -1;
    if (q.length < 1) {
      hideSuggest();
      return;
    }
    searchUI.suggestTimer = setTimeout(() => loadSuggestions(q), 180);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveSuggest(+1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveSuggest(-1);
    } else if (e.key === 'Enter') {
      const sel = searchUI.suggestions[searchUI.suggestIndex];
      const q = sel ? sel.symbol : input.value.trim();
      if (sel) input.value = sel.symbol;
      if (q) runSearch(q);
      hideSuggest();
    } else if (e.key === 'Escape') {
      hideSuggest();
    }
  });
  input.addEventListener('blur', () => {
    setTimeout(hideSuggest, 120); // allow click on suggestion to register
  });
  go.addEventListener('click', () => {
    const q = input.value.trim();
    if (q) runSearch(q);
  });

  input.focus();
}

async function loadSuggestions(q) {
  try {
    const rows = await api(`/api/search/suggest?q=${encodeURIComponent(q)}&limit=6`);
    renderSuggest(rows);
  } catch (e) {
    hideSuggest();
  }
}

function renderSuggest(rows) {
  const host = $('#search-suggest');
  const wrap = host.parentElement;
  host.innerHTML = '';
  searchUI.suggestions = rows || [];
  searchUI.suggestIndex = -1;
  if (!rows || !rows.length) {
    hideSuggest();
    return;
  }
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    const li = h(
      'li',
      {
        class: 'search-sugg-row',
        role: 'option',
        onMousedown: (e) => {
          e.preventDefault(); // don't blur the input before click fires
          $('#search-input').value = r.symbol;
          runSearch(r.symbol);
          hideSuggest();
        },
        onMouseenter: () => highlightSuggest(i),
      },
      h('span', { class: 'ss-sym mono' }, r.symbol),
      h('span', { class: 'ss-name' }, r.name),
      h('span', { class: 'ss-sector muted' }, r.sector || '—')
    );
    host.append(li);
  }
  host.classList.remove('hidden');
  if (wrap) wrap.setAttribute('aria-expanded', 'true');
}

function moveSuggest(delta) {
  if (!searchUI.suggestions.length) return;
  const n = searchUI.suggestions.length;
  let i = searchUI.suggestIndex + delta;
  if (i < 0) i = n - 1;
  if (i >= n) i = 0;
  highlightSuggest(i);
}

function highlightSuggest(i) {
  searchUI.suggestIndex = i;
  const rows = $$('#search-suggest .search-sugg-row');
  rows.forEach((r, idx) => r.classList.toggle('is-active', idx === i));
}

function hideSuggest() {
  const host = $('#search-suggest');
  if (!host) return;
  host.classList.add('hidden');
  const wrap = host.parentElement;
  if (wrap) wrap.setAttribute('aria-expanded', 'false');
}

async function runSearch(query) {
  if (searchUI.inFlight) return;
  searchUI.inFlight = true;
  searchUI.lastQuery = query;
  hideSuggest();

  const host = $('#search-result');
  host.innerHTML = '<div class="callout"><div class="callout-title">Analyzing…</div>' +
    `Running technicals on <span class="mono">${escapeHtml(query)}</span> (${searchUI.timeframe}). ` +
    'Pulling live data, computing SMA/RSI/MACD/ATR, and building the plan.</div>';

  try {
    const r = await api(
      `/api/search?q=${encodeURIComponent(query)}&timeframe=${encodeURIComponent(searchUI.timeframe)}`
    );
    const sym = r.resolved && r.resolved.symbol;
    if (searchUI._lastSearchChartSymbol != null && sym && searchUI._lastSearchChartSymbol !== sym) {
      state.searchChartSessionZoom = { start: 0, end: 1 };
    }
    if (sym) searchUI._lastSearchChartSymbol = sym;
    searchUI.lastSearchPayload = r;
    searchUI.lastQueryResolvedSymbol = sym || null;
    let td = null;
    if (sym) {
      try {
        td = await loadSearchTickerDetail(sym);
      } catch (e) {
        td = null;
      }
    }
    searchUI.lastTickerDetail = td;
    renderSearchResult(r, td);
  } catch (e) {
    host.innerHTML =
      `<div class="callout callout-warning">` +
      `<div class="callout-title">No match for &ldquo;${escapeHtml(query)}&rdquo;</div>` +
      `Couldn't resolve that to a ticker, or we didn't have enough price ` +
      `history to run the analysis. Details: <span class="mono">${escapeHtml(String(e))}</span>` +
      `</div>`;
  } finally {
    searchUI.inFlight = false;
  }
}

function renderSearchResult(r, tickerD) {
  const host = $('#search-result');
  host.innerHTML = '';
  const resolved = r.resolved;
  const plan = r.stock_plan;
  const report = r.report;
  searchUI.lastSearchPayload = r;
  searchUI.lastQueryResolvedSymbol = resolved.symbol;
  if (tickerD) searchUI.lastTickerDetail = tickerD;

  const actionCls = plan.action === 'Buy'  ? 'action-buy'
                  : plan.action === 'Sell' ? 'action-sell'
                  : 'action-hold';

  // Row 1: hero — symbol, name, last, change, source chip.
  host.append(
    h('div', { class: 'search-hero-card' },
      h('div', { class: 'shc-left' },
        h('div', { class: 'shc-sym' }, resolved.symbol),
        h('div', { class: 'shc-name' },
          resolved.name,
          h('span', { class: 'shc-sector muted' },
            ' · ', resolved.sector || '—',
            resolved.exchange ? ` · ${resolved.exchange}` : ''
          )
        )
      ),
      h('div', { class: 'shc-right' },
        h('div', { class: 'shc-price' }, fmtUSD(report.price_action.last)),
        h('div', {
          class: 'shc-change ' + (report.price_action.change_pct >= 0 ? 'pos' : 'neg'),
        }, fmtPct(report.price_action.change_pct)),
        h('div', { class: 'shc-meta mono muted' },
          `${report.timeframe} · source ${report.source} · ${report.as_of.slice(0,10)}`
        )
      )
    )
  );

  // Row 2: big recommendation card.
  const rr = plan.risk_reward != null ? `${plan.risk_reward.toFixed(2)}×` : '—';
  host.append(
    h('div', { class: `rec-card ${actionCls}` },
      h('div', { class: 'rec-head' },
        h('div', { class: 'rec-action-wrap' },
          h('span', { class: `rec-action ${actionCls}` }, plan.action.toUpperCase()),
          h('span', { class: 'rec-conf mono' },
            `conviction ${Math.round((plan.confidence || 0) * 100)}%`
          )
        ),
        h('div', { class: 'rec-horizon mono muted' }, `Horizon · ${plan.time_horizon}`)
      ),
      h('div', { class: 'rec-grid' },
        recTile('Entry',    fmtUSD(plan.entry_price),
          plan.entry_zone_low !== plan.entry_zone_high
            ? `zone ${fmtUSD(plan.entry_zone_low)} – ${fmtUSD(plan.entry_zone_high)}`
            : 'market'
        ),
        recTile('Target (exit)', fmtUSD(plan.target_price),
          `+${Math.abs((plan.target_price - plan.entry_price) / plan.entry_price * 100).toFixed(2)}% vs entry`,
          'tile-pos'
        ),
        recTile('Stop loss', fmtUSD(plan.stop_loss),
          `-${Math.abs((plan.entry_price - plan.stop_loss) / plan.entry_price * 100).toFixed(2)}% vs entry`,
          'tile-neg'
        ),
        recTile('Risk / reward', rr,
          `expected move ±${(plan.expected_move_pct || 0).toFixed(2)}%`
        )
      ),
      h('div', { class: 'rec-rationale' }, plan.rationale),
      plan.caveats && plan.caveats.length
        ? h('ul', { class: 'rec-caveats' },
            ...plan.caveats.map((c) => h('li', {}, c))
          )
        : null
    )
  );

  // Row 3: Yahoo-style OHLC (same as Ticker detail / Analyst)
  const sRangePills = [
    ['1d', '1D'],
    ['5d', '5D'],
    ['1m', '1M'],
    ['6m', '6M'],
    ['1y', '1Y'],
    ['ytd', 'YTD'],
  ];
  const sViewPills = [
    ['mountain', 'Mountain'],
    ['candle', 'Candle'],
    ['line', 'Line'],
    ['bar', 'Bar (OHLC)'],
  ];
  host.append(
    h(
      'div',
      { class: 'card card-lg' },
      h(
        'div',
        { class: 'card-header card-header--chart' },
        h(
          'div',
          { class: 'card-header-titles' },
          h('span', { class: 'card-title', id: 'search-price-card-title' },
            `${resolved.symbol} · price`,
          ),
          h(
            'span',
            { class: 'card-trail mono', id: 'search-price-card-trail' },
            `${report.chart.close.length} bars · ${report.timeframe}`,
          ),
        ),
        h(
          'button',
          {
            type: 'button',
            class: 'btn-ghost btn-enlarge-chart',
            id: 'search-btn-enlarge-chart',
            hidden: true,
          },
          'Enlarge chart',
        ),
      ),
      h(
        'div',
        { class: 'card-body card-body--ticker-chart' },
        h(
          'div',
          { class: 'detail-chart-toolbar', id: 'search-chart-toolbar' },
          h(
            'div',
            { class: 'detail-chart-toolbar__row', role: 'group', 'aria-label': 'Chart range' },
            h('span', { class: 'detail-chart-toolbar__label muted' }, 'Range'),
            h(
              'div',
              { class: 'pill-row' },
              ...sRangePills.map(([rk, lab]) =>
                h(
                  'button',
                  {
                    type: 'button',
                    class: 'pill' + (state.searchChart.range === rk ? ' is-active' : ''),
                    'data-search-range': rk,
                  },
                  lab,
                ),
              ),
            ),
          ),
          h(
            'div',
            { class: 'detail-chart-toolbar__row', role: 'group', 'aria-label': 'Chart style' },
            h('span', { class: 'detail-chart-toolbar__label muted' }, 'View'),
            h(
              'div',
              { class: 'pill-row' },
              ...sViewPills.map(([vk, lab]) =>
                h(
                  'button',
                  {
                    type: 'button',
                    class: 'pill' + (state.searchChart.view === vk ? ' is-active' : ''),
                    'data-search-view': vk,
                  },
                  lab,
                ),
              ),
            ),
          ),
        ),
        h('div', { id: 'search-price-chart' },
          h('div', { class: 'chart-placeholder' }, tickerD ? '…' : 'Loading…')),
      ),
    ),
  );
  if (tickerD) {
    renderPriceChart(tickerD, 'search');
  } else {
    const ph = $('#search-price-chart');
    if (ph) {
      ph.innerHTML = '';
      ph.append(
        h('div', { class: 'chart-placeholder' },
          'Could not load price chart for this symbol.',
        ),
      );
    }
  }
  syncSearchChartToolbar();

  // Row 4: compact technical summary from the report.
  host.append(
    h('div', { class: 'search-tech' },
      techBox('Trend',  (report.price_action.trend || '—').toUpperCase()),
      techBox('RSI(14)',
        report.rsi.value != null ? report.rsi.value.toFixed(0) : '—',
        report.rsi.state),
      techBox('Stoch(14/3/3)',
        report.stochastic.pct_k != null && report.stochastic.pct_d != null
          ? `${report.stochastic.pct_k.toFixed(0)}/${report.stochastic.pct_d.toFixed(0)}`
          : '—',
        report.stochastic.state || ''),
      techBox('SMA 50 / 200',
        (report.sma.sma50 != null ? fmtUSD(report.sma.sma50) : '—') +
        ' / ' +
        (report.sma.sma200 != null ? fmtUSD(report.sma.sma200) : '—'),
        report.sma.stack),
      techBox('MACD hist',
        report.macd.histogram != null ? report.macd.histogram.toFixed(3) : '—',
        report.macd.state),
      techBox('ADX(14)',
        report.adx.value != null ? report.adx.value.toFixed(0) : '—',
        report.adx.directional_bias || ''),
      techBox('BB(20)',
        report.bollinger.pct_b != null ? report.bollinger.pct_b.toFixed(2) : '—',
        report.bollinger.position),
      techBox('ATR(14)',
        report.atr.value != null ? report.atr.value.toFixed(2) : '—',
        report.atr.regime),
      techBox('Net bias',
        report.bull_pct != null && report.bear_pct != null
          ? `${report.bull_pct.toFixed(0)}% bull / ${report.bear_pct.toFixed(0)}% bear`
          : '—',
        'from composite −1…+1',
        'tech-box--net-bias'),
      techBox('Verdict', report.verdict,
        `composite ${fmtScore(report.composite_score || 0)} · conv ${Math.round((report.conviction || 0) * 100)}%`)
    )
  );

  // Row 5: full written narrative from the analyst.
  host.append(
    h('div', { class: 'section-header' },
      h('h2', {}, 'Full technical analysis'),
      h('span', { class: 'section-sub mono' },
        `${resolved.symbol} · ${report.timeframe}`)
    ),
    h('div', { class: 'report-narrative' },
      ...(report.narrative || '').split('\n\n').map((p) => h('p', {}, p))
    ),
    h('div', { class: 'callout callout-warning' },
      h('div', { class: 'callout-title' }, 'Not financial advice'),
      'This is a deterministic, rule-based technical plan from price ' +
      'action and indicator state — not a forecast and not tailored to your ' +
      'account size, risk tolerance, or portfolio. Validate with your own ' +
      'risk management before acting.'
    )
  );
}

function recTile(label, value, sub, extra = '') {
  return h('div', { class: `rec-tile ${extra}` },
    h('div', { class: 'rec-tile-label' }, label),
    h('div', { class: 'rec-tile-value mono' }, value),
    h('div', { class: 'rec-tile-sub muted' }, sub || '')
  );
}

function techBox(label, value, sub = '', extraClass = '') {
  return h('div', { class: 'tech-box' + (extraClass ? ` ${extraClass}` : '') },
    h('div', { class: 'tech-label muted' }, label),
    h('div', { class: 'tech-value mono' }, value),
    sub ? h('div', { class: 'tech-sub muted' }, sub) : null
  );
}

// ---------- Signal report download ------------------------------------------
//
// Backed by the /api/report/signals* endpoints. The Markdown variant is the
// primary deliverable — readable as-is, paste-able into Slack / email, and
// versionable. JSON is for external tools; TXT is for terminals. "View in
// browser" opens the Markdown endpoint in a new tab so the user can read it
// without downloading.

function initSignalReportMenu() {
  const wrap = document.getElementById('download-report');
  if (!wrap) return;
  const host = wrap.closest('.report-download');
  if (!host) return;

  function currentTimeframe() {
    if (analyst.timeframe) return analyst.timeframe;
    const active = document.querySelector('.view-analyst .analyst-timeframes .pill.is-active[data-tf]');
    return (active && active.dataset.tf) || 'daily';
  }

  function openMenu() { host.classList.add('is-open'); }
  function closeMenu() { host.classList.remove('is-open'); }
  function toggleMenu() { host.classList.toggle('is-open'); }

  wrap.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    toggleMenu();
  });

  document.addEventListener('click', (e) => {
    if (!host.contains(e.target)) closeMenu();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeMenu();
  });

  host.querySelectorAll('.report-menu a').forEach((a) => {
    a.addEventListener('click', (e) => {
      e.preventDefault();
      const kind = a.dataset.report;
      const tf = currentTimeframe();
      const stamp = new Date().toISOString().slice(0, 10);

      if (kind === 'md') {
        // Use the download flag so the browser saves the file with a
        // dated, human-friendly name set by the server.
        window.location.href = `/api/report/signals.md?timeframe=${tf}&download=true`;
      } else if (kind === 'json') {
        downloadBlob(
          `/api/report/signals?timeframe=${tf}`,
          `sianna_signal_report_${tf}_${stamp}.json`,
          'application/json',
        );
      } else if (kind === 'txt') {
        downloadBlob(
          `/api/report/signals.txt?timeframe=${tf}`,
          `sianna_signal_report_${tf}_${stamp}.txt`,
          'text/plain',
        );
      } else if (kind === 'view') {
        window.open(`/api/report/signals.md?timeframe=${tf}`, '_blank', 'noopener');
      }
      closeMenu();
    });
  });
}

async function downloadBlob(url, filename, mime) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    const blob = new Blob([text], { type: mime });
    const href = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(href);
  } catch (err) {
    alert(`Report download failed: ${err.message || err}`);
  }
}

// ---------- Boot ------------------------------------------------------------

function showClientError(context, err) {
  const msg = err && err.message != null ? String(err.message) : String(err);
  const bar = document.getElementById('app-client-error');
  if (bar) {
    bar.textContent = `${context}: ${msg}`;
    bar.classList.add('is-visible');
    bar.removeAttribute('hidden');
    return;
  }
  // Fallback if the banner was removed from the template.
  console.error(context, err);
}

function boot() {
  try {
    initTabs();
    initFilters();
    startESTClock();
    initChartEnlargeModal();
    initDetailChartToolbar();
    void initPoweredBy();
    void loadDashboard();
    initRefresh();
    initSignalReportMenu();
  } catch (e) {
    showClientError('App failed to start', e);
  }
}

window.addEventListener('error', (e) => {
  if (e.error) {
    showClientError('Script error', e.error);
  } else {
    showClientError('Script error', new Error(e.message));
  }
});

// Log (do not show a second banner) — parallel API failures already
// report inside each card, and a global banner per rejection is noisy.
window.addEventListener('unhandledrejection', (e) => {
  console.error('unhandledrejection', e.reason);
});

boot();
