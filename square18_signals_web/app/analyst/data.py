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

import concurrent.futures
import json
import logging
import math
import os
import random
import threading
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
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# Per-timeframe cache freshness, tuned for a "near-live" dashboard:
#   * 1h/4h: default 5 min (shared intraday file cache)
#   * 1D session chart: separate, shorter default (2 min) so the detail
#     view can poll yfinance that often without hammering 1h/4h
#   * daily: 15 min — Yahoo free tier is ~15–20 min delayed anyway
#   * weekly: stable
# Override via SQUARE18_OHLCV_TTL_* env vars.
_CACHE_TTL_INTRADAY = int(os.environ.get("SQUARE18_OHLCV_TTL_INTRADAY", 5 * 60))
# Disk cache for get_ohlcv_1d_intraday() only; align with static DETAIL_CHART_POLL_MS.
_CACHE_TTL_1D_INTRADAY = int(os.environ.get("SQUARE18_OHLCV_TTL_1D_INTRADAY", 2 * 60))
_CACHE_TTL_DAILY    = int(os.environ.get("SQUARE18_OHLCV_TTL_DAILY",    15 * 60))
_CACHE_TTL_WEEKLY   = int(os.environ.get("SQUARE18_OHLCV_TTL_WEEKLY",   24 * 60 * 60))

# yfinance can hang indefinitely on a stuck socket; run each pull in a worker with a hard cap.
_YF_REQUEST_TIMEOUT = float(os.environ.get("SQUARE18_YF_TIMEOUT", "6"))
_YF_FAIL_STREAK_THRESHOLD = int(os.environ.get("SQUARE18_YF_FAIL_STREAK_THRESHOLD", "1"))
_YF_CIRCUIT_COOLDOWN_SEC = int(os.environ.get("SQUARE18_YF_CIRCUIT_COOLDOWN_SEC", "300"))
_YF_CIRCUIT_UNTIL = 0.0
_YF_FAIL_STREAK = 0
_YF_CIRCUIT_LOCK = threading.Lock()


def _threaded_with_timeout(fn, timeout_sec: float):
    """Run *fn* in a one-off thread; on timeout or any error, return None."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=timeout_sec)
    except (concurrent.futures.TimeoutError, Exception):
        fut.cancel()
        return None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


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

    Checks on-disk cache first, then Tradier, then yfinance, then
    synthesizes. Cache entries older than ``_CACHE_TTL_SECONDS`` are
    considered stale for intraday timeframes.
    """
    cached = _read_cache(symbol, timeframe)
    if cached is not None:
        return cached

    series = _fetch_tradier(symbol, timeframe)
    if series is None:
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


def _fetch_tradier(symbol: str, timeframe: Timeframe) -> Optional[OHLCV]:
    """Fetch OHLCV from Tradier API."""
    from .tradier_client import get_historical_quotes, get_timesales, is_configured
    if not is_configured():
        return None

    import pandas as pd
    end_dt = datetime.now()

    try:
        if timeframe in ("daily", "weekly"):
            start_dt = end_dt - timedelta(days=5*365 if timeframe == "weekly" else 2*365)
            data = get_historical_quotes(
                symbol, 
                timeframe, 
                start_dt.strftime("%Y-%m-%d"), 
                end_dt.strftime("%Y-%m-%d")
            )
            if not data:
                return None
            
            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns:
                return None
                
            # Tradier history returns 'date'
            df["timestamp"] = pd.to_datetime(df["date"]).dt.tz_localize("America/New_York").dt.tz_convert("UTC")
            df = df.set_index("timestamp").sort_index()
            
        elif timeframe in ("1h", "4h"):
            # Fetch 40 days of 15min data (gives ~2000 bars, enough for 200 SMA on 1h/4h)
            start_dt = end_dt - timedelta(days=40)
            data = get_timesales(
                symbol, 
                "15min", 
                start_dt.strftime("%Y-%m-%d %H:%M"), 
                end_dt.strftime("%Y-%m-%d %H:%M")
            )
            if not data:
                return None
                
            df = pd.DataFrame(data)
            if df.empty or "close" not in df.columns:
                return None
                
            # Tradier timesales returns 'time'
            df["timestamp"] = pd.to_datetime(df["time"]).dt.tz_localize("America/New_York").dt.tz_convert("UTC")
            df = df.set_index("timestamp").sort_index()
            
            # Resample 15min to 1h or 4h
            agg = {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
            resample_rule = "1h" if timeframe == "1h" else "4h"
            df = df.resample(resample_rule).agg(agg).dropna(subset=["open", "close"])
            
            if df.empty:
                return None
        else:
            return None

        ts = [x.to_pydatetime().isoformat() for x in df.index]
        return OHLCV(
            symbol=symbol.upper(),
            timeframe=timeframe,
            timestamps=ts,
            open=[float(x) for x in df["open"].tolist()],
            high=[float(x) for x in df["high"].tolist()],
            low=[float(x) for x in df["low"].tolist()],
            close=[float(x) for x in df["close"].tolist()],
            volume=[float(x) for x in df["volume"].tolist()],
            source="tradier",
        )
    except Exception as e:
        print(f"Tradier OHLCV fetch failed for {symbol}: {e}")
        return None

def _fetch_yfinance(symbol: str, timeframe: Timeframe) -> Optional[OHLCV]:
    global _YF_FAIL_STREAK, _YF_CIRCUIT_UNTIL  # noqa: PLW0603
    now = time.time()
    with _YF_CIRCUIT_LOCK:
        if now < _YF_CIRCUIT_UNTIL:
            return None

    def _pull() -> Optional[OHLCV]:
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

    out = _threaded_with_timeout(_pull, _YF_REQUEST_TIMEOUT)  # type: ignore[return-value]
    now2 = time.time()
    with _YF_CIRCUIT_LOCK:
        if out is None:
            _YF_FAIL_STREAK += 1
            if _YF_FAIL_STREAK >= _YF_FAIL_STREAK_THRESHOLD:
                _YF_CIRCUIT_UNTIL = now2 + _YF_CIRCUIT_COOLDOWN_SEC
        else:
            _YF_FAIL_STREAK = 0
            _YF_CIRCUIT_UNTIL = 0.0
    return out


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


# ---------------------------------------------------------------------------
# One-day intraday chart (1m / 2m / 5m) — smooth Yahoo-style 1D view
# ---------------------------------------------------------------------------

def _cache_path_1d_intraday(symbol: str) -> Path:
    return _CACHE_DIR / f"{symbol.upper()}__1d_intraday.json"


def _read_cache_1d_intraday(symbol: str) -> Optional[OHLCV]:
    p = _cache_path_1d_intraday(symbol)
    if not p.exists():
        return None
    try:
        age = time.time() - p.stat().st_mtime
        if age > _CACHE_TTL_1D_INTRADAY:
            return None
        payload = json.loads(p.read_text())
        return OHLCV(**payload)
    except Exception:
        return None


def _write_cache_1d_intraday(series: OHLCV) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path_1d_intraday(series.symbol).write_text(json.dumps(asdict(series)))
    except Exception:
        pass


def _df_to_ohlcv_1h_style(symbol: str, df, source: str) -> OHLCV:
    """Build OHLCV from a yfinance intraday dataframe (minute/2m/5m bars)."""
    df = df.sort_index().dropna(subset=["Open", "High", "Low", "Close"])
    if "Volume" in df.columns:
        df["Volume"] = df["Volume"].fillna(0)
    else:
        df["Volume"] = 0
    if df.empty:
        raise ValueError("empty frame")
    ts = [x.to_pydatetime().astimezone(timezone.utc).isoformat() for x in df.index]
    return OHLCV(
        symbol=symbol.upper(),
        timeframe="1h",
        timestamps=ts,
        open=[float(x) for x in df["Open"].tolist()],
        high=[float(x) for x in df["High"].tolist()],
        low=[float(x) for x in df["Low"].tolist()],
        close=[float(x) for x in df["Close"].tolist()],
        volume=[float(x) for x in df["Volume"].tolist()],
        source=source,
    )


def _filter_last_ny_session_day(df):
    """Keep rows whose calendar date in New York matches the last row (one session)."""
    try:
        idx = df.index
        if getattr(idx, "tz", None) is None:
            try:
                idx_ny = idx.tz_localize("America/New_York", ambiguous="infer", nonexistent="shift_forward")
            except Exception:
                idx_ny = idx.tz_localize("UTC").tz_convert("America/New_York")
        else:
            idx_ny = idx.tz_convert("America/New_York")
        last_d = idx_ny[-1].date()
        mask = [ts.date() == last_d for ts in idx_ny]
        out = df.loc[mask]
        return out if len(out) >= 2 else df
    except Exception:
        return df


def _fetch_tradier_1d_intraday(symbol: str) -> Optional[OHLCV]:
    """1m/5m with extended hours, matching Yahoo's dense 1D line."""
    from .tradier_client import get_timesales, is_configured
    if not is_configured():
        return None

    import pandas as pd
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=5)

    try:
        # Try 1min first for highest resolution
        data = get_timesales(
            symbol, 
            "1min", 
            start_dt.strftime("%Y-%m-%d %H:%M"), 
            end_dt.strftime("%Y-%m-%d %H:%M")
        )
        if not data:
            # Fallback to 5min
            data = get_timesales(
                symbol, 
                "5min", 
                start_dt.strftime("%Y-%m-%d %H:%M"), 
                end_dt.strftime("%Y-%m-%d %H:%M")
            )
            
        if not data:
            return None
            
        df = pd.DataFrame(data)
        if df.empty or "close" not in df.columns:
            return None
            
        df["timestamp"] = pd.to_datetime(df["time"]).dt.tz_localize("America/New_York").dt.tz_convert("UTC")
        df = df.set_index("timestamp").sort_index()
        
        # Filter to last trading day
        df = _filter_last_ny_session_day(df)
        if len(df) < 8:
            return None
            
        # Map columns to Yahoo style for _df_to_ohlcv_1h_style
        df = df.rename(columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume"
        })
        
        return _df_to_ohlcv_1h_style(symbol, df, "tradier")
    except Exception as e:
        print(f"Tradier 1d intraday fetch failed for {symbol}: {e}")
        return None

def _fetch_yfinance_1d_intraday(symbol: str) -> Optional[OHLCV]:
    """1m/2m/5m with extended hours, matching Yahoo's dense 1D line."""

    def _pull() -> Optional[OHLCV]:
        try:
            import yfinance as yf  # type: ignore
        except Exception:
            return None
        meta = TICKER_MAP.get(symbol.upper(), {})
        yf_symbol = meta.get("yfinance_symbol", symbol)
        t = yf.Ticker(yf_symbol)

        for period, interval in (("1d", "1m"), ("1d", "2m"), ("5d", "5m"), ("5d", "15m")):
            try:
                df = t.history(
                    period=period,
                    interval=interval,
                    prepost=True,
                    auto_adjust=True,
                    actions=False,
                )
            except Exception:
                continue
            if df is None or df.empty or "Close" not in df.columns:
                continue
            if period == "1d" and interval in ("1m", "2m"):
                df = _filter_last_ny_session_day(df)
            if period == "5d" and interval in ("5m", "15m"):
                df = _filter_last_ny_session_day(df)
            try:
                o = _df_to_ohlcv_1h_style(symbol, df, "yfinance")
            except Exception:
                continue
            if len(o) < 8:
                continue
            return o
        return None

    return _threaded_with_timeout(_pull, _YF_REQUEST_TIMEOUT)  # type: ignore[return-value]


def _synthesize_1d_intraday_from_1h(symbol: str) -> Optional[OHLCV]:
    """Smooth, dense line when yfinance has no 1m: interpolate last session of 1h bars."""
    h = get_ohlcv(symbol, "1h")
    if len(h) < 2:
        return None
    nuse = min(10, len(h))
    t0s = h.timestamps[-nuse:]
    cls = h.close[-nuse:]
    t_start = datetime.fromisoformat(
        t0s[0].replace("Z", "+00:00")
    )
    t_end = datetime.fromisoformat(
        t0s[-1].replace("Z", "+00:00")
    )
    span_sec = max(60, (t_end - t_start).total_seconds())
    n_out = min(400, max(80, int(span_sec // 60) + 1))
    ts_out: list[str] = []
    o_out: list[float] = []
    h_out: list[float] = []
    l_out: list[float] = []
    c_out: list[float] = []
    v_out: list[float] = []
    for k in range(n_out):
        alpha = k / max(1, n_out - 1)
        pos = alpha * (nuse - 1)
        i = int(pos)
        f = pos - i
        i2 = min(i + 1, nuse - 1)
        c0 = float(cls[i])
        c1 = float(cls[i2])
        c = c0 * (1.0 - f) + c1 * f
        tick = t_start + timedelta(seconds=alpha * span_sec)
        ts_out.append(tick.astimezone(timezone.utc).isoformat())
        o_out.append(c)
        h_out.append(c * 1.0002)
        l_out.append(c * 0.9998)
        c_out.append(c)
        v_out.append(0.0)
    return OHLCV(
        symbol=symbol.upper(),
        timeframe="1h",
        timestamps=ts_out,
        open=o_out,
        high=h_out,
        low=l_out,
        close=c_out,
        volume=v_out,
        source="synthetic-1d-intra",
    )


def get_ohlcv_1d_intraday(symbol: str) -> OHLCV:
    """Return dense intraday series for 1D chart (minute bars when available)."""
    sym = symbol.upper()
    cached = _read_cache_1d_intraday(sym)
    if cached is not None:
        return cached
        
    s = _fetch_tradier_1d_intraday(sym)
    if s is None or len(s) < 2:
        s = _fetch_yfinance_1d_intraday(sym)
    if s is None or len(s) < 2:
        s = _synthesize_1d_intraday_from_1h(sym)
    if s is None or len(s) < 2:
        h = get_ohlcv(sym, "1h")
        n = min(16, len(h))
        if n >= 2 and len(h) >= n:
            s = OHLCV(
                symbol=h.symbol,
                timeframe="1h",
                timestamps=h.timestamps[-n:],
                open=h.open[-n:],
                high=h.high[-n:],
                low=h.low[-n:],
                close=h.close[-n:],
                volume=h.volume[-n:],
                source=h.source,
            )
        else:
            s = h
    _write_cache_1d_intraday(s)
    return s
