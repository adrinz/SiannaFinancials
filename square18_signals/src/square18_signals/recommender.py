"""Strategy recommender — pick option strategies from market context.

This is a **rule-based** recommender, not ML. The rules encode the
classic retail-trader decision table:

    Direction      IV rank      Preferred structure
    ──────────────────────────────────────────────────────────────
    Bull           Low   (<30)   Debit: long call OR bull call spread
    Bull           High  (>60)   Credit: bull put spread (or CSP)
    Bear           Low   (<30)   Debit: long put OR bear put spread
    Bear           High  (>60)   Credit: bear call spread
    Neutral        High  (>50)   Iron condor (short premium, defined risk)
    Neutral        Low   (<30)   Skip (low edge) OR long straddle if
                                 expecting vol expansion
    Earnings in window           Reduce size or skip entirely

Premiums are estimated via Black-Scholes using the `MarketContext.iv`
so that metrics (max gain / max loss / break-evens / POP) are internally
consistent. In production, you would replace the estimated premiums with
the live option chain mid-prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .pricing import black_scholes_price
from .strategies import (
    Strategy,
    StrategyMetrics,
    bear_call_spread,
    bull_call_spread,
    bull_put_spread,
    cash_secured_put,
    iron_condor,
    long_call,
    long_put,
    long_straddle,
    strategy_metrics,
)

Direction = Literal["bull", "bear", "neutral"]


@dataclass(frozen=True)
class MarketContext:
    """All the context the recommender needs to score strategies.

    - `spot`: current share price.
    - `iv`: current ATM implied volatility (annualized decimal, e.g. 0.28).
    - `iv_rank`: 0..100 — IV rank vs trailing 52-week history.
    - `direction`: from the upstream factor engine.
    - `conviction`: 0..1 — confidence in the direction signal.
    - `dte`: preferred days-to-expiration. Recommender converts to years.
    - `risk_free_rate`: cont. comp. (decimal, e.g. 0.045).
    - `dividend_yield`: cont. comp. (decimal, default 0).
    - `earnings_in_window`: True if an earnings event lands before `dte`
      days — flips the recommender toward smaller, defined-risk, or
      "skip" suggestions.
    """

    symbol: str
    spot: float
    iv: float
    iv_rank: float
    direction: Direction
    conviction: float = 0.6
    dte: int = 35
    risk_free_rate: float = 0.045
    dividend_yield: float = 0.0
    earnings_in_window: bool = False

    def __post_init__(self) -> None:
        if self.spot <= 0:
            raise ValueError("spot must be > 0")
        if self.iv < 0:
            raise ValueError("iv must be >= 0")
        if not 0.0 <= self.iv_rank <= 100.0:
            raise ValueError("iv_rank must be in [0, 100]")
        if self.direction not in ("bull", "bear", "neutral"):
            raise ValueError(f"unknown direction: {self.direction}")
        if not 0.0 <= self.conviction <= 1.0:
            raise ValueError("conviction must be in [0, 1]")
        if self.dte <= 0:
            raise ValueError("dte must be > 0")

    @property
    def T(self) -> float:  # noqa: N802 — T is the convention in finance
        """Time to expiration in years."""
        return self.dte / 365.0


@dataclass(frozen=True)
class Recommendation:
    strategy: Strategy
    metrics: StrategyMetrics
    rationale: str
    fit_score: float  # 0..1 — higher = better fit with the context
    tags: tuple[str, ...] = field(default=())


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------


def _strike_spacing(spot: float) -> float:
    """Typical listed-strike spacing in USD for a given spot price."""
    if spot < 25:
        return 0.5
    if spot < 100:
        return 1.0
    if spot < 200:
        return 2.5
    if spot < 500:
        return 5.0
    return 10.0


def _round_strike(x: float, spot: float) -> float:
    step = _strike_spacing(spot)
    return round(x / step) * step


def _one_sd_move(ctx: MarketContext) -> float:
    """One-sigma lognormal move in dollars over the horizon."""
    import math

    return ctx.spot * ctx.iv * math.sqrt(ctx.T)


# ---------------------------------------------------------------------------
# Pricing helper
# ---------------------------------------------------------------------------


def _price(
    ctx: MarketContext,
    strike: float,
    kind: Literal["call", "put"],
    iv_override: float | None = None,
) -> float:
    """Black-Scholes fair value per share for a given leg."""
    sigma = iv_override if iv_override is not None else ctx.iv
    return black_scholes_price(
        S=ctx.spot,
        K=strike,
        T=ctx.T,
        r=ctx.risk_free_rate,
        sigma=sigma,
        option_type=kind,
        q=ctx.dividend_yield,
    )


# ---------------------------------------------------------------------------
# Candidate builders (each returns a Strategy or raises)
# ---------------------------------------------------------------------------


def _build_long_call(ctx: MarketContext) -> Strategy:
    # Slightly OTM call, ~1/2 SD above spot.
    strike = _round_strike(ctx.spot + 0.5 * _one_sd_move(ctx), ctx.spot)
    premium = _price(ctx, strike, "call")
    return long_call(strike, premium)


def _build_long_put(ctx: MarketContext) -> Strategy:
    strike = _round_strike(ctx.spot - 0.5 * _one_sd_move(ctx), ctx.spot)
    premium = _price(ctx, strike, "put")
    return long_put(strike, premium)


def _build_bull_call_spread(ctx: MarketContext) -> Strategy:
    sd = _one_sd_move(ctx)
    long_strike = _round_strike(ctx.spot + 0.25 * sd, ctx.spot)
    short_strike = _round_strike(ctx.spot + 1.25 * sd, ctx.spot)
    if short_strike <= long_strike:
        short_strike = long_strike + _strike_spacing(ctx.spot)
    return bull_call_spread(
        long_strike=long_strike,
        short_strike=short_strike,
        long_premium=_price(ctx, long_strike, "call"),
        short_premium=_price(ctx, short_strike, "call"),
    )


def _build_bear_call_spread(ctx: MarketContext) -> Strategy:
    sd = _one_sd_move(ctx)
    short_strike = _round_strike(ctx.spot + 0.75 * sd, ctx.spot)
    long_strike = _round_strike(ctx.spot + 1.75 * sd, ctx.spot)
    if long_strike <= short_strike:
        long_strike = short_strike + _strike_spacing(ctx.spot)
    return bear_call_spread(
        short_strike=short_strike,
        long_strike=long_strike,
        short_premium=_price(ctx, short_strike, "call"),
        long_premium=_price(ctx, long_strike, "call"),
    )


def _build_bull_put_spread(ctx: MarketContext) -> Strategy:
    sd = _one_sd_move(ctx)
    short_strike = _round_strike(ctx.spot - 0.75 * sd, ctx.spot)
    long_strike = _round_strike(ctx.spot - 1.75 * sd, ctx.spot)
    if long_strike >= short_strike:
        long_strike = short_strike - _strike_spacing(ctx.spot)
    return bull_put_spread(
        short_strike=short_strike,
        long_strike=long_strike,
        short_premium=_price(ctx, short_strike, "put"),
        long_premium=_price(ctx, long_strike, "put"),
    )


def _build_cash_secured_put(ctx: MarketContext) -> Strategy:
    strike = _round_strike(ctx.spot - 0.75 * _one_sd_move(ctx), ctx.spot)
    return cash_secured_put(strike, _price(ctx, strike, "put"))


def _build_iron_condor(ctx: MarketContext) -> Strategy:
    sd = _one_sd_move(ctx)
    put_short = _round_strike(ctx.spot - 1.0 * sd, ctx.spot)
    put_long = _round_strike(ctx.spot - 2.0 * sd, ctx.spot)
    call_short = _round_strike(ctx.spot + 1.0 * sd, ctx.spot)
    call_long = _round_strike(ctx.spot + 2.0 * sd, ctx.spot)
    # Make sure ordering is strict, nudge by one step if rounding collides.
    step = _strike_spacing(ctx.spot)
    if put_long >= put_short:
        put_long = put_short - step
    if call_long <= call_short:
        call_long = call_short + step
    return iron_condor(
        put_long_strike=put_long,
        put_short_strike=put_short,
        call_short_strike=call_short,
        call_long_strike=call_long,
        put_long_premium=_price(ctx, put_long, "put"),
        put_short_premium=_price(ctx, put_short, "put"),
        call_short_premium=_price(ctx, call_short, "call"),
        call_long_premium=_price(ctx, call_long, "call"),
    )


def _build_long_straddle(ctx: MarketContext) -> Strategy:
    strike = _round_strike(ctx.spot, ctx.spot)
    return long_straddle(
        strike=strike,
        call_premium=_price(ctx, strike, "call"),
        put_premium=_price(ctx, strike, "put"),
    )


# ---------------------------------------------------------------------------
# Fit scoring
# ---------------------------------------------------------------------------


def _direction_fit(ctx: MarketContext, strategy_direction: Direction) -> float:
    """1.0 if direction matches context, 0.5 if neutral vs directional, 0 otherwise."""
    if strategy_direction == ctx.direction:
        return 1.0
    if strategy_direction == "neutral" or ctx.direction == "neutral":
        return 0.5
    return 0.0


def _iv_fit(iv_rank: float, preference: Literal["low", "high", "any"]) -> float:
    """Score how well IV rank matches the strategy's ideal IV regime."""
    # "low" preference: debit structures → want low IV (lower = better).
    # "high" preference: credit structures → want high IV (higher = better).
    if preference == "low":
        return max(0.0, min(1.0, (100.0 - iv_rank) / 100.0))
    if preference == "high":
        return max(0.0, min(1.0, iv_rank / 100.0))
    return 0.5


def _event_penalty(ctx: MarketContext) -> float:
    return 0.6 if ctx.earnings_in_window else 1.0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def recommend_strategies(
    ctx: MarketContext,
    *,
    max_results: int = 3,
) -> list[Recommendation]:
    """Return up to `max_results` ranked strategy recommendations.

    Ranking is by `fit_score` (descending), which blends:

    1. **Direction fit** — does the strategy profit in the forecast regime?
    2. **IV fit** — debit structures prefer low IV, credit prefer high IV.
    3. **Event penalty** — strategies with undefined risk get dinged when
       earnings fall inside the holding window.

    Only strategies that make directional sense are considered — e.g.
    we don't suggest a bull call spread on a bear signal.
    """
    if max_results <= 0:
        raise ValueError("max_results must be > 0")

    candidates: list[tuple[Strategy, Direction, Literal["low", "high", "any"], str, tuple[str, ...]]] = []

    if ctx.direction == "bull":
        if ctx.iv_rank < 50:
            candidates.append((
                _build_bull_call_spread(ctx),
                "bull", "low",
                "Bullish signal with low-ish IV rank — a defined-risk debit spread "
                "captures upside while paying the cheaper premium.",
                ("debit", "defined-risk"),
            ))
            candidates.append((
                _build_long_call(ctx),
                "bull", "low",
                "Pure long delta — uncapped upside, higher theta bleed. "
                "Use when you want maximum leverage to the move.",
                ("debit", "unlimited-upside"),
            ))
        if ctx.iv_rank > 40:
            candidates.append((
                _build_bull_put_spread(ctx),
                "bull", "high",
                "Elevated IV favors credit structures. A bull put spread "
                "collects premium while you wait; defined risk below.",
                ("credit", "defined-risk"),
            ))
            candidates.append((
                _build_cash_secured_put(ctx),
                "bull", "high",
                "Cash-secured put — you'd be a willing owner at the strike, "
                "and high IV juices the premium you collect.",
                ("credit", "willing-owner", "undefined-downside"),
            ))

    elif ctx.direction == "bear":
        if ctx.iv_rank < 50:
            candidates.append((
                _build_long_put(ctx),
                "bear", "low",
                "Bearish signal with low IV — long put is cheap and offers "
                "uncapped downside participation.",
                ("debit",),
            ))
        if ctx.iv_rank > 40:
            candidates.append((
                _build_bear_call_spread(ctx),
                "bear", "high",
                "High IV favors premium sellers. Bear call spread caps risk "
                "above resistance and collects credit.",
                ("credit", "defined-risk"),
            ))

    else:  # neutral
        if ctx.iv_rank > 40:
            candidates.append((
                _build_iron_condor(ctx),
                "neutral", "high",
                "Range-bound view + elevated IV = iron condor. Collect premium "
                "in a defined-risk box around the expected-move cone.",
                ("credit", "defined-risk", "range-bound"),
            ))
        if ctx.iv_rank < 30:
            candidates.append((
                _build_long_straddle(ctx),
                "neutral", "low",
                "Low IV + expecting a vol expansion → long straddle. Profits "
                "on a big move in either direction, loses to time decay.",
                ("debit", "vol-expansion"),
            ))

    scored: list[Recommendation] = []
    for strat, strat_dir, iv_pref, rationale, tags in candidates:
        metrics = strategy_metrics(
            strat, ctx.spot, ctx.T, ctx.risk_free_rate, ctx.iv, ctx.dividend_yield
        )
        dir_score = _direction_fit(ctx, strat_dir)
        iv_score = _iv_fit(ctx.iv_rank, iv_pref)
        event_mult = _event_penalty(ctx)
        # Weight direction and IV equally; multiply by event penalty.
        fit = 0.5 * (dir_score + iv_score) * event_mult
        # Small boost for conviction.
        fit = min(1.0, fit + 0.1 * ctx.conviction * dir_score)
        # Penalty for undefined-loss when the recommender is uncertain.
        if "undefined-downside" in tags and ctx.conviction < 0.55:
            fit *= 0.85
        if ctx.earnings_in_window and "undefined-downside" in tags:
            fit *= 0.5  # compounded hit: vol + undefined loss is bad combo

        scored.append(
            Recommendation(
                strategy=strat,
                metrics=metrics,
                rationale=rationale,
                fit_score=fit,
                tags=tags,
            )
        )

    scored.sort(key=lambda r: r.fit_score, reverse=True)
    return scored[:max_results]
