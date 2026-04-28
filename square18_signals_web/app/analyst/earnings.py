"""Upcoming earnings calendar for the Screener tab.

Two data paths:

1. **Broad** (default): pulls Nasdaq's public earnings calendar API
   day-by-day across the requested window, filters to symbols in the
   configured screener universe (S&P 500 snapshot), and decorates each
   row with the latest quote from the broad movers cache. This covers
   ~500 names without per-ticker yfinance fan-out.

2. **Curated fallback**: when the Nasdaq path fails (network, rate
   limit, schema drift), we walk the small curated TICKERS list with
   per-ticker ``yfinance.Ticker.calendar`` lookups so the card never
   goes empty.

Results are cached in-process for 30 minutes so the three screener
cards on the same refresh cycle share one fetch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional
import json
import time
import urllib.error
import urllib.request

from .constants import SCREENER_EARNINGS_WINDOW_DAYS, TICKERS
from .movers import _broad_universe_rows
from .report import overview_rows, reset_overview_rows_cache
from .universe import universe_by_symbol


_CACHE_TTL_SECONDS = 30 * 60
_cache: dict[str, object] = {"ts": 0.0, "rows": [], "source": ""}


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


# ---------------------------------------------------------------------------
# Broad path: Nasdaq earnings calendar
# ---------------------------------------------------------------------------

_NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={iso}"
_NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _fetch_nasdaq_day(iso_date: str, timeout: float = 6.0) -> list[dict]:
    """Return raw Nasdaq rows for a single date or [] on failure."""
    url = _NASDAQ_URL.format(iso=iso_date)
    req = urllib.request.Request(url, headers=_NASDAQ_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return []
    except Exception:
        return []
    rows = (((payload or {}).get("data") or {}).get("rows")) or []
    return rows if isinstance(rows, list) else []


def _broad_earnings(window_days: int) -> list[EarningsRow]:
    """Walk the next ``window_days`` and pull Nasdaq earnings for each."""
    universe = universe_by_symbol()
    if not universe:
        return []

    quote_index = {q.symbol: q for q in _broad_universe_rows()}
    overview_index: dict[str, object] = {}
    try:
        overview_index = {r.symbol: r for r in overview_rows("daily")}
    except Exception:
        overview_index = {}

    today = date.today()
    out: list[EarningsRow] = []
    seen: set[str] = set()
    for offset in range(0, window_days + 1):
        d = today + timedelta(days=offset)
        iso = d.isoformat()
        rows = _fetch_nasdaq_day(iso)
        if not rows:
            continue
        for row in rows:
            sym = (row.get("symbol") or "").strip().upper()
            if not sym or sym in seen:
                continue
            meta = universe.get(sym)
            if meta is None:
                continue  # filter to configured universe
            seen.add(sym)
            quote = quote_index.get(sym)
            ov = overview_index.get(sym)
            out.append(
                EarningsRow(
                    symbol=sym,
                    name=meta["name"],
                    sector=meta["sector"],
                    earnings_date=iso,
                    days_until=offset,
                    last=quote.last if quote else (ov.last if ov else None),
                    change_pct=(
                        quote.change_pct if quote
                        else (ov.change_pct if ov else None)
                    ),
                    verdict=ov.verdict if ov else None,
                )
            )
    out.sort(key=lambda r: (r.earnings_date, r.symbol))
    return out


# ---------------------------------------------------------------------------
# Curated fallback: per-ticker yfinance calendar over the small TICKERS list
# ---------------------------------------------------------------------------


def _coerce_date(value: object) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
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
    try:
        tk = yf.Ticker(symbol)
    except Exception:
        return None

    candidates: list[date] = []
    cal = getattr(tk, "calendar", None)
    if cal is not None:
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
                row = cal.loc["Earnings Date"]  # type: ignore[index]
                values = row.values if hasattr(row, "values") else [row]
                for item in values:
                    d = _coerce_date(item)
                    if d:
                        candidates.append(d)
            except Exception:
                pass

    if not candidates:
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


def _curated_earnings(window_days: int) -> list[EarningsRow]:
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return []

    overview_by_symbol = {}
    try:
        overview_by_symbol = {r.symbol: r for r in overview_rows("daily")}
    except Exception:
        overview_by_symbol = {}

    today = date.today()
    rows: list[EarningsRow] = []
    for meta in TICKERS:
        symbol = meta["symbol"]
        if symbol == "VIX":
            continue
        yf_sym = meta.get("yfinance_symbol") or symbol
        d = _next_earnings_date(yf, yf_sym)
        if d is None:
            continue
        days_until = (d - today).days
        if days_until < 0 or days_until > window_days:
            continue
        ov = overview_by_symbol.get(symbol)
        rows.append(
            EarningsRow(
                symbol=symbol,
                name=meta["name"],
                sector=meta["sector"],
                earnings_date=d.isoformat(),
                days_until=days_until,
                last=ov.last if ov else None,
                change_pct=ov.change_pct if ov else None,
                verdict=ov.verdict if ov else None,
            )
        )
    rows.sort(key=lambda r: (r.earnings_date, r.symbol))
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def upcoming_earnings(
    window_days: int = SCREENER_EARNINGS_WINDOW_DAYS,
) -> list[EarningsRow]:
    """Return upcoming earnings within ``window_days`` for the screener.

    Tries the broad Nasdaq path first; falls back to the curated yfinance
    walk if the Nasdaq path returns nothing. Cached in-process for 30
    minutes regardless of which path produced the data.
    """
    rows, _src = upcoming_earnings_with_source(window_days)
    return rows


def upcoming_earnings_with_source(
    window_days: int = SCREENER_EARNINGS_WINDOW_DAYS,
) -> tuple[list[EarningsRow], str]:
    """Same as :func:`upcoming_earnings` but also returns the source label."""
    now = time.time()
    cached_rows = _cache.get("rows") or []
    cached_src = _cache.get("source") or ""
    if cached_rows and now - float(_cache.get("ts") or 0) < _CACHE_TTL_SECONDS:
        return _filter_window(list(cached_rows), window_days), str(cached_src)  # type: ignore[arg-type]

    rows = _broad_earnings(window_days)
    source = "sp500"
    if not rows:
        rows = _curated_earnings(window_days)
        source = "curated" if rows else "unavailable"

    _cache["rows"] = rows
    _cache["ts"] = now
    _cache["source"] = source
    return _filter_window(rows, window_days), source


def _filter_window(rows: list[EarningsRow], window_days: int) -> list[EarningsRow]:
    return [r for r in rows if 0 <= r.days_until <= window_days]


def reset_cache() -> None:
    """Test helper — flush the in-process cache."""
    _cache["rows"] = []
    _cache["ts"] = 0.0
    _cache["source"] = ""
    reset_overview_rows_cache()


def earnings_within_window_days(
    symbol: str,
    meta: dict,
    *,
    window_days: int,
) -> tuple[str, int] | None:
    """Next earnings `(iso_date, days_until)` via yfinance if within ``window_days``.

    Aligns with the screener "upcoming earnings" window — used for Analyst tab UI.
    """
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return None
    yf_sym = (meta.get("yfinance_symbol") or symbol).upper()
    d = _next_earnings_date(yf, yf_sym)
    if d is None:
        return None
    today = date.today()
    days_until = (d - today).days
    if days_until < 0 or days_until > window_days:
        return None
    return (d.isoformat(), days_until)
