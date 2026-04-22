"""Options strategies — payoff math, metrics, and factories.

A `Strategy` is a list of `StrategyLeg` objects. Each leg has a kind
(`"call"`, `"put"`, or `"stock"`), a side (`"long"` or `"short"`),
a strike (None for stock), a premium (per share — for stock this is
the entry price), and a quantity in **share-equivalents** (100 per
standard options contract).

All PnL values are in **dollars** for the given quantities.

For a plain-vanilla European option, the PnL at expiration is piecewise
linear in spot S_T, with kinks at the strikes. This module exploits
that fact to compute exact max gain, max loss, break-evens, and
probability of profit under the Black-Scholes risk-neutral measure.

Factories (`long_call`, `bull_call_spread`, `iron_condor`, …) accept a
`contracts` kwarg that multiplies per-share premiums by 100 to produce
per-contract dollar metrics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

from .pricing import _d1_d2, _norm_cdf  # internal: risk-neutral lognormal

CONTRACT_MULTIPLIER: int = 100

LegKind = Literal["call", "put", "stock"]
LegSide = Literal["long", "short"]


@dataclass(frozen=True)
class StrategyLeg:
    """A single leg of an options strategy.

    Attributes:
        kind: "call", "put", or "stock".
        side: "long" or "short".
        strike: strike price per share. Must be ``None`` iff ``kind == "stock"``.
        premium: premium paid per share (for stock: entry price per share).
        quantity: share-equivalents. 100.0 == one standard options contract.
    """

    kind: LegKind
    side: LegSide
    strike: float | None = None
    premium: float = 0.0
    quantity: float = float(CONTRACT_MULTIPLIER)

    def __post_init__(self) -> None:
        if self.kind == "stock":
            if self.strike is not None:
                raise ValueError("stock leg must not have a strike")
        else:
            if self.strike is None:
                raise ValueError(f"{self.kind} leg requires a strike")
            if self.strike <= 0:
                raise ValueError(f"strike must be > 0, got {self.strike}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        if self.premium < 0:
            raise ValueError(f"premium must be >= 0, got {self.premium}")

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1

    def intrinsic(self, S_T: float) -> float:
        """Intrinsic value at expiration, per share, always >= 0."""
        if self.kind == "call":
            assert self.strike is not None
            return max(S_T - self.strike, 0.0)
        if self.kind == "put":
            assert self.strike is not None
            return max(self.strike - S_T, 0.0)
        return S_T  # stock

    def pnl(self, S_T: float) -> float:
        """PnL in dollars at expiration given spot S_T."""
        return self.sign * self.quantity * (self.intrinsic(S_T) - self.premium)

    def slope(self, S_T: float) -> float:
        """dPnL/dS_T at an interior point (i.e. not exactly at the strike).

        For stock, slope is ±quantity everywhere. For calls, slope flips at
        the strike from 0 (below) to ±1 (above). For puts, from ∓1 (below)
        to 0 (above). At exactly S_T == strike the left-derivative is used.
        """
        if self.kind == "stock":
            return self.sign * self.quantity
        assert self.strike is not None
        if self.kind == "call":
            active = 1.0 if S_T > self.strike else 0.0
            return self.sign * self.quantity * active
        # put
        active = 1.0 if S_T < self.strike else 0.0
        return -self.sign * self.quantity * active


@dataclass(frozen=True)
class Strategy:
    """A composed options strategy — a sequence of legs with a display name."""

    name: str
    legs: tuple[StrategyLeg, ...]

    def __post_init__(self) -> None:
        if not self.legs:
            raise ValueError("strategy must have at least one leg")

    def pnl(self, S_T: float) -> float:
        return sum(leg.pnl(S_T) for leg in self.legs)

    def net_debit(self) -> float:
        """Positive = net debit paid. Negative = net credit received."""
        return sum(leg.sign * leg.quantity * leg.premium for leg in self.legs)

    def kinks(self) -> list[float]:
        """Sorted unique strikes across all option legs."""
        return sorted({leg.strike for leg in self.legs if leg.strike is not None})


@dataclass(frozen=True)
class StrategyMetrics:
    """Summary metrics for a strategy at expiration.

    - `net_debit`: positive = debit, negative = credit.
    - `max_gain` / `max_loss`: in dollars. `math.inf` when unbounded.
      `max_loss` is reported as a **positive magnitude** (so "lose $500"
      is `max_loss=500.0`).
    - `breakevens`: sorted tuple of spot prices where PnL == 0.
    - `probability_of_profit`: risk-neutral P(PnL > 0) under BSM lognormal.
    """

    net_debit: float
    max_gain: float
    max_loss: float
    breakevens: tuple[float, ...]
    probability_of_profit: float


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _mult(contracts: int) -> float:
    if contracts <= 0:
        raise ValueError(f"contracts must be > 0, got {contracts}")
    return float(contracts * CONTRACT_MULTIPLIER)


def long_call(strike: float, premium: float, *, contracts: int = 1) -> Strategy:
    return Strategy(
        name=f"Long {strike:g} call",
        legs=(
            StrategyLeg("call", "long", strike=strike, premium=premium, quantity=_mult(contracts)),
        ),
    )


def long_put(strike: float, premium: float, *, contracts: int = 1) -> Strategy:
    return Strategy(
        name=f"Long {strike:g} put",
        legs=(
            StrategyLeg("put", "long", strike=strike, premium=premium, quantity=_mult(contracts)),
        ),
    )


def bull_call_spread(
    long_strike: float,
    short_strike: float,
    long_premium: float,
    short_premium: float,
    *,
    contracts: int = 1,
) -> Strategy:
    if short_strike <= long_strike:
        raise ValueError("bull call spread: short strike must be > long strike")
    q = _mult(contracts)
    return Strategy(
        name=f"Bull call spread {long_strike:g}/{short_strike:g}",
        legs=(
            StrategyLeg("call", "long", strike=long_strike, premium=long_premium, quantity=q),
            StrategyLeg("call", "short", strike=short_strike, premium=short_premium, quantity=q),
        ),
    )


def bear_call_spread(
    short_strike: float,
    long_strike: float,
    short_premium: float,
    long_premium: float,
    *,
    contracts: int = 1,
) -> Strategy:
    if long_strike <= short_strike:
        raise ValueError("bear call spread: long strike must be > short strike")
    q = _mult(contracts)
    return Strategy(
        name=f"Bear call spread {short_strike:g}/{long_strike:g}",
        legs=(
            StrategyLeg("call", "short", strike=short_strike, premium=short_premium, quantity=q),
            StrategyLeg("call", "long", strike=long_strike, premium=long_premium, quantity=q),
        ),
    )


def bull_put_spread(
    short_strike: float,
    long_strike: float,
    short_premium: float,
    long_premium: float,
    *,
    contracts: int = 1,
) -> Strategy:
    if long_strike >= short_strike:
        raise ValueError("bull put spread: long put strike must be < short put strike")
    q = _mult(contracts)
    return Strategy(
        name=f"Bull put spread {long_strike:g}/{short_strike:g}",
        legs=(
            StrategyLeg("put", "short", strike=short_strike, premium=short_premium, quantity=q),
            StrategyLeg("put", "long", strike=long_strike, premium=long_premium, quantity=q),
        ),
    )


def iron_condor(
    put_long_strike: float,
    put_short_strike: float,
    call_short_strike: float,
    call_long_strike: float,
    put_long_premium: float,
    put_short_premium: float,
    call_short_premium: float,
    call_long_premium: float,
    *,
    contracts: int = 1,
) -> Strategy:
    if not (put_long_strike < put_short_strike < call_short_strike < call_long_strike):
        raise ValueError(
            "iron condor strikes must satisfy:"
            " put_long < put_short < call_short < call_long"
        )
    q = _mult(contracts)
    return Strategy(
        name=(
            f"Iron condor {put_long_strike:g}/{put_short_strike:g}"
            f"/{call_short_strike:g}/{call_long_strike:g}"
        ),
        legs=(
            StrategyLeg("put", "long", strike=put_long_strike, premium=put_long_premium, quantity=q),
            StrategyLeg("put", "short", strike=put_short_strike, premium=put_short_premium, quantity=q),
            StrategyLeg("call", "short", strike=call_short_strike, premium=call_short_premium, quantity=q),
            StrategyLeg("call", "long", strike=call_long_strike, premium=call_long_premium, quantity=q),
        ),
    )


def long_straddle(strike: float, call_premium: float, put_premium: float, *, contracts: int = 1) -> Strategy:
    q = _mult(contracts)
    return Strategy(
        name=f"Long straddle {strike:g}",
        legs=(
            StrategyLeg("call", "long", strike=strike, premium=call_premium, quantity=q),
            StrategyLeg("put", "long", strike=strike, premium=put_premium, quantity=q),
        ),
    )


def long_strangle(
    put_strike: float,
    call_strike: float,
    put_premium: float,
    call_premium: float,
    *,
    contracts: int = 1,
) -> Strategy:
    if put_strike >= call_strike:
        raise ValueError("long strangle: put strike must be < call strike")
    q = _mult(contracts)
    return Strategy(
        name=f"Long strangle {put_strike:g}/{call_strike:g}",
        legs=(
            StrategyLeg("put", "long", strike=put_strike, premium=put_premium, quantity=q),
            StrategyLeg("call", "long", strike=call_strike, premium=call_premium, quantity=q),
        ),
    )


def cash_secured_put(strike: float, premium: float, *, contracts: int = 1) -> Strategy:
    """Cash-secured put — sell a put, hold collateral to cover assignment."""
    return Strategy(
        name=f"Cash-secured {strike:g} put",
        legs=(
            StrategyLeg("put", "short", strike=strike, premium=premium, quantity=_mult(contracts)),
        ),
    )


def covered_call(
    share_entry_price: float,
    call_strike: float,
    call_premium: float,
    *,
    shares: int = 100,
) -> Strategy:
    """Long stock + short call against it (one contract per 100 shares)."""
    contracts = shares // CONTRACT_MULTIPLIER
    if contracts <= 0 or shares % CONTRACT_MULTIPLIER != 0:
        raise ValueError(
            f"covered call: shares must be a positive multiple of"
            f" {CONTRACT_MULTIPLIER} (got {shares})"
        )
    return Strategy(
        name=f"Covered call {call_strike:g} @ {share_entry_price:g}",
        legs=(
            StrategyLeg("stock", "long", premium=share_entry_price, quantity=float(shares)),
            StrategyLeg("call", "short", strike=call_strike, premium=call_premium, quantity=_mult(contracts)),
        ),
    )


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def _payoff_slope_at_extreme(strategy: Strategy, side: Literal["left", "right"]) -> float:
    """Slope of total PnL outside the kink range (S_T -> 0 or S_T -> +inf)."""
    total = 0.0
    for leg in strategy.legs:
        if leg.kind == "stock":
            total += leg.sign * leg.quantity
        elif leg.kind == "call":
            # Call is active (slope +1) only *above* its strike.
            if side == "right":
                total += leg.sign * leg.quantity
        else:  # put
            # Put is active (slope -1) only *below* its strike.
            if side == "left":
                total += -leg.sign * leg.quantity
    return total


def _find_breakevens(
    strategy: Strategy,
    pnl_at_kinks: Sequence[tuple[float, float]],
    slope_left: float,
    slope_right: float,
) -> list[float]:
    """Return sorted list of break-even points (PnL == 0).

    `pnl_at_kinks` is (strike, pnl) sorted by strike. Because the PnL is
    piecewise linear between kinks and linear below the first / above the
    last strike with known slopes, we can find zero-crossings exactly.
    """
    eps_zero = 1e-10
    breakevens: list[float] = []

    # Segment below the lowest strike: linear from PnL at S=0 to pnl_at_kinks[0].
    # Easier: use pnl_at_kinks[0] and extrapolate leftward at slope_left.
    if pnl_at_kinks:
        first_strike, first_pnl = pnl_at_kinks[0]
        # For S_T < first_strike: pnl(S) = first_pnl + slope_left * (S - first_strike)
        # Solve for pnl == 0:   S = first_strike - first_pnl / slope_left
        if abs(slope_left) > eps_zero:
            s = first_strike - first_pnl / slope_left
            if 0 < s < first_strike:
                breakevens.append(s)

        # Segments between adjacent kinks: linear, just interpolate.
        for (s1, p1), (s2, p2) in zip(pnl_at_kinks, pnl_at_kinks[1:]):
            # Same sign? no zero-crossing (unless one is exactly zero).
            if p1 == 0:
                breakevens.append(s1)
            if p1 * p2 < 0:
                # linear interpolation
                frac = p1 / (p1 - p2)
                breakevens.append(s1 + frac * (s2 - s1))
        # Check last kink exactly-zero separately to avoid dup with segment logic.
        last_strike, last_pnl = pnl_at_kinks[-1]
        if last_pnl == 0 and (not breakevens or breakevens[-1] != last_strike):
            breakevens.append(last_strike)

        # Segment above the highest strike: pnl(S) = last_pnl + slope_right * (S - last_strike)
        if abs(slope_right) > eps_zero:
            s = last_strike - last_pnl / slope_right
            if s > last_strike:
                breakevens.append(s)

    # De-dup and sort
    deduped: list[float] = []
    for b in sorted(breakevens):
        if not deduped or abs(b - deduped[-1]) > 1e-9:
            deduped.append(b)
    return deduped


def _risk_neutral_prob_below(
    x: float, S: float, T: float, r: float, sigma: float, q: float
) -> float:
    """Risk-neutral P(S_T <= x) under Black-Scholes lognormal."""
    if x <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        # Degenerate: S_T is deterministic at S * exp((r-q)T). Step function.
        fwd = S * math.exp((r - q) * max(T, 0.0))
        return 1.0 if x >= fwd else 0.0
    # ln(S_T) ~ N(ln(S) + (r - q - 0.5 sigma^2) T, sigma^2 T)
    # P(S_T <= x) = N((ln(x/S) - (r - q - 0.5 sigma^2) T) / (sigma sqrt(T)))
    num = math.log(x / S) - (r - q - 0.5 * sigma * sigma) * T
    return _norm_cdf(num / (sigma * math.sqrt(T)))


def strategy_metrics(
    strategy: Strategy,
    S: float,
    T: float,
    r: float,
    sigma: float,
    q: float = 0.0,
) -> StrategyMetrics:
    """Exact max gain / max loss / break-evens / risk-neutral POP.

    Args:
        strategy: the composed strategy.
        S: current spot (used only for POP).
        T: years to expiration (used only for POP).
        r: risk-free rate, cont. comp.
        sigma: annualized vol (used only for POP).
        q: dividend yield, cont. comp., default 0.

    The max gain / max loss / break-evens are pure payoff-at-expiration
    properties and are independent of S/T/r/sigma/q — those arguments only
    enter the probability-of-profit computation.
    """
    kinks = strategy.kinks()
    # Always evaluate at S=0 too, so we capture pnl at the left boundary.
    eval_points: list[float] = sorted({0.0, *kinks})
    pnl_vals = [(S_T, strategy.pnl(S_T)) for S_T in eval_points]

    slope_left = _payoff_slope_at_extreme(strategy, "left")
    slope_right = _payoff_slope_at_extreme(strategy, "right")

    # Max gain / max loss. Interior extrema are at kinks. Tails are
    # +inf iff slope has the corresponding sign.
    interior_max = max(p for _, p in pnl_vals)
    interior_min = min(p for _, p in pnl_vals)

    # Left tail: slope at S_T just above 0 -> slope_left (for puts: pnl grows
    # as S_T decreases if slope_left < 0 -> so as S_T -> 0, pnl -> pnl(0) -
    # slope_left * 0; actually we already evaluate at 0, so interior covers it).
    # The unbounded case for the left is only if any long put or short stock
    # can push payoff at S_T=0 to very large values, which is captured by
    # evaluating at S_T=0. No infinite left tail for any standard strategy.

    # Right tail: unbounded up if slope_right > 0 (long call/stock net positive),
    # unbounded down if slope_right < 0 (short call/stock net negative).
    if slope_right > 1e-12:
        max_gain = math.inf
    else:
        max_gain = interior_max
    if slope_right < -1e-12:
        max_loss = math.inf
    else:
        max_loss = -interior_min  # magnitude

    breakevens = _find_breakevens(strategy, pnl_vals[1:] if pnl_vals and pnl_vals[0][0] == 0 else pnl_vals, slope_left, slope_right)
    # Note: we pass the kinks (strict strikes) to _find_breakevens so its
    # "below first strike" segment reasoning holds. S=0 was used only for
    # anchoring max/min.

    # Probability of profit — integrate mass over intervals where PnL > 0.
    # The PnL function is piecewise linear with break-evens as zero-crossings,
    # so between consecutive break-evens (and outside the extreme ones) the
    # sign is constant. We sample a representative point in each interval.
    pop = _probability_of_profit(
        strategy, breakevens, slope_left, slope_right, S, T, r, sigma, q
    )

    return StrategyMetrics(
        net_debit=strategy.net_debit(),
        max_gain=max_gain,
        max_loss=max_loss,
        breakevens=tuple(breakevens),
        probability_of_profit=pop,
    )


def _probability_of_profit(
    strategy: Strategy,
    breakevens: list[float],
    slope_left: float,
    slope_right: float,
    S: float,
    T: float,
    r: float,
    sigma: float,
    q: float,
) -> float:
    """Sum risk-neutral probability mass over intervals with positive PnL."""
    if not breakevens:
        # No zero-crossings: PnL sign is constant. Check at the current spot.
        return 1.0 if strategy.pnl(S) > 0 else 0.0

    # Build disjoint intervals partitioning (0, +inf) at break-evens.
    cuts = [0.0, *breakevens, math.inf]
    # For each interval, pick a representative point (midpoint, with reasonable
    # clamping for the unbounded tail) and evaluate PnL sign.
    total_mass = 0.0
    for lo, hi in zip(cuts, cuts[1:]):
        if hi == math.inf:
            rep = max(lo * 2.0, lo + max(1.0, abs(slope_right)))
        else:
            rep = 0.5 * (lo + hi) if lo > 0 else hi * 0.5
        if strategy.pnl(rep) <= 0:
            continue
        p_lo = 0.0 if lo == 0.0 else _risk_neutral_prob_below(lo, S, T, r, sigma, q)
        p_hi = 1.0 if hi == math.inf else _risk_neutral_prob_below(hi, S, T, r, sigma, q)
        total_mass += max(0.0, p_hi - p_lo)
    return total_mass
