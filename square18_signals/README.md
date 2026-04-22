# square18_signals

Options pricing, greeks, implied volatility, strategy payoffs, and a
rule-based strategy recommender for the **Square18 Signals** stock &
options analyzer.

This is the first M1/M2 slice of the roadmap outlined in the project
canvas: the math and decision engine that powers the "Options Strategy
Recommender" panel on the Ticker Detail view.

- Zero runtime dependencies (pure Python + stdlib `math`).
- Deterministic — no hidden state, no network calls, fully unit-tested.
- Works in constrained sandboxes; `numpy` is *not* required.

## Install / run locally

```bash
# from repo root
cd square18_signals
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # installs pytest for tests
pytest -v                 # 82 passing tests
```

If you don't want a venv, the `tests/conftest.py` shim adds `src/` to
`sys.path`, so `python3 -m pytest square18_signals/tests/ --rootdir=square18_signals`
from the repo root also works.

## Public API

```python
from square18_signals import (
    # pricing
    black_scholes_price, call_price, put_price, greeks, implied_vol,
    # iv utilities
    iv_rank, iv_percentile,
    # strategy primitives
    Strategy, StrategyLeg, strategy_metrics,
    # strategy factories
    long_call, long_put,
    bull_call_spread, bear_call_spread, bull_put_spread,
    iron_condor, long_straddle, long_strangle,
    cash_secured_put, covered_call,
    # recommender
    MarketContext, Recommendation, recommend_strategies,
)
```

### Pricing a European option

```python
from square18_signals import call_price, greeks, implied_vol

price = call_price(S=100, K=105, T=30/365, r=0.045, sigma=0.28)
g = greeks(S=100, K=105, T=30/365, r=0.045, sigma=0.28, option_type="call")
print(g.delta, g.theta_per_day, g.vega_per_pct)

# Solve for the IV that reproduces a market price
iv = implied_vol(target_price=3.40, S=100, K=105, T=30/365, r=0.045, option_type="call")
```

### Computing strategy metrics

```python
from square18_signals import bull_call_spread, strategy_metrics

spread = bull_call_spread(
    long_strike=415, short_strike=430,
    long_premium=12.40, short_premium=6.10,
    contracts=1,
)
m = strategy_metrics(spread, S=412.83, T=35/365, r=0.045, sigma=0.28)
print(m.net_debit)              # 630.0
print(m.max_gain, m.max_loss)   # 870.0, 630.0
print(m.breakevens)             # (421.30,)
print(m.probability_of_profit)  # ~0.48 under BSM risk-neutral
```

### Running the recommender

```python
from square18_signals import MarketContext, recommend_strategies

ctx = MarketContext(
    symbol="NVDA", spot=412.83,
    iv=0.32, iv_rank=24,
    direction="bull", conviction=0.82,
    dte=35,
)
for rec in recommend_strategies(ctx):
    print(f"{rec.strategy.name:40s} fit={rec.fit_score:.2f} "
          f"POP={rec.metrics.probability_of_profit:.2f}")
    print(f"  {rec.rationale}")
```

Example output:

```
Bull call spread 420/445                 fit=0.85 POP=0.46
  Bullish signal with low-ish IV rank — a defined-risk debit spread…
Long 425 call                            fit=0.78 POP=0.39
  Pure long delta — uncapped upside, higher theta bleed…
```

## Math conventions

| Quantity        | Units                                                     |
| --------------- | --------------------------------------------------------- |
| Spot / strike   | USD per share                                             |
| Premium         | USD per share                                             |
| Volatility `σ`  | annualized decimal (0.25 = 25%)                           |
| Time `T`        | years (30 days ≈ 30/365)                                  |
| Rate `r`, div `q` | continuously-compounded decimal                         |
| `quantity` on a leg | **share-equivalents** — 100 per standard options contract |
| PnL / metrics   | USD for the given quantities                              |

`theta` follows the **trader convention** (dP/dt where `t` is calendar
time): long-option theta is *negative* — price decays as time passes.
Divide by 365 for "per day" via `.theta_per_day`.

### Black-Scholes formulas used

```
d1 = (ln(S/K) + (r - q + σ²/2) T) / (σ √T)
d2 = d1 - σ √T

C = S e^{-qT} N(d1) - K e^{-rT} N(d2)
P = K e^{-rT} N(-d2) - S e^{-qT} N(-d1)
```

The normal CDF uses `math.erf`, which is exact to float precision.
There is no table lookup or polynomial approximation — no accuracy
trade-off.

### Strategy payoff math

Every strategy is a sum of piecewise-linear leg PnLs in spot `S_T`,
with kinks at the strikes. That means:

- **Max gain / max loss** are found by evaluating PnL at all kinks
  (plus `S_T = 0` for the left boundary) and inspecting the asymptotic
  slopes `slope_left = Σ slopes at S_T → 0+` and
  `slope_right = Σ slopes at S_T → ∞`. If `slope_right > 0` the
  strategy has unbounded upside; similarly for `slope_right < 0`
  (unbounded downside, e.g. a naked short call).
- **Break-evens** are the zero-crossings. Between consecutive kinks
  we solve by linear interpolation; outside the outermost kinks we
  extrapolate at the known asymptotic slope. No numerical root
  finder is needed — the math is exact.
- **Probability of profit** is computed under the Black-Scholes
  risk-neutral measure. The distribution of `S_T` is lognormal:
  `ln(S_T) ~ N(ln(S) + (r − q − σ²/2) T, σ² T)`. We sum
  `P(S_T ∈ interval)` over every interval between break-evens
  whose representative point has positive PnL. Equivalently,
  this is the closed-form analog of a numerical `∫ 𝟙[PnL>0] · f(S_T) dS_T`.

  Note: this is the **risk-neutral** POP, which is what a fair pricing
  model implies. Real-world POP under a positive drift (the stock's
  expected return) would be slightly higher for bullish strategies
  and lower for bearish ones. Retail brokers typically display the
  risk-neutral flavor.

### IV rank vs IV percentile

```
iv_rank       = (IV_now − min(history))  /  (max(history) − min(history)) · 100
iv_percentile = |{ x ∈ history : x < IV_now }|  /  |history| · 100
```

IV rank is sensitive to outliers; IV percentile is robust. The
recommender uses rank today; switching to percentile is a one-line
change.

## Strategy decision table (recommender)

| Direction | IV rank     | Preferred structure                         |
| --------- | ----------- | ------------------------------------------- |
| Bull      | Low  (<50)  | Long call OR bull call debit spread         |
| Bull      | High (>40)* | Bull put credit spread OR cash-secured put  |
| Bear      | Low  (<50)  | Long put                                    |
| Bear      | High (>40)* | Bear call credit spread                     |
| Neutral   | High (>40)  | Iron condor                                 |
| Neutral   | Low  (<30)  | Long straddle (betting on vol expansion)    |

(*) Overlapping thresholds intentionally surface multiple candidates in
the "neighborhood" regime so the user can pick. Rankings within a
regime are by `fit_score = 0.5 · (direction_fit + iv_fit) ·
event_penalty + 0.1 · conviction · direction_fit`.

Strategies with undefined downside (e.g. naked CSP) are penalized when
conviction is low, or when earnings falls in the holding window.

## What's deliberately out of scope here

- **American-style early exercise** — Black-Scholes is European. For
  American options on dividend-paying stocks, a binomial tree or
  finite-difference solver is needed. That's a future module.
- **Skew / term-structure vol surface** — we accept a single ATM IV.
  A full skew model plugs in at the `_price()` helper in
  `recommender.py`.
- **Live data** — everything here is deterministic math. The data
  provider layer (Polygon/Tradier/yfinance) is separate and is the
  next module to build.
- **Backtesting** — coming in M3 with `vectorbt`.

## Tests

Run the full suite:

```bash
pytest -v
```

What's covered (82 tests):

- **Pricing** — Hull textbook benchmarks, put-call parity on 5 param
  sets, monotonicity in S/K/σ, degenerate `T=0` and `σ=0` cases,
  input validation, option-type aliases.
- **Greeks** — all five greeks vs finite differences on calls and puts,
  delta bounds, greeks at expiration, unit conversions.
- **Implied vol** — round-trip on 6 param sets (ATM/ITM/OTM, calls
  and puts, dividend and non-dividend), out-of-bounds rejection,
  monotonicity as price approaches intrinsic, deep-OTM bisection
  fallback.
- **Strategies** — per-leg validation, PnL at every kink for every
  factory (long call, long put, spreads, condor, straddle, strangle,
  CSP, covered call), exact max gain / max loss / break-evens vs
  closed-form, POP bounds in [0,1], POP monotone in vol for straddle
  (up) and condor (down), contract-count scaling.
- **Recommender** — direction routing (bull/bear/neutral × low/high IV),
  sort order, fit-score bounds, earnings-in-window penalty,
  `max_results` respected, context-input validation.
