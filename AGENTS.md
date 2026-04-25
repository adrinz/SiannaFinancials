# AGENTS.md

Guidance for coding agents working in this repository.

## Scope

This file applies to the full workspace rooted at `Sianna Financials/`.

## Repo Map

- `square18_signals/`: deterministic options math and recommender package
- `square18_signals_web/`: FastAPI backend + static frontend
- `Claude.md`: full project context and operational notes

## Working Rules

1. Preserve deterministic behavior in `square18_signals` (no hidden network calls).
2. Keep API contracts stable for frontend consumers in `square18_signals_web/static/app.js`.
3. Prefer graceful degradation over hard failures for external data providers.
4. Avoid introducing unnecessary dependencies or build systems for the frontend.
5. Keep edits focused and minimal; do not refactor unrelated areas.

## Run/Test

From workspace root:

```bash
# run app
bash square18_signals_web/run.sh

# key tests
python3 -m pytest square18_signals/tests -q
python3 -m pytest square18_signals_web/tests/test_e2e_app.py -q
python3 -m pytest square18_signals_web/tests/test_market_news.py -q
python3 -m pytest square18_signals_web/tests/test_screener.py -q
```

Optional browser E2E:

```bash
python3 -m playwright install chromium
python3 -m pytest square18_signals_web/tests/test_e2e_ui_playwright.py -q
```

## High-Impact Files

- API entry: `square18_signals_web/app/main.py`
- Market/news aggregation: `square18_signals_web/app/analyst/market.py`
- Earnings calendar helper: `square18_signals_web/app/analyst/earnings.py`
- Broad market movers: `square18_signals_web/app/analyst/movers.py`
- Universe loader (S&P 500, dynamic CSV + bundle fallback): `square18_signals_web/app/analyst/universe.py`
- S&P 500 offline fallback data: `square18_signals_web/app/analyst/data/sp500.json` (`SQUARE18_SP500_REFRESH_HOURS`, `SQUARE18_SP500_CSV_URL`)
- Frontend behavior: `square18_signals_web/static/app.js`
- Core strategy logic: `square18_signals/src/square18_signals/recommender.py`
- Strategy pricing/payoff: `square18_signals/src/square18_signals/strategies.py`

## Environment Variables

- `ANTHROPIC_API_KEY` enables optional LLM endpoints
- `ANTHROPIC_MODEL` optional LLM model override
- `SQUARE18_LLM_CACHE_TTL` optional LLM cache TTL
- `PORT`, `HOST`, `PYTHON` supported by `square18_signals_web/run.sh`
- `SQUARE18_SP500_REFRESH_HOURS`, `SQUARE18_SP500_CSV_URL` — S&P 500 list refresh (see `app/analyst/universe.py`)

