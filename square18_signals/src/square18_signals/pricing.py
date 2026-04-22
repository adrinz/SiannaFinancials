"""Black-Scholes-Merton pricing, greeks, and implied volatility.

All formulas follow the standard Black-Scholes-Merton model for European
options on a dividend-paying underlying:

    d1 = (ln(S/K) + (r - q + σ²/2) T) / (σ √T)
    d2 = d1 - σ √T

    C = S e^{-qT} N(d1) - K e^{-rT} N(d2)
    P = K e^{-rT} N(-d2) - S e^{-qT} N(-d1)

Where:
    S: spot price (per share)
    K: strike price (per share)
    T: time to expiration, in years
    r: continuously-compounded risk-free rate (decimal)
    q: continuously-compounded dividend yield (decimal)
    σ: annualized volatility (decimal, e.g. 0.25 for 25%)

All functions are pure-Python, no numpy dependency, so they run in
constrained sandboxes. Vectorization can be layered on later.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Literal


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


OptionTypeLike = OptionType | Literal["call", "put", "c", "p", "C", "P"]


def _coerce_type(option_type: OptionTypeLike) -> OptionType:
    if isinstance(option_type, OptionType):
        return option_type
    s = str(option_type).lower()
    if s in ("call", "c"):
        return OptionType.CALL
    if s in ("put", "p"):
        return OptionType.PUT
    raise ValueError(f"unknown option_type: {option_type!r}")


# Standard normal pdf and cdf — exact up to float precision via math.erf.

_SQRT_2 = math.sqrt(2.0)
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _validate_common(S: float, K: float, T: float, sigma: float) -> None:
    if S <= 0:
        raise ValueError(f"spot S must be > 0 (got {S})")
    if K <= 0:
        raise ValueError(f"strike K must be > 0 (got {K})")
    if T < 0:
        raise ValueError(f"time-to-expiration T must be >= 0 (got {T})")
    if sigma < 0:
        raise ValueError(f"volatility sigma must be >= 0 (got {sigma})")


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float, q: float) -> tuple[float, float]:
    # Caller is responsible for avoiding T=0 or sigma=0 here.
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return d1, d2


def _deterministic_price(
    S: float, K: float, T: float, r: float, q: float, option_type: OptionType
) -> float:
    """Closed-form price when sigma=0 or T=0 (payoff is deterministic)."""
    # Forward price of the underlying, discounted.
    fwd = S * math.exp(-q * T)
    disc_strike = K * math.exp(-r * T)
    if option_type == OptionType.CALL:
        return max(0.0, fwd - disc_strike)
    return max(0.0, disc_strike - fwd)


def black_scholes_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: OptionTypeLike = OptionType.CALL,
    q: float = 0.0,
) -> float:
    """Black-Scholes-Merton price for a European option.

    Args:
        S: spot price
        K: strike
        T: years to expiration
        r: risk-free rate (cont. comp.)
        sigma: annualized vol
        option_type: "call" or "put"
        q: dividend yield (cont. comp.), default 0

    Returns:
        Fair value per share.
    """
    _validate_common(S, K, T, sigma)
    opt = _coerce_type(option_type)

    if T == 0.0 or sigma == 0.0:
        return _deterministic_price(S, K, T, r, q, opt)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_S = S * math.exp(-q * T)
    disc_K = K * math.exp(-r * T)
    if opt == OptionType.CALL:
        return disc_S * _norm_cdf(d1) - disc_K * _norm_cdf(d2)
    return disc_K * _norm_cdf(-d2) - disc_S * _norm_cdf(-d1)


def call_price(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    return black_scholes_price(S, K, T, r, sigma, OptionType.CALL, q)


def put_price(S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    return black_scholes_price(S, K, T, r, sigma, OptionType.PUT, q)


@dataclass(frozen=True)
class Greeks:
    """Option greeks.

    Conventions (these match most retail broker displays):

    - `delta`: dPrice / dSpot (per $1 move).
    - `gamma`: d²Price / dSpot² (per $1 move).
    - `vega`: dPrice / dσ, per +1.00 change in annualized vol (i.e. +100
      vol points). Divide by 100 for "per 1%", or use `vega_per_pct`.
    - `theta`: dPrice / dt where `t` is calendar time, per +1 year.
      This matches the standard trader convention — long-option theta
      is negative (price decays as time passes). `theta_per_day = theta / 365`.
    - `rho`: dPrice / dr per +1.00 change in the interest rate.
      Use `rho_per_pct` for "per 1%".
    """

    delta: float
    gamma: float
    vega: float  # per 1.0 change in vol (i.e. +100 vol points)
    theta: float  # per 1.0 year
    rho: float  # per 1.0 change in rate
    # Convenience accessors for the usual "per 1 day" / "per 1 vol point":
    @property
    def theta_per_day(self) -> float:
        return self.theta / 365.0

    @property
    def vega_per_pct(self) -> float:
        return self.vega / 100.0

    @property
    def rho_per_pct(self) -> float:
        return self.rho / 100.0


def greeks(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: OptionTypeLike = OptionType.CALL,
    q: float = 0.0,
) -> Greeks:
    """Analytical Black-Scholes greeks with dividend yield q."""
    _validate_common(S, K, T, sigma)
    opt = _coerce_type(option_type)

    # Degenerate cases: return well-defined limits.
    if T == 0.0:
        intrinsic_sign = 1.0 if (opt == OptionType.CALL and S > K) or (opt == OptionType.PUT and S < K) else 0.0
        delta = intrinsic_sign * (1.0 if opt == OptionType.CALL else -1.0)
        return Greeks(delta=delta, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)

    if sigma == 0.0:
        # Deterministic: gamma/vega are zero; delta is 0 or ±exp(-qT) depending on moneyness.
        fwd = S * math.exp((r - q) * T)
        in_the_money = (opt == OptionType.CALL and fwd > K) or (opt == OptionType.PUT and fwd < K)
        if not in_the_money:
            return Greeks(delta=0.0, gamma=0.0, vega=0.0, theta=0.0, rho=0.0)
        if opt == OptionType.CALL:
            delta = math.exp(-q * T)
            theta = -q * S * math.exp(-q * T) + r * K * math.exp(-r * T) * (-1.0)
            rho = K * T * math.exp(-r * T)
            return Greeks(delta=delta, gamma=0.0, vega=0.0, theta=theta, rho=rho)
        delta = -math.exp(-q * T)
        theta = q * S * math.exp(-q * T) - r * K * math.exp(-r * T) * (-1.0)
        rho = -K * T * math.exp(-r * T)
        return Greeks(delta=delta, gamma=0.0, vega=0.0, theta=theta, rho=rho)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    sqrt_t = math.sqrt(T)
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)
    pdf_d1 = _norm_pdf(d1)

    gamma = disc_q * pdf_d1 / (S * sigma * sqrt_t)
    vega = S * disc_q * pdf_d1 * sqrt_t  # per 1.0 vol (i.e. 100%)

    if opt == OptionType.CALL:
        delta = disc_q * _norm_cdf(d1)
        theta = (
            -(S * disc_q * pdf_d1 * sigma) / (2.0 * sqrt_t)
            - r * K * disc_r * _norm_cdf(d2)
            + q * S * disc_q * _norm_cdf(d1)
        )
        rho = K * T * disc_r * _norm_cdf(d2)
    else:
        delta = disc_q * (_norm_cdf(d1) - 1.0)
        theta = (
            -(S * disc_q * pdf_d1 * sigma) / (2.0 * sqrt_t)
            + r * K * disc_r * _norm_cdf(-d2)
            - q * S * disc_q * _norm_cdf(-d1)
        )
        rho = -K * T * disc_r * _norm_cdf(-d2)

    return Greeks(delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho)


def _price_bounds(
    S: float, K: float, T: float, r: float, q: float, option_type: OptionType
) -> tuple[float, float]:
    """No-arbitrage price bounds for a European option.

    Call:  max(S e^{-qT} - K e^{-rT}, 0)  <=  C  <=  S e^{-qT}
    Put:   max(K e^{-rT} - S e^{-qT}, 0)  <=  P  <=  K e^{-rT}
    """
    disc_S = S * math.exp(-q * T)
    disc_K = K * math.exp(-r * T)
    if option_type == OptionType.CALL:
        return max(disc_S - disc_K, 0.0), disc_S
    return max(disc_K - disc_S, 0.0), disc_K


class IVNotFoundError(RuntimeError):
    """Raised when implied-vol solver cannot converge within the price bounds."""


def implied_vol(
    target_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: OptionTypeLike = OptionType.CALL,
    q: float = 0.0,
    *,
    tol: float = 1e-8,
    max_iter: int = 100,
    initial_guess: float = 0.2,
    vol_lo: float = 1e-6,
    vol_hi: float = 5.0,
) -> float:
    """Solve for the annualized volatility implied by `target_price`.

    Uses Newton-Raphson on vega with a bisection fallback. Falls back
    immediately to bisection when vega is tiny (deep OTM/ITM), which is where
    Newton often oscillates.

    Raises:
        ValueError: if the target price is outside no-arbitrage bounds.
        IVNotFoundError: if the solver fails to converge.
    """
    if target_price < 0:
        raise ValueError(f"target_price must be >= 0 (got {target_price})")
    _validate_common(S, K, T, 0.0)
    opt = _coerce_type(option_type)

    if T == 0.0:
        # Only the intrinsic is possible; any other target has no solution.
        intrinsic = _deterministic_price(S, K, T, r, q, opt)
        if abs(target_price - intrinsic) < tol:
            return 0.0
        raise ValueError(
            "cannot imply vol at T=0: target_price must equal intrinsic"
            f" ({intrinsic}), got {target_price}"
        )

    lo_px, hi_px = _price_bounds(S, K, T, r, q, opt)
    # Allow a tiny numerical slack at the boundaries.
    eps_px = max(1e-10, 1e-8 * max(1.0, hi_px))
    if target_price < lo_px - eps_px or target_price > hi_px + eps_px:
        raise ValueError(
            f"target_price {target_price} outside no-arbitrage bounds"
            f" [{lo_px}, {hi_px}] for a {opt.value}"
        )

    # Clamp target for numerical stability.
    tp = min(max(target_price, lo_px), hi_px)

    # --- Newton-Raphson attempt ---
    sigma = max(initial_guess, vol_lo * 10)
    for _ in range(max_iter):
        price = black_scholes_price(S, K, T, r, sigma, opt, q)
        diff = price - tp
        if abs(diff) < tol:
            return sigma
        g = greeks(S, K, T, r, sigma, opt, q)
        if g.vega < 1e-10:
            break  # fall through to bisection
        step = diff / g.vega
        # Damped update — never step past the valid range, never negative.
        new_sigma = sigma - step
        if new_sigma <= vol_lo or new_sigma >= vol_hi or not math.isfinite(new_sigma):
            break
        sigma = new_sigma

    # --- Bisection fallback (guaranteed to converge if bounds straddle root) ---
    lo, hi = vol_lo, vol_hi
    lo_val = black_scholes_price(S, K, T, r, lo, opt, q) - tp
    hi_val = black_scholes_price(S, K, T, r, hi, opt, q) - tp
    # If hi isn't high enough, expand once. Prices are monotone increasing in sigma.
    expansions = 0
    while lo_val * hi_val > 0 and expansions < 5:
        hi *= 2.0
        hi_val = black_scholes_price(S, K, T, r, hi, opt, q) - tp
        expansions += 1
    if lo_val * hi_val > 0:
        raise IVNotFoundError(
            f"could not bracket implied vol for target={target_price}"
            f" (S={S}, K={K}, T={T}, r={r}, q={q}, type={opt.value})"
        )

    for _ in range(200):
        mid = 0.5 * (lo + hi)
        mid_val = black_scholes_price(S, K, T, r, mid, opt, q) - tp
        if abs(mid_val) < tol or (hi - lo) < tol:
            return mid
        if lo_val * mid_val < 0:
            hi, hi_val = mid, mid_val
        else:
            lo, lo_val = mid, mid_val
    raise IVNotFoundError("bisection failed to converge")
