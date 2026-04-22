"""Tests for IV rank and IV percentile utilities."""
from __future__ import annotations

import pytest

from square18_signals.iv import iv_percentile, iv_rank


def test_iv_rank_min_max():
    history = [0.20, 0.25, 0.30, 0.35, 0.40]
    assert iv_rank(0.20, history) == pytest.approx(0.0)
    assert iv_rank(0.40, history) == pytest.approx(100.0)
    assert iv_rank(0.30, history) == pytest.approx(50.0)


def test_iv_rank_clamped_outside_history():
    history = [0.20, 0.25, 0.30, 0.35, 0.40]
    assert iv_rank(0.10, history) == 0.0
    assert iv_rank(0.50, history) == 100.0


def test_iv_rank_flat_history_is_neutral():
    history = [0.3] * 10
    assert iv_rank(0.3, history) == 50.0
    assert iv_rank(0.5, history) == 50.0  # no information


def test_iv_percentile_counts_strictly_below():
    history = [0.10, 0.20, 0.20, 0.30, 0.40]
    # Current IV == 0.25: 3 readings strictly below (0.10, 0.20, 0.20)
    assert iv_percentile(0.25, history) == pytest.approx(60.0)
    # Current IV == 0.20: 1 strictly below (only 0.10)
    assert iv_percentile(0.20, history) == pytest.approx(20.0)
    # Current IV at the max: 4/5 strictly below
    assert iv_percentile(0.40, history) == pytest.approx(80.0)


def test_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        iv_rank(-0.1, [0.2, 0.3])
    with pytest.raises(ValueError):
        iv_rank(0.2, [])
    with pytest.raises(ValueError):
        iv_percentile(0.2, [0.2, -0.1])
