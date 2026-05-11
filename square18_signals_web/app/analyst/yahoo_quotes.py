"""Best-effort Yahoo Finance quotes to align options tickets with broker UIs.

Uses the same free yfinance path as the rest of the app. When Yahoo is
unreachable or a strike/expiry is not listed, callers fall back to bar
close + model pricing.

* Underlying: last / regular market price (or last daily close) with a
  short in-process cache to avoid fan-out during screener builds.
* Options: (bid+ask)/2 for the chain row nearest the requested strike on
  the listed expiry **nearest** the ticket expiry (Yahoo’s chain dates,
  not our internal roll-to-Friday logic, may differ by a day or two).
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Optional

from .constants import TICKER_MAP

_YF_TIMEOUT = 12.0
_SPOT_CACHE: dict[str, tuple[float, float]] = {}
_OPT_CACHE: dict[str, tuple[float, float]] = {}
_SI_CACHE: dict[str, tuple[float, float]] = {}   # short interest % of float
# Tunable when not using bypass — lower defaults so Cost/contract is less stale.
_SPOT_TTL_SEC = float(os.environ.get("SQUARE18_SPOT_QUOTE_TTL_SEC", "30"))
_OPT_TTL_SEC = float(os.environ.get("SQUARE18_OPTIONS_QUOTE_TTL_SEC", "30"))
_SI_TTL_SEC = 3600.0  # short interest refreshes slowly (FINRA data is biweekly)


def _mapped_symbol(sym: str) -> str:
    m = TICKER_MAP.get(sym.upper(), {})
    return m.get("yfinance_symbol", sym)


def _run_yf(fn, timeout: float = _YF_TIMEOUT) -> Any:
    with ThreadPoolExecutor(max_workers=1) as ex:
        try:
            return ex.submit(fn).result(timeout=timeout)
        except Exception:
            return None


def yf_short_interest_pct(sym: str) -> Optional[float]:
    """Return short interest as a fraction of float (0.20 = 20% of float shorted).

    Sourced from Yahoo Finance `.info` via yfinance. Data is biweekly (FINRA)
    so results are cached for 1 hour. Returns ``None`` when unavailable.
    """
    u = sym.upper()
    now = time.time()
    hit = _SI_CACHE.get(u)
    if hit and (now - hit[0]) < _SI_TTL_SEC:
        return hit[1]

    def _pull() -> Optional[float]:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            return None
        try:
            info = yf.Ticker(_mapped_symbol(u)).info
            v = info.get("shortPercentOfFloat") or info.get("shortRatio")
            if v is not None:
                # shortPercentOfFloat is 0–1 fraction; shortRatio is days-to-cover
                # Only use shortPercentOfFloat here
                spof = info.get("shortPercentOfFloat")
                return float(spof) if spof is not None else None
        except Exception:
            pass
        return None

    v = _run_yf(_pull, timeout=6.0)
    if v is not None and 0 < v <= 1.0:
        _SI_CACHE[u] = (now, v)
        return v
    return None


def yf_last_price(sym: str, *, bypass_cache: bool = False) -> Optional[float]:
    """Latest tradable last from Yahoo (fast_info / history), or None.

    Set ``bypass_cache=True`` to force a fresh pull (used for Analyst trade
    tickets so spot aligns with live option mids).
    """
    u = sym.upper()
    now = time.time()
    if not bypass_cache:
        t_old = _SPOT_CACHE.get(u)
        if t_old and now - t_old[0] < _SPOT_TTL_SEC:
            return t_old[1]
    yfs = _mapped_symbol(u)

    def _pull() -> Optional[float]:
        import yfinance as yf  # type: ignore

        t = yf.Ticker(yfs)
        try:
            fi = t.fast_info
            for k in ("last_price", "lastPrice", "regularMarketPrice", "previous_close"):
                try:
                    v = fi[k]  # type: ignore[index]
                except (KeyError, TypeError, Exception):
                    v = None
                if v is not None and float(v) > 0:
                    return float(v)
        except Exception:
            pass
        try:
            h = t.history(period="5d", interval="1d", auto_adjust=True)
            if h is not None and not h.empty and "Close" in h.columns:
                return float(h["Close"].iloc[-1])
        except Exception:
            pass
        return None

    v = _run_yf(_pull)
    if v is not None and v > 0:
        _SPOT_CACHE[u] = (now, v)
    return v


def yf_option_mid_per_share(
    sym: str,
    strike: float,
    is_call: bool,
    expiry: date,
    *,
    bypass_cache: bool = False,
) -> Optional[float]:
    """(bid+ask)/2 or last for the option row nearest *strike*; or None.

    *expiry* is the ticket’s target date; the **nearest** listed chain
    expiry to that date is used (same weeklies/dailies as Robinhood’s list).

    Yahoo’s free chain is typically **delayed** (~15 min). ``bypass_cache``
    skips only **our** in-process cache so each Analyst report recomputes
    from the latest yfinance snapshot for that HTTP request — not the same as
    a paid real-time NBBO tape.
    """
    u = sym.upper()
    yfs = _mapped_symbol(u)
    cache_key = f"{u}|{round(float(strike), 4):.4f}|{int(is_call)}|{expiry.isoformat()}"
    now = time.time()
    if not bypass_cache:
        t_old = _OPT_CACHE.get(cache_key)
        if t_old and now - t_old[0] < _OPT_TTL_SEC:
            return t_old[1]

    def _pull() -> Optional[float]:
        import yfinance as yf  # type: ignore

        t = yf.Ticker(yfs)
        opts: tuple = tuple(getattr(t, "options", None) or ())
        if not opts:
            return None
        best: Optional[str] = None
        best_d = 10_000
        for s in opts:
            try:
                d_ex = datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
            except Exception:
                continue
            diff = abs((d_ex - expiry).days)
            if diff < best_d:
                best_d = diff
                best = str(s)[:10]
        if not best:
            return None
        if best_d > 40:
            return None
        ch = t.option_chain(best)
        chain = ch.calls if is_call else ch.puts
        if chain is None or chain.empty:
            return None
        strikes = [float(x) for x in chain["strike"].tolist()]
        if not strikes:
            return None
        idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - float(strike)))
        row = chain.iloc[idx]
        bid = float(row.get("bid", 0) or 0)
        ask = float(row.get("ask", 0) or 0)
        lastp = float(row.get("lastPrice", 0) or 0)
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        if lastp > 0:
            return lastp
        if ask > 0:
            return ask
        if bid > 0:
            return bid
        return None

    v = _run_yf(_pull)
    if v is not None and v > 0:
        _OPT_CACHE[cache_key] = (now, v)
    return v
