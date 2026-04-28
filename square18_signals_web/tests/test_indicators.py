"""Unit tests for the analyst TA indicators."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

# Make the web app + signals package importable without install.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.analyst.factors import bull_bear_balance_percent  # noqa: E402
from app.analyst.indicators import (  # noqa: E402
    adx,
    atr,
    bollinger,
    ema,
    macd,
    pivots,
    rsi,
    sma,
    stochastic,
    support_resistance,
)


# ---------------------------------------------------------------------------
# SMA / EMA
# ---------------------------------------------------------------------------


def test_bull_bear_balance_maps_composite_minus1_to_plus1():
    assert bull_bear_balance_percent(-1.0) == (0.0, 100.0)
    assert bull_bear_balance_percent(1.0) == (100.0, 0.0)
    b, br = bull_bear_balance_percent(0.0)
    assert b == pytest.approx(50.0) and br == pytest.approx(50.0)


def test_sma_basic():
    got = sma([1, 2, 3, 4, 5], 3)
    assert got[:2] == [None, None]
    assert got[2] == pytest.approx(2.0)
    assert got[3] == pytest.approx(3.0)
    assert got[4] == pytest.approx(4.0)


def test_sma_constant_series_equals_value():
    got = sma([7.0] * 20, 10)
    for v in got[9:]:
        assert v == pytest.approx(7.0)


def test_ema_converges_toward_sma_on_constant_series():
    got = ema([5.0] * 50, 10)
    assert got[9] == pytest.approx(5.0)
    assert got[-1] == pytest.approx(5.0)


def test_ema_reacts_faster_than_sma():
    closes = [10.0] * 10 + [20.0] * 10
    s = sma(closes, 5)
    e = ema(closes, 5)
    # At the jump + 1 bar, EMA should already be closer to 20 than SMA.
    idx = 10
    assert abs(e[idx] - 20) < abs(s[idx] - 20)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


def test_rsi_is_100_on_pure_uptrend():
    closes = [i for i in range(1, 40)]  # strictly increasing
    r = rsi(closes, 14)
    # Every bar is a gain → avg_loss = 0 → RSI = 100.
    assert r[-1] == pytest.approx(100.0)


def test_rsi_is_0_on_pure_downtrend():
    closes = list(range(40, 1, -1))
    r = rsi(closes, 14)
    assert r[-1] == pytest.approx(0.0)


def test_rsi_around_50_on_whipsaw():
    closes = []
    v = 100.0
    for i in range(80):
        v += 1 if i % 2 == 0 else -1
        closes.append(v)
    r = rsi(closes, 14)
    assert 40 <= r[-1] <= 60


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


def test_macd_signs_follow_direction():
    up = [100 + i * 0.5 for i in range(100)]
    down = [100 - i * 0.5 for i in range(100)]
    m_up, s_up, h_up = macd(up)
    m_dn, s_dn, _ = macd(down)
    assert m_up[-1] > 0 and s_up[-1] > 0
    assert m_dn[-1] < 0 and s_dn[-1] < 0
    # On a perfectly linear drift, both EMAs converge to the same slope so
    # MACD == signal and the histogram collapses to ~0 (floating noise).
    assert h_up[40] is not None
    assert abs(h_up[40]) < 1e-6


def test_macd_short_series_returns_nones():
    m, s, h = macd([100.0, 101.0])
    assert all(v is None for v in m)
    assert all(v is None for v in s)


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------


def test_atr_positive_on_volatile_bars():
    n = 40
    highs = [100 + i + 1 for i in range(n)]
    lows = [100 + i - 1 for i in range(n)]
    closes = [100 + i for i in range(n)]
    a = atr(highs, lows, closes, 14)
    assert a[-1] is not None
    assert a[-1] > 0


def test_atr_zero_on_flatline():
    closes = [50.0] * 40
    a = atr(closes, closes, closes, 14)
    assert a[-1] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Pivots / S&R
# ---------------------------------------------------------------------------


def test_pivots_find_obvious_peaks():
    # Triangle series: peak at index 10.
    highs = [float(min(i, 20 - i)) for i in range(21)]
    lows = [-h for h in highs]
    sh, sl = pivots(highs, lows, lookback=3)
    assert 10 in sh
    assert 10 in sl  # symmetric by construction


def test_bollinger_mid_is_sma20():
    closes = [float(100 + (-1) ** i * 0.5) for i in range(80)]
    mid, up, lo = bollinger(closes, 20, 2.0)
    assert mid[-1] is not None and up[-1] is not None and lo[-1] is not None
    assert up[-1] > mid[-1] > lo[-1]


def test_adx_positive_on_trend():
    n = 160
    highs = [100.0 + i * 0.2 for i in range(n)]
    lows = [99.0 + i * 0.2 for i in range(n)]
    closes = [99.5 + i * 0.2 for i in range(n)]
    a, pdi, mdi = adx(highs, lows, closes, 14)
    assert a[-1] is not None and pdi[-1] is not None and mdi[-1] is not None
    assert pdi[-1] > mdi[-1]
    assert a[-1] > 20.0


def test_stochastic_k_bounded_and_uptrend_high_k():
    n = 120
    lows = [100.0 + i * 0.1 * 1.005 for i in range(n)]
    highs = [101.5 + i * 0.1 * 1.005 for i in range(n)]
    closes = [100.5 + i * 0.1 * 1.005 for i in range(n)]
    k, d = stochastic(highs, lows, closes, 14, 3, 3)
    assert k[-1] is not None and d[-1] is not None
    assert 0.0 <= k[-1] <= 100.0
    assert 0.0 <= d[-1] <= 100.0
    assert k[-1] > 60.0


def test_support_resistance_clusters_dedupe():
    highs = [100, 101, 100, 102, 100, 101.2, 100, 103, 100]
    lows = [95, 95, 95, 95, 95, 95, 95, 95, 95]
    last = 99
    supports, resistances = support_resistance(
        highs, lows, last, lookback=1, max_levels=5
    )
    # Two peaks are within 0.5% (101 and 101.2) → should dedupe.
    assert len(resistances) <= 3
    # 102 and 103 should survive.
    assert any(abs(r - 103) < 0.01 for r in resistances)
