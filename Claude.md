# Claude Project Context: Sianna Financials

This document is the single-source context brief for working on the `Sianna Financials` workspace.

## 1) Workspace Overview

The workspace contains two related Python projects:

- `square18_signals`: core options math + strategy recommender library.
- `square18_signals_web`: FastAPI backend + static frontend UI that consumes the library.

Primary product behavior:

- Dashboard with market regime, today's signals table, market pulse,
  options highlights, crypto, and news cards.
- Ticker detail with factors and options strategy recommendations.
- Free-form Search view (Buy / Sell / Hold plan for any ticker).
- Stock Screener tab — daily price jumps, daily price dips, and an
  upcoming earnings calendar (default **7 days**). Scope is the **S&P 500**.
- Analyst tab — full technical reports with enhanced multi-layer signal pipeline,
  options intelligence, and a concrete trade ticket with Greeks and P&L scenarios.

## 2) Project Structure

Top-level layout:

- `square18_signals/`
  - `src/square18_signals/`: pricing, IV, strategies, recommender, Greeks
  - `tests/`: unit tests for math/recommender
  - `pyproject.toml`: package config (hatchling, Python >=3.10)
- `square18_signals_web/`
  - `app/main.py`: FastAPI app + routes
  - `app/services.py`: dashboard/detail service composition
  - `app/analyst/`: analyst pipeline (data, indicators, market, search,
    report, earnings, movers, universe, llm, options_flow, regime, signal_config)
  - `app/analyst/data/sp500.json`: offline / cold-start S&P 500 list
  - `static/`: `index.html`, `app.js`, `styles.css`
  - `tests/`: API E2E, Playwright UI E2E, indicators, market news, screener, trade-plan math
  - `run.sh`: app launcher (sources `.env` when present)
  - `.env.example`: template for local secrets (copy to `.env`)
  - `signal_thresholds.json`: configurable signal quality thresholds (auto-reloads)
  - `backtest_verdict.json`: walk-forward hit-rate + profit-factor stats
  - `tools/backtest_verdict.py`: walk-forward backtest + `--search-tau` τ tuning

## 3) Tech Stack

- Python 3.10+
- FastAPI + Uvicorn
- Pydantic v2
- `python-dotenv` (loads `square18_signals_web/.env` at startup)
- Optional `tradier` and `yfinance` for live market/news data (Tradier is primary for real-time OHLCV and Options, yfinance is fallback)
- Optional LLM providers for enrichment (`gemini` recommended free tier, `anthropic` optional)
- `pytest` for tests
- `playwright` for browser E2E
- Frontend: plain HTML/CSS/vanilla JS (no npm/bundler)

## 4) How To Run

From workspace root:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r square18_signals_web/requirements.txt

# Set up LLM (optional)
cp square18_signals_web/.env.example square18_signals_web/.env
# Edit .env: SQUARE18_LLM_PROVIDER=auto and GEMINI_API_KEY=...

bash square18_signals_web/run.sh
```

App URL: `http://127.0.0.1:8000`

Modes:
- Dev (default): `bash square18_signals_web/run.sh`
- Prod-style (no reload): `bash square18_signals_web/run.sh prod`
- Custom port: `PORT=9000 bash square18_signals_web/run.sh`

## 5) Testing

Key test commands from workspace root:

```bash
python3 -m pytest square18_signals/tests -q
python3 -m pytest square18_signals_web/tests/test_e2e_app.py -q
python3 -m pytest square18_signals_web/tests/test_e2e_ui_playwright.py -q
python3 -m pytest square18_signals_web/tests/test_market_news.py -q

# Signal threshold re-tune (needs network, ~30s)
cd square18_signals_web && python -m tools.backtest_verdict --search-tau --out backtest_verdict.json
```

Install Playwright browser once if needed:

```bash
python3 -m playwright install chromium
```

## 6) Core API Endpoints (Web App)

Dashboard + detail:

- `GET /api/health`
- `GET /api/regime`
- `GET /api/screen?filter=all|buy|sell|hold`
- `GET /api/ticker/{symbol}`
- `GET /api/market/pulse?timeframe=1h|4h|daily|weekly`
- `GET /api/options/highlights?timeframe=1h|4h|daily|weekly`
- `GET /api/crypto/snapshot`
- `GET /api/news?limit=...`

Screener tab (universe = S&P 500):

- `GET /api/screener/movers?timeframe=daily&limit=10&quick=0|1`
- `GET /api/screener/jumps?timeframe=daily&limit=10`
- `GET /api/screener/dips?timeframe=daily&limit=10`
- `GET /api/screener/earnings?window_days=7&limit=50`

Search + analyst:

- `GET /api/search?q=...&timeframe=...`
- `GET /api/search/suggest?q=...&limit=...`
- `GET /api/analyst/tickers`
- `GET /api/analyst/overview?timeframe=...`
- `GET /api/etf/signals?timeframe=...`
- `GET /api/analyst/report/{symbol}?timeframe=...&fresh_quotes=0|1`
  - `fresh_quotes=1` (default) bypasses in-process quote caches for live premium/spot
  - Returns `signal_warnings[]`, `signal_probability`, `mtf_confluence`, `regime_gate`,
    `options_flow` (UOA/term/skew), and trade ticket with Greeks + P&L scenarios
- `GET /api/analyst/llm-config`
- `GET /api/analyst/polish/{symbol}?timeframe=...` (LLM optional)
- `GET /api/analyst/brief?timeframe=...` (LLM optional)
- `POST /api/analyst/explain/{symbol}` (LLM optional)

Report exports:

- `GET /api/report/signals?timeframe=...`
- `GET /api/report/signals.md?timeframe=...&download=true|false`
- `GET /api/report/signals.txt?timeframe=...`

## 7) Signal Pipeline Architecture

The Analyst tab runs a multi-step enhanced pipeline for each ticker:

```
OHLCV (Tradier / yfinance, disk-cached)
    ↓
_compute_raw_score(pa, vs, sma, rsi, macd, adx, stoch, bollinger)
    ↓
_apply_triple_mean_reversion_gate   ← RSI+Stoch+Bollinger all extreme → NEUTRAL
    ↓
_apply_mean_reversion_gate          ← RSI overbought + Bollinger stretched
    ↓
_apply_earnings_gate                ← earnings proximity → score dampened
    ↓
_apply_mtf                          ← higher-TF score bonus/veto
    ↓
_apply_regime_gate                  ← VIX level + market breadth
    ↓
_score_to_verdict                   ← thresholds from signal_thresholds.json
    ↓
get_options_flow                    ← UOA, term structure, put-call skew (Tier-2)
    ↓
signal_warnings[]                   ← 37+ quality checks surfaced in UI
    ↓
_build_trade_plan                   ← premium (Yahoo chain or BS), Greeks, P&L scenarios
    ↓
ReportOut (verdict, composite_score, conviction, signal_probability, options_flow, ...)
```

Key scoring weights in `_compute_raw_score`:
- Trend (0.35), SMA stack/cross (0.28), RSI (0.12 overbought / +0.08 bullish),
  MACD cross (RSI-filtered, 0.07–0.15), MACD histogram (0.05), MACD line vs zero (0.03),
  Stochastic (0.05 extremes / 0.03 cross), Bollinger %B (0.04 extremes),
  Volume (0.06+0.04), ADX (0.04), Chart patterns (double-top/bottom ±0.08)

Conviction: proportional to `abs(score)`, capped at 0.85 (never 100%).

## 8) Optional LLM Layer

Environment variables (set in `square18_signals_web/.env` or shell export):

- `TRADIER_API_KEY` and `TRADIER_ENV` (enables real-time Tradier data)
- `SQUARE18_LLM_PROVIDER` (`auto` | `gemini` | `anthropic`, default `auto`)
- `GEMINI_API_KEY` (recommended free-tier provider via Google AI Studio)
- `GEMINI_MODEL` (optional override; default `gemini-2.5-flash`)
- `ANTHROPIC_API_KEY` (optional alternative provider)
- `ANTHROPIC_MODEL` (optional override; default `claude-sonnet-4-5`)
- `SQUARE18_LLM_CACHE_TTL` (optional cache TTL in seconds)
- `SQUARE18_OHLCV_TTL_INTRADAY`, `SQUARE18_OHLCV_TTL_DAILY`, etc. (cache TTLs)
- `SQUARE18_SPOT_QUOTE_TTL_SEC`, `SQUARE18_OPTIONS_QUOTE_TTL_SEC` (quote caches)
- `SQUARE18_SP500_REFRESH_HOURS`, `SQUARE18_SP500_CSV_URL`
- `PORT`, `HOST`, `PYTHON` (launcher)

If no configured provider key is set:
- Deterministic core analysis still works fully
- LLM endpoints return 503; UI chips/brief card hide automatically

## 9) Important Implementation Notes

- `app/main.py` loads `.env` via `python-dotenv` at import time (before Uvicorn starts).
- `run.sh` also `source .env` for the shell environment.
- Never commit `square18_signals_web/.env` (gitignored). Use `.env.example` as template.
- `signal_thresholds.json` is checked every 60s at runtime — update and the app picks it up without restart.
- Backtest results in `backtest_verdict.json` feed `signal_probability` per symbol and aggregate hit-rates shown in the UI.
- `app/analyst/regime.py` provides `vix_quote()` and `breadth_above_50d()` shared by both `services.py` and `report.py` (avoids circular imports).
- Keep API shapes stable; UI rendering expects specific keys (especially `ReportOut` and `TradePlan`).
- Tests avoid brittle exact-price assertions due to variable market data.
- `_score_and_verdict` in `report.py` is a thin wrapper used only for the backtest tool's `_score_window`; the live pipeline calls `_compute_raw_score` directly in `build_report`.

## 10) Known Operational Realities

- The app uses Tradier for real-time OHLCV and Options data. If Tradier is unconfigured or unreachable, it falls back to `yfinance`.
- Network/provider instability can affect `yfinance` calls; a circuit breaker prevents 403 bans.
- Yahoo Finance data is delayed ~15–20 min during US market hours. Tradier data is real-time (or 15m delayed in sandbox).
- Short interest data (`yf_short_interest_pct`) is FINRA biweekly — cached 1h.
- `fresh_quotes=1` on the analyst report bypasses in-process caches.
- 4H bars are resampled from 15m/1H data (calendar 4H buckets, not US session-aligned).
- UI E2E tests require Playwright + Chromium availability.

## 11) Recommended Workflow for Changes

1. Modify backend/frontend code.
2. Run targeted tests first (`test_e2e_app.py`, `test_indicators.py`, `test_trade_plan_derived_math.py`).
3. If scoring changed, re-run backtest: `python -m tools.backtest_verdict --search-tau`.
4. Verify live endpoint: `GET /api/analyst/report/AAPL?timeframe=daily`.
5. Hard-refresh browser (Cmd+Shift+R) to pick up new `app.js`/`styles.css` (bump the `?v=` cache-buster in `index.html`).
6. Commit + push.

## 12) Quick File Reference

- Core app entry: `square18_signals_web/app/main.py`
- Signal pipeline: `square18_signals_web/app/analyst/report.py`
- Signal config / threshold loader: `square18_signals_web/app/analyst/signal_config.py`
- Market regime helpers: `square18_signals_web/app/analyst/regime.py`
- Options intelligence (Tier-2): `square18_signals_web/app/analyst/options_flow.py`
- Indicators (RSI, MACD, ATR, divergence): `square18_signals_web/app/analyst/indicators.py`
- Ticker constants / symbol map: `square18_signals_web/app/analyst/constants.py`
- Market/news aggregations: `square18_signals_web/app/analyst/market.py`
- Yahoo quotes + short interest: `square18_signals_web/app/analyst/yahoo_quotes.py`
- Earnings calendar helper: `square18_signals_web/app/analyst/earnings.py`
- Broad market movers: `square18_signals_web/app/analyst/movers.py`
- Universe loader: `square18_signals_web/app/analyst/universe.py`
- S&P 500 offline snapshot: `square18_signals_web/app/analyst/data/sp500.json`
- Walk-forward backtest tool: `square18_signals_web/tools/backtest_verdict.py`
- Signal thresholds config: `square18_signals_web/signal_thresholds.json`
- Frontend logic: `square18_signals_web/static/app.js`
- Frontend layout: `square18_signals_web/static/index.html`
- Frontend styling: `square18_signals_web/static/styles.css`
- Math/recommender package: `square18_signals/src/square18_signals/`
- Market-news tests: `square18_signals_web/tests/test_market_news.py`
- Screener tests: `square18_signals_web/tests/test_screener.py`
- Trade plan math tests: `square18_signals_web/tests/test_trade_plan_derived_math.py`
