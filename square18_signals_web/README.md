# square18_signals_web

A local **web UI for Sianna Financials** (the stocks & options
analyzer). FastAPI backend wrapping the `square18_signals` Python
package, serving a single-page app built with plain HTML, CSS, and
vanilla JS — no npm build step, no framework, no lock file.

Launch once with `./run.sh` and open <http://127.0.0.1:8000>.

## What's new

- **Stock Screener tab** — three quick views: today's biggest price
  jumps, today's biggest price dips, and a calendar of upcoming
  earnings (next 14 days). Reuses the dashboard's analyst pipeline so
  verdicts stay consistent. Earnings come from `yfinance` with a
  graceful empty-list fallback when the provider is offline.
- **Sianna Financials** brand (formerly "Square18 Signals").
- All timestamps render in **US Eastern (America/New_York)** — live
  clock in the top bar and a full date/time in the footer.
- **Manual Refresh button** in the top bar plus **automatic data
  refresh every 5 minutes** (with visibility-aware catch-up when the
  tab comes back into focus).
- Fancier **ticker-detail chart** — close line, SMA(5/10/20), EMA(9),
  Bollinger(20, 2σ) envelope, hover crosshair with tooltip, legend.
- **Powered by Claude Sonnet 4.5** footer badge; set
  `ANTHROPIC_API_KEY` to enable narrative polish, the Claude desk brief,
  and per-ticket Q&A (see "Claude layer" below).

## What it shows

**Dashboard**

- Market-regime banner (VIX, breadth, put/call, trend score).
- Five-up KPI strip (universe size, longs, shorts, holds, VIX).
- Today's signals table — click any row to open its detail view.
- Filter pills (All / Buy / Sell / Hold).

**Screener**

- Three cards on a dedicated tab:
  - **Daily price jumps** — top gainers in the curated universe today.
  - **Daily price dips** — top losers today.
  - **Upcoming earnings** — companies in the universe reporting
    within the next 14 days, decorated with current price, day's
    change, and dashboard verdict.
- Click any row to jump to the full Ticker Detail view.
- Auto-refreshes alongside the rest of the dashboard.

**Ticker detail**

- Hero strip with price, Δ, composite score, confidence, IV rank,
  and expected move (1σ) over the recommender's horizon.
- 30-session price sparkline (colored by direction).
- Horizontal factor bar chart and the "why this signal" factor table.
- **Strategy recommender panel** — up to 4 cards, each showing:
  legs, max gain / max loss / POP, break-evens, net debit/credit,
  rationale, tags, and a fit-score bar. Metrics come from the real
  `square18_signals` pricing + payoff + POP engine.

## Requirements

- Python **3.10+**
- `pip install -r requirements.txt` (FastAPI, Uvicorn, Pydantic v2)

The sibling `../square18_signals/src/` is added to `sys.path`
automatically at startup — no install of the math package needed.

## Quick start

```bash
cd square18_signals_web
pip install -r requirements.txt
./run.sh
```

Then open <http://127.0.0.1:8000/>.

Auto-reload is enabled by default; pass `prod` to disable:
`./run.sh prod`. Set `PORT=9000` to change the port.

## Testing

Run the API-level E2E suite (FastAPI `TestClient`):

```bash
python -m pytest tests/test_e2e_app.py -q
```

Run real browser E2E (Playwright) for Dashboard / Ticker detail / Search / Analyst:

```bash
# one-time browser install
python -m playwright install chromium

# run browser tests
python -m pytest tests/test_e2e_ui_playwright.py -q
```

Run both together:

```bash
python -m pytest tests/test_e2e_app.py tests/test_e2e_ui_playwright.py -q
```

## API endpoints

| Method | Path                                 | Description                                     |
| ------ | ------------------------------------ | ----------------------------------------------- |
| GET    | `/api/health`                        | liveness probe                                  |
| GET    | `/api/regime`                        | market-regime banner + universe counters        |
| GET    | `/api/screen?filter=…`               | screener rows; `filter` ∈ all/buy/sell/hold     |
| GET    | `/api/ticker/{symbol}`               | full detail payload + strategy recommendations  |
| GET    | `/api/screener/jumps?limit=…`        | today's top gainers from the curated universe   |
| GET    | `/api/screener/dips?limit=…`         | today's top losers from the curated universe    |
| GET    | `/api/screener/earnings?window_days=…` | upcoming earnings dates (yfinance-backed)     |
| GET    | `/api/analyst/tickers`               | analyst universe (symbol/name/sector)           |
| GET    | `/api/analyst/overview?timeframe=…`  | verdict + recommendation row per ticker         |
| GET    | `/api/analyst/report/{symbol}`       | full technical report for a ticker              |
| GET    | `/api/analyst/llm-config`            | `{enabled, model}` for the Claude layer         |
| GET    | `/api/analyst/polish/{symbol}`       | Claude-polished narrative (requires API key)    |
| GET    | `/api/analyst/brief?timeframe=…`     | Claude-synthesised daily desk brief             |
| POST   | `/api/analyst/explain/{symbol}`      | Q&A grounded in the ticker's report             |

Browse the auto-generated Swagger UI at <http://127.0.0.1:8000/docs>.

## Claude layer (optional)

The analytical core is **fully deterministic** — Python math, rule-based
signals, reproducible. Claude Sonnet 4.5 is layered on top for three
narrow tasks, all fail-open:

| Task              | Where              | Purpose                                            |
| ----------------- | ------------------ | -------------------------------------------------- |
| Narrative polish  | Analyst report     | Rewrites the deterministic narrative as fluent prose |
| Daily desk brief  | Top of Analyst tab | Cross-ticker synthesis grouped by sector / bias    |
| Ticket Q&A        | End of a report    | "Why this strike?", "What invalidates the thesis?" |

Prompts wrap the structured report in `<facts>` tags and instruct
Claude to **never introduce a number not present inside**. If Claude is
unreachable or the API key is unset, the app still renders the full
deterministic analysis — the Claude UI chips and brief card simply hide.

Enable:

```bash
export ANTHROPIC_API_KEY=sk-ant-…
# optional overrides:
export ANTHROPIC_MODEL=claude-sonnet-4-5          # default
export SQUARE18_LLM_CACHE_TTL=86400               # 1 day cache per prompt
./run.sh prod
```

Responses are cached on disk at `~/.cache/square18_signals/llm/` keyed
by `sha256(input)` so re-opening the same ticker doesn't re-bill.

Cost ballpark (Sonnet 4.5 pricing): a polished narrative is ~1–2k input
tokens / ~400 output tokens, the daily brief ~2–4k input / ~300 output.
A day of heavy use per user is on the order of pennies.

## Architecture

```
┌──────────────────────────────┐      ┌────────────────────────────┐
│   static/index.html          │      │   app/main.py (FastAPI)    │
│   static/app.js (vanilla)    │ ───▶ │   app/services.py          │
│   static/styles.css (dark)   │      │   app/data.py (mock)       │
└──────────────────────────────┘      └──────────┬─────────────────┘
                                                 │
                                                 ▼
                              ┌────────────────────────────────────┐
                              │   square18_signals (math layer)    │
                              │   pricing · greeks · IV · strategies│
                              │   · recommender                    │
                              └────────────────────────────────────┘
```

**Data flow** for the ticker-detail view:

1. Browser calls `GET /api/ticker/NVDA`.
2. `services.ticker_detail` loads the snapshot from `data.UNIVERSE`,
   computes IV rank via `iv_rank(current_iv, iv_history)`, builds a
   `MarketContext`, and calls `recommend_strategies(ctx)`.
3. Each recommended strategy is priced via Black-Scholes for its
   legs; payoffs, break-evens, and POP are computed by
   `strategy_metrics()` inside the package.
4. The response is serialized by Pydantic models, with `math.inf`
   mapped to `null` so the UI can render `∞`.
5. The frontend caches per-symbol responses in `state.details` to
   avoid refetching.

## Replacing the mock data

Everything lives behind `app/data.py`. To plug in a live provider,
implement a module that exposes the same `UNIVERSE`, `BY_SYMBOL`,
`MARKET_REGIME`, and `counts()` interface — or just populate those
variables from your source at startup. The rest of the stack is
data-agnostic.

## Frontend notes

- No framework, no bundler. One HTML file, one CSS file, one JS file.
- Charts are pure inline SVG — sparkline for price, horizontal bars
  for factor contributions. Under ~200 lines combined.
- Theme uses `color-mix()` and CSS custom properties; tweak the
  `:root` block at the top of `styles.css` to re-skin.

## What's deliberately not here

- No authentication, no websockets, no real-time push.
- No persistence — state is request-scoped.
- No production deployment config (Dockerfile / systemd unit). The
  backend is a standard ASGI app, so `gunicorn -k uvicorn.workers.UvicornWorker`
  would work out of the box.
- Live market data — see "Replacing the mock data" above.
