"""Tests for Black-Scholes pricing, greeks, and implied-volatility solver."""
from __future__ import annotations

import math

import pytest

from square18_signals.pricing import (
    IVNotFoundError,
    OptionType,
    black_scholes_price,
    call_price,
    greeks,
    implied_vol,
    put_price,
)


# ---------------------------------------------------------------------------
# Known closed-form benchmarks (Hull, "Options, Futures, and Other Derivatives")
# ---------------------------------------------------------------------------


def test_hull_example_call_price():
    """Hull ch.15 example: S=42, K=40, r=10%, sigma=20%, T=0.5 -> C ≈ 4.7594."""
    c = call_price(S=42, K=40, T=0.5, r=0.10, sigma=0.20)
    assert c == pytest.approx(4.7594, abs=1e-4)


def test_hull_example_put_price():
    """Same inputs give P ≈ 0.8086 via put-call parity."""
    p = put_price(S=42, K=40, T=0.5, r=0.10, sigma=0.20)
    assert p == pytest.approx(0.8086, abs=1e-4)


# ---------------------------------------------------------------------------
# Put-call parity: C - P = S e^{-qT} - K e^{-rT}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "S, K, T, r, sigma, q",
    [
        (100, 100, 0.5, 0.04, 0.25, 0.0),
        (100, 110, 1.0, 0.05, 0.30, 0.02),
        (50, 45, 0.25, 0.03, 0.40, 0.0),
        (420, 430, 35 / 365.0, 0.045, 0.28, 0.005),
        (250, 200, 2.0, 0.06, 0.50, 0.0),
    ],
)
def test_put_call_parity(S, K, T, r, sigma, q):
    c = call_price(S, K, T, r, sigma, q)
    p = put_price(S, K, T, r, sigma, q)
    lhs = c - p
    rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert lhs == pytest.approx(rhs, abs=1e-8)


# ---------------------------------------------------------------------------
# Monotonicity properties
# ---------------------------------------------------------------------------


def test_call_monotone_in_spot():
    prev = 0.0
    for S in range(50, 200, 10):
        c = call_price(S=S, K=100, T=0.5, r=0.04, sigma=0.25)
        assert c >= prev
        prev = c


def test_put_monotone_in_strike():
    prev = 0.0
    for K in range(50, 200, 10):
        p = put_price(S=100, K=K, T=0.5, r=0.04, sigma=0.25)
        assert p >= prev
        prev = p


def test_price_monotone_in_sigma():
    prev_c = 0.0
    prev_p = 0.0
    for sigma in [0.05, 0.10, 0.20, 0.30, 0.50, 1.0]:
        c = call_price(S=100, K=100, T=1.0, r=0.04, sigma=sigma)
        p = put_price(S=100, K=100, T=1.0, r=0.04, sigma=sigma)
        assert c >= prev_c
        assert p >= prev_p
        prev_c, prev_p = c, p


# ---------------------------------------------------------------------------
# Degenerate cases
# ---------------------------------------------------------------------------


def test_expiration_call_is_intrinsic():
    assert call_price(S=110, K=100, T=0.0, r=0.04, sigma=0.25) == pytest.approx(10.0)
    assert call_price(S=90, K=100, T=0.0, r=0.04, sigma=0.25) == pytest.approx(0.0)


def test_expiration_put_is_intrinsic():
    assert put_price(S=90, K=100, T=0.0, r=0.04, sigma=0.25) == pytest.approx(10.0)
    assert put_price(S=110, K=100, T=0.0, r=0.04, sigma=0.25) == pytest.approx(0.0)


def test_zero_vol_call_is_discounted_forward_minus_strike():
    S, K, T, r, q = 110.0, 100.0, 0.5, 0.05, 0.0
    expected = max(0.0, S * math.exp(-q * T) - K * math.exp(-r * T))
    assert call_price(S, K, T, r, 0.0, q) == pytest.approx(expected)


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        call_price(S=-1, K=100, T=0.5, r=0.04, sigma=0.2)
    with pytest.raises(ValueError):
        call_price(S=100, K=0, T=0.5, r=0.04, sigma=0.2)
    with pytest.raises(ValueError):
        call_price(S=100, K=100, T=-0.1, r=0.04, sigma=0.2)
    with pytest.raises(ValueError):
        call_price(S=100, K=100, T=0.5, r=0.04, sigma=-0.1)


def test_option_type_aliases():
    base = black_scholes_price(100, 100, 0.5, 0.04, 0.25, OptionType.CALL)
    assert black_scholes_price(100, 100, 0.5, 0.04, 0.25, "call") == base
    assert black_scholes_price(100, 100, 0.5, 0.04, 0.25, "c") == base
    assert black_scholes_price(100, 100, 0.5, 0.04, 0.25, "C") == base
    with pytest.raises(ValueError):
        black_scholes_price(100, 100, 0.5, 0.04, 0.25, "bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Greeks — analytic vs finite-difference
# ---------------------------------------------------------------------------


def _fd_delta(kind, S, K, T, r, sigma, q, h=0.01):
    up = black_scholes_price(S + h, K, T, r, sigma, kind, q)
    dn = black_scholes_price(S - h, K, T, r, sigma, kind, q)
    return (up - dn) / (2 * h)


def _fd_vega(kind, S, K, T, r, sigma, q, h=1e-4):
    up = black_scholes_price(S, K, T, r, sigma + h, kind, q)
    dn = black_scholes_price(S, K, T, r, sigma - h, kind, q)
    return (up - dn) / (2 * h)


def _fd_theta(kind, S, K, T, r, sigma, q, h=1 / 365.0):
    """Finite-difference theta in the standard trader convention: dP/dt where
    `t` is calendar time. Since dt = -dT (T = time to expiration), this is
    -dP/dT = (P(T-h) - P(T+h)) / (2h)."""
    up = black_scholes_price(S, K, T + h, r, sigma, kind, q)
    dn = black_scholes_price(S, K, max(T - h, 1e-8), r, sigma, kind, q)
    return (dn - up) / (2 * h)


def _fd_rho(kind, S, K, T, r, sigma, q, h=1e-5):
    up = black_scholes_price(S, K, T, r + h, sigma, kind, q)
    dn = black_scholes_price(S, K, T, r - h, sigma, kind, q)
    return (up - dn) / (2 * h)


def _fd_gamma(kind, S, K, T, r, sigma, q, h=0.01):
    up = black_scholes_price(S + h, K, T, r, sigma, kind, q)
    mid = black_scholes_price(S, K, T, r, sigma, kind, q)
    dn = black_scholes_price(S - h, K, T, r, sigma, kind, q)
    return (up - 2 * mid + dn) / (h * h)


@pytest.mark.parametrize("kind", ["call", "put"])
def test_greeks_match_finite_differences(kind):
    S, K, T, r, sigma, q = 100.0, 100.0, 0.5, 0.04, 0.25, 0.01
    g = greeks(S, K, T, r, sigma, kind, q)
    assert g.delta == pytest.approx(_fd_delta(kind, S, K, T, r, sigma, q), abs=1e-5)
    assert g.gamma == pytest.approx(_fd_gamma(kind, S, K, T, r, sigma, q), abs=1e-3)
    assert g.vega == pytest.approx(_fd_vega(kind, S, K, T, r, sigma, q), abs=1e-3)
    # Both our code and `_fd_theta` use the standard trader convention
    # (theta = dP/dt, calendar-time) — so they must match directly.
    assert g.theta == pytest.approx(_fd_theta(kind, S, K, T, r, sigma, q), abs=1e-3)
    assert g.rho == pytest.approx(_fd_rho(kind, S, K, T, r, sigma, q), abs=1e-3)


def test_call_delta_bounds():
    g = greeks(S=100, K=100, T=0.5, r=0.04, sigma=0.25, option_type="call")
    assert 0.0 < g.delta < 1.0
    g_itm = greeks(S=200, K=100, T=0.5, r=0.04, sigma=0.25, option_type="call")
    assert 0.95 < g_itm.delta <= 1.0
    g_otm = greeks(S=50, K=100, T=0.5, r=0.04, sigma=0.25, option_type="call")
    assert 0.0 <= g_otm.delta < 0.05


def test_put_delta_bounds():
    g = greeks(S=100, K=100, T=0.5, r=0.04, sigma=0.25, option_type="put")
    assert -1.0 < g.delta < 0.0


def test_greeks_at_expiration_are_finite():
    g = greeks(S=110, K=100, T=0.0, r=0.04, sigma=0.25, option_type="call")
    assert g.delta == 1.0
    assert g.gamma == 0.0
    assert g.vega == 0.0
    g2 = greeks(S=90, K=100, T=0.0, r=0.04, sigma=0.25, option_type="call")
    assert g2.delta == 0.0


def test_greeks_conversions():
    g = greeks(S=100, K=100, T=0.5, r=0.04, sigma=0.25, option_type="call")
    assert g.vega_per_pct == pytest.approx(g.vega / 100.0)
    assert g.theta_per_day == pytest.approx(g.theta / 365.0)
    assert g.rho_per_pct == pytest.approx(g.rho / 100.0)


# ---------------------------------------------------------------------------
# Implied volatility — round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "S, K, T, r, sigma, q, kind",
    [
        (100, 100, 0.5, 0.04, 0.25, 0.0, "call"),
        (100, 100, 0.5, 0.04, 0.25, 0.0, "put"),
        (100, 120, 0.25, 0.03, 0.40, 0.01, "call"),
        (100, 80, 1.5, 0.05, 0.60, 0.02, "put"),
        (50, 55, 30 / 365.0, 0.045, 0.15, 0.0, "call"),
        (420, 415, 45 / 365.0, 0.05, 0.35, 0.0, "call"),
    ],
)
def test_iv_roundtrip(S, K, T, r, sigma, q, kind):
    price = black_scholes_price(S, K, T, r, sigma, kind, q)
    iv = implied_vol(price, S, K, T, r, option_type=kind, q=q)
    assert iv == pytest.approx(sigma, abs=1e-6)


def test_iv_raises_when_price_outside_bounds():
    # Call price strictly above spot is impossible (upper bound is S e^{-qT}).
    with pytest.raises(ValueError):
        implied_vol(target_price=150, S=100, K=100, T=0.5, r=0.04, option_type="call")
    # Negative target price.
    with pytest.raises(ValueError):
        implied_vol(target_price=-1, S=100, K=100, T=0.5, r=0.04, option_type="call")


def test_iv_monotone_as_price_approaches_intrinsic():
    """As the target price drops toward intrinsic, implied vol must drop too.

    The price-vs-vol surface is flat near sigma=0 for deep ITM options
    (vega -> 0), so we can't assert a specific tiny IV from a tiny price
    nudge — but we *can* assert monotonicity, which is what matters.
    """
    S, K, T, r = 120, 100, 0.5, 0.04
    intrinsic = S - K * math.exp(-r * T)
    # Price points strictly decreasing toward intrinsic.
    prices = [intrinsic + delta for delta in (2.0, 1.0, 0.5, 0.1, 0.01)]
    ivs = [implied_vol(px, S, K, T, r, option_type="call") for px in prices]
    for a, b in zip(ivs, ivs[1:]):
        assert a > b, f"IV not monotone: {ivs}"
    assert ivs[-1] < ivs[0] * 0.5


def test_iv_deep_otm_converges():
    # Deep OTM: tiny vega but still solvable via bisection.
    S, K, T, r, sigma = 100, 200, 0.25, 0.04, 0.80
    price = call_price(S, K, T, r, sigma)
    iv = implied_vol(price, S, K, T, r, option_type="call")
    assert iv == pytest.approx(sigma, abs=1e-4)


def test_iv_not_found_error_type():
    assert issubclass(IVNotFoundError, RuntimeError)
