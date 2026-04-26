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

async function api(path) {
  const controller = new AbortController();
  const t = setTimeout(() => controller.abort(), API_FETCH_TIMEOUT_MS);
  try {
    const r = await fetch(path, { signal: controller.signal });
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
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

// ---------- Application state ----------------------------------------------

const state = {
  view: 'dashboard',
  filter: 'all',
  activeSymbol: null,
  universe: [], // all symbols (for the detail chip row)
  details: {}, // symbol -> cached detail payload
};

// ---------- Router / nav ----------------------------------------------------

function switchView(view, opts = {}) {
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
      } else if (target === 'search') {
        switchView('search');
        initSearchOnce();
      } else if (target === 'screener') {
        switchView('screener');
        loadScreener();
      } else if (target === 'etf') {
        switchView('etf');
        initEtfOnce();
      } else if (target === 'copy-trade') {
        switchView('copy-trade');
        initCopyTradeOnce();
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
      const side7 = c.change_pct_7d >= 0 ? 'pos' : 'neg';
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
          `7d ${c.change_pct_7d >= 0 ? '+' : ''}${c.change_pct_7d.toFixed(2)}%`)
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
  const m = {
    'cnbc-rss': 'CNBC',
    'marketwatch-rss': 'MarketWatch',
    'internal-snapshot': 'Sianna (snapshot)',
  };
  return m[src] != null ? m[src] : src;
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
  } catch (e) {
    ul.innerHTML = `<li class="loading">Failed: ${escapeHtml(String(e))}</li>`;
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
  state.activeSymbol = symbol;
  renderChipRow();

  let detail = state.details[symbol];
  if (!detail) {
    $('#detail-hero').innerHTML = '<div class="hero-loading mono">Loading ' + symbol + '…</div>';
    try {
      detail = await api(`/api/ticker/${encodeURIComponent(symbol)}`);
      state.details[symbol] = detail;
    } catch (e) {
      $('#detail-hero').innerHTML = `<div class="hero-loading mono">Failed: ${e}</div>`;
      return;
    }
  }
  renderDetail(detail);
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
  renderRecommender(d);
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

function renderPriceChart(d) {
  const host = $('#price-chart');
  host.innerHTML = '';
  const series = d.price_30d || [];
  if (series.length < 2) {
    host.append(h('div', { class: 'chart-placeholder' }, 'No data'));
    return;
  }
  host.append(fancyPriceChart(series, d.row.direction));
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

function fancyPriceChart(series, direction) {
  const w = 880;
  const h0 = 260;
  const pad = { top: 18, right: 18, bottom: 26, left: 58 };
  const innerW = w - pad.left - pad.right;
  const innerH = h0 - pad.top - pad.bottom;
  const n = series.length;

  // Overlays — compute from closes only (no extra API payload required).
  const sma5 = _sma(series, Math.min(5, n - 1));
  const sma10 = _sma(series, Math.min(10, n - 1));
  const sma20 = _sma(series, Math.min(20, n - 1));
  const ema9 = _ema(series, Math.min(9, n - 1));
  const bandWin = Math.min(20, n - 1);
  const bb = _bollinger(series, bandWin, 2);

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

  const root = svg('svg', {
    class: 'fancy-chart',
    viewBox: `0 0 ${w} ${h0}`,
    preserveAspectRatio: 'none',
    style: `height: ${h0}px;`,
  });

  // Defs: gradient for area fill + clip path.
  const defs = svg('defs');
  const gradId = 'grad-' + Math.random().toString(36).slice(2, 8);
  const grad = svg('linearGradient', { id: gradId, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(
    svg('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': 0.35 }),
    svg('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0 }),
  );
  defs.append(grad);
  root.append(defs);

  // Horizontal gridlines + y-axis labels
  for (let g = 0; g <= 4; g++) {
    const v = lo + (range * g) / 4;
    const y = pad.top + innerH - ((v - lo) / range) * innerH;
    root.append(
      svg('line', {
        x1: pad.left, y1: y, x2: w - pad.right, y2: y,
        stroke: 'var(--stroke-soft)', 'stroke-dasharray': '2 4',
      }),
      svg('text', {
        x: pad.left - 8, y: y + 3,
        'text-anchor': 'end',
        fill: 'var(--text-dim)',
        'font-size': 10,
        'font-family': 'var(--mono)',
      }, '$' + v.toFixed(v > 100 ? 0 : 2))
    );
  }

  // X-axis ticks (roughly 6 labels showing session N)
  const nTicks = Math.min(6, n);
  for (let t = 0; t < nTicks; t++) {
    const idx = Math.round(((n - 1) * t) / (nTicks - 1));
    const x = pad.left + idx * xStep;
    root.append(
      svg('text', {
        x, y: h0 - 6,
        'text-anchor': 'middle',
        fill: 'var(--text-dim)',
        'font-size': 10,
        'font-family': 'var(--mono)',
      }, `d${idx - (n - 1)}`)
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
  if (bandD) {
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
  root.append(svg('path', {
    d: toPath(sma20),
    fill: 'none', stroke: '#8b9dd9', 'stroke-width': 1.1, 'stroke-opacity': 0.85,
  }));
  root.append(svg('path', {
    d: toPath(sma10),
    fill: 'none', stroke: '#d9a36b', 'stroke-width': 1.2, 'stroke-opacity': 0.9,
  }));
  root.append(svg('path', {
    d: toPath(ema9),
    fill: 'none', stroke: '#bf7af0', 'stroke-width': 1.2,
    'stroke-dasharray': '4 2', 'stroke-opacity': 0.85,
  }));
  root.append(svg('path', {
    d: toPath(sma5),
    fill: 'none', stroke: '#f2d47a', 'stroke-width': 1.2, 'stroke-opacity': 0.85,
  }));

  // Price line on top
  root.append(svg('path', {
    d: lineD, fill: 'none', stroke: color, 'stroke-width': 2,
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

  const wrap = h('div', { class: 'chart-wrap' }, root, tip,
    h('div', { class: 'chart-legend mono' },
      legendSwatch(color, 'Close'),
      legendSwatch('#f2d47a', `SMA${Math.min(5, n - 1)}`),
      legendSwatch('#d9a36b', `SMA${Math.min(10, n - 1)}`),
      legendSwatch('#8b9dd9', `SMA${Math.min(20, n - 1)}`),
      legendSwatch('#bf7af0', `EMA${Math.min(9, n - 1)}`, true),
      legendSwatch('var(--info)', `BB(${bandWin},2)`, false, true),
    ),
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
    const rectWrap = wrap.getBoundingClientRect();
    const relX = (x / w) * rectWrap.width;
    const relY = (y / h0) * rectWrap.height;
    const tipW = 160;
    tip.style.left = `${Math.min(rectWrap.width - tipW - 8, Math.max(8, relX + 12))}px`;
    tip.style.top = `${Math.max(8, relY - 60)}px`;
  });
  hitArea.addEventListener('mouseleave', () => {
    crossG.setAttribute('opacity', 0);
    tip.classList.add('hidden');
  });

  return wrap;
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
  host.append(factorBars(d.factors));
}

function factorBars(factors) {
  const w = 400;
  const h0 = 200;
  const pad = { top: 10, right: 40, bottom: 10, left: 110 };
  const innerW = w - pad.left - pad.right;
  const innerH = h0 - pad.top - pad.bottom;
  const rowH = innerH / factors.length;
  const cx = pad.left + innerW / 2;

  const maxMag = Math.max(1, ...factors.map((f) => Math.abs(f.score)));

  const root = svg(
    'svg',
    { class: 'factor-bars', viewBox: `0 0 ${w} ${h0}`, preserveAspectRatio: 'none' }
  );

  // Center zero line
  root.append(
    svg('line', {
      x1: cx, y1: pad.top, x2: cx, y2: pad.top + innerH,
      stroke: 'var(--stroke)',
    })
  );

  factors.forEach((f, i) => {
    const y = pad.top + i * rowH + rowH * 0.18;
    const barH = rowH * 0.64;
    const barW = (Math.abs(f.score) / maxMag) * (innerW / 2 - 6);
    const color =
      f.score > 0.15 ? 'var(--success)' :
      f.score < -0.15 ? 'var(--danger)'  :
      'var(--text-dim)';

    // label
    root.append(
      svg(
        'text',
        {
          x: pad.left - 8,
          y: y + barH / 2 + 3,
          'text-anchor': 'end',
          fill: 'var(--text-muted)',
          'font-size': 11,
        },
        f.name
      )
    );
    // bar
    root.append(
      svg('rect', {
        x: f.score >= 0 ? cx : cx - barW,
        y,
        width: barW,
        height: barH,
        fill: color,
        'fill-opacity': 0.85,
        rx: 2,
      })
    );
    // value text
    root.append(
      svg(
        'text',
        {
          x: f.score >= 0 ? cx + barW + 4 : cx - barW - 4,
          y: y + barH / 2 + 3,
          'text-anchor': f.score >= 0 ? 'start' : 'end',
          fill: 'var(--text)',
          'font-size': 10,
          'font-family': 'var(--mono)',
        },
        (f.score >= 0 ? '+' : '') + f.score.toFixed(2)
      )
    );
  });

  return root;
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
  reports: {}, // key = sym|tf
  llm: { enabled: false, model: '' },
  polishCache: {}, // key = sym|tf -> polished narrative
  briefCache: {},  // key = tf -> brief text
};

const etf = {
  initialized: false,
  timeframe: 'daily',
  overview: [],
};

const copyTrade = {
  initialized: false,
  creators: [],
  activeId: null,
};

async function initAnalystOnce() {
  if (analyst.initialized) return;
  analyst.initialized = true;

  $$('.analyst-timeframes .pill').forEach((p) => {
    p.addEventListener('click', () => {
      if (analyst.timeframe === p.dataset.tf) return;
      analyst.timeframe = p.dataset.tf;
      $$('.analyst-timeframes .pill').forEach((x) =>
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

async function loadAnalystOverview() {
  const list = $('#overview-list');
  list.innerHTML = '<div class="muted" style="padding:10px 12px;">Loading…</div>';
  const allRec = $('#all-recs-tbody');
  if (allRec) allRec.innerHTML = '<tr><td colspan="9" class="loading">Computing all recommendations…</td></tr>';
  try {
    const rows = await api(
      `/api/analyst/overview?timeframe=${encodeURIComponent(analyst.timeframe)}`
    );
    analyst.overview = rows;
    renderOverviewList();
    renderAllRecsTable();
    renderTickerStrip();
    const firstSource = rows.find((r) => r.source)?.source || '—';
    $('#analyst-source').textContent = firstSource;
  } catch (e) {
    list.innerHTML = `<div class="muted" style="padding:10px 12px;">Failed: ${e}</div>`;
    if (allRec) allRec.innerHTML = `<tr><td colspan="9" class="loading">Failed: ${e}</td></tr>`;
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

async function initCopyTradeOnce() {
  if (copyTrade.initialized) return;
  copyTrade.initialized = true;
  const sel = $('#copytrade-select');
  const trail = $('#copytrade-trail');
  if (!sel) return;
  try {
    const list = await api('/api/copy-trade/creators');
    copyTrade.creators = list;
    sel.innerHTML = '';
    for (const c of list) {
      sel.append(h('option', { value: c.id }, c.name));
    }
    if (list.length) {
      copyTrade.activeId = list[0].id;
      sel.value = copyTrade.activeId;
    }
  } catch (e) {
    if (trail) trail.textContent = 'Failed to load creators: ' + e;
    return;
  }
  const btn = $('#copytrade-refresh');
  if (btn) {
    btn.addEventListener('click', () => loadCopyTradeData(true));
  }
  sel.addEventListener('change', () => {
    copyTrade.activeId = sel.value;
    loadCopyTradeData(true);
  });
  loadCopyTradeData(true);
}

async function loadCopyTradeData(refresh) {
  const id = copyTrade.activeId
    || (copyTrade.creators[0] && copyTrade.creators[0].id);
  if (!id) return;
  const tbody = $('#copytrade-holdings-tbody');
  const trail = $('#copytrade-trail');
  const ul = $('#copytrade-signals');
  if (tbody) {
    tbody.innerHTML =
      '<tr><td colspan="6" class="loading">Loading holdings…</td></tr>';
  }
  if (ul) ul.innerHTML = '<li class="loading">Loading activity…</li>';
  const q = refresh ? '?refresh=1' : '?refresh=0';
  try {
    const hold = await api(
      `/api/copy-trade/holdings/${encodeURIComponent(id)}${q}`
    );
    if (trail) {
      const parts = [
        `source: ${hold.source}`,
        hold.as_of ? `as of ${hold.as_of}` : null,
        hold.message ? hold.message : null,
      ].filter(Boolean);
      trail.textContent = parts.join(' · ');
    }
    if (tbody) {
      tbody.innerHTML = '';
      if (!hold.rows || !hold.rows.length) {
        tbody.append(
          h('tr', {},
            h('td', { colspan: 6, class: 'loading' },
              hold.as_of
                ? 'No rows (check SEC parse or use another portfolio).'
                : 'No data.'))
        );
      } else {
        for (const r of hold.rows) {
          const sym = r.symbol || '—';
          const open = (ev) => {
            if (r.symbol) {
              ev.stopPropagation();
              switchView('detail', { symbol: r.symbol });
            }
          };
          tbody.append(
            h('tr', {
              class: r.symbol ? 'clickable' : '',
              onClick: open,
            },
            h('td', { class: 'sym mono' }, sym),
            h('td', { class: 'name' }, r.name),
            h('td', { class: 'num mono' }, fmtUSD(r.value_usd)),
            h('td', { class: 'num mono' }, (r.weight_pct != null
              ? r.weight_pct.toFixed(2) : '—') + '%'),
            h('td', { class: 'num mono' }, (r.value_000s != null
              ? r.value_000s.toLocaleString() : '—')),
            h('td', { class: 'mono muted' }, r.cusip)
            )
          );
        }
      }
    }
    const sigs = await api(
      `/api/copy-trade/signals?creator_id=${encodeURIComponent(id)}&limit=25`
    );
    if (ul) {
      ul.innerHTML = '';
      const rows = (sigs && sigs.rows) || [];
      if (!rows.length) {
        ul.append(
          h('li', { class: 'muted' }, 'No change signals yet — refresh after a new filing is saved.')
        );
      } else {
        for (const s of rows) {
          const kind = s.kind || '';
          const kcls = kind === 'INCREASED' ? 'pos'
            : kind === 'DECREASED' || kind === 'EXIT' ? 'neg' : 'muted';
          ul.append(
            h('li', { class: 'copytrade-sig-item' },
              h('span', { class: `copytrade-sig-kind mono ${kcls}` }, kind),
              s.symbol
                ? h('span', { class: 'mono' }, s.symbol)
                : '',
              h('span', { class: 'copytrade-sig-msg' },
                escapeHtml(s.message || '')),
              s.as_of
                ? h('span', { class: 'muted mono' }, s.as_of)
                : ''
            )
          );
        }
      }
    }
  } catch (e) {
    if (tbody) {
      tbody.innerHTML = `<tr><td colspan="6" class="loading">Failed: ${escapeHtml(
        String(e)
      )}</td></tr>`;
    }
    if (trail) trail.textContent = 'Error: ' + e;
  }
}

async function loadAnalystReport(symbol) {
  analyst.activeSymbol = symbol;
  renderTickerStrip();
  renderOverviewList();

  const key = `${symbol}|${analyst.timeframe}`;
  let rpt = analyst.reports[key];
  if (!rpt) {
    $('#analyst-report').innerHTML =
      `<div class="callout"><div class="callout-title">Loading ${symbol} · ${analyst.timeframe}</div>` +
      `Computing indicators and composing forecast…</div>`;
    try {
      rpt = await api(
        `/api/analyst/report/${encodeURIComponent(symbol)}` +
        `?timeframe=${encodeURIComponent(analyst.timeframe)}`
      );
      analyst.reports[key] = rpt;
    } catch (e) {
      $('#analyst-report').innerHTML =
        `<div class="callout callout-warning"><div class="callout-title">Failed to load ${symbol}</div>${e}</div>`;
      return;
    }
  }
  renderAnalystReport(rpt);
}

function renderAnalystReport(r) {
  const root = $('#analyst-report');
  root.innerHTML = '';

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
        h('div', { class: 'report-hero-headline' }, r.headline)
      ),
      h(
        'div',
        { class: 'report-hero-meta' },
        heroMetaCard(fmtUSD(r.price_action.last), 'Last price'),
        heroMetaCard(fmtPct(r.price_action.change_pct), 'Change', r.price_action.change_pct >= 0 ? 'pos' : 'neg'),
        heroMetaCard(r.timeframe.toUpperCase(), 'Timeframe'),
        heroMetaCard(r.source, 'Source')
      )
    )
  );

  // Indicator grid
  const rsi = r.rsi;
  const macd = r.macd;
  const atr = r.atr;
  const smaB = r.sma;
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
        atr.pct_of_price != null ? `${atr.pct_of_price.toFixed(2)}% of spot` : '—',
        (atr.pct_of_price ?? 0) > 4 ? 'warning' : ''
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

  // Price chart card
  root.append(
    h(
      'div',
      { class: 'card chart-card' },
      h(
        'div',
        { class: 'card-header' },
        h('span', { class: 'card-title' },
          `Price · ${r.timeframe} · with 50 / 200 SMA`),
        h('span', { class: 'card-trail mono' },
          `${r.chart.close.length} bars`)
      ),
      h(
        'div',
        { class: 'card-body' },
        priceWithSmaChart(r.chart, r.price_action.supports, r.price_action.resistances)
      )
    )
  );

  // Narrative — deterministic by default; optionally polished by Claude.
  const narrativeHost = h(
    'div',
    { class: 'report-narrative', id: 'narrative-host' },
    ...r.narrative.split('\n\n').map((para) => h('p', {}, para))
  );
  const polishBtn = analyst.llm.enabled
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

  // Recommended option strategy (clean directional ticket — always populated)
  root.append(tradeTicketCard(r.options.trade_plan, r.verdict, r.options.headline));

  // Optional: ask Claude about this specific report.
  if (analyst.llm.enabled) {
    root.append(explainBox(r));
  }
}

// ---------- Claude LLM: narrative polish, brief, explain ------------------

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

function tradeTicketCard(tp, verdict, headline) {
  const type = tp.contract_type; // always 'call' or 'put'
  const action = type === 'call' ? 'BUY CALL' : 'BUY PUT';
  const tone = type === 'call' ? 'pos' : 'neg';
  const expiryDate = fmtExpiry(tp.expiry_date, tp.expiry_dte);

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

  const statRow = h(
    'div',
    { class: 'ticket-stats' },
    ticketStat(
      tp.estimated_premium != null ? `$${tp.estimated_premium.toFixed(2)}` : '—',
      'Est. premium / sh'
    ),
    ticketStat(
      tp.cost_per_contract != null ? fmtUSD(tp.cost_per_contract) : '—',
      'Cost / contract'
    ),
    ticketStat(
      tp.break_even != null ? `$${tp.break_even.toFixed(2)}` : '—',
      'Break-even'
    ),
    ticketStat(`$${tp.spot_at_entry.toFixed(2)}`, 'Spot at entry'),
    ticketStat(
      tp.one_sigma_move_usd != null ? `±$${tp.one_sigma_move_usd.toFixed(2)}` : '—',
      `1σ move (${tp.expiry_dte}D)`
    )
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

  const rationale = h('div', { class: 'rationale ticket-rationale' }, tp.rationale);

  return h(
    'div',
    { class: 'ticket-card ticket-card-concrete' },
    hero,
    statRow,
    planRow,
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

function priceWithSmaChart(chart, supports, resistances) {
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
  for (const v of supports) all.push(v);
  for (const v of resistances) all.push(v);
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

  // Support / resistance horizontal lines
  for (const s of supports) {
    const [, y] = xy(s, 0);
    root.append(svg('line', {
      x1: pad.left, y1: y, x2: w - pad.right, y2: y,
      stroke: 'var(--success)', 'stroke-opacity': 0.35,
      'stroke-dasharray': '4 4',
    }));
  }
  for (const s of resistances) {
    const [, y] = xy(s, 0);
    root.append(svg('line', {
      x1: pad.left, y1: y, x2: w - pad.right, y2: y,
      stroke: 'var(--danger)', 'stroke-opacity': 0.35,
      'stroke-dasharray': '4 4',
    }));
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
  analyst.overview = [];
  analyst.reports = {};
  analyst.polishCache = {};
  analyst.briefCache = {};
  etf.overview = [];

  try {
    await loadDashboard();
    if (state.view === 'detail' && state.activeSymbol) {
      await openTicker(state.activeSymbol);
    }
    if (state.view === 'analyst' || analyst.initialized) {
      loadAnalystOverview();
      if (analyst.activeSymbol) loadAnalystReport(analyst.activeSymbol);
      if (analyst.llm.enabled) loadDailyBrief({ force: reason === 'manual' });
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
    if (state.view === 'copy-trade' && copyTrade.initialized) {
      loadCopyTradeData(true);
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
};

function initSearchOnce() {
  if (searchUI.initialized) return;
  searchUI.initialized = true;

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
    renderSearchResult(r);
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

function renderSearchResult(r) {
  const host = $('#search-result');
  host.innerHTML = '';
  const resolved = r.resolved;
  const plan = r.stock_plan;
  const report = r.report;

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

  // Row 3: fancy chart reuse.
  host.append(
    h('div', { class: 'card card-lg' },
      h('div', { class: 'card-header' },
        h('span', { class: 'card-title' }, `${resolved.symbol} · price with indicators`),
        h('span', { class: 'card-trail mono' },
          `${report.chart.close.length} bars · ${report.timeframe}`
        )
      ),
      h('div', { class: 'card-body' },
        fancyPriceChart(
          report.chart.close,
          report.verdict === 'BULLISH' ? 'bull'
            : report.verdict === 'BEARISH' ? 'bear' : 'neutral'
        )
      )
    )
  );

  // Row 4: compact technical summary from the report.
  host.append(
    h('div', { class: 'search-tech' },
      techBox('Trend',  (report.price_action.trend || '—').toUpperCase()),
      techBox('RSI(14)',
        report.rsi.value != null ? report.rsi.value.toFixed(0) : '—',
        report.rsi.state),
      techBox('SMA 50 / 200',
        (report.sma.sma50 != null ? fmtUSD(report.sma.sma50) : '—') +
        ' / ' +
        (report.sma.sma200 != null ? fmtUSD(report.sma.sma200) : '—'),
        report.sma.stack),
      techBox('MACD hist',
        report.macd.histogram != null ? report.macd.histogram.toFixed(3) : '—',
        report.macd.state),
      techBox('ATR(14)',
        report.atr.value != null ? report.atr.value.toFixed(2) : '—',
        report.atr.regime),
      techBox('Verdict', report.verdict,
        `composite ${fmtScore(report.composite_score || 0)}`)
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

function techBox(label, value, sub = '') {
  return h('div', { class: 'tech-box' },
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
    const active = document.querySelector('.analyst-timeframes .pill.is-active');
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
