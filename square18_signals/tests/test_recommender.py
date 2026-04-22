"""Tests for the rule-based strategy recommender."""
from __future__ import annotations

import pytest

from square18_signals.recommender import MarketContext, recommend_strategies


def _ctx(**overrides) -> MarketContext:
    defaults = dict(
        symbol="TEST",
        spot=100.0,
        iv=0.28,
        iv_rank=30.0,
        direction="bull",
        conviction=0.7,
        dte=35,
        risk_free_rate=0.045,
        dividend_yield=0.0,
        earnings_in_window=False,
    )
    defaults.update(overrides)
    return MarketContext(**defaults)


# ---------------------------------------------------------------------------
# Direction routing
# ---------------------------------------------------------------------------


def test_bull_low_iv_prefers_debit_structures():
    recs = recommend_strategies(_ctx(direction="bull", iv_rank=20))
    assert recs, "expected at least one recommendation"
    top = recs[0]
    assert "debit" in top.tags
    # None of the results should be credit-only for a clean bull/low-IV case.


def test_bull_high_iv_prefers_credit_structures():
    recs = recommend_strategies(_ctx(direction="bull", iv_rank=75))
    assert recs
    top = recs[0]
    assert "credit" in top.tags


def test_bear_low_iv_suggests_long_put():
    recs = recommend_strategies(_ctx(direction="bear", iv_rank=20))
    names = [r.strategy.name.lower() for r in recs]
    assert any("long" in n and "put" in n for n in names)


def test_bear_high_iv_suggests_bear_call_spread():
    recs = recommend_strategies(_ctx(direction="bear", iv_rank=75))
    names = [r.strategy.name.lower() for r in recs]
    assert any("bear call spread" in n for n in names)


def test_neutral_high_iv_suggests_iron_condor():
    recs = recommend_strategies(_ctx(direction="neutral", iv_rank=60))
    names = [r.strategy.name.lower() for r in recs]
    assert any("iron condor" in n for n in names)


def test_neutral_low_iv_suggests_long_straddle():
    recs = recommend_strategies(_ctx(direction="neutral", iv_rank=20))
    names = [r.strategy.name.lower() for r in recs]
    assert any("straddle" in n for n in names)


# ---------------------------------------------------------------------------
# Scoring properties
# ---------------------------------------------------------------------------


def test_recommendations_sorted_by_fit():
    recs = recommend_strategies(_ctx(direction="bull", iv_rank=45), max_results=3)
    for a, b in zip(recs, recs[1:]):
        assert a.fit_score >= b.fit_score


def test_fit_scores_are_in_unit_interval():
    for direction in ("bull", "bear", "neutral"):
        for iv_rank in (10, 50, 90):
            recs = recommend_strategies(_ctx(direction=direction, iv_rank=iv_rank))
            for r in recs:
                assert 0.0 <= r.fit_score <= 1.0


def test_earnings_in_window_lowers_scores():
    no_earn = recommend_strategies(_ctx(direction="bull", iv_rank=30))
    with_earn = recommend_strategies(
        _ctx(direction="bull", iv_rank=30, earnings_in_window=True)
    )
    if no_earn and with_earn:
        assert with_earn[0].fit_score <= no_earn[0].fit_score


def test_max_results_respected():
    recs = recommend_strategies(_ctx(direction="bull", iv_rank=50), max_results=1)
    assert len(recs) == 1


def test_rejects_bad_max_results():
    with pytest.raises(ValueError):
        recommend_strategies(_ctx(), max_results=0)


# ---------------------------------------------------------------------------
# Output validity
# ---------------------------------------------------------------------------


def test_every_recommendation_has_valid_metrics():
    recs = recommend_strategies(_ctx(direction="bull", iv_rank=35))
    for r in recs:
        m = r.metrics
        assert 0.0 <= m.probability_of_profit <= 1.0
        # Max loss should be non-negative (magnitude).
        assert m.max_loss >= 0.0
        # Break-evens should be strictly positive prices.
        for be in m.breakevens:
            assert be > 0


def test_rationale_non_empty():
    recs = recommend_strategies(_ctx(direction="bull", iv_rank=25))
    for r in recs:
        assert r.rationale
        assert len(r.rationale) > 20


# ---------------------------------------------------------------------------
# Context validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"spot": 0},
        {"spot": -1},
        {"iv": -0.1},
        {"iv_rank": -5},
        {"iv_rank": 150},
        {"direction": "sideways"},
        {"conviction": 1.5},
        {"dte": 0},
    ],
)
def test_market_context_rejects_bad_inputs(overrides):
    with pytest.raises(ValueError):
        _ctx(**overrides)
