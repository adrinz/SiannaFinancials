"""End-to-end demo: feed three tickers into the recommender and print
the ranked suggestions with full metrics.

Run from the repo root:

    python3 square18_signals/examples/demo.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

# Make the src-layout package importable without a full install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from square18_signals import (
    MarketContext,
    call_price,
    greeks,
    implied_vol,
    iv_rank,
    recommend_strategies,
)


def _fmt_money(x: float) -> str:
    if math.isinf(x):
        return "∞"
    return f"${x:,.2f}"


def _demo_pricing() -> None:
    print("— Pricing check: NVDA 30-day 420 call at 28% IV —")
    S, K, T, r, sigma = 412.83, 420.0, 30 / 365.0, 0.045, 0.28
    fair = call_price(S, K, T, r, sigma)
    g = greeks(S, K, T, r, sigma, "call")
    print(f"  Fair value (per share): ${fair:.3f}  (per contract: ${fair * 100:.2f})")
    print(
        f"  Delta={g.delta:+.3f}  Gamma={g.gamma:+.4f}  "
        f"Vega/1%={g.vega_per_pct:+.3f}  Theta/day={g.theta_per_day:+.3f}"
    )
    # Round-trip IV.
    recovered = implied_vol(fair, S, K, T, r, option_type="call")
    print(f"  Implied vol from fair price: {recovered:.4f}  (input 0.2800)")
    print()


def _demo_iv_rank() -> None:
    print("— IV rank example —")
    history = [0.18 + 0.05 * math.sin(i / 10.0) for i in range(252)]
    today = 0.23
    rank = iv_rank(today, history)
    print(
        f"  current IV {today:.2f} vs 252d history "
        f"[{min(history):.2f}, {max(history):.2f}] → IV rank {rank:.1f}"
    )
    print()


def _demo_recommender() -> None:
    print("— Recommender: three market contexts —")
    contexts = [
        MarketContext(
            symbol="NVDA", spot=412.83, iv=0.32, iv_rank=24,
            direction="bull", conviction=0.82, dte=35,
        ),
        MarketContext(
            symbol="TSLA", spot=241.07, iv=0.55, iv_rank=68,
            direction="bear", conviction=0.70, dte=30,
        ),
        MarketContext(
            symbol="SPY", spot=518.72, iv=0.16, iv_rank=42,
            direction="neutral", conviction=0.55, dte=45,
        ),
    ]

    for ctx in contexts:
        print(
            f"\n  {ctx.symbol}  spot={ctx.spot}  IV={ctx.iv:.2f}  "
            f"IVR={ctx.iv_rank:.0f}  dir={ctx.direction}  dte={ctx.dte}"
        )
        recs = recommend_strategies(ctx, max_results=3)
        for i, rec in enumerate(recs, 1):
            m = rec.metrics
            be = ", ".join(f"{b:.2f}" for b in m.breakevens) or "—"
            side = "debit" if m.net_debit >= 0 else "credit"
            amt = abs(m.net_debit)
            print(
                f"    {i}. {rec.strategy.name:38s}  fit={rec.fit_score:.2f}"
                f"  POP={m.probability_of_profit:.0%}"
            )
            print(
                f"       max gain {_fmt_money(m.max_gain):>10s}  "
                f"max loss {_fmt_money(m.max_loss):>10s}  "
                f"break-even(s) {be}"
            )
            print(f"       net {side} ${amt:,.2f} — {rec.rationale}")


if __name__ == "__main__":
    _demo_pricing()
    _demo_iv_rank()
    _demo_recommender()
