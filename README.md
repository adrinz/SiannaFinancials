# Sianna Financials

Sianna Financials is a stocks and options analyzer workspace with:

- a deterministic options/recommender engine (`square18_signals`)
- a FastAPI + static web app (`square18_signals_web`)

## Workspace Layout

- `square18_signals/`  
  Core pricing, IV, strategy payoff, and strategy recommendation library.
- `square18_signals_web/`  
  Web application (API + UI) that serves dashboard, ticker detail, search, screener, and analyst views.
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
```

## Notes

- Python 3.10+ recommended.
- `square18_signals_web` imports the sibling `square18_signals/src` path at runtime.
- News feed has multi-level fallback logic (yfinance -> RSS -> internal snapshot).
- See `Claude.md` for deeper architecture, endpoint, and operational details.

