"""Tier-2 options intelligence: UOA (#8), term-structure slope (#9), put-call skew (#10).

Data is sourced from Tradier options chains when configured, with Yahoo
as a best-effort fallback.

What each signal does
---------------------
#8 UOA-lite (volume/OI ratio)
    For each OTM strike, volume / max(1, open_interest) > UOA_RATIO_THRESHOLD
    flags "unusual" activity.  Net call UOA vs net put UOA contributes a
    directional kicker (±MAX_UOA_ADJ) to the composite score.

#9 Term-structure slope
    front-month ATM IV / back-month (~90d) ATM IV.
    Ratio > BACKWARDATION_THRESHOLD → market is stressed / event-driven.
    Dampens a bull signal when we're in backwardation; adds a small bear kicker.

#10 Put-call skew
    Average OTM put IV / average OTM call IV in a ±SKEW_WINDOW_PCT band.
    Elevated put skew (>SKEW_ELEVATED) dampens bull calls, reinforces bears.
    Elevated call skew (<SKEW_CALL) confirms bulls, dampens bears.

Score-adjustment cap
    ``flow_score_adj`` is capped at ±MAX_FLOW_ADJ so options flow never
    overrides a strong technical picture — it only tilts a borderline call.
"""
from __future__ import annotations

import os
import threading
import time
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .constants import yfinance_disabled

# ── Cache ────────────────────────────────────────────────────────────────────
_CHAIN_LOCK = threading.Lock()
_CHAIN_CACHE: dict[str, tuple[float, object]] = {}  # sym -> (ts, ChainSnapshot)
_CHAIN_TTL = int(os.environ.get("SQUARE18_CHAIN_TTL_SEC", "300"))  # 5 min

# ── Tuning constants ─────────────────────────────────────────────────────────
UOA_RATIO_THRESHOLD = 3.0       # V/OI floor to classify a strike as "unusual"
UOA_MIN_VOLUME = 50             # ignore trivially thin strikes
UOA_OTM_BAND = 0.20             # only look at strikes within ±20% of spot for UOA
MAX_UOA_ADJ = 0.08              # max score adjustment from UOA alone

SKEW_WINDOW_PCT = 0.15          # OTM band for skew calc (±15% of spot)
SKEW_ELEVATED = 1.20            # put/call IV ratio: above → elevated put skew
SKEW_CALL = 0.85                # below → call skew (unusual, confirms bulls)
MAX_SKEW_ADJ = 0.05             # max score adjustment from skew

BACKWARDATION_THRESHOLD = 1.05  # front/back IV slope above → backwardation
MAX_TERM_ADJ = 0.05             # max score adjustment from term structure

MAX_FLOW_ADJ = 0.12             # hard cap on total flow_score_adj


# ── Data shapes ──────────────────────────────────────────────────────────────

@dataclass
class ChainSnapshot:
    """Minimal options chain extract for one symbol (≤3 nearest expiries)."""
    sym: str
    spot: float
    expiries: list[str]          # ISO date strings, sorted ascending
    calls: list[dict]            # rows: strike, volume, openInterest, impliedVolatility
    puts: list[dict]
    source: str                  # "tradier" | "yfinance" | "unavailable"


@dataclass
class OptionsFlowBlock:
    """Options intelligence output — attached to ReportOut."""

    # UOA
    uoa_bull: float = 0.0        # 0..1 net unusual call intensity
    uoa_bear: float = 0.0        # 0..1 net unusual put intensity
    uoa_note: str = ""

    # Term structure
    term_slope: Optional[float] = None   # front/back ATM IV ratio
    front_iv: Optional[float] = None
    back_iv: Optional[float] = None
    term_note: str = ""

    # Skew
    skew: Optional[float] = None         # OTM put IV / OTM call IV
    skew_note: str = ""

    # Extra IV context
    atm_iv: Optional[float] = None       # nearest-expiry ATM IV
    iv_baseline_ratio: Optional[float] = None  # ATM IV / DEFAULT_IV baseline
    implied_move_30d_pct: Optional[float] = None

    # Net adjustment applied to composite score (±MAX_FLOW_ADJ)
    flow_score_adj: float = 0.0

    source: str = "unavailable"


# ── Chain fetcher ─────────────────────────────────────────────────────────────

def _fetch_chain(sym: str, spot: float, bypass_cache: bool = False) -> ChainSnapshot:
    """Pull the nearest ≤3 expiries from Tradier (primary) or Yahoo (fallback)."""
    u = sym.upper()
    now = time.time()
    if not bypass_cache:
        with _CHAIN_LOCK:
            hit = _CHAIN_CACHE.get(u)
        if hit and (now - hit[0]) < _CHAIN_TTL:
            return hit[1]  # type: ignore[return-value]

    from .tradier_client import get_option_expirations, get_option_chain, is_configured as is_tradier_configured
    
    # Try Tradier first
    if is_tradier_configured():
        try:
            exps = get_option_expirations(u)
            if exps:
                selected = exps[:3]  # nearest 3 expiries
                all_calls: list[dict] = []
                all_puts: list[dict] = []
                for exp in selected:
                    chain = get_option_chain(u, exp)
                    if not chain:
                        continue
                    for opt in chain:
                        # Map Tradier keys to expected Yahoo-style keys for downstream compatibility
                        greeks = opt.get("greeks") or {}
                        row = {
                            "strike": float(opt.get("strike") or 0),
                            "volume": float(opt.get("volume") or 0),
                            "openInterest": float(opt.get("open_interest") or 0),
                            "impliedVolatility": float(greeks.get("mid_iv") or 0),
                            "bid": float(opt.get("bid") or 0),
                            "ask": float(opt.get("ask") or 0),
                            "lastPrice": float(opt.get("last") or 0),
                            "delta": float(greeks.get("delta") or 0),
                            "gamma": float(greeks.get("gamma") or 0),
                            "theta": float(greeks.get("theta") or 0),
                            "vega": float(greeks.get("vega") or 0),
                            "_expiry": exp
                        }
                        if opt.get("option_type") == "call":
                            all_calls.append(row)
                        elif opt.get("option_type") == "put":
                            all_puts.append(row)
                
                if all_calls or all_puts:
                    snap = ChainSnapshot(
                        sym=u, spot=spot, expiries=selected,
                        calls=all_calls, puts=all_puts, source="tradier"
                    )
                    with _CHAIN_LOCK:
                        _CHAIN_CACHE[u] = (time.time(), snap)
                    return snap
        except Exception as e:
            print(f"Tradier options flow fetch failed: {e}")

    # Fallback to Yahoo (unless strict mode disables all Yahoo paths)
    if yfinance_disabled():
        snap = ChainSnapshot(sym=u, spot=spot, expiries=[], calls=[], puts=[], source="unavailable")
        with _CHAIN_LOCK:
            _CHAIN_CACHE[u] = (time.time(), snap)
        return snap

    def _pull() -> ChainSnapshot:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            return ChainSnapshot(sym=u, spot=spot, expiries=[], calls=[], puts=[], source="unavailable")

        from .constants import TICKER_MAP
        yfs = TICKER_MAP.get(u, {}).get("yfinance_symbol", u)
        try:
            tk = yf.Ticker(yfs)
            exps = list(getattr(tk, "options", None) or [])
        except Exception:
            return ChainSnapshot(sym=u, spot=spot, expiries=[], calls=[], puts=[], source="unavailable")

        if not exps:
            return ChainSnapshot(sym=u, spot=spot, expiries=[], calls=[], puts=[], source="unavailable")

        selected = exps[:3]  # nearest 3 expiries
        all_calls: list[dict] = []
        all_puts: list[dict] = []
        for exp in selected:
            try:
                ch = tk.option_chain(exp)
                for row in (ch.calls.to_dict("records") if (ch.calls is not None and not ch.calls.empty) else []):
                    all_calls.append({**row, "_expiry": exp})
                for row in (ch.puts.to_dict("records") if (ch.puts is not None and not ch.puts.empty) else []):
                    all_puts.append({**row, "_expiry": exp})
            except Exception:
                continue

        return ChainSnapshot(
            sym=u, spot=spot, expiries=selected,
            calls=all_calls, puts=all_puts, source="yfinance",
        )

    from .yahoo_quotes import _run_yf
    snap = _run_yf(_pull, timeout=15.0) or ChainSnapshot(
        sym=u, spot=spot, expiries=[], calls=[], puts=[], source="unavailable"
    )
    with _CHAIN_LOCK:
        _CHAIN_CACHE[u] = (time.time(), snap)
    return snap


# ── Signal calculators ────────────────────────────────────────────────────────

def _uoa(snap: ChainSnapshot) -> tuple[float, float, str]:
    """#8 — Volume/OI ratio analysis.  Returns (bull_score 0..1, bear_score 0..1, note)."""
    spot = snap.spot
    if not snap.calls and not snap.puts:
        return 0.0, 0.0, ""

    lo, hi = spot * (1 - UOA_OTM_BAND), spot * (1 + UOA_OTM_BAND)

    def _score_legs(rows: list[dict], is_call: bool) -> float:
        total_uoa = 0.0
        n_unusual = 0
        for r in rows:
            k = float(r.get("strike") or 0)
            vol = float(r.get("volume") or 0)
            oi = float(r.get("openInterest") or 0)
            if vol < UOA_MIN_VOLUME:
                continue
            # OTM only
            if is_call and k <= spot:
                continue
            if not is_call and k >= spot:
                continue
            if not (lo <= k <= hi):
                continue
            ratio = vol / max(1.0, oi)
            if ratio >= UOA_RATIO_THRESHOLD:
                total_uoa += min(ratio / 10.0, 1.0)  # normalise
                n_unusual += 1
        return round(min(1.0, total_uoa / max(1, n_unusual)) if n_unusual else 0.0, 3)

    bull = _score_legs(snap.calls, is_call=True)
    bear = _score_legs(snap.puts, is_call=False)

    parts: list[str] = []
    if bull >= 0.3:
        parts.append(f"unusual call OI/vol ({bull:.2f})")
    if bear >= 0.3:
        parts.append(f"unusual put OI/vol ({bear:.2f})")
    note = " · ".join(parts) if parts else "no unusual flow"
    return bull, bear, note


def _term_structure(snap: ChainSnapshot) -> tuple[Optional[float], Optional[float], Optional[float], str]:
    """#9 — Term-structure slope: front ATM IV / back ATM IV.

    Returns (slope, front_iv, back_iv, note).
    """
    exps = snap.expiries
    if len(exps) < 2:
        return None, None, None, ""

    spot = snap.spot

    def _atm_iv(rows: list[dict], expiry: str) -> Optional[float]:
        near = [r for r in rows if r.get("_expiry") == expiry]
        if not near:
            return None
        best: Optional[dict] = None
        best_dist = float("inf")
        for r in near:
            k = float(r.get("strike") or 0)
            iv = float(r.get("impliedVolatility") or 0)
            if iv <= 0:
                continue
            d = abs(k - spot)
            if d < best_dist:
                best_dist = d
                best = r
        if not best:
            return None
        iv = float(best.get("impliedVolatility") or 0)
        return round(iv, 4) if iv > 0 else None

    all_rows = snap.calls + snap.puts
    front_iv = _atm_iv(all_rows, exps[0])
    # Target the expiry closest to ~45-90 days for back IV
    back_exp = exps[-1]
    for exp in exps[1:]:
        try:
            d = (datetime.strptime(exp, "%Y-%m-%d") - datetime.utcnow()).days
            if d >= 30:
                back_exp = exp
                break
        except Exception:
            pass
    back_iv = _atm_iv(all_rows, back_exp)

    if front_iv is None or back_iv is None or back_iv <= 0:
        return None, front_iv, back_iv, ""

    slope = round(front_iv / back_iv, 3)
    if slope > BACKWARDATION_THRESHOLD:
        note = f"backwardation {slope:.2f}× (front IV {front_iv:.0%} > back {back_iv:.0%}) — event/stress"
    elif slope < 0.95:
        note = f"contango {slope:.2f}× (front {front_iv:.0%} < back {back_iv:.0%}) — calm"
    else:
        note = f"flat structure {slope:.2f}× ({front_iv:.0%} / {back_iv:.0%})"
    return slope, front_iv, back_iv, note


def _skew(snap: ChainSnapshot) -> tuple[Optional[float], str]:
    """#10 — OTM put IV / OTM call IV (in ±SKEW_WINDOW_PCT band from spot)."""
    spot = snap.spot
    lo_put = spot * (1 - SKEW_WINDOW_PCT)
    hi_put = spot
    lo_call = spot
    hi_call = spot * (1 + SKEW_WINDOW_PCT)

    def _avg_iv(rows: list[dict], lo: float, hi: float) -> Optional[float]:
        ivs = []
        for r in rows:
            k = float(r.get("strike") or 0)
            if not (lo < k <= hi):
                continue
            iv = float(r.get("impliedVolatility") or 0)
            if iv > 0:
                ivs.append(iv)
        return round(sum(ivs) / len(ivs), 4) if ivs else None

    put_iv = _avg_iv(snap.puts, lo_put, hi_put)
    call_iv = _avg_iv(snap.calls, lo_call, hi_call)

    if put_iv is None or call_iv is None or call_iv <= 0:
        return None, ""

    ratio = round(put_iv / call_iv, 3)
    if ratio >= SKEW_ELEVATED:
        note = f"put skew {ratio:.2f}× (puts {put_iv:.0%} > calls {call_iv:.0%}) — bearish hedging"
    elif ratio <= SKEW_CALL:
        note = f"call skew {ratio:.2f}× (calls {call_iv:.0%} > puts {put_iv:.0%}) — bullish lean"
    else:
        note = f"neutral skew {ratio:.2f}×"
    return ratio, note


# ── Score adjustment composer ─────────────────────────────────────────────────

def _compute_adj(
    verdict: str,
    uoa_bull: float,
    uoa_bear: float,
    term_slope: Optional[float],
    skew_ratio: Optional[float],
) -> float:
    """Combine UOA, term, skew into a single bounded score adjustment."""
    adj = 0.0

    # UOA: strong call flow boosts bull / bear signal; conflict dampens
    net_uoa = uoa_bull - uoa_bear
    if verdict == "BULLISH":
        adj += net_uoa * MAX_UOA_ADJ
    elif verdict == "BEARISH":
        adj -= net_uoa * MAX_UOA_ADJ  # bear wins when puts dominate
    else:
        adj += net_uoa * MAX_UOA_ADJ * 0.4  # gentle nudge for NEUTRAL

    # Term structure: backwardation stresses bull signals
    if term_slope is not None:
        if term_slope >= BACKWARDATION_THRESHOLD:
            if verdict == "BULLISH":
                adj -= MAX_TERM_ADJ * min(1.0, (term_slope - 1.0) * 5)
            elif verdict == "BEARISH":
                adj += MAX_TERM_ADJ * 0.4  # mild confirmation
        elif term_slope < 0.95:
            if verdict == "BULLISH":
                adj += MAX_TERM_ADJ * 0.3  # calm structure is mildly supportive

    # Skew: elevated put skew = market hedging downside
    if skew_ratio is not None:
        if skew_ratio >= SKEW_ELEVATED:
            if verdict == "BULLISH":
                adj -= MAX_SKEW_ADJ * min(1.0, (skew_ratio - 1.0) * 2)
            elif verdict == "BEARISH":
                adj += MAX_SKEW_ADJ * 0.5
        elif skew_ratio <= SKEW_CALL:
            if verdict == "BULLISH":
                adj += MAX_SKEW_ADJ * 0.4
            elif verdict == "BEARISH":
                adj -= MAX_SKEW_ADJ * 0.3

    return round(max(-MAX_FLOW_ADJ, min(MAX_FLOW_ADJ, adj)), 4)


# ── Public API ────────────────────────────────────────────────────────────────

def get_options_flow(
    sym: str,
    spot: float,
    verdict: str,
    *,
    bypass_cache: bool = False,
) -> OptionsFlowBlock:
    """Fetch chain and compute all three options intelligence signals.

    Returns an ``OptionsFlowBlock`` with ``flow_score_adj`` capped at
    ±MAX_FLOW_ADJ.  Always returns a valid object; falls back gracefully
    when market data is unreachable.
    """
    try:
        snap = _fetch_chain(sym, spot, bypass_cache=bypass_cache)
        if snap.source == "unavailable" or (not snap.calls and not snap.puts):
            return OptionsFlowBlock(source="unavailable")

        uoa_bull, uoa_bear, uoa_note = _uoa(snap)
        term_slope, front_iv, back_iv, term_note = _term_structure(snap)
        skew_ratio, skew_note = _skew(snap)
        atm_iv = front_iv if front_iv is not None else back_iv
        iv_baseline_ratio: Optional[float] = None
        implied_move_30d_pct: Optional[float] = None
        if atm_iv is not None and atm_iv > 0:
            from .constants import DEFAULT_IV
            base_iv = float(DEFAULT_IV.get(sym.upper(), 0.35))
            if base_iv > 0:
                iv_baseline_ratio = round(atm_iv / base_iv, 2)
            implied_move_30d_pct = round(atm_iv * math.sqrt(30.0 / 365.0) * 100.0, 2)

        adj = _compute_adj(verdict, uoa_bull, uoa_bear, term_slope, skew_ratio)

        return OptionsFlowBlock(
            uoa_bull=uoa_bull,
            uoa_bear=uoa_bear,
            uoa_note=uoa_note,
            term_slope=term_slope,
            front_iv=front_iv,
            back_iv=back_iv,
            term_note=term_note,
            skew=skew_ratio,
            skew_note=skew_note,
            atm_iv=atm_iv,
            iv_baseline_ratio=iv_baseline_ratio,
            implied_move_30d_pct=implied_move_30d_pct,
            flow_score_adj=adj,
            source=snap.source,
        )
    except Exception:
        return OptionsFlowBlock(source="unavailable")


def option_liquidity_at_strike(
    sym: str,
    strike: float,
    is_call: bool,
) -> dict:
    """Return OI, bid-ask spread, and live Greeks for the nearest chain row to *strike*.

    Uses the in-process chain cache populated by ``get_options_flow``;
    returns an empty dict when no chain data is available.  Callers
    should call ``get_options_flow`` first so the cache is warm.
    """
    u = sym.upper()
    with _CHAIN_LOCK:
        hit = _CHAIN_CACHE.get(u)
    if not hit:
        return {}
    _, snap = hit[0], hit[1]
    rows = snap.calls if is_call else snap.puts
    if not rows:
        return {}
    nearest = min(
        rows,
        key=lambda r: abs(float(r.get("strike") or 0) - float(strike)),
        default=None,
    )
    if nearest is None:
        return {}
    oi = int(nearest.get("openInterest") or 0)
    bid = float(nearest.get("bid") or 0)
    ask = float(nearest.get("ask") or 0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else None
    spread_pct = round((ask - bid) / mid * 100, 1) if mid and mid > 0 else None
    
    return {
        "oi": oi, 
        "bid": bid, 
        "ask": ask, 
        "spread_pct": spread_pct,
        "delta": nearest.get("delta"),
        "gamma": nearest.get("gamma"),
        "theta": nearest.get("theta"),
        "vega": nearest.get("vega")
    }
