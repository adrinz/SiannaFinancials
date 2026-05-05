# Sianna Financials

Sianna Financials is a stocks and options analyzer workspace with:

- a deterministic options/recommender engine (`square18_signals`)
- a FastAPI + static web app (`square18_signals_web`)

## Workspace Layout

- `square18_signals/`  
  Core pricing, IV, strategy payoff, and strategy recommendation library.
  Includes Black-Scholes pricing, analytical Greeks (δ, γ, θ, ν, ρ), and
  strategy payoff/POP computation.
- `square18_signals_web/`  
  Web application (API + UI) that serves dashboard, ticker detail, search,
  screener (S&P 500 jumps / dips / earnings), ETF signals, and the Analyst
  tab with a multi-layer enhanced signal pipeline.
- `CLAUDE.md`  
  Full assistant-oriented project context and runbook.

## Quick Start

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install -r square18_signals_web/requirements.txt

# Copy .env.example and set ANTHROPIC_API_KEY to enable Claude features
cp square18_signals_web/.env.example square18_signals_web/.env
# edit .env with your key

bash square18_signals_web/run.sh
```

Open: <http://127.0.0.1:8000>

## Signal Engine — What's New

The Analyst tab now runs a **multi-layer enhanced signal pipeline**:

| Layer | What it does |
|-------|-------------|
| **Tier-1 scoring** | Configurable BULLISH/BEARISH thresholds (tuned via walk-forward backtest); MTF confluence bonus/veto; VIX + breadth regime gate; calibrated hit-rate probability |
| **Tier-2 options intelligence** | UOA (unusual volume/OI), term-structure slope, put-call skew — from Yahoo chain |
| **37 misleading-signal fixes** | Triple mean-reversion gate; RSI divergence detection; MACD RSI zero-line filter; ADX slope; overnight gap detection; chart patterns in score; symbol-class guards; IV crush warning; earnings proximity gate; and more |
| **Trade ticket** | Black-Scholes Greeks (δ, θ, ν); P&L scenarios at target/flat/stop; break-even as % move; theta as % of premium/week; short interest squeeze warning; weekend theta note; low-DTE gamma alert; options chain OI + spread check |

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

# Re-tune signal thresholds from fresh data
cd square18_signals_web && python -m tools.backtest_verdict --search-tau --out backtest_verdict.json
```

## Notes

- Python 3.10+ required.
- `square18_signals_web` imports the sibling `square18_signals/src` path at runtime — no separate install.
- `python-dotenv` is included; `square18_signals_web/.env` is loaded on startup (gitignored).
- News: CNBC RSS → MarketWatch RSS → internal snapshot fallback.
- S&P 500 constituents fetched on a TTL (default 24h, `SQUARE18_SP500_REFRESH_HOURS`); `sp500.json` used offline.
- See `CLAUDE.md` for full architecture, endpoint, and operational details.
