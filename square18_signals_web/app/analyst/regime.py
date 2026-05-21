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
_breadth_refresh_pending: set[str] = set()
BREADTH_CACHE_TTL_SEC = 300.0
BREADTH_STALE_SERVE_SEC = 1800.0  # serve stale breadth up to 30m while refreshing


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


def _store_breadth(timeframe: str, pct: float) -> None:
    with _breadth_lock:
        _breadth_cache[timeframe] = (time.time(), pct)


def _peek_breadth(timeframe: str) -> float | None:
    now = time.time()
    with _breadth_lock:
        hit = _breadth_cache.get(timeframe)
        if hit and (now - hit[0]) < BREADTH_STALE_SERVE_SEC:
            return hit[1]
    return None


def schedule_breadth_refresh(timeframe: str = "daily") -> None:
    """Recompute breadth in the background without blocking HTTP handlers."""
    with _breadth_lock:
        if timeframe in _breadth_refresh_pending:
            return
        _breadth_refresh_pending.add(timeframe)

    def _run() -> None:
        try:
            pct = _compute_breadth(timeframe)
            _store_breadth(timeframe, pct)
        finally:
            with _breadth_lock:
                _breadth_refresh_pending.discard(timeframe)

    threading.Thread(
        target=_run,
        name=f"breadth-refresh-{timeframe}",
        daemon=True,
    ).start()


def breadth_above_50d(timeframe: str = "daily") -> float:
    """Cached % of tickers above their 50-bar SMA (non-blocking on cold cache)."""
    now = time.time()
    with _breadth_lock:
        hit = _breadth_cache.get(timeframe)
        if hit and (now - hit[0]) < BREADTH_CACHE_TTL_SEC:
            return hit[1]

    stale = _peek_breadth(timeframe)
    if stale is not None:
        schedule_breadth_refresh(timeframe)
        return stale

    schedule_breadth_refresh(timeframe)
    return 50.0
