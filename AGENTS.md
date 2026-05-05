# AGENTS.md

Guidance for coding agents working in this repository.

## Scope

This file applies to the full workspace rooted at `Sianna Financials/`.

## Repo Map

- `square18_signals/`: deterministic options math + Greeks library
- `square18_signals_web/`: FastAPI backend + static frontend
- `CLAUDE.md`: full project context, architecture, and runbook

## Working Rules

1. Preserve deterministic behavior in `square18_signals` (no hidden network calls).
2. Keep API contracts stable for frontend consumers in `square18_signals_web/static/app.js`.
3. Prefer graceful degradation over hard failures for external data providers.
4. Avoid introducing unnecessary dependencies or build systems for the frontend.
5. Keep edits focused and minimal; do not refactor unrelated areas.
6. After any scoring change in `_compute_raw_score`, re-run the backtest.
7. Bump the `?v=` cache-buster in `static/index.html` after frontend changes.
8. Never commit `square18_signals_web/.env` (gitignored). Use `.env.example`.

## Run/Test

From workspace root:

```bash
# run app (sources .env automatically)
bash square18_signals_web/run.sh

# key tests
python3 -m pytest square18_signals/tests -q
python3 -m pytest square18_signals_web/tests/test_e2e_app.py -q
python3 -m pytest square18_signals_web/tests/test_market_news.py -q
python3 -m pytest square18_signals_web/tests/test_screener.py -q
python3 -m pytest square18_signals_web/tests/test_indicators.py -q
python3 -m pytest square18_signals_web/tests/test_trade_plan_derived_math.py -q

# re-tune signal thresholds after scoring changes (~30s)
cd square18_signals_web && python -m tools.backtest_verdict --search-tau --out backtest_verdict.json
```

Optional browser E2E (Playwright Chromium):

```bash
pip3 install -r square18_signals_web/requirements.txt
python3 -m playwright install chromium
cd square18_signals_web && PYTHONPATH="../square18_signals/src:$PWD" python3 -m pytest tests/test_e2e_ui_playwright.py -v
```

## High-Impact Files

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI app + all routes |
| `app/analyst/report.py` | Full signal pipeline: `_compute_raw_score`, gates, warnings, `_build_trade_plan` |
| `app/analyst/signal_config.py` | Threshold loader (hot-reload from `signal_thresholds.json`) |
| `app/analyst/regime.py` | VIX quote + breadth helpers (shared by report + services) |
| `app/analyst/options_flow.py` | Tier-2 UOA/term-structure/skew from Yahoo chain |
| `app/analyst/indicators.py` | All indicator series + `rsi_divergence()` |
| `app/analyst/constants.py` | TICKERS, TICKER_MAP, DEFAULT_IV |
| `app/analyst/yahoo_quotes.py` | Spot price, option mid, short interest (all cached) |
| `signal_thresholds.json` | Tunable BULL/BEAR thresholds + MTF/regime config |
| `backtest_verdict.json` | Walk-forward stats (per-symbol hit-rate, PF, calibrated probability) |
| `tools/backtest_verdict.py` | Walk-forward backtest + `--search-tau` τ grid-search |
| `static/app.js` | All frontend logic |
| `static/index.html` | Page shell (contains `?v=` cache-buster strings) |

## Signal Pipeline Quick Reference

```
_compute_raw_score(pa, vs, sma, rsi, macd, adx, stoch, bb)
  ↓ _apply_triple_mean_reversion_gate
  ↓ _apply_mean_reversion_gate
  ↓ _apply_earnings_gate
  ↓ _apply_mtf           (multi-timeframe bonus/veto)
  ↓ _apply_regime_gate   (VIX + breadth)
  ↓ _score_to_verdict    (thresholds from signal_thresholds.json)
  ↓ get_options_flow     (UOA / term / skew — Tier-2)
  ↓ signal_warnings[]    (37+ quality checks)
  ↓ _build_trade_plan    (premium, Greeks, P&L scenarios)
```

Conviction: proportional to `abs(score)`, capped at 0.85.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Enables Claude LLM endpoints |
| `ANTHROPIC_MODEL` | Model override (default `claude-sonnet-4-5`) |
| `SQUARE18_LLM_CACHE_TTL` | LLM disk-cache TTL in seconds |
| `PORT`, `HOST`, `PYTHON` | Launcher controls |
| `SQUARE18_OHLCV_TTL_INTRADAY` | 1h/4h OHLCV disk cache (default 300s) |
| `SQUARE18_OHLCV_TTL_DAILY` | Daily OHLCV disk cache (default 900s) |
| `SQUARE18_SPOT_QUOTE_TTL_SEC` | Spot quote in-process cache (default 30s) |
| `SQUARE18_OPTIONS_QUOTE_TTL_SEC` | Options chain mid cache (default 30s) |
| `SQUARE18_CHAIN_TTL_SEC` | Options flow chain cache (default 300s) |
| `SQUARE18_SP500_REFRESH_HOURS` | S&P 500 CSV refresh interval (default 24h) |
| `SQUARE18_SP500_CSV_URL` | Override for S&P 500 CSV URL |
