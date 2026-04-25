"""Market-wide movers helper for the Stock Screener.

Fetches the latest two daily closes for the configured screener
universe (S&P 500 by default) in a single batched ``yfinance.download``
call, computes today's % change, and returns sorted top gainers /
losers. Results are cached in-process for 10 minutes so the three
screener cards on the same refresh cycle don't trigger a fresh
fan-out.

When the broad fetch fails (network/yfinance offline), callers should
fall back to the curated overview pipeline. ``movers_with_fallback``
encapsulates that decision in one place.

Public surface
--------------
``broad_movers(side, limit)``
    Best-effort broad-universe movers; returns [] on failure.
``movers_with_fallback(side, limit)``
    Broad + curated fallback; always returns up to ``limit`` rows when
    any data path is healthy.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional
import threading
import time

from .report import overview_rows
from .universe import sp500_universe, universe_by_symbol


_CACHE_TTL_SECONDS = 10 * 60
_cache: dict[str, object] = {"ts": 0.0, "rows": []}
# Jumps and dips are requested in parallel; without this, two cold calls
# each run a full ~500-ticker yfinance download roughly doubling wait time.
_broad_fetch_lock = threading.Lock()


@dataclass
class MoverItem:
    symbol: str
    name: str
    sector: str
    last: float
    change_pct: float
    verdict: Optional[str] = None
    composite_score: Optional[float] = None
    rsi: Optional[float] = None


def _enrich_with_curated_signals(rows: list[MoverItem]) -> list[MoverItem]:
    """Decorate broad-universe rows with verdicts from the curated pipeline.

    The deterministic analyst pipeline only runs on the curated TICKERS
    list (it's expensive). For symbols that overlap both lists we pull
    the verdict / composite_score / RSI through; for the rest those
    fields stay ``None``.
    """
    try:
        ov = {r.symbol: r for r in overview_rows("daily")}
    except Exception:
        ov = {}
    for r in rows:
        v = ov.get(r.symbol)
        if v is not None:
            r.verdict = v.verdict
            r.composite_score = v.composite_score
            r.rsi = v.rsi
    return rows


def _fetch_universe_quotes() -> list[MoverItem]:
    """Single batched yfinance download for the whole universe.

    Returns one ``MoverItem`` per symbol that produced two valid daily
    closes. Empty list on any failure.
    """
    universe = sp500_universe()
    if not universe:
        return []

    symbols = [meta["symbol"] for meta in universe]
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return []

    try:
        # group_by="ticker" gives a top-level column per symbol so we
        # can iterate the universe without rebuilding the frame shape.
        df = yf.download(
            tickers=" ".join(symbols),
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception:
        return []

    if df is None or getattr(df, "empty", True):
        return []

    by_symbol = universe_by_symbol()
    rows: list[MoverItem] = []
    for sym in symbols:
        meta = by_symbol.get(sym)
        if not meta:
            continue
        try:
            sub = df[sym] if sym in df.columns.get_level_values(0) else None  # type: ignore[union-attr]
        except Exception:
            sub = None
        if sub is None or getattr(sub, "empty", True):
            continue
        try:
            closes = sub["Close"].dropna()
        except Exception:
            continue
        if len(closes) < 2:
            continue
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2])
        if prev == 0 or last != last or prev != prev:  # NaN guard
            continue
        change_pct = round(((last - prev) / prev) * 100.0, 2)
        rows.append(
            MoverItem(
                symbol=sym,
                name=meta["name"],
                sector=meta["sector"],
                last=round(last, 2),
                change_pct=change_pct,
            )
        )
    return rows


def _broad_universe_rows() -> list[MoverItem]:
    """Cache wrapper around the batched yfinance download."""
    now = time.time()
    cached = _cache.get("rows") or []
    if cached and now - float(_cache.get("ts") or 0) < _CACHE_TTL_SECONDS:
        return list(cached)  # type: ignore[arg-type]
    with _broad_fetch_lock:
        now = time.time()
        cached2 = _cache.get("rows") or []
        if cached2 and now - float(_cache.get("ts") or 0) < _CACHE_TTL_SECONDS:
            return list(cached2)  # type: ignore[arg-type]
        rows = _fetch_universe_quotes()
        if rows:
            _cache["rows"] = rows
            _cache["ts"] = now
        return rows


def _split_broad_to_jumps_dips(
    all_rows: list[MoverItem], limit: int
) -> tuple[list[MoverItem], list[MoverItem]]:
    """One sorted/enriched top-N list per side from a single broad download."""
    if not all_rows:
        return [], []
    jumps = [r for r in all_rows if r.change_pct > 0]
    jumps.sort(key=lambda r: r.change_pct, reverse=True)
    dips = [r for r in all_rows if r.change_pct < 0]
    dips.sort(key=lambda r: r.change_pct)
    return (
        _enrich_with_curated_signals(jumps[:limit]),
        _enrich_with_curated_signals(dips[:limit]),
    )


def broad_movers(side: Literal["jumps", "dips"], limit: int = 10) -> list[MoverItem]:
    """Top broad-universe movers; returns [] when the broad fetch fails."""
    j, d = _split_broad_to_jumps_dips(_broad_universe_rows(), limit)
    return j if side == "jumps" else d


def _curated_movers(side: Literal["jumps", "dips"], limit: int) -> list[MoverItem]:
    """Curated 19-ticker fallback using the existing analyst pipeline."""
    try:
        rows = overview_rows("daily")
    except Exception:
        return []
    items = [
        MoverItem(
            symbol=r.symbol,
            name=r.name,
            sector=r.sector,
            last=r.last,
            change_pct=r.change_pct,
            verdict=r.verdict,
            composite_score=r.composite_score,
            rsi=r.rsi,
        )
        for r in rows
    ]
    if side == "jumps":
        items = [m for m in items if m.change_pct > 0]
        items.sort(key=lambda m: m.change_pct, reverse=True)
    else:
        items = [m for m in items if m.change_pct < 0]
        items.sort(key=lambda m: m.change_pct)
    return items[:limit]


def movers_with_fallback(
    side: Literal["jumps", "dips"], limit: int = 10
) -> tuple[list[MoverItem], str]:
    """Return movers plus the source label (``"sp500"`` / ``"curated"``)."""
    rows = broad_movers(side, limit)
    if rows:
        return rows, "sp500"
    return _curated_movers(side, limit), "curated"


def movers_pair_curated_only(
    limit: int = 10,
) -> tuple[list[MoverItem], list[MoverItem], str, str]:
    """Curated lists only (fast) — for instant first paint / ``quick=1`` API."""
    j = _curated_movers("jumps", limit)
    d = _curated_movers("dips", limit)
    return j, d, "curated", "curated"


def movers_pair_with_fallback(
    limit: int = 10,
) -> tuple[list[MoverItem], list[MoverItem], str, str]:
    """Jumps and dips from **one** broad download when possible, else curated."""
    all_rows = _broad_universe_rows()
    if all_rows:
        j, d = _split_broad_to_jumps_dips(all_rows, limit)
        return j, d, "sp500", "sp500"
    j2 = _curated_movers("jumps", limit)
    d2 = _curated_movers("dips", limit)
    return j2, d2, "curated", "curated"


def reset_cache() -> None:
    """Test helper — flush the in-process broad-universe cache."""
    with _broad_fetch_lock:
        _cache["rows"] = []
        _cache["ts"] = 0.0
