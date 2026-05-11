"""Best-effort quotes to align options tickets with broker UIs.

Uses Tradier API if configured, otherwise falls back to Yahoo Finance.
When neither is reachable or a strike/expiry is not listed, callers fall back
to bar close + model pricing.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Optional

from .constants import TICKER_MAP
from .tradier_client import get_quotes, get_option_chain, get_option_expirations, is_configured as is_tradier_configured

_YF_TIMEOUT = 12.0
_SPOT_CACHE: dict[str, tuple[float, float]] = {}
_OPT_CACHE: dict[str, tuple[float, float]] = {}
_SI_CACHE: dict[str, tuple[float, float]] = {}   # short interest % of float

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
    Sourced from Yahoo Finance `.info` via yfinance.
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
    """Latest tradable last from Tradier (primary) or Yahoo (fallback)."""
    u = sym.upper()
    now = time.time()
    if not bypass_cache:
        t_old = _SPOT_CACHE.get(u)
        if t_old and now - t_old[0] < _SPOT_TTL_SEC:
            return t_old[1]
            
    # Try Tradier first
    if is_tradier_configured():
        try:
            quotes = get_quotes([u])
            if quotes and len(quotes) > 0:
                last_price = quotes[0].get("last")
                if last_price and float(last_price) > 0:
                    _SPOT_CACHE[u] = (now, float(last_price))
                    return float(last_price)
        except Exception:
            pass

    # Fallback to Yahoo
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
    Uses Tradier API if available, falls back to Yahoo Finance.
    """
    u = sym.upper()
    cache_key = f"{u}|{round(float(strike), 4):.4f}|{int(is_call)}|{expiry.isoformat()}"
    now = time.time()
    if not bypass_cache:
        t_old = _OPT_CACHE.get(cache_key)
        if t_old and now - t_old[0] < _OPT_TTL_SEC:
            return t_old[1]

    # Try Tradier first
    if is_tradier_configured():
        try:
            # Find nearest expiration
            expirations = get_option_expirations(u)
            if expirations:
                best_exp = None
                best_d = 10_000
                for exp in expirations:
                    d_ex = datetime.strptime(exp, "%Y-%m-%d").date()
                    diff = abs((d_ex - expiry).days)
                    if diff < best_d:
                        best_d = diff
                        best_exp = exp
                
                if best_exp and best_d <= 40:
                    chain = get_option_chain(u, best_exp)
                    if chain:
                        # Filter by call/put
                        opt_type = "call" if is_call else "put"
                        filtered = [opt for opt in chain if opt.get("option_type") == opt_type]
                        if filtered:
                            # Find nearest strike
                            nearest = min(filtered, key=lambda x: abs(float(x.get("strike", 0)) - float(strike)))
                            bid = float(nearest.get("bid", 0) or 0)
                            ask = float(nearest.get("ask", 0) or 0)
                            lastp = float(nearest.get("last", 0) or 0)
                            
                            v = None
                            if bid > 0 and ask > 0:
                                v = (bid + ask) / 2.0
                            elif lastp > 0:
                                v = lastp
                            elif ask > 0:
                                v = ask
                            elif bid > 0:
                                v = bid
                                
                            if v is not None:
                                _OPT_CACHE[cache_key] = (now, v)
                                return v
        except Exception:
            pass

    # Fallback to Yahoo
    yfs = _mapped_symbol(u)
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
