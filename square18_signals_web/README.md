# square18_signals_web

A **web UI for Sianna Financials** — stocks & options analyzer. FastAPI backend wrapping the
`square18_signals` Python package, serving a single-page app built with plain HTML, CSS, and
vanilla JS. No npm build step, no framework.

Launch once with `./run.sh` and open <http://127.0.0.1:8000>.

---

## What the app does

**Dashboard**
- Market-regime banner (VIX, breadth, put/call, trend score).
- Today's signals table — click any row to open Ticker detail.
- Filter pills (All / Buy / Sell / Hold).
- Market news (CNBC RSS → MarketWatch RSS → internal snapshot).

**Screener** (S&P 500 universe)
- Daily price jumps, daily price dips, upcoming earnings (7-day window).
- Two-phase load: quick tracked-list first, then full S&P 500 upgrade.

**ETF signals**
- Verdict + composite + recommended option for a fixed basket of liquid ETFs.

**Analyst tab** ← primary enhanced view
- Universe verdict table with all recommendations at a glance.
- Full technical report per ticker: indicators, price chart, narrative, trade ticket.
- **Multi-layer signal pipeline** (see below).
- Optional Claude Sonnet 4.5 narrative polish, desk brief, and Q&A.

**Ticker detail / Search**
- OHLC chart, factor breakdown, strategy recommendations.
- Free-form search: Buy / Sell / Hold plan for any ticker.

---

## Signal pipeline — what's been built

### Tier-1 signal quality (configurable, backtest-tuned)

| Feature | File |
|---------|------|
| Tunable BULLISH/BEARISH thresholds via `signal_thresholds.json` | `app/analyst/signal_config.py` |
| Walk-forward τ search: `python -m tools.backtest_verdict --search-tau` | `tools/backtest_verdict.py` |
| Multi-timeframe (MTF) confluence bonus/veto | `app/analyst/report.py` |
| Regime gate: VIX level + market breadth | `app/analyst/regime.py` |
| Calibrated historical hit-rate probability on every report | `app/analyst/report.py` |
| Conviction capped at 85% (proportional to score, never 100%) | `_score_to_verdict` |

### Tier-2 options intelligence (Yahoo chain, no extra subscription)

| Signal | What it measures |
|--------|-----------------|
| UOA (unusual volume/OI) | Smart-money directional flow — call vs put pressure |
| Term-structure slope | Front/back month IV ratio; backwardation = event stress |
| Put-call skew | OTM put IV / OTM call IV; elevated = market hedging downside |

Source: `app/analyst/options_flow.py` (5-min in-process chain cache).

### Misleading-signal fixes (37 total across 5 audit rounds)

**Scoring model fixes**
- Stochastic + Bollinger %B added to core score (were computed but ignored).
- `factors.py` RSI overbought direction corrected (+0.1 → −0.3).
- MACD zero-line RSI filter: halves bonus when RSI is on the wrong side of midline.
- MACD histogram deceleration: new `decelerating_bull`/`decelerating_bear` states.
- MACD line vs zero: small ±0.03 momentum confirmation.
- Chart patterns (double-top/bottom, support/resistance) added to score.
- Distribution day (high volume + down bar) fixed — was asymmetric.
- Triple mean-reversion override: RSI ≥70 + Stoch ≥80 + %B ≥0.90 → score forced near NEUTRAL.
- RSI divergence detection in `indicators.py` using swing pivots.
- Volume for index symbols (VIX) now correctly returns neutral stats.

**Signal warnings surfaced in the UI**
- Synthetic data, earnings proximity, earnings-vs-expiry alignment
- ADX absent/weak (ranging market), ADX slope (declining strong trend)
- Price extension >12% from 50d SMA, near-term pullback within uptrend
- All-factors saturation (peak alignment = trend exhaustion)
- Bollinger squeeze (direction undetermined)
- Borderline score (within 0.12 of threshold)
- Dual RSI + Stochastic overbought/oversold simultaneously
- MACD cross quality (RSI midline check), MACD histogram deceleration
- IV crush risk (backwardation + earnings proximity)
- Overnight gap detection (≥3% open vs prior close)
- VIX intraday spike (+4 pts in one session)
- Extreme volume >5× (meme/squeeze/news indicator)
- Low average daily volume (<500k/day)
- Symbol-class guards: QUBT/QBTS/SMR/OKLO (speculative), COIN (crypto-correlated), BYDDY (ADR)
- Sector ETF headwind/tailwind (SMH, XLK, XLF, XLE cross-checked)
- Options chain OI and bid-ask spread at recommended strike
- Short interest >20% of float on BEARISH signals (squeeze risk)
- Weekend theta (Thu/Fri entry with short DTE)
- Low DTE gamma explosion (DTE ≤14)
- Options cost-effectiveness vs 1σ expected move

**Trade ticket enhancements**
- Black-Scholes Greeks: Δ, θ/day, ν/1%IV
- P&L scenarios: BS-repriced estimates at target price / flat 14d / stop loss
- Break-even shown as both $ price **and** % move needed
- Theta shown as weekly % of premium consumed
- Signal validity window: "valid while price > $X and RSI > 50"
- Timeframe scope note and 1–2% position-sizing guidance
- "Max loss / contract" label (was "Cost / contract")

---

## Requirements

- Python **3.10+**
- `pip install -r requirements.txt` (FastAPI, Uvicorn, Pydantic v2, python-dotenv, yfinance, anthropic, playwright)

The sibling `../square18_signals/src/` is added to `sys.path` automatically at startup.

---

## Quick start

```bash
cd square18_signals_web
pip install -r requirements.txt

# Enable Claude features (optional)
cp .env.example .env
# Edit .env: ANTHROPIC_API_KEY=sk-ant-...

./run.sh          # dev mode with auto-reload
./run.sh prod     # no reload
PORT=9000 ./run.sh
```

Then open <http://127.0.0.1:8000/>.

---

## Testing

```bash
# API E2E (FastAPI TestClient)
python -m pytest tests/test_e2e_app.py -q

# Deterministic unit tests
python -m pytest tests/test_indicators.py tests/test_trade_plan_derived_math.py tests/test_market_news.py -q

# Signal thresholds re-tune (needs network, ~30s)
python -m tools.backtest_verdict --search-tau --out backtest_verdict.json

# Browser E2E (Playwright)
python -m playwright install chromium
python -m pytest tests/test_e2e_ui_playwright.py -q
```

---

## API endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/api/health` | Liveness probe |
| GET | `/api/regime` | Market-regime banner + universe counters |
| GET | `/api/screen?filter=…` | Screener rows; filter ∈ all/buy/sell/hold |
| GET | `/api/ticker/{symbol}` | Full detail payload + strategy recommendations |
| GET | `/api/market/pulse?timeframe=…` | Top gainers/losers + sector heatmap |
| GET | `/api/options/highlights?timeframe=…` | Top call/put recommendations |
| GET | `/api/crypto/snapshot` | Crypto prices + 7-day spark |
| GET | `/api/news?limit=…` | Dashboard news (CNBC/MarketWatch/snapshot) |
| GET | `/api/screener/movers?quick=0\|1` | Jumps + dips in one body |
| GET | `/api/screener/jumps` | Top S&P 500 gainers |
| GET | `/api/screener/dips` | Top S&P 500 losers |
| GET | `/api/screener/earnings?window_days=…` | Upcoming S&P 500 earnings |
| GET | `/api/analyst/tickers` | Analyst universe (symbol/name/sector) |
| GET | `/api/analyst/overview?timeframe=…` | Verdict + recommendation row per ticker |
| GET | `/api/analyst/report/{symbol}?timeframe=…&fresh_quotes=0\|1` | Full enhanced technical report |
| GET | `/api/etf/signals?timeframe=…` | ETF watchlist verdicts |
| GET | `/api/analyst/llm-config` | Claude layer status |
| GET | `/api/analyst/polish/{symbol}` | Claude narrative polish |
| GET | `/api/analyst/brief?timeframe=…` | Claude daily desk brief |
| POST | `/api/analyst/explain/{symbol}` | Claude Q&A on a specific report |
| GET | `/api/report/signals?timeframe=…` | Full signal report (JSON) |
| GET | `/api/report/signals.md` | Signal report (Markdown, downloadable) |
| GET | `/api/report/signals.txt` | Signal report (plain text) |

Browse Swagger UI at <http://127.0.0.1:8000/docs>.

---

## Claude layer (optional)

The analytical core is **fully deterministic** — rule-based, reproducible, no LLM in the signal path.
Claude Sonnet 4.5 is layered on top for three narrow tasks, all fail-open:

| Task | Where | Purpose |
| ---- | ----- | ------- |
| Narrative polish | Analyst report | Rewrites deterministic narrative as fluent prose |
| Daily desk brief | Top of Analyst tab | Cross-ticker synthesis by sector/bias |
| Ticket Q&A | End of report | "Why this strike?", "What invalidates the thesis?" |

Prompts wrap the structured report in `<facts>` tags. Claude is instructed never to introduce numbers not present in the payload.

**Enable:**

```bash
# Recommended: use .env (gitignored)
cp .env.example .env
# Set ANTHROPIC_API_KEY in .env, then:
./run.sh

# Or export directly:
export ANTHROPIC_API_KEY=sk-ant-…
export ANTHROPIC_MODEL=claude-sonnet-4-5   # default
export SQUARE18_LLM_CACHE_TTL=86400        # 24h disk cache per prompt
./run.sh prod
```

Responses are cached at `~/.cache/square18_signals/llm/` keyed by `sha256(input)`.

---

## Key files

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI app + all routes |
| `app/analyst/report.py` | Full signal pipeline: scoring, gates, warnings, trade ticket |
| `app/analyst/signal_config.py` | Load/cache `signal_thresholds.json` (hot-reload, 60s TTL) |
| `app/analyst/regime.py` | VIX + market breadth helpers (shared by report + services) |
| `app/analyst/options_flow.py` | Tier-2: UOA, term structure, put-call skew from Yahoo chain |
| `app/analyst/indicators.py` | RSI, MACD, ATR, ADX, Bollinger, Stochastic, RSI divergence |
| `app/analyst/constants.py` | TICKERS, TICKER_MAP, DEFAULT_IV, anchor prices |
| `app/analyst/market.py` | News aggregation, market pulse, options highlights |
| `app/analyst/yahoo_quotes.py` | Spot price + option chain mid + short interest (cached) |
| `signal_thresholds.json` | Tunable verdict thresholds (regenerate with `backtest_verdict --search-tau`) |
| `backtest_verdict.json` | Walk-forward hit-rate + profit-factor per verdict/symbol |
| `tools/backtest_verdict.py` | Walk-forward backtest + τ grid-search |
| `static/app.js` | All UI logic (vanilla JS, no framework) |
| `static/styles.css` | Dark theme, responsive |
| `.env.example` | Template for local secrets (copy to `.env`, never commit) |
