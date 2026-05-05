"""Market-regime helpers (VIX + breadth) shared by report.py and services.py.

Separating this into its own module avoids the circular-import that would
arise from report.py importing services.py (which already imports report.py).
"""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .constants import Timeframe

_breadth_lock = threading.Lock()
_breadth_cache: dict[str, tuple[float, float]] = {}  # tf -> (ts, pct)
BREADTH_CACHE_TTL_SEC = 300.0


def vix_quote() -> tuple[float, float]:
    """Return (vix_last, vix_1d_change). Falls back to a sensible default."""
    try:
        from .data import get_ohlcv
        series = get_ohlcv("VIX", "daily")
        if len(series) >= 2:
            last = float(series.close[-1])
            prev = float(series.close[-2])
            return last, last - prev
        if len(series) == 1:
            return float(series.close[-1]), 0.0
    except Exception:
        pass
    return 17.0, 0.0


def _compute_breadth(timeframe: str) -> float:
    """% of tracked equities trading above their 50-bar SMA (uncached)."""
    from .constants import TICKER_MAP
    from .data import get_ohlcv
    from .indicators import sma

    symbols = [s for s in TICKER_MAP if s != "VIX"]
    above = total = 0
    for sym in symbols:
        try:
            series = get_ohlcv(sym, timeframe)  # type: ignore[arg-type]
        except Exception:
            continue
        closes = series.close
        if len(closes) < 50:
            continue
        s50 = sma(closes, 50)
        if not s50 or s50[-1] is None:
            continue
        total += 1
        if closes[-1] > s50[-1]:
            above += 1
    return (above / total * 100.0) if total else 50.0


def breadth_above_50d(timeframe: str = "daily") -> float:
    """Cached % of tickers above their 50-bar SMA."""
    now = time.time()
    with _breadth_lock:
        hit = _breadth_cache.get(timeframe)
        if hit and (now - hit[0]) < BREADTH_CACHE_TTL_SEC:
            return hit[1]

    pct = _compute_breadth(timeframe)

    with _breadth_lock:
        now2 = time.time()
        hit2 = _breadth_cache.get(timeframe)
        if hit2 and (now2 - hit2[0]) < BREADTH_CACHE_TTL_SEC:
            return hit2[1]
        _breadth_cache[timeframe] = (time.time(), pct)
    return pct
