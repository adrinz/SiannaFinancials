"""OHLCV data source with graceful fallback.

Strategy
--------
1. Primary: ``yfinance`` if installed *and* network is available. Tries
   once per symbol+timeframe, result is cached on disk as JSON in
   ``~/.cache/square18_signals/ohlcv/``.
2. Fallback: a seeded geometric Brownian motion generator that produces
   a plausible 1h base series; every coarser timeframe resamples from
   it. Deterministic across calls — same symbol always yields the same
   series so the UI is stable while offline.

No matter which path serves a request, the returned ``OHLCV`` shape is
identical, so the rest of the analyst stack is data-source agnostic.
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .constants import (
    ANCHOR_PRICES,
    DEFAULT_IV,
    HOURS_PER_SESSION,
    TICKER_MAP,
    Timeframe,
)

_CACHE_DIR = Path.home() / ".cache" / "square18_signals" / "ohlcv"

# Per-timeframe cache freshness, tuned for a "near-live" dashboard:
#   * intraday bars refresh every 5 min so the Refresh button actually
#     pulls new data mid-session
#   * daily refreshes every 15 min, so prices move during US market hours
#     without hammering Yahoo (free tier is ~15-20 min delayed anyway)
#   * weekly is stable — 1 day is plenty
# Override via the SQUARE18_OHLCV_TTL_* env vars if you need to go faster
# (for a paid feed) or slower (for offline demo).
_CACHE_TTL_INTRADAY = int(os.environ.get("SQUARE18_OHLCV_TTL_INTRADAY", 5 * 60))
_CACHE_TTL_DAILY    = int(os.environ.get("SQUARE18_OHLCV_TTL_DAILY",    15 * 60))
_CACHE_TTL_WEEKLY   = int(os.environ.get("SQUARE18_OHLCV_TTL_WEEKLY",   24 * 60 * 60))


def _ttl_for(timeframe: Timeframe) -> int:
    if timeframe in ("1h", "4h"):
        return _CACHE_TTL_INTRADAY
    if timeframe == "daily":
        return _CACHE_TTL_DAILY
    return _CACHE_TTL_WEEKLY


@dataclass(frozen=True)
class OHLCV:
    symbol: str
    timeframe: Timeframe
    timestamps: list[str]  # ISO-8601 strings, UTC
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    volume: list[float]
    source: str  # "yfinance" | "synthetic"

    def __len__(self) -> int:
        return len(self.close)

    @property
    def last(self) -> float:
        return self.close[-1]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_ohlcv(symbol: str, timeframe: Timeframe) -> OHLCV:
    """Return OHLCV for ``symbol`` at ``timeframe``.

    Checks on-disk cache first, then yfinance (if available), then
    synthesizes. Cache entries older than ``_CACHE_TTL_SECONDS`` are
    considered stale for intraday timeframes.
    """
    cached = _read_cache(symbol, timeframe)
    if cached is not None:
        return cached

    series = _fetch_yfinance(symbol, timeframe)
    if series is None:
        series = _synthesize(symbol, timeframe)

    _write_cache(series)
    return series


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_path(symbol: str, timeframe: Timeframe) -> Path:
    return _CACHE_DIR / f"{symbol.upper()}__{timeframe}.json"


def _read_cache(symbol: str, timeframe: Timeframe) -> Optional[OHLCV]:
    p = _cache_path(symbol, timeframe)
    if not p.exists():
        return None
    try:
        age = time.time() - p.stat().st_mtime
        if age > _ttl_for(timeframe):
            return None
        payload = json.loads(p.read_text())
        return OHLCV(**payload)
    except Exception:
        return None


def _write_cache(series: OHLCV) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(series.symbol, series.timeframe).write_text(
            json.dumps(asdict(series))
        )
    except Exception:
        # Caching is best-effort; never break the request because we
        # couldn't write to disk.
        pass


# ---------------------------------------------------------------------------
# yfinance path
# ---------------------------------------------------------------------------

_YFINANCE_PARAMS: dict[Timeframe, dict] = {
    # (period, interval) — yfinance constraints:
    #  1h requires period <= 730d, interval="1h".
    #  4h not native — resample from 1h.
    #  daily and weekly pull 5y of daily bars.
    "1h":     {"period": "180d",  "interval": "1h"},
    "4h":     {"period": "360d",  "interval": "1h"},  # resampled below
    "daily":  {"period": "2y",    "interval": "1d"},
    "weekly": {"period": "5y",    "interval": "1wk"},
}


def _fetch_yfinance(symbol: str, timeframe: Timeframe) -> Optional[OHLCV]:
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return None

    # Some identifiers (indices like VIX) need a different Yahoo symbol.
    meta = TICKER_MAP.get(symbol.upper(), {})
    yf_symbol = meta.get("yfinance_symbol", symbol)

    cfg = _YFINANCE_PARAMS[timeframe]
    try:
        t = yf.Ticker(yf_symbol)
        df = t.history(
            period=cfg["period"],
            interval=cfg["interval"],
            auto_adjust=True,
            actions=False,
        )
    except Exception:
        return None

    if df is None or df.empty or "Close" not in df.columns:
        return None

    # Ensure chronological order, drop NaNs defensively. Indices (e.g. ^VIX)
    # don't report volume — fill with 0 so downstream stats don't NaN-explode.
    df = df.sort_index().dropna(subset=["Open", "High", "Low", "Close"])
    if "Volume" in df.columns:
        df["Volume"] = df["Volume"].fillna(0)
    else:
        df["Volume"] = 0
    if df.empty:
        return None

    if timeframe == "4h":
        df = _resample_intraday_to_4h(df)
        if df is None or df.empty:
            return None

    ts = [x.to_pydatetime().astimezone(timezone.utc).isoformat() for x in df.index]
    return OHLCV(
        symbol=symbol.upper(),
        timeframe=timeframe,
        timestamps=ts,
        open=[float(x) for x in df["Open"].tolist()],
        high=[float(x) for x in df["High"].tolist()],
        low=[float(x) for x in df["Low"].tolist()],
        close=[float(x) for x in df["Close"].tolist()],
        volume=[float(x) for x in df["Volume"].tolist()],
        source="yfinance",
    )


def _resample_intraday_to_4h(df):
    try:
        agg = {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
        out = df.resample("4h").agg(agg).dropna(subset=["Open", "Close"])
        return out
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Synthetic fallback (deterministic per symbol)
# ---------------------------------------------------------------------------


def _synthesize(symbol: str, timeframe: Timeframe) -> OHLCV:
    base_1h = _synthesize_1h_base(symbol)

    if timeframe == "1h":
        return base_1h
    if timeframe == "4h":
        return _resample_bars(base_1h, step=4, new_timeframe="4h")
    if timeframe == "daily":
        return _resample_bars(
            base_1h, step=HOURS_PER_SESSION, new_timeframe="daily"
        )
    if timeframe == "weekly":
        daily = _resample_bars(
            base_1h, step=HOURS_PER_SESSION, new_timeframe="daily"
        )
        return _resample_daily_to_weekly(daily)
    raise ValueError(f"unknown timeframe: {timeframe}")


def _synthesize_1h_base(symbol: str) -> OHLCV:
    """Generate ~6 months of seeded 1h OHLCV bars."""
    meta = TICKER_MAP.get(symbol.upper())
    anchor = ANCHOR_PRICES.get(symbol.upper(), 100.0)
    annual_vol = DEFAULT_IV.get(symbol.upper(), 0.30)
    annual_drift = (meta or {}).get("bias", 0.02)

    sessions = 130  # ~6 months of trading days
    total_bars = sessions * HOURS_PER_SESSION
    dt_years = 1.0 / (252 * HOURS_PER_SESSION)

    rng = random.Random((hash(symbol.upper()) & 0xFFFFFFFF) ^ 0xBEEFBEEF)

    mu = annual_drift
    sigma = annual_vol
    drift_per = (mu - 0.5 * sigma * sigma) * dt_years
    vol_per = sigma * math.sqrt(dt_years)

    closes: list[float] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    volumes: list[float] = []

    # Walk backwards from today so the last bar is "now".
    price = anchor
    # Seed regime — ends bullish/bearish near present based on bias sign.
    for i in range(total_bars):
        shock = rng.gauss(0, 1)
        log_ret = drift_per + vol_per * shock
        new_price = price * math.exp(log_ret)
        bar_open = price
        bar_close = new_price
        hi = max(bar_open, bar_close) * (1 + abs(rng.gauss(0, 0.0015)))
        lo = min(bar_open, bar_close) * (1 - abs(rng.gauss(0, 0.0015)))
        opens.append(round(bar_open, 4))
        closes.append(round(bar_close, 4))
        highs.append(round(hi, 4))
        lows.append(round(lo, 4))
        # Volume: log-normal around a ticker-scaled mean with spikes.
        base_vol = max(1e6 * (anchor / 100.0) ** 0.3, 5e5)
        vol_shock = math.exp(rng.gauss(0, 0.45))
        volumes.append(round(base_vol * vol_shock, 0))
        price = new_price

    # Rebase closing price to the anchor (keep the shape but end near anchor).
    scale = anchor / closes[-1]
    opens = [x * scale for x in opens]
    closes = [x * scale for x in closes]
    highs = [x * scale for x in highs]
    lows = [x * scale for x in lows]

    # Timestamps: walk backwards in market hours.
    timestamps = _generate_session_timestamps(total_bars)

    return OHLCV(
        symbol=symbol.upper(),
        timeframe="1h",
        timestamps=timestamps,
        open=opens,
        high=highs,
        low=lows,
        close=closes,
        volume=volumes,
        source="synthetic",
    )


def _generate_session_timestamps(total_bars: int) -> list[str]:
    """ISO timestamps walking backwards from now, skipping weekends.

    Puts bars on the hour during a 09:30-16:00 ET session; we approximate
    by using UTC 14:30-21:00 (fine for a synthetic baseline — real data
    via yfinance carries authoritative timestamps).
    """
    out: list[datetime] = []
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    # Snap to the nearest session end.
    d = end.date()
    cur = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).replace(
        hour=20
    )
    while len(out) < total_bars:
        # Skip weekends.
        if cur.weekday() < 5:
            for h_off in range(HOURS_PER_SESSION):
                ts = cur.replace(hour=14 + h_off)  # 14:00 → 20:00 UTC
                out.append(ts)
                if len(out) >= total_bars:
                    break
        cur = cur - timedelta(days=1)
    out.sort()
    return [t.isoformat() for t in out]


def _resample_bars(src: OHLCV, step: int, new_timeframe: Timeframe) -> OHLCV:
    """Aggregate groups of ``step`` consecutive bars in ``src`` into new bars."""
    n = len(src)
    timestamps: list[str] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[float] = []
    i = 0
    while i + step <= n:
        timestamps.append(src.timestamps[i + step - 1])
        opens.append(src.open[i])
        highs.append(max(src.high[i : i + step]))
        lows.append(min(src.low[i : i + step]))
        closes.append(src.close[i + step - 1])
        volumes.append(sum(src.volume[i : i + step]))
        i += step
    return OHLCV(
        symbol=src.symbol,
        timeframe=new_timeframe,
        timestamps=timestamps,
        open=opens,
        high=highs,
        low=lows,
        close=closes,
        volume=volumes,
        source=src.source,
    )


def _resample_daily_to_weekly(daily: OHLCV) -> OHLCV:
    """Group daily bars by ISO week-ending-Friday."""
    timestamps: list[str] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[float] = []

    bucket: list[int] = []
    for i, ts_iso in enumerate(daily.timestamps):
        ts = datetime.fromisoformat(ts_iso)
        bucket.append(i)
        # Close the bucket on Friday (weekday 4) or at the end of data.
        if ts.weekday() == 4 or i == len(daily.timestamps) - 1:
            start = bucket[0]
            end = bucket[-1]
            timestamps.append(daily.timestamps[end])
            opens.append(daily.open[start])
            highs.append(max(daily.high[start : end + 1]))
            lows.append(min(daily.low[start : end + 1]))
            closes.append(daily.close[end])
            volumes.append(sum(daily.volume[start : end + 1]))
            bucket = []

    return OHLCV(
        symbol=daily.symbol,
        timeframe="weekly",
        timestamps=timestamps,
        open=opens,
        high=highs,
        low=lows,
        close=closes,
        volume=volumes,
        source=daily.source,
    )
