"""Square18 Signals — options pricing, greeks, IV, and strategy recommender.

Public API:

    from square18_signals import (
        black_scholes_price,
        greeks,
        implied_vol,
        iv_rank,
        iv_percentile,
        Strategy,
        StrategyLeg,
        recommend_strategies,
    )

All monetary values are **per-share** (multiply by 100 for per-contract).
Volatilities are **annualized** decimals (0.25 = 25%).
Rates and dividend yields are **continuously-compounded** decimals.
Times are in **years** (30 days ~= 30/365).
"""

from .pricing import (
    OptionType,
    black_scholes_price,
    call_price,
    greeks,
    implied_vol,
    put_price,
)
from .iv import iv_percentile, iv_rank
from .strategies import (
    Strategy,
    StrategyLeg,
    StrategyMetrics,
    bear_call_spread,
    bull_call_spread,
    bull_put_spread,
    cash_secured_put,
    covered_call,
    iron_condor,
    long_call,
    long_put,
    long_straddle,
    long_strangle,
    strategy_metrics,
)
from .recommender import (
    MarketContext,
    Recommendation,
    recommend_strategies,
)

__all__ = [
    "OptionType",
    "black_scholes_price",
    "call_price",
    "put_price",
    "greeks",
    "implied_vol",
    "iv_rank",
    "iv_percentile",
    "Strategy",
    "StrategyLeg",
    "StrategyMetrics",
    "strategy_metrics",
    "long_call",
    "long_put",
    "bull_call_spread",
    "bear_call_spread",
    "bull_put_spread",
    "iron_condor",
    "long_straddle",
    "long_strangle",
    "cash_secured_put",
    "covered_call",
    "MarketContext",
    "Recommendation",
    "recommend_strategies",
]

__version__ = "0.1.0"
