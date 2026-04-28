"""Technical indicators — pure Python, stdlib only.

Every function operates on plain lists of floats aligned with a shared
bar index, and returns lists of the same length. Values that cannot be
computed yet (insufficient lookback) are ``None`` so the UI can skip
them cleanly.

Conventions
-----------
- ``close`` is the ordered, contiguous list of closes (oldest → newest).
- Wilder's smoothing is used for RSI and ATR, matching TradingView / most
  charting packages.
- MACD uses the standard 12 / 26 / 9 EMA recipe.
- Stochastic defaults (14, 3, 3): raw %K over ``k_period`` highs/lows,
  then SMA-smoothed %K and %D (standard "full stoch" convention).
"""
from __future__ import annotations

import math
from typing import Optional

__all__ = [
    "sma", "ema", "rsi", "macd", "atr", "rolling_std",
    "adx", "bollinger", "stochastic",
    "pivots", "support_resistance",
]


def sma(values: list[float], period: int) -> list[Optional[float]]:
    """Simple moving average with a trailing window of ``period`` bars."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[Optional[float]] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential moving average seeded from the initial SMA."""
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1)
    for i in range(period, len(values)):
        prev = out[i - 1]
        assert prev is not None
        out[i] = alpha * values[i] + (1 - alpha) * prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    """Relative Strength Index with Wilder's smoothing."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if n <= period:
        return out

    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gains[i] = max(diff, 0.0)
        losses[i] = max(-diff, 0.0)

    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    def _rsi_from(avg_g: float, avg_l: float) -> float:
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """Return (macd_line, signal_line, histogram) aligned with ``closes``."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    macd_line: list[Optional[float]] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]
    # Seed signal line from the first available MACD point.
    seed_idx = next((i for i, v in enumerate(macd_line) if v is not None), None)
    signal_line: list[Optional[float]] = [None] * len(closes)
    if seed_idx is not None and len(closes) - seed_idx >= signal:
        seed_window = macd_line[seed_idx : seed_idx + signal]
        # seed_window has no Nones by construction of seed_idx
        seed_vals = [v for v in seed_window if v is not None]
        seed = sum(seed_vals) / len(seed_vals)
        idx = seed_idx + signal - 1
        signal_line[idx] = seed
        alpha = 2.0 / (signal + 1)
        for i in range(idx + 1, len(closes)):
            prev = signal_line[i - 1]
            cur = macd_line[i]
            if prev is None or cur is None:
                continue
            signal_line[i] = alpha * cur + (1 - alpha) * prev
    hist: list[Optional[float]] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, hist


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[Optional[float]]:
    """Average True Range with Wilder's smoothing."""
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs/lows/closes must share length")
    out: list[Optional[float]] = [None] * n
    if n <= period:
        return out

    tr = [0.0] * n
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    first_atr = sum(tr[1 : period + 1]) / period
    out[period] = first_atr
    for i in range(period + 1, n):
        prev = out[i - 1]
        assert prev is not None
        out[i] = (prev * (period - 1) + tr[i]) / period
    return out


def bollinger(
    closes: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[
    list[Optional[float]],
    list[Optional[float]],
    list[Optional[float]],
]:
    """Bollinger Bands: middle = SMA, upper/lower = middle ± ``num_std`` × σ.

    σ uses the same trailing window as ``rolling_std`` (sample std).
    """
    mid = sma(closes, period)
    std_s = rolling_std(closes, period)
    upper: list[Optional[float]] = [None] * len(closes)
    lower: list[Optional[float]] = [None] * len(closes)
    for i in range(len(closes)):
        m, s = mid[i], std_s[i]
        if m is not None and s is not None:
            upper[i] = m + num_std * s
            lower[i] = m - num_std * s
    return mid, upper, lower


def stochastic(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    k_period: int = 14,
    k_smooth: int = 3,
    d_smooth: int = 3,
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """Stochastic %K and %D (oscillator 0–100).

    Raw %K uses the highest high / lowest low over ``k_period`` bars and
    compares the current close to that range. Fast %K applies a
    ``k_smooth``-bar simple average of raw; %D is a ``d_smooth``-bar
    average of %K (TradingView-style 14 / 3 / 3 when defaults are used).
    """
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs/lows/closes must share length")
    if k_period < 1 or k_smooth < 1 or d_smooth < 1:
        raise ValueError("periods must be >= 1")

    k_line: list[Optional[float]] = [None] * n
    d_line: list[Optional[float]] = [None] * n

    first_raw = k_period - 1
    if n <= first_raw:
        return k_line, d_line

    raw: list[Optional[float]] = [None] * n
    for i in range(first_raw, n):
        lo = min(lows[i - k_period + 1 : i + 1])
        hi = max(highs[i - k_period + 1 : i + 1])
        denom = hi - lo
        raw[i] = 50.0 if denom <= 1e-15 else 100.0 * (closes[i] - lo) / denom

    first_k_idx = k_period + k_smooth - 2
    for i in range(first_k_idx, n):
        w_raw = raw[i - k_smooth + 1 : i + 1]
        k_line[i] = sum(float(x) for x in w_raw) / float(k_smooth)

    first_d_idx = k_period + k_smooth + d_smooth - 3
    for i in range(first_d_idx, n):
        w_k = k_line[i - d_smooth + 1 : i + 1]
        d_line[i] = sum(float(x) for x in w_k) / float(d_smooth)

    return k_line, d_line


def adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> tuple[
    list[Optional[float]],
    list[Optional[float]],
    list[Optional[float]],
]:
    """Wilder ADX with +DI / −DI (Welles Wilder formulation).

    Returns ``(adx, plus_di, minus_di)``, aligned bar-for-bar with ``closes``.
    First finite values appear after roughly ``2 × period`` bars.
    """
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("highs/lows/closes must share length")
    adx_out: list[Optional[float]] = [None] * n
    pdi_out: list[Optional[float]] = [None] * n
    mdi_out: list[Optional[float]] = [None] * n
    # First finite ADX sits at index ``2*period - 1`` (0-based).
    if n < 2 * period:
        return adx_out, pdi_out, mdi_out

    tr_raw = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0.0
        tr_raw[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    cur_tr = sum(tr_raw[1 : period + 1]) / period
    cur_pp = sum(plus_dm[1 : period + 1]) / period
    cur_mm = sum(minus_dm[1 : period + 1]) / period
    dx_hist: list[Optional[float]] = [None] * n

    for i in range(period, n):
        if i > period:
            cur_tr = (cur_tr * (period - 1) + tr_raw[i]) / period
            cur_pp = (cur_pp * (period - 1) + plus_dm[i]) / period
            cur_mm = (cur_mm * (period - 1) + minus_dm[i]) / period

        tr_d = cur_tr if cur_tr > 1e-15 else 1e-15
        pdi_val = 100.0 * (cur_pp / tr_d)
        mdi_val = 100.0 * (cur_mm / tr_d)
        pdi_out[i] = pdi_val
        mdi_out[i] = mdi_val
        denom = pdi_val + mdi_val
        dx_hist[i] = (
            100.0 * abs(pdi_val - mdi_val) / denom if denom > 1e-15 else 0.0
        )

    # ADX is Wilder-smoothed DX; first finite ADX averages ``period`` DX samples.
    first_adx_bar = 2 * period - 1
    if first_adx_bar >= n:
        return adx_out, pdi_out, mdi_out

    avg_dx = sum(dx_hist[i] or 0.0 for i in range(period, first_adx_bar + 1)) / period
    adx_out[first_adx_bar] = avg_dx

    prev_adx = avg_dx
    for k in range(first_adx_bar + 1, n):
        dv = dx_hist[k]
        if dv is None:
            continue
        prev_adx = (prev_adx * (period - 1) + dv) / period
        adx_out[k] = prev_adx

    return adx_out, pdi_out, mdi_out


def rolling_std(values: list[float], period: int) -> list[Optional[float]]:
    """Sample standard deviation over trailing ``period`` bars."""
    if period <= 1:
        raise ValueError("period must be > 1")
    n = len(values)
    out: list[Optional[float]] = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / (period - 1)
        out[i] = math.sqrt(var)
    return out


# ---------------------------------------------------------------------------
# Support / resistance via pivots
# ---------------------------------------------------------------------------


def pivots(
    highs: list[float],
    lows: list[float],
    lookback: int = 5,
) -> tuple[list[int], list[int]]:
    """Return indices of (swing_high_pivots, swing_low_pivots).

    A bar at index ``i`` is a swing high if ``highs[i]`` is strictly
    greater than all ``highs`` within ±``lookback`` bars. Analogously for
    swing lows.
    """
    n = len(highs)
    sh: list[int] = []
    sl: list[int] = []
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback : i + lookback + 1]
        window_l = lows[i - lookback : i + lookback + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            sh.append(i)
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            sl.append(i)
    return sh, sl


def support_resistance(
    highs: list[float],
    lows: list[float],
    last_close: float,
    lookback: int = 5,
    max_levels: int = 3,
) -> tuple[list[float], list[float]]:
    """Derive the most recent support (below) and resistance (above) levels.

    Picks the most recent pivot lows below ``last_close`` as supports and
    the most recent pivot highs above ``last_close`` as resistances, up to
    ``max_levels`` each. De-duplicates levels that cluster within 0.5%.
    """
    sh_idx, sl_idx = pivots(highs, lows, lookback=lookback)

    supports_raw = [lows[i] for i in sl_idx if lows[i] < last_close]
    resistances_raw = [highs[i] for i in sh_idx if highs[i] > last_close]

    # Most recent first, then cluster-dedupe.
    supports_raw = list(reversed(supports_raw))
    resistances_raw = list(reversed(resistances_raw))

    def _dedupe(levels: list[float]) -> list[float]:
        out: list[float] = []
        for lv in levels:
            if all(abs(lv - other) / max(other, 1e-9) > 0.005 for other in out):
                out.append(lv)
            if len(out) >= max_levels:
                break
        return out

    return _dedupe(supports_raw), _dedupe(resistances_raw)
