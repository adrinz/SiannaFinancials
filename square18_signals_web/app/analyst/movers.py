"""Market-wide movers helper for the Stock Screener.

Fetches live quotes for the configured screener universe (S&P 500 by
default) from Tradier, computes today's % change from ``prevclose``,
and returns sorted top gainers / losers. Results are cached in-process
for 10 minutes so the three screener cards on the same refresh cycle
don't trigger a fresh fan-out.

When the broad fetch fails (network/provider offline), callers fall
back to the curated overview pipeline. ``movers_with_fallback``
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

from .models import OverviewRow
from .report import overview_rows_fast, reset_overview_rows_cache
from .universe import sp500_universe, universe_by_symbol


_CACHE_TTL_SECONDS = 10 * 60
_TRADIER_QUOTE_BATCH_SIZE = 80
_cache: dict[str, object] = {"ts": 0.0, "rows": []}
# Jumps and dips are requested in parallel; this lock avoids duplicate
# broad-universe quote fan-out on simultaneous cold requests.
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


def _enrich_with_curated_signals(
    rows: list[MoverItem],
    ov: dict[str, OverviewRow] | None = None,
) -> list[MoverItem]:
    """Decorate broad-universe rows with verdicts from the curated pipeline.

    The deterministic analyst pipeline only runs on the curated TICKERS
    list (it's expensive). For symbols that overlap both lists we pull
    the verdict / composite_score / RSI through; for the rest those
    fields stay ``None``.

    Pass a prebuilt ``ov`` from a single :func:`overview_rows` call so
    jumps + dips (and the screener earnings card) do not each fan out
    a full per-ticker ``build_report`` pass.
    """
    if ov is None:
        try:
            ov = {r.symbol: r for r in overview_rows_fast("daily")}
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
    """Fetch broad-universe movers from Tradier quotes.

    Returns one ``MoverItem`` per symbol that has both ``last`` and
    ``prevclose``. Empty list on any failure.
    """
    universe = sp500_universe()
    if not universe:
        return []

    try:
        from .tradier_client import get_quotes, is_configured as is_tradier_configured
    except Exception:
        return []
    if not is_tradier_configured():
        return []

    symbols = [meta["symbol"] for meta in universe]
    by_symbol = universe_by_symbol()
    quote_rows: list[dict] = []
    for i in range(0, len(symbols), _TRADIER_QUOTE_BATCH_SIZE):
        chunk = symbols[i:i + _TRADIER_QUOTE_BATCH_SIZE]
        try:
            got = get_quotes(chunk, include_greeks=False)
        except Exception:
            got = []
        if got:
            quote_rows.extend(got)
    if not quote_rows:
        return []

    rows: list[MoverItem] = []
    for q in quote_rows:
        sym = str(q.get("symbol") or "").upper()
        if not sym:
            continue
        meta = by_symbol.get(sym)
        if not meta:
            continue
        try:
            last = float(q.get("last"))
            prev = float(q.get("prevclose"))
        except Exception:
            continue
        if prev <= 0 or last <= 0:
            continue
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
    """Cache wrapper around broad Tradier quote fetches."""
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
    try:
        ov = {r.symbol: r for r in overview_rows_fast("daily")}
    except Exception:
        ov = {}
    return (
        _enrich_with_curated_signals(jumps[:limit], ov),
        _enrich_with_curated_signals(dips[:limit], ov),
    )


def broad_movers(side: Literal["jumps", "dips"], limit: int = 10) -> list[MoverItem]:
    """Top broad-universe movers; returns [] when the broad fetch fails."""
    j, d = _split_broad_to_jumps_dips(_broad_universe_rows(), limit)
    return j if side == "jumps" else d


def _curated_movers_pair(limit: int) -> tuple[list[MoverItem], list[MoverItem]]:
    """One :func:`overview_rows` call, then split to jumps + dips (sorted)."""
    try:
        rows = overview_rows_fast("daily")
    except Exception:
        return [], []
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
    jumps = [m for m in items if m.change_pct > 0]
    jumps.sort(key=lambda m: m.change_pct, reverse=True)
    dips = [m for m in items if m.change_pct < 0]
    dips.sort(key=lambda m: m.change_pct)
    return jumps[:limit], dips[:limit]


def movers_with_fallback(
    side: Literal["jumps", "dips"], limit: int = 10
) -> tuple[list[MoverItem], str]:
    """Return movers plus the source label (``"sp500"`` / ``"curated"``)."""
    rows = broad_movers(side, limit)
    if rows:
        return rows, "sp500"
    j, d = _curated_movers_pair(limit)
    return (j if side == "jumps" else d), "curated"


def movers_pair_curated_only(
    limit: int = 10,
) -> tuple[list[MoverItem], list[MoverItem], str, str]:
    """Curated lists only (fast) — for instant first paint / ``quick=1`` API."""
    j, d = _curated_movers_pair(limit)
    return j, d, "curated", "curated"


def movers_pair_with_fallback(
    limit: int = 10,
) -> tuple[list[MoverItem], list[MoverItem], str, str]:
    """Jumps and dips from **one** broad download when possible, else curated."""
    all_rows = _broad_universe_rows()
    if all_rows:
        j, d = _split_broad_to_jumps_dips(all_rows, limit)
        return j, d, "sp500", "sp500"
    j2, d2 = _curated_movers_pair(limit)
    return j2, d2, "curated", "curated"


def reset_cache() -> None:
    """Test helper — flush the in-process broad-universe cache."""
    with _broad_fetch_lock:
        _cache["rows"] = []
        _cache["ts"] = 0.0
    reset_overview_rows_cache()
