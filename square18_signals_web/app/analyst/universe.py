"""Broad market universe loader for the Stock Screener.

The Screener uses the S&P 500. Constituents are **refreshed on a
schedule** from a public dataset (CSV on GitHub) with a bundled
``data/sp500.json`` as offline / cold-start fallback. When a refresh
fails but an earlier in-process fetch succeeded, the last good list
is served as **stale**; the next network attempt is scheduled for at
most one hour later (or the normal TTL, whichever is shorter) so we
do not spam the host on every request.

Environment
-----------
``SQUARE18_SP500_REFRESH_HOURS`` (default ``24``)
    How long a successful **remote** fetch stays valid before the next
    attempt. ``0`` means re-fetch on every ``sp500_universe()`` call
    (handy in tests; avoid in production).

``SQUARE18_SP500_CSV_URL`` (optional)
    Override the default constituents CSV URL.

Public surface
--------------
``sp500_universe()``
    List of ``{symbol, name, sector}`` dicts for the S&P 500.
``universe_by_symbol()``
    ``{symbol: meta}`` index for the same data.
``universe_source()``
    ``"remote"``, ``"bundle"``, or ``"stale"`` (serving last good remote
    after a failed refresh).
``reset_cache()``
    Test helper: clear caches; the next read re-resolves the universe.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_BUNDLED = _HERE / "data" / "sp500.json"
_DEFAULT_CSV = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
    "main/data/constituents.csv"
)

_HOURS = float(os.environ.get("SQUARE18_SP500_REFRESH_HOURS", "24"))
# 0 = always attempt network when resolver runs (e.g. tests)
_TTL_SECONDS = 0.0 if _HOURS <= 0 else _HOURS * 3600.0
_CSV_URL = (os.environ.get("SQUARE18_SP500_CSV_URL") or "").strip() or _DEFAULT_CSV
_STALE_RETRY_SECONDS = min(_TTL_SECONDS if _TTL_SECONDS > 0 else 3600.0, 3600.0)

_REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; SiannaFinancials/1.0; +https://github.com/adrinz/SiannaFinancials)",
    "Accept": "text/csv, text/plain, */*",
}

_state: dict[str, object] = {
    "rows": [],
    "valid_until": 0.0,
    "source": "bundle",
    "last_remote_rows": None,
}


def _normalize_rows(raw: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in raw:
        symbol = (row.get("symbol") or "").strip().upper()
        name = (row.get("name") or "").strip()
        sector = (row.get("sector") or "").strip() or "—"
        if symbol and name:
            out.append({"symbol": symbol, "name": name, "sector": sector})
    return sorted(out, key=lambda r: r["symbol"])


def _load_bundled() -> list[dict]:
    try:
        with _BUNDLED.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return _normalize_rows(
        {
            "symbol": (r or {}).get("symbol"),
            "name": (r or {}).get("name"),
            "sector": (r or {}).get("sector"),
        }
        for r in data
    )


def _parse_constituents_csv(text: str) -> list[dict] | None:
    f = io.StringIO(text)
    try:
        reader = csv.DictReader(f)
    except Exception:
        return None
    rows: list[dict] = []
    for r in reader:
        if not r:
            continue
        sym = (r.get("Symbol") or r.get("symbol") or "").strip()
        name = (r.get("Security") or r.get("name") or "").strip()
        sector = (r.get("GICS Sector") or r.get("Sector") or r.get("sector") or "").strip()
        if sym and name:
            rows.append({"symbol": sym, "name": name, "sector": sector or "—"})
    if len(rows) < 400:
        return None
    return _normalize_rows(rows)


def _fetch_remote_csv() -> list[dict] | None:
    req = urllib.request.Request(_CSV_URL, headers=_REQ_HEADERS, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    except Exception:
        return None
    return _parse_constituents_csv(text)


def sp500_universe() -> list[dict]:
    """Return the S&P 500 list, refreshing from the network when due."""
    now = time.time()
    if _state["rows"] and now < float(_state.get("valid_until") or 0):
        return list(_state["rows"])  # type: ignore[arg-type]

    remote = _fetch_remote_csv()
    if remote:
        _state["rows"] = remote
        _state["source"] = "remote"
        _state["last_remote_rows"] = remote
        _state["valid_until"] = now + _TTL_SECONDS
        return list(remote)

    last = _state.get("last_remote_rows")
    if last:
        _state["rows"] = last
        _state["source"] = "stale"
        # Retry sooner than a full day when the feed is down.
        _state["valid_until"] = now + _STALE_RETRY_SECONDS
        return list(last)  # type: ignore[arg-type]

    bundled = _load_bundled()
    _state["rows"] = bundled
    _state["source"] = "bundle"
    _state["valid_until"] = now + _TTL_SECONDS
    return list(bundled)


def universe_by_symbol() -> dict[str, dict]:
    return {row["symbol"]: row for row in sp500_universe()}


def universe_source() -> str:
    if not _state.get("rows"):
        sp500_universe()
    return str(_state.get("source") or "bundle")


def reset_cache() -> None:
    _state["rows"] = []
    _state["valid_until"] = 0.0
    _state["source"] = "bundle"
    _state["last_remote_rows"] = None
