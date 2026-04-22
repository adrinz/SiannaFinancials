"""Tests for options strategy payoffs, metrics, and probability of profit."""
from __future__ import annotations

import math

import pytest

from square18_signals.strategies import (
    CONTRACT_MULTIPLIER,
    Strategy,
    StrategyLeg,
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


# ---------------------------------------------------------------------------
# Leg-level validation
# ---------------------------------------------------------------------------


def test_leg_rejects_stock_with_strike():
    with pytest.raises(ValueError):
        StrategyLeg(kind="stock", side="long", strike=100.0)


def test_leg_requires_strike_for_options():
    with pytest.raises(ValueError):
        StrategyLeg(kind="call", side="long")


def test_leg_rejects_negative_premium():
    with pytest.raises(ValueError):
        StrategyLeg(kind="call", side="long", strike=100, premium=-1.0)


# ---------------------------------------------------------------------------
# Long call — the simplest case
# ---------------------------------------------------------------------------


def test_long_call_pnl_at_key_points():
    s = long_call(strike=100, premium=3.0, contracts=1)
    assert s.pnl(S_T=90) == pytest.approx(-300.0)  # OTM: lose premium
    assert s.pnl(S_T=100) == pytest.approx(-300.0)  # ATM at expiry = lose premium
    assert s.pnl(S_T=103) == pytest.approx(0.0)  # break-even
    assert s.pnl(S_T=120) == pytest.approx(1700.0)  # (20-3)*100


def test_long_call_metrics():
    s = long_call(100, 3.0)
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.25)
    assert m.net_debit == pytest.approx(300.0)
    assert m.max_loss == pytest.approx(300.0)
    assert m.max_gain == math.inf
    assert m.breakevens == pytest.approx((103.0,))
    # POP: probability S_T > 103, with S=100, T=0.5, sigma=0.25, r=0.04.
    # Rough sanity: should be between 30% and 55%.
    assert 0.3 < m.probability_of_profit < 0.55


# ---------------------------------------------------------------------------
# Long put
# ---------------------------------------------------------------------------


def test_long_put_pnl_and_metrics():
    s = long_put(100, 2.5)
    assert s.pnl(S_T=90) == pytest.approx(750.0)  # (10 - 2.5) * 100
    assert s.pnl(S_T=97.5) == pytest.approx(0.0)  # break-even
    assert s.pnl(S_T=110) == pytest.approx(-250.0)
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.25)
    assert m.max_loss == pytest.approx(250.0)
    # Max gain is bounded at S_T=0: (100-2.5)*100 = 9750.
    assert m.max_gain == pytest.approx(9750.0)
    assert m.breakevens == pytest.approx((97.5,))


# ---------------------------------------------------------------------------
# Bull call spread (debit)
# ---------------------------------------------------------------------------


def test_bull_call_spread_classic_metrics():
    """Long 100 call @ 3, short 110 call @ 1 -> width 10, debit 2, max gain 8."""
    s = bull_call_spread(
        long_strike=100, short_strike=110,
        long_premium=3.0, short_premium=1.0,
    )
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.25)
    assert m.net_debit == pytest.approx(200.0)  # debit
    assert m.max_gain == pytest.approx(800.0)
    assert m.max_loss == pytest.approx(200.0)
    assert m.breakevens == pytest.approx((102.0,))


def test_bull_call_spread_rejects_bad_ordering():
    with pytest.raises(ValueError):
        bull_call_spread(long_strike=110, short_strike=100, long_premium=1.0, short_premium=3.0)


# ---------------------------------------------------------------------------
# Bear call spread (credit)
# ---------------------------------------------------------------------------


def test_bear_call_spread_classic_metrics():
    """Short 100 call @ 3, long 110 call @ 1 -> credit 2, width 10, max loss 8."""
    s = bear_call_spread(
        short_strike=100, long_strike=110,
        short_premium=3.0, long_premium=1.0,
    )
    m = strategy_metrics(s, S=95, T=0.5, r=0.04, sigma=0.25)
    assert m.net_debit == pytest.approx(-200.0)  # credit
    assert m.max_gain == pytest.approx(200.0)
    assert m.max_loss == pytest.approx(800.0)
    assert m.breakevens == pytest.approx((102.0,))


# ---------------------------------------------------------------------------
# Bull put spread (credit)
# ---------------------------------------------------------------------------


def test_bull_put_spread_metrics():
    """Short 100 put @ 3, long 90 put @ 1 -> credit 2, width 10, max loss 8."""
    s = bull_put_spread(
        short_strike=100, long_strike=90,
        short_premium=3.0, long_premium=1.0,
    )
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.25)
    assert m.net_debit == pytest.approx(-200.0)
    assert m.max_gain == pytest.approx(200.0)
    assert m.max_loss == pytest.approx(800.0)
    assert m.breakevens == pytest.approx((98.0,))


# ---------------------------------------------------------------------------
# Iron condor (credit, defined risk)
# ---------------------------------------------------------------------------


def test_iron_condor_bounded():
    s = iron_condor(
        put_long_strike=80, put_short_strike=90,
        call_short_strike=110, call_long_strike=120,
        put_long_premium=0.5, put_short_premium=2.0,
        call_short_premium=2.0, call_long_premium=0.5,
    )
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.25)
    credit_per_share = (2.0 + 2.0) - (0.5 + 0.5)
    assert m.net_debit == pytest.approx(-credit_per_share * CONTRACT_MULTIPLIER)
    assert m.max_gain == pytest.approx(credit_per_share * CONTRACT_MULTIPLIER)
    # Wing width 10, credit 3 -> max loss (10 - 3) * 100 = 700.
    assert m.max_loss == pytest.approx(700.0)
    lo, hi = m.breakevens
    assert lo == pytest.approx(87.0)
    assert hi == pytest.approx(113.0)
    # POP is P(87 < S_T < 113) under the risk-neutral lognormal. At S=100,
    # sigma=0.25, T=0.5 that is ~54%. Allow a reasonably wide band.
    assert 0.45 < m.probability_of_profit < 0.75


def test_iron_condor_rejects_bad_strike_order():
    with pytest.raises(ValueError):
        iron_condor(
            put_long_strike=80, put_short_strike=95,
            call_short_strike=90, call_long_strike=120,
            put_long_premium=0.5, put_short_premium=2.0,
            call_short_premium=2.0, call_long_premium=0.5,
        )


# ---------------------------------------------------------------------------
# Long straddle / strangle
# ---------------------------------------------------------------------------


def test_long_straddle_two_breakevens():
    s = long_straddle(strike=100, call_premium=4.0, put_premium=3.5)
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.4)
    total_debit = (4.0 + 3.5) * CONTRACT_MULTIPLIER
    assert m.net_debit == pytest.approx(total_debit)
    assert m.max_loss == pytest.approx(total_debit)
    assert m.max_gain == math.inf
    lo, hi = m.breakevens
    assert lo == pytest.approx(100 - 7.5)
    assert hi == pytest.approx(100 + 7.5)


def test_long_strangle_breakevens():
    s = long_strangle(put_strike=95, call_strike=105, put_premium=1.5, call_premium=1.5)
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.4)
    lo, hi = m.breakevens
    assert lo == pytest.approx(95 - 3.0)
    assert hi == pytest.approx(105 + 3.0)


def test_long_strangle_rejects_bad_order():
    with pytest.raises(ValueError):
        long_strangle(put_strike=110, call_strike=100, put_premium=1, call_premium=1)


# ---------------------------------------------------------------------------
# Cash-secured put & covered call
# ---------------------------------------------------------------------------


def test_cash_secured_put_classic():
    s = cash_secured_put(strike=180, premium=2.35)
    # Collateral 180*100 = 18000. Max gain = premium collected = 235.
    m = strategy_metrics(s, S=185, T=0.5, r=0.04, sigma=0.3)
    assert m.net_debit == pytest.approx(-235.0)
    assert m.max_gain == pytest.approx(235.0)
    # Max loss if assigned at $0: (180 - 2.35) * 100 = 17765.
    assert m.max_loss == pytest.approx(17765.0)
    assert m.breakevens == pytest.approx((177.65,))


def test_covered_call_classic():
    s = covered_call(share_entry_price=100.0, call_strike=110.0, call_premium=2.0)
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.25)
    # Max gain = (strike - entry + premium) * shares = (110-100+2)*100 = 1200.
    assert m.max_gain == pytest.approx(1200.0)
    # Break-even on the stock = entry - premium = 98.
    assert m.breakevens[0] == pytest.approx(98.0)


def test_covered_call_rejects_misaligned_shares():
    with pytest.raises(ValueError):
        covered_call(100.0, 110.0, 2.0, shares=50)


# ---------------------------------------------------------------------------
# POP sanity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sigma", [0.10, 0.20, 0.40])
def test_pop_is_between_zero_and_one(sigma):
    s = bull_call_spread(
        long_strike=100, short_strike=110,
        long_premium=4.0, short_premium=1.0,
    )
    m = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=sigma)
    assert 0.0 <= m.probability_of_profit <= 1.0


def test_pop_grows_with_vol_for_long_straddle():
    # Straddle POP increases as volatility rises (wider distribution).
    s = long_straddle(strike=100, call_premium=4.0, put_premium=3.5)
    m_low = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.15)
    m_hi = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.60)
    assert m_hi.probability_of_profit > m_low.probability_of_profit


def test_pop_shrinks_with_vol_for_iron_condor():
    s = iron_condor(
        put_long_strike=80, put_short_strike=90,
        call_short_strike=110, call_long_strike=120,
        put_long_premium=0.5, put_short_premium=2.0,
        call_short_premium=2.0, call_long_premium=0.5,
    )
    m_low = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.15)
    m_hi = strategy_metrics(s, S=100, T=0.5, r=0.04, sigma=0.60)
    assert m_low.probability_of_profit > m_hi.probability_of_profit


# ---------------------------------------------------------------------------
# Symmetry / equivalence
# ---------------------------------------------------------------------------


def test_two_contracts_scales_linearly():
    s1 = long_call(100, 3.0, contracts=1)
    s2 = long_call(100, 3.0, contracts=2)
    assert s2.pnl(120) == pytest.approx(2 * s1.pnl(120))
    m1 = strategy_metrics(s1, S=100, T=0.5, r=0.04, sigma=0.25)
    m2 = strategy_metrics(s2, S=100, T=0.5, r=0.04, sigma=0.25)
    assert m2.max_loss == pytest.approx(2 * m1.max_loss)
    # POP does not change with contract count.
    assert m1.probability_of_profit == pytest.approx(m2.probability_of_profit)


def test_strategy_rejects_empty_legs():
    with pytest.raises(ValueError):
        Strategy(name="empty", legs=())
