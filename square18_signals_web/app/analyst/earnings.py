"""Upcoming earnings calendar for the Screener tab.

Best-effort lookup of next earnings dates via ``yfinance``. Each ticker is
queried independently; failures are swallowed so a single bad symbol can
never break the screener card. Results are cached in-process for a short
window so repeated requests within the auto-refresh cycle are cheap.

Public surface
--------------
``upcoming_earnings(window_days=14)`` returns a list of
:class:`EarningsRow` sorted by date ascending. Tickers without a known
upcoming earnings date inside the window are simply omitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional
import time

from .constants import TICKERS
from .report import overview_rows


# In-process cache so the three screener cards (jumps/dips/earnings)
# don't each trigger a fresh yfinance fan-out. The cache is invalidated
# by wall-clock TTL — any deeper invalidation should happen by restarting
# the worker, which the auto-refresh loop already covers.
_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes
_cache: dict[str, object] = {"ts": 0.0, "rows": []}


@dataclass
class EarningsRow:
    symbol: str
    name: str
    sector: str
    earnings_date: str  # ISO date, e.g. "2026-04-29"
    days_until: int
    last: Optional[float]
    change_pct: Optional[float]
    verdict: Optional[str]


def _yfinance_symbol(meta: dict) -> str:
    return meta.get("yfinance_symbol") or meta["symbol"]


def _coerce_date(value: object) -> Optional[date]:
    """Best-effort conversion of yfinance calendar values to a ``date``."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # pandas Timestamp / numpy datetime64 expose .to_pydatetime() or are
    # convertible via str().
    to_py = getattr(value, "to_pydatetime", None)
    if callable(to_py):
        try:
            return to_py().date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def _next_earnings_date(yf, symbol: str) -> Optional[date]:
    """Pull the next earnings date from yfinance, tolerant of API drift."""
    try:
        tk = yf.Ticker(symbol)
    except Exception:
        return None

    candidates: list[date] = []

    cal = getattr(tk, "calendar", None)
    if cal is not None:
        # Newer yfinance returns a dict; older versions return a DataFrame.
        if isinstance(cal, dict):
            for key in ("Earnings Date", "earnings_date", "EarningsDate"):
                v = cal.get(key)
                if isinstance(v, (list, tuple)):
                    for item in v:
                        d = _coerce_date(item)
                        if d:
                            candidates.append(d)
                else:
                    d = _coerce_date(v)
                    if d:
                        candidates.append(d)
        else:
            try:
                # DataFrame path: row "Earnings Date" with column 0/1.
                row = cal.loc["Earnings Date"]  # type: ignore[index]
                values = row.values if hasattr(row, "values") else [row]
                for item in values:
                    d = _coerce_date(item)
                    if d:
                        candidates.append(d)
            except Exception:
                pass

    if not candidates:
        # Fallback to earnings_dates frame (most reliable on newer yfinance).
        try:
            df = tk.get_earnings_dates(limit=8)  # type: ignore[attr-defined]
        except Exception:
            df = None
        if df is not None and getattr(df, "index", None) is not None:
            try:
                today = date.today()
                for idx in df.index:
                    d = _coerce_date(idx)
                    if d and d >= today:
                        candidates.append(d)
            except Exception:
                pass

    today = date.today()
    future = sorted(d for d in candidates if d >= today)
    return future[0] if future else None


def upcoming_earnings(window_days: int = 14) -> list[EarningsRow]:
    """Return upcoming earnings within ``window_days`` for the curated universe.

    Sorted ascending by ``earnings_date``. Best-effort: yfinance failures
    silently skip the affected ticker. Cached in-process for 30 minutes.
    """
    now = time.time()
    cached_rows = _cache.get("rows") or []
    if cached_rows and now - float(_cache.get("ts") or 0) < _CACHE_TTL_SECONDS:
        return _filter_window(cached_rows, window_days)  # type: ignore[arg-type]

    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return []

    # Decorate yfinance results with the latest price/verdict so the UI
    # can show context next to each earnings entry.
    overview_by_symbol = {r.symbol: r for r in overview_rows("daily")}

    rows: list[EarningsRow] = []
    today = date.today()
    for meta in TICKERS:
        symbol = meta["symbol"]
        if symbol == "VIX":  # index, no earnings
            continue
        d = _next_earnings_date(yf, _yfinance_symbol(meta))
        if d is None:
            continue
        ov = overview_by_symbol.get(symbol)
        rows.append(
            EarningsRow(
                symbol=symbol,
                name=meta["name"],
                sector=meta["sector"],
                earnings_date=d.isoformat(),
                days_until=(d - today).days,
                last=ov.last if ov else None,
                change_pct=ov.change_pct if ov else None,
                verdict=ov.verdict if ov else None,
            )
        )

    rows.sort(key=lambda r: r.earnings_date)
    _cache["rows"] = rows
    _cache["ts"] = now
    return _filter_window(rows, window_days)


def _filter_window(rows: list[EarningsRow], window_days: int) -> list[EarningsRow]:
    cutoff = window_days
    return [r for r in rows if 0 <= r.days_until <= cutoff]


def reset_cache() -> None:
    """Test helper — flush the in-process cache."""
    _cache["rows"] = []
    _cache["ts"] = 0.0
