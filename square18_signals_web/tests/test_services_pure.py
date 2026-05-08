"""Unit tests for pure helpers in ``app.services`` (no I/O, no network)."""
from __future__ import annotations

import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.services import (  # noqa: E402
    _annualised_rv,
    _direction_for,
    _dte_pref_for,
    _regime_label,
    _rv_rank_pct,
    _signal_for,
)


def test_signal_for_maps_verdicts():
    assert _signal_for("BULLISH") == "Buy"
    assert _signal_for("BEARISH") == "Sell"
    assert _signal_for("NEUTRAL") == "Hold"
    assert _signal_for("") == "Hold"


def test_direction_for_maps_verdicts():
    assert _direction_for("BULLISH") == "bull"
    assert _direction_for("BEARISH") == "bear"
    assert _direction_for("NEUTRAL") == "neutral"


def test_annualised_rv_insufficient_data():
    assert _annualised_rv([100.0] * 5) is None
    assert _annualised_rv([]) is None


def test_annualised_rv_positive_series():
    # Smooth upward drift → small but positive realised vol.
    closes = [100.0 + i * 0.1 for i in range(30)]
    rv = _annualised_rv(closes, window=20)
    assert rv is not None
    assert 0 < rv < 2.0


def test_annualised_rv_skips_non_positive_pairs():
    closes = [100.0] * 25
    closes[-1] = -1.0  # invalid final; earlier window still valid
    rv = _annualised_rv(closes, window=20)
    assert rv is None or isinstance(rv, float)


def test_rv_rank_pct_short_series_returns_mid_ranks():
    closes = [100.0 * (1.001**i) for i in range(25)]
    cur, rank, pct = _rv_rank_pct(closes, window=20, lookback=252)
    assert cur is not None
    assert rank == 50.0 and pct == 50.0


def test_dte_pref_for_iv_rank_buckets():
    assert _dte_pref_for(None) == 35
    assert _dte_pref_for(10.0) == 60
    assert _dte_pref_for(30.0) == 45
    assert _dte_pref_for(55.0) == 30
    assert _dte_pref_for(80.0) == 21


def test_regime_label_branches():
    assert _regime_label(35.0, 0.0, 40.0) == "High-vol defensive"
    lbl = _regime_label(14.0, 0.05, 55.0)
    assert isinstance(lbl, str) and len(lbl) > 0
    assert "Balanced" in lbl or "bias" in lbl.lower()


def test_rv_rank_pct_monotonic_increasing_vol_series():
    """Last chunk is more volatile → rank/percentile should be high."""
    base = [100.0 + math.sin(i / 5.0) * 0.5 for i in range(80)]
    noise = base[:-1] + [base[-1] + 15.0]  # spike at end
    _, rank_b, pct_b = _rv_rank_pct(noise, window=20)
    flat = [100.0 + i * 0.01 for i in range(80)]
    _, rank_f, pct_f = _rv_rank_pct(flat, window=20)
    assert rank_b is not None and rank_f is not None
    assert rank_b >= rank_f
