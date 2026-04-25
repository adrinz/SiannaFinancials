"""Broad market universe loader for the Stock Screener.

The Screener tab scans the S&P 500 instead of the curated TICKERS list
used elsewhere in the app. The constituents are shipped as a static
JSON snapshot under ``data/sp500.json`` so the screener works offline
and stays deterministic in tests. A periodic refresh script can update
the snapshot over time.

Public surface
--------------
``sp500_universe()``
    Return the list of {symbol, name, sector} dicts for the S&P 500.
    Returns an empty list if the snapshot file is missing or unreadable.

``UNIVERSE_BY_SYMBOL``
    Lazily-built ``{symbol: meta}`` index for fast lookups when joining
    earnings calendar entries against the universe.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SNAPSHOT = _HERE / "data" / "sp500.json"


@lru_cache(maxsize=1)
def sp500_universe() -> list[dict]:
    """Return the S&P 500 constituent list from the bundled snapshot."""
    try:
        with _SNAPSHOT.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    for row in data:
        symbol = (row.get("symbol") or "").strip().upper()
        name = (row.get("name") or "").strip()
        sector = (row.get("sector") or "").strip() or "—"
        if symbol and name:
            out.append({"symbol": symbol, "name": name, "sector": sector})
    return out


@lru_cache(maxsize=1)
def universe_by_symbol() -> dict[str, dict]:
    """``{symbol: meta}`` index for the configured screener universe."""
    return {row["symbol"]: row for row in sp500_universe()}


def reset_cache() -> None:
    """Test helper — clear the lru caches so reloads pick up new data."""
    sp500_universe.cache_clear()
    universe_by_symbol.cache_clear()
