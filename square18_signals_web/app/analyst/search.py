"""Free-form stock search → Buy / Sell / Hold with entry and exit levels.

Unlike ``build_report`` which is keyed to the curated TICKER_MAP, this
module accepts any ticker symbol (or common company name) and resolves
it via yfinance. The resulting plan is **stock-centric** (entry / target /
stop in dollars) rather than an options ticket, which is the right shape
for a search result card.

Design
------
1. ``resolve_symbol(query)`` — turn "apple", "Apple Inc", or "aapl" into
   a canonical ticker + human name + sector. Uses yfinance.Search when
   available, falls back to direct symbol probing.
2. ``build_stock_plan(report)`` — derive Buy/Sell/Hold + entry/target/
   stop from an already-computed ``ReportOut``. The math is intentionally
   rule-based so the recommendation is reproducible.
3. ``search(query, timeframe)`` — the one-shot public entry point used
   by the /api/search endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .constants import TICKER_MAP, Timeframe
from .data import get_ohlcv
from .models import ReportOut
from .report import build_report


Action = Literal["Buy", "Sell", "Hold"]


@dataclass(frozen=True)
class ResolvedSymbol:
    symbol: str
    name: str
    sector: str
    currency: str = "USD"
    exchange: str = ""


@dataclass(frozen=True)
class StockPlan:
    action: Action
    confidence: float         # 0..1
    entry_price: float
    entry_zone_low: float
    entry_zone_high: float
    target_price: float
    stop_loss: float
    risk_reward: Optional[float]
    expected_move_pct: float
    time_horizon: str
    rationale: str
    caveats: list[str]


@dataclass(frozen=True)
class SearchResult:
    resolved: ResolvedSymbol
    report: ReportOut
    stock_plan: StockPlan


# ---------------------------------------------------------------------------
# Symbol resolver
# ---------------------------------------------------------------------------


# Handful of common company names → tickers so we don't need yfinance.Search
# just to handle "apple" or "microsoft". The full list is on yfinance.
_NAME_ALIASES: dict[str, str] = {
    "apple": "AAPL", "apple inc": "AAPL",
    "microsoft": "MSFT",
    "amazon": "AMZN", "amazon.com": "AMZN",
    "alphabet": "GOOGL", "google": "GOOGL",
    "meta": "META", "facebook": "META", "meta platforms": "META",
    "tesla": "TSLA",
    "nvidia": "NVDA",
    "netflix": "NFLX",
    "palantir": "PLTR",
    "coinbase": "COIN",
    "exxon": "XOM", "exxon mobil": "XOM",
    "palo alto": "PANW", "palo alto networks": "PANW",
    "oracle": "ORCL",
    "ibm": "IBM", "international business machines": "IBM",
    "berkshire": "BRK-B", "berkshire hathaway": "BRK-B",
    "jpmorgan": "JPM", "jp morgan": "JPM",
    "bank of america": "BAC",
    "walmart": "WMT",
    "disney": "DIS", "walt disney": "DIS",
    "ford": "F", "ford motor": "F",
    "general motors": "GM",
    "intel": "INTC",
    "advanced micro devices": "AMD", "amd": "AMD",
    "broadcom": "AVGO",
    "pfizer": "PFE",
    "johnson & johnson": "JNJ", "johnson and johnson": "JNJ",
    "visa": "V",
    "mastercard": "MA",
    "uber": "UBER",
    "lyft": "LYFT",
    "airbnb": "ABNB",
    "shopify": "SHOP",
    "salesforce": "CRM",
    "adobe": "ADBE",
    "costco": "COST",
    "starbucks": "SBUX",
    "mcdonalds": "MCD", "mcdonald's": "MCD",
    "boeing": "BA",
    "chevron": "CVX",
    "nuscale": "SMR",
    "oklo": "OKLO",
}


def _looks_like_symbol(q: str) -> bool:
    """Cheap heuristic: 1-6 chars, letters/digits/dot/dash only."""
    s = q.strip()
    if not (1 <= len(s) <= 6):
        return False
    return all(c.isalnum() or c in ("-", ".", "^") for c in s)


def resolve_symbol(query: str) -> ResolvedSymbol | None:
    """Turn a free-form query into a canonical (symbol, name, sector)."""
    q = (query or "").strip()
    if not q:
        return None

    candidates: list[str] = []
    q_lower = q.lower()
    # Alias map first — cheapest, handles common name queries.
    if q_lower in _NAME_ALIASES:
        candidates.append(_NAME_ALIASES[q_lower])
    # Uppercase direct form.
    if _looks_like_symbol(q):
        candidates.append(q.upper())
    # If nothing matched yet, try yfinance.Search for free-text.
    if not candidates:
        for s in _yfinance_search(q):
            if s not in candidates:
                candidates.append(s)

    for sym in candidates:
        resolved = _probe_symbol(sym)
        if resolved is not None:
            return resolved
    return None


def _yfinance_search(q: str) -> list[str]:
    """Best-effort free-text → [symbol,...] via yfinance.Search."""
    try:
        import yfinance as yf  # type: ignore
        # yfinance.Search appears in 0.2.50+. Guard for older versions.
        Search = getattr(yf, "Search", None)
        if Search is None:
            return []
        res = Search(q, max_results=5)
        quotes = getattr(res, "quotes", None) or []
        out: list[str] = []
        for q_ in quotes:
            sym = q_.get("symbol") if isinstance(q_, dict) else None
            if sym and sym not in out:
                out.append(sym)
        return out
    except Exception:
        return []


def _probe_symbol(sym: str) -> ResolvedSymbol | None:
    """Return metadata for ``sym`` if we can find it, else None.

    Curated tickers are resolved from ``TICKER_MAP`` directly (no network
    roundtrip); everything else hits ``yfinance.Ticker(sym).info`` and is
    accepted only if the symbol has a real short name + market.
    """
    sym = sym.upper()
    meta = TICKER_MAP.get(sym)
    if meta is not None:
        return ResolvedSymbol(
            symbol=sym,
            name=meta["name"],
            sector=meta["sector"],
        )

    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return None

    try:
        t = yf.Ticker(sym)
        info = {}
        # yfinance 0.2.50+: fast_info + get_info; older: .info
        try:
            info = t.get_info() or {}
        except Exception:
            try:
                info = t.info or {}
            except Exception:
                info = {}
        name = (
            info.get("longName")
            or info.get("shortName")
            or info.get("displayName")
        )
        if not name:
            return None
        sector = (
            info.get("sector")
            or info.get("quoteType")
            or info.get("industry")
            or "—"
        )
        currency = info.get("currency") or "USD"
        exchange = info.get("fullExchangeName") or info.get("exchange") or ""
        return ResolvedSymbol(
            symbol=sym,
            name=str(name),
            sector=str(sector),
            currency=str(currency),
            exchange=str(exchange),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Stock plan
# ---------------------------------------------------------------------------


_HORIZON_BY_TF: dict[Timeframe, str] = {
    "1h":     "1–3 trading days",
    "4h":     "3–10 trading days",
    "daily":  "2–6 weeks",
    "weekly": "6–16 weeks",
}


def build_stock_plan(report: ReportOut) -> StockPlan:
    """Derive a Buy / Sell / Hold plan with dollar-level entry, target, stop.

    Rules
    -----
    * Action is driven by ``verdict`` (BULLISH / BEARISH / NEUTRAL) at a
      minimum conviction floor. Low-conviction bull/bear verdicts are
      demoted to Hold.
    * Entry zone = current ± 0.25 × ATR(14).
    * Target     = nearest structural level within ±1.5 × expected-move,
                   otherwise spot ± 1 × expected-move.
    * Stop       = max(1 × ATR, 1.5% of spot) from entry.
    * Risk-reward uses |target-entry| / |entry-stop|.

    The math mirrors the options trade-plan construction in ``report.py``
    so the two recommendations agree in direction and magnitude — the
    search view is the stock-level companion to the options ticket.
    """
    pa = report.price_action
    last = float(pa.last)
    atr = float(report.atr.value) if report.atr and report.atr.value else last * 0.02
    conviction = float(report.conviction or 0.0)

    # Decide action from verdict + conviction floor.
    min_conviction = 0.40
    action: Action
    if report.verdict == "BULLISH" and conviction >= min_conviction:
        action = "Buy"
    elif report.verdict == "BEARISH" and conviction >= min_conviction:
        action = "Sell"
    else:
        action = "Hold"

    # Expected move (rough): ATR * sqrt(horizon bars). Daily ≈ 5 bars/week;
    # we target a ~2-week horizon on daily → sqrt(10).
    bars_ahead = {
        "1h": 8, "4h": 10, "daily": 10, "weekly": 6,
    }.get(report.timeframe, 10)
    expected_move = atr * (bars_ahead ** 0.5)
    expected_move_pct = (expected_move / last) * 100 if last else 0.0

    entry_low = max(0.0, last - 0.25 * atr)
    entry_high = last + 0.25 * atr
    entry = last  # market-style entry at current price by default

    # Pick a target from structural levels when one sits inside ±1.5× EM.
    supports = [float(x) for x in (pa.supports or [])]
    resistances = [float(x) for x in (pa.resistances or [])]

    def _first_within(levels: list[float], bound: float, direction: str) -> Optional[float]:
        for v in levels:
            if direction == "up" and v > last and (v - last) <= bound:
                return v
            if direction == "down" and v < last and (last - v) <= bound:
                return v
        return None

    if action == "Buy":
        struct_target = _first_within(sorted(resistances), 1.5 * expected_move, "up")
        target = struct_target if struct_target is not None else last + expected_move
        min_stop_dist = max(atr, 0.015 * last)
        struct_stop = None
        for s in sorted(supports, reverse=True):
            if s < last - min_stop_dist:
                struct_stop = s
                break
        stop = struct_stop if struct_stop is not None else last - min_stop_dist
    elif action == "Sell":
        struct_target = _first_within(sorted(supports, reverse=True), 1.5 * expected_move, "down")
        target = struct_target if struct_target is not None else last - expected_move
        min_stop_dist = max(atr, 0.015 * last)
        struct_stop = None
        for r in sorted(resistances):
            if r > last + min_stop_dist:
                struct_stop = r
                break
        stop = struct_stop if struct_stop is not None else last + min_stop_dist
    else:  # Hold
        target = last + expected_move
        stop = last - expected_move
        entry = last
        entry_low = last
        entry_high = last

    reward = abs(target - entry)
    risk = abs(entry - stop) or 1e-9
    rr = round(reward / risk, 2) if action != "Hold" else None

    # One-line rationale — deterministic, pulls from report fields.
    rationale_parts: list[str] = []
    trend = pa.trend or "range"
    rsi_val = report.rsi.value if report.rsi else None
    sma50 = report.sma.sma50 if report.sma else None
    sma200 = report.sma.sma200 if report.sma else None

    if action == "Buy":
        rationale_parts.append(f"{trend} on {report.timeframe}")
        if sma50 and sma200 and sma50 > sma200:
            rationale_parts.append("SMA50 > SMA200")
        if rsi_val is not None:
            rationale_parts.append(f"RSI {rsi_val:.0f}")
        rationale = "Bullish: " + ", ".join(rationale_parts) + "."
    elif action == "Sell":
        rationale_parts.append(f"{trend} on {report.timeframe}")
        if sma50 and sma200 and sma50 < sma200:
            rationale_parts.append("SMA50 < SMA200")
        if rsi_val is not None:
            rationale_parts.append(f"RSI {rsi_val:.0f}")
        rationale = "Bearish: " + ", ".join(rationale_parts) + "."
    else:
        rationale_parts.append(f"conviction {int(conviction * 100)}%")
        if rsi_val is not None:
            rationale_parts.append(f"RSI {rsi_val:.0f}")
        rationale = "Mixed signals — wait for cleaner structure (" + ", ".join(rationale_parts) + ")."

    caveats: list[str] = []
    if report.timeframe in ("1h", "4h"):
        caveats.append("Intraday signals decay fast — refresh before acting.")
    if report.price_action.patterns:
        caveats.append("Price is reacting at a structural level — respect the stop.")
    if report.source == "synthetic":
        caveats.append("Live data unavailable — recommendation computed from synthetic history.")

    return StockPlan(
        action=action,
        confidence=round(conviction, 3),
        entry_price=round(entry, 2),
        entry_zone_low=round(entry_low, 2),
        entry_zone_high=round(entry_high, 2),
        target_price=round(target, 2),
        stop_loss=round(stop, 2),
        risk_reward=rr,
        expected_move_pct=round(expected_move_pct, 2),
        time_horizon=_HORIZON_BY_TF.get(report.timeframe, "2–6 weeks"),
        rationale=rationale,
        caveats=caveats,
    )


# ---------------------------------------------------------------------------
# Top-level search
# ---------------------------------------------------------------------------


def search(query: str, timeframe: Timeframe = "daily") -> SearchResult:
    """Resolve ``query`` → analyst report → stock plan.

    Raises ``ValueError`` for unknown symbols or insufficient history.
    """
    resolved = resolve_symbol(query)
    if resolved is None:
        raise ValueError(f"No ticker matched '{query}'")

    meta = {
        "symbol": resolved.symbol,
        "name": resolved.name,
        "sector": resolved.sector,
        "bias": 0.0,
    }
    report = build_report(
        resolved.symbol, timeframe, meta_override=meta, fresh_quotes=True
    )
    plan = build_stock_plan(report)
    return SearchResult(resolved=resolved, report=report, stock_plan=plan)


def suggest(query: str, limit: int = 6) -> list[ResolvedSymbol]:
    """Return up to ``limit`` ticker suggestions for autocomplete."""
    q = (query or "").strip()
    if len(q) < 1:
        return []

    out: list[ResolvedSymbol] = []
    seen: set[str] = set()

    q_lower = q.lower()

    # 1) Curated tickers matching prefix on symbol or name (instant).
    for sym, meta in TICKER_MAP.items():
        if sym.lower().startswith(q_lower) or q_lower in meta["name"].lower():
            if sym not in seen:
                out.append(ResolvedSymbol(sym, meta["name"], meta["sector"]))
                seen.add(sym)
                if len(out) >= limit:
                    return out

    # 2) Direct alias hit.
    if q_lower in _NAME_ALIASES:
        sym = _NAME_ALIASES[q_lower]
        if sym not in seen:
            r = _probe_symbol(sym)
            if r is not None:
                out.append(r)
                seen.add(sym)

    # 3) Fall through to yfinance.Search for anything else.
    if len(out) < limit:
        for sym in _yfinance_search(q):
            if sym in seen:
                continue
            r = _probe_symbol(sym)
            if r is not None:
                out.append(r)
                seen.add(sym)
                if len(out) >= limit:
                    break
    return out[:limit]
