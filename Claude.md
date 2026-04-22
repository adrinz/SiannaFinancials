# Claude Project Context: Sianna Financials

This document is the single-source context brief for working on the `Sianna Financials` workspace.

## 1) Workspace Overview

The workspace contains two related Python projects:

- `square18_signals`: core options math + strategy recommender library.
- `square18_signals_web`: FastAPI backend + static frontend UI that consumes the library.

Primary product behavior:

- Dashboard with market regime, screener, crypto, and news cards.
- Ticker detail with factors and options strategy recommendations.
- Analyst views with report generation and optional Claude-powered prose.

## 2) Project Structure

Top-level layout:

- `square18_signals/`
  - `src/square18_signals/`: pricing, IV, strategies, recommender
  - `tests/`: unit tests for math/recommender
  - `pyproject.toml`: package config (hatchling, Python >=3.10)
- `square18_signals_web/`
  - `app/main.py`: FastAPI app + routes
  - `app/services.py`: dashboard/detail service composition
  - `app/analyst/`: analyst pipeline (data, indicators, market, search, report, llm)
  - `static/`: `index.html`, `app.js`, `styles.css`
  - `tests/`: API E2E, Playwright UI E2E, indicators, market news tests
  - `run.sh`: app launcher script

## 3) Tech Stack

- Python 3.10+
- FastAPI + Uvicorn
- Pydantic v2
- Optional `yfinance` for live market/news data
- Optional `anthropic` for LLM enrichment
- `pytest` for tests
- `playwright` for browser E2E
- Frontend: plain HTML/CSS/vanilla JS (no npm/bundler)

## 4) How To Run

From workspace root:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r square18_signals_web/requirements.txt
bash square18_signals_web/run.sh
```

App URL:

- `http://127.0.0.1:8000`

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

Search + analyst:

- `GET /api/search?q=...&timeframe=...`
- `GET /api/search/suggest?q=...&limit=...`
- `GET /api/analyst/tickers`
- `GET /api/analyst/overview?timeframe=...`
- `GET /api/analyst/report/{symbol}?timeframe=...`
- `GET /api/analyst/llm-config`
- `GET /api/analyst/polish/{symbol}?timeframe=...` (LLM optional)
- `GET /api/analyst/brief?timeframe=...` (LLM optional)
- `POST /api/analyst/explain/{symbol}` (LLM optional)

Report exports:

- `GET /api/report/signals?timeframe=...`
- `GET /api/report/signals.md?timeframe=...&download=true|false`
- `GET /api/report/signals.txt?timeframe=...`

## 7) Data and Fallback Behavior

General behavior:

- App attempts live data where available.
- Endpoints are designed to degrade gracefully instead of hard-failing UI cards.

News behavior (important):

- Primary source: `yfinance` ticker news.
- Fallback source: MarketWatch RSS (`topstories`, `marketpulse`).
- Final fallback: internal snapshot headlines generated from current analyst overview.
- If all fail, source reports `unavailable`.

This logic lives in:

- `square18_signals_web/app/analyst/market.py`

## 8) Optional LLM Layer

Environment variables:

- `ANTHROPIC_API_KEY` (enables LLM endpoints/features)
- `ANTHROPIC_MODEL` (optional override)
- `SQUARE18_LLM_CACHE_TTL` (optional cache TTL)

If unset/unavailable:

- deterministic core analysis still works
- LLM endpoints return disabled/fail-open behavior

## 9) Important Implementation Notes

- `square18_signals_web/app/main.py` inserts sibling `square18_signals/src` into `sys.path` at runtime.
- The frontend relies on specific DOM IDs/classes in `static/index.html` and behavior in `static/app.js`.
- Keep API shapes stable; UI rendering expects specific keys.
- Tests avoid brittle exact-price assertions due to variable market data.

## 10) Known Operational Realities

- Network/provider instability can affect `yfinance` calls.
- Some environments block external feeds; fallbacks are required.
- UI E2E tests require Playwright + Chromium availability.

## 11) Recommended Workflow for Changes

1. Modify backend/frontend code.
2. Run targeted tests first (`test_e2e_app.py`, relevant unit tests).
3. Run broader web test subset as needed.
4. Verify live endpoint behavior (`/api/news`, `/api/screen`, `/api/ticker/...`).
5. Restart server if needed to ensure reloaded code.

## 12) Quick File Reference

- Core app entry: `square18_signals_web/app/main.py`
- Market/news aggregations: `square18_signals_web/app/analyst/market.py`
- Frontend logic: `square18_signals_web/static/app.js`
- Frontend layout: `square18_signals_web/static/index.html`
- Frontend styling: `square18_signals_web/static/styles.css`
- Math/recommender package: `square18_signals/src/square18_signals/`
- New market-news tests: `square18_signals_web/tests/test_market_news.py`

