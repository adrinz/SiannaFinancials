# Sianna Financials

Sianna Financials is a stocks and options analyzer workspace with:

- a deterministic options/recommender engine (`square18_signals`)
- a FastAPI + static web app (`square18_signals_web`)

## Workspace Layout

- `square18_signals/`  
  Core pricing, IV, strategy payoff, and strategy recommendation library.
- `square18_signals_web/`  
  Web application (API + UI) that serves dashboard, ticker detail, search, screener (S&P 500 jumps / dips / earnings), ETF signals, copy-trade (13F research), and analyst views.
- `Claude.md`  
  Full assistant-oriented project context and runbook.

## Quick Start

From workspace root:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r square18_signals_web/requirements.txt
bash square18_signals_web/run.sh
```

Open:

- <http://127.0.0.1:8000>

## Test Commands

```bash
# Core engine tests
python3 -m pytest square18_signals/tests -q

# Web API E2E
python3 -m pytest square18_signals_web/tests/test_e2e_app.py -q

# Browser E2E (requires Playwright browser install)
python3 -m playwright install chromium
python3 -m pytest square18_signals_web/tests/test_e2e_ui_playwright.py -q

# Market news fallback tests
python3 -m pytest square18_signals_web/tests/test_market_news.py -q

# Stock screener tests (jumps / dips / earnings)
python3 -m pytest square18_signals_web/tests/test_screener.py -q

# Copy trade (13F parser + API)
python3 -m pytest square18_signals_web/tests/test_copy_trade.py -q
```

## Notes

- Python 3.10+ recommended.
- `square18_signals_web` imports the sibling `square18_signals/src` path at runtime.
- News feed has multi-level fallback logic (CNBC RSS -> MarketWatch RSS -> internal snapshot).
- Stock screener scans the S&P 500: constituents are **fetched** from a public S&P 500 CSV on a TTL (default 24h, see `SQUARE18_SP500_REFRESH_HOURS` in `app/analyst/universe.py`, optional `SQUARE18_SP500_CSV_URL`); a bundled `sp500.json` is used for cold start / offline, with **stale** last-good data if the network feed fails. Movers use batched `yfinance`; the earnings card defaults to **7 days** ahead (`SCREENER_EARNINGS_WINDOW_DAYS` in `app/analyst/constants.py`). The **tracked** ticker list in `TICKERS` (dashboard + screener quick path) is used when the broad sources are down.
- See `Claude.md` for deeper architecture, endpoint, and operational details.

