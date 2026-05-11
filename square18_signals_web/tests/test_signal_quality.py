"""Tests for signal calibration and backtest quality helpers."""
from __future__ import annotations

import app.analyst.signal_config as sc
from tools import backtest_verdict as bv


def test_probability_for_signal_uses_score_bins_before_base(monkeypatch):
    cfg = {
        "thresholds": {
            "BULLISH": {"probability_pct": 57.0},
            "BEARISH": {"probability_pct": 54.0},
            "NEUTRAL": {"probability_pct": 52.0},
        },
        "calibration": {
            "BULLISH": [
                {"min_score": 0.30, "max_score": 0.50, "hit_rate": 58.2},
                {"min_score": 0.50, "max_score": 1.00, "hit_rate": 63.7},
            ]
        },
    }
    monkeypatch.setattr(sc, "load_signal_config", lambda: cfg)
    assert sc.probability_for_signal("BULLISH", 0.42) == 58.2
    assert sc.probability_for_signal("BULLISH", 0.76) == 63.7
    # Out of calibration bins: fallback to verdict-level base probability.
    assert sc.probability_for_signal("BEARISH", -0.51) == 54.0


def test_option_proxy_return_penalizes_theta_drag():
    move = 1.2
    p1 = bv._option_proxy_return_pct("BULLISH", 0.30, move, horizon=5)
    p2 = bv._option_proxy_return_pct("BULLISH", 0.30, move, horizon=10)
    assert p2 < p1


def test_calibration_bins_shapes_and_hit_rates():
    rows = [
        bv.BarResult("BULLISH", 0.4, 0.34, 1.0, 1.1),
        bv.BarResult("BULLISH", 0.5, 0.36, -0.5, -1.0),
        bv.BarResult("BULLISH", 0.8, 0.74, 2.0, 3.0),
    ]
    bins = bv._calibration_bins(rows, "BULLISH")
    assert len(bins) >= 2
    low = next(b for b in bins if b["min_score"] == 0.2 and b["max_score"] == 0.35)
    assert low["n"] == 1
    mid = next(b for b in bins if b["min_score"] == 0.35 and b["max_score"] == 0.5)
    assert mid["n"] == 1
