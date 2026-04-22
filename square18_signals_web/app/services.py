"""Business logic layer — turns the live analyst pipeline into API payloads.

This module is the bridge between the deterministic analyst engine
(``app.analyst.*``) and the web UI's contracts (``app.models``). Everything
here now derives from real OHLCV data fetched through ``get_ohlcv`` —
no hardcoded ticker fixtures are used anymore.

Flow per request:
    /api/screen  -> overview_rows(daily)  -> TickerRowOut[]
    /api/regime  -> market_pulse(daily) + VIX quote -> RegimeEnvelope
    /api/tickers/{sym} -> build_report(daily) -> TickerDetailOut

The synthetic TickerSnapshot fixtures in ``app/data.py`` remain in the
codebase for backward compatibility / test seeding, but are no longer
consumed by any production endpoint.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

from square18_signals import (
    MarketContext,
    recommend_strategies,
)

from .analyst.constants import DEFAULT_IV, TICKER_MAP, Timeframe
from .analyst.data import get_ohlcv
from .analyst.market import market_pulse
from .analyst.models import OverviewRow, ReportOut
from .analyst.report import build_report, overview_rows
from .models import (
    CountsOut,
    ExpectedMoveOut,
    FactorOut,
    LegOut,
    MetricsOut,
    RecommendationOut,
    RegimeEnvelope,
    RegimeOut,
    StrategyOut,
    TickerDetailOut,
    TickerRowOut,
    clamp_inf,
)


# ---------------------------------------------------------------------------
# Verdict -> signal mapping
# ---------------------------------------------------------------------------


def _signal_for(verdict: str) -> str:
    return {"BULLISH": "Buy", "BEARISH": "Sell"}.get(verdict, "Hold")


def _direction_for(verdict: str) -> str:
    return {"BULLISH": "bull", "BEARISH": "bear"}.get(verdict, "neutral")


# ---------------------------------------------------------------------------
# IV proxy from realised close-to-close volatility
#
# The free data tier doesn't give us option-chain IV. We compute a
# statistically-meaningful proxy: trailing 20-bar realised vol annualised,
# and rank it against the last year of the same series. This drives the
# iv / iv_rank / iv_percentile columns on the dashboard and feeds the
# options recommender with a defensible vol input.
# ---------------------------------------------------------------------------


def _annualised_rv(closes: list[float], window: int = 20) -> Optional[float]:
    if len(closes) < window + 2:
        return None
    rets: list[float] = []
    for a, b in zip(closes[-(window + 1):-1], closes[-window:]):
        if a <= 0 or b <= 0:
            continue
        rets.append(math.log(b / a))
    if len(rets) < 2:
        return None
    sd = statistics.pstdev(rets)
    return sd * math.sqrt(252)


def _rv_rank_pct(closes: list[float], window: int = 20, lookback: int = 252) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (current_rv, rank_0_100, percentile_0_100)."""
    current = _annualised_rv(closes, window=window)
    if current is None:
        return None, None, None
    series: list[float] = []
    for end in range(window + 1, min(len(closes), lookback + window + 1) + 1):
        rv = _annualised_rv(closes[:end], window=window)
        if rv is not None:
            series.append(rv)
    if len(series) < 10:
        return current, 50.0, 50.0
    lo, hi = min(series), max(series)
    rank = 0.0 if hi == lo else (current - lo) / (hi - lo) * 100.0
    below = sum(1 for v in series if v <= current)
    pct = below / len(series) * 100.0
    return current, max(0.0, min(100.0, rank)), max(0.0, min(100.0, pct))


def _dte_pref_for(rv_rank: Optional[float]) -> int:
    """High IV rank → closer DTE (premium-rich), low → further out."""
    if rv_rank is None:
        return 35
    if rv_rank >= 75:
        return 21
    if rv_rank >= 50:
        return 30
    if rv_rank >= 25:
        return 45
    return 60


# ---------------------------------------------------------------------------
# Row / detail builders
# ---------------------------------------------------------------------------


def _row_from_overview(r: OverviewRow, closes: list[float]) -> TickerRowOut:
    rv, rank, pct = _rv_rank_pct(closes)
    iv = rv if rv is not None else DEFAULT_IV.get(r.symbol, 0.30)
    signal = _signal_for(r.verdict)
    direction = _direction_for(r.verdict)
    return TickerRowOut(
        symbol=r.symbol,
        name=r.name,
        sector=r.sector,
        price=r.last,
        change_pct=r.change_pct,
        signal=signal,          # type: ignore[arg-type]
        direction=direction,    # type: ignore[arg-type]
        composite_score=round(r.composite_score, 3),
        confidence=round(r.conviction, 3),
        rsi=round(r.rsi, 1) if r.rsi is not None else 50.0,
        iv=round(iv, 3),
        iv_rank=round(rank, 1) if rank is not None else 50.0,
        iv_percentile=round(pct, 1) if pct is not None else 50.0,
        dte_pref=_dte_pref_for(rank),
        earnings_in_window=False,
    )


def regime_envelope(last_scan_iso: str, timeframe: Timeframe = "daily") -> RegimeEnvelope:
    """Build the dashboard regime banner + counts from live analyst data.

    * VIX level + 1-day change come from the real VIX OHLCV series.
    * Breadth (% above 50d SMA) is computed across the tracked universe.
    * Trend score = average composite_score across the universe, which
      already rolls up MA stack, MACD, RSI posture, and price action.
    * Put/call ratio is still a fixture (needs an options-chain feed).
    * Label is derived from the pair (vix_level, trend_score).
    """
    pulse = market_pulse(timeframe)
    rows = overview_rows(timeframe)

    # Trend score: universe-wide mean composite.
    if rows:
        trend_score = sum(r.composite_score for r in rows) / len(rows)
    else:
        trend_score = 0.0

    # Breadth: % of tracked tickers trading above their 50d SMA.
    breadth_pct = _breadth_above_50d(timeframe)

    # VIX spot + change.
    vix_last, vix_change = _vix_quote()

    # Put/call remains a placeholder — no free feed. Could be wired
    # to CBOE CSV later.
    put_call = 0.92

    label = _regime_label(vix_last, trend_score, breadth_pct)

    longs = sum(1 for r in rows if r.verdict == "BULLISH")
    shorts = sum(1 for r in rows if r.verdict == "BEARISH")
    holds = sum(1 for r in rows if r.verdict == "NEUTRAL")

    return RegimeEnvelope(
        regime=RegimeOut(
            label=label,
            vix=round(vix_last, 2),
            vix_change=round(vix_change, 2),
            breadth_pct_above_50d=round(breadth_pct, 1),
            put_call_ratio=round(put_call, 2),
            trend_score=round(trend_score, 3),
        ),
        counts=CountsOut(
            universe_size=len(TICKER_MAP),
            scanned=len(rows),
            longs=longs,
            shorts=shorts,
            holds=holds,
        ),
        last_scan_iso=last_scan_iso,
    )


def _vix_quote() -> tuple[float, float]:
    """Return (vix_last, vix_1d_change). Falls back to a sensible default."""
    try:
        series = get_ohlcv("VIX", "daily")
        if len(series) >= 2:
            last = float(series.close[-1])
            prev = float(series.close[-2])
            chg = last - prev
            return last, chg
        if len(series) == 1:
            return float(series.close[-1]), 0.0
    except Exception:
        pass
    return 17.0, 0.0


def _breadth_above_50d(timeframe: Timeframe) -> float:
    """% of tracked equities trading above their 50-bar SMA."""
    from .analyst.indicators import sma

    # Equities only — VIX above its own 50d means more fear, which
    # inverts the usual "bullish" reading, so we exclude it.
    symbols = [s for s in TICKER_MAP.keys() if s != "VIX"]
    above = 0
    total = 0
    for sym in symbols:
        try:
            series = get_ohlcv(sym, timeframe)
        except Exception:
            continue
        closes = series.close
        if len(closes) < 50:
            continue
        s50 = sma(closes, 50)
        if not s50 or s50[-1] is None:
            continue
        total += 1
        if closes[-1] > s50[-1]:
            above += 1
    if total == 0:
        return 50.0
    return above / total * 100.0


def _regime_label(vix: float, trend_score: float, breadth_pct: float) -> str:
    """Human-readable market posture."""
    if vix >= 28:
        return "High-vol defensive"
    if vix >= 22:
        base = "Choppy, elevated vol"
    elif vix <= 13:
        base = "Low-vol drift"
    else:
        base = "Balanced"
    if trend_score >= 0.35 and breadth_pct >= 60:
        return f"{base} — risk-on bias"
    if trend_score <= -0.35 and breadth_pct <= 40:
        return f"{base} — risk-off bias"
    if trend_score >= 0.15:
        return f"{base} — leaning bullish"
    if trend_score <= -0.15:
        return f"{base} — leaning bearish"
    return f"{base} — neutral"


def screener_rows(signal_filter: str = "all", timeframe: Timeframe = "daily") -> list[TickerRowOut]:
    """Live signals table, driven by ``overview_rows(timeframe)``."""
    rows_live = overview_rows(timeframe)
    out: list[TickerRowOut] = []
    want = signal_filter.lower()
    for r in rows_live:
        signal = _signal_for(r.verdict)
        if want != "all" and signal.lower() != want:
            continue
        closes: list[float] = []
        try:
            series = get_ohlcv(r.symbol, timeframe)
            closes = series.close
        except Exception:
            closes = []
        out.append(_row_from_overview(r, closes))
    # Buy/Sell by |composite|, Holds last.
    out.sort(key=lambda x: (x.signal == "Hold", -abs(x.composite_score)))
    return out


def ticker_detail(symbol: str, timeframe: Timeframe = "daily") -> TickerDetailOut | None:
    """Detail payload for a single ticker, sourced from the analyst pipeline."""
    sym = symbol.upper()
    meta = TICKER_MAP.get(sym)
    if meta is None:
        return None

    try:
        report = build_report(sym, timeframe)
    except Exception:
        return None

    try:
        series = get_ohlcv(sym, timeframe)
        closes = series.close
    except Exception:
        closes = report.chart.close

    # Equivalent OverviewRow so we can reuse _row_from_overview.
    tp = report.options.trade_plan
    synthetic_row = OverviewRow(
        symbol=report.symbol,
        name=report.name,
        sector=report.sector,
        last=round(report.price_action.last, 2),
        change_pct=round(report.price_action.change_pct, 2),
        verdict=report.verdict,
        conviction=report.conviction,
        composite_score=report.composite_score,
        rsi=round(report.rsi.value, 1) if report.rsi.value is not None else None,
        trend=report.price_action.trend,
        source=report.source,
        rec_contract_type=tp.contract_type,
        rec_strike=tp.strike,
        rec_expiry_date=tp.expiry_date,
        rec_expiry_dte=tp.expiry_dte,
        rec_premium=tp.estimated_premium,
        rec_cost_per_contract=tp.cost_per_contract,
        rec_break_even=tp.break_even,
        rec_target=tp.target_price,
        rec_stop=tp.stop_loss,
        rec_risk_reward=tp.risk_reward,
    )
    row = _row_from_overview(synthetic_row, closes)

    # 30-bar price window for the sparkline / chart.
    price_30 = [round(float(c), 2) for c in closes[-30:]] if closes else report.chart.close[-30:]

    # Expected move: use the recommender's trade-plan one-sigma when present,
    # otherwise derive from realised vol.
    if tp.one_sigma_move_usd is not None and tp.one_sigma_move_pct is not None:
        expected = ExpectedMoveOut(
            one_sigma_usd=round(float(tp.one_sigma_move_usd), 2),
            one_sigma_pct=round(float(tp.one_sigma_move_pct), 2),
        )
    else:
        iv_used = row.iv
        dte = max(1, row.dte_pref)
        one_sigma_usd = row.price * iv_used * math.sqrt(dte / 365.0)
        expected = ExpectedMoveOut(
            one_sigma_usd=round(one_sigma_usd, 2),
            one_sigma_pct=round(iv_used * math.sqrt(dte / 365.0) * 100.0, 2),
        )

    ctx = MarketContext(
        symbol=row.symbol,
        spot=row.price,
        iv=row.iv,
        iv_rank=row.iv_rank,
        direction=row.direction,
        conviction=row.confidence,
        dte=row.dte_pref,
        risk_free_rate=0.045,
        dividend_yield=0.0,
        earnings_in_window=row.earnings_in_window,
    )
    try:
        recs = recommend_strategies(ctx, max_results=4)
    except Exception:
        recs = []

    factors = _derive_factors(report)

    return TickerDetailOut(
        row=row,
        factors=factors,
        price_30d=price_30,
        expected_move=expected,
        recommendations=[
            RecommendationOut(
                strategy=StrategyOut(
                    name=r.strategy.name,
                    legs=[
                        LegOut(
                            kind=leg.kind,
                            side=leg.side,
                            strike=leg.strike,
                            premium=round(leg.premium, 3),
                            quantity=leg.quantity,
                        )
                        for leg in r.strategy.legs
                    ],
                ),
                metrics=MetricsOut(
                    net_debit=round(r.metrics.net_debit, 2),
                    max_gain=clamp_inf(r.metrics.max_gain),
                    max_loss=clamp_inf(r.metrics.max_loss),
                    breakevens=[round(b, 2) for b in r.metrics.breakevens],
                    probability_of_profit=round(r.metrics.probability_of_profit, 4),
                ),
                rationale=r.rationale,
                fit_score=round(r.fit_score, 3),
                tags=list(r.tags),
            )
            for r in recs
        ],
    )


def _derive_factors(report: ReportOut) -> list[FactorOut]:
    """Turn an analyst report into the 4 factor scores the UI expects.

    Scores are in [-1, +1]. We expose the deterministic components so the
    user can see *why* the verdict is what it is, rather than a black box.
    """
    # Trend: SMA stack + price action direction.
    sma = report.sma
    pa_trend = report.price_action.trend
    trend_score = 0.0
    trend_notes: list[str] = []
    if sma.stacked_bullish:
        trend_score += 0.6
        trend_notes.append("50d above 200d")
    if sma.stacked_bearish:
        trend_score -= 0.6
        trend_notes.append("50d below 200d")
    if sma.golden_cross_recent:
        trend_score += 0.2
        trend_notes.append("recent golden cross")
    if sma.death_cross_recent:
        trend_score -= 0.2
        trend_notes.append("recent death cross")
    if pa_trend == "uptrend":
        trend_score += 0.2
    elif pa_trend == "downtrend":
        trend_score -= 0.2
    trend_score = max(-1.0, min(1.0, trend_score))
    trend_note = ", ".join(trend_notes) if trend_notes else f"price action: {pa_trend}"

    # Momentum: RSI posture + MACD state.
    rsi = report.rsi
    macd = report.macd
    mom = 0.0
    mom_notes: list[str] = []
    if rsi.state == "bullish":
        mom += 0.4
        mom_notes.append(f"RSI {rsi.value:.0f}")
    elif rsi.state == "bearish":
        mom -= 0.4
        mom_notes.append(f"RSI {rsi.value:.0f}")
    elif rsi.state == "overbought":
        mom += 0.1
        mom_notes.append(f"RSI overbought ({rsi.value:.0f})")
    elif rsi.state == "oversold":
        mom -= 0.1
        mom_notes.append(f"RSI oversold ({rsi.value:.0f})")
    if macd.histogram_direction == "rising":
        mom += 0.3
        mom_notes.append("MACD hist rising")
    elif macd.histogram_direction == "falling":
        mom -= 0.3
        mom_notes.append("MACD hist falling")
    if macd.bullish_cross_recent:
        mom += 0.2
        mom_notes.append("bull cross")
    if macd.bearish_cross_recent:
        mom -= 0.2
        mom_notes.append("bear cross")
    mom = max(-1.0, min(1.0, mom))
    mom_note = ", ".join(mom_notes) if mom_notes else "quiet"

    # Mean reversion: inverse of stretch vs 50d SMA.
    mr = 0.0
    if sma.price_vs_sma50_pct is not None:
        stretch = sma.price_vs_sma50_pct
        if stretch > 8:
            mr -= min(0.8, (stretch - 8) / 10)
            mr_note = f"stretched +{stretch:.1f}% vs 50d"
        elif stretch < -8:
            mr += min(0.8, (-stretch - 8) / 10)
            mr_note = f"stretched {stretch:.1f}% vs 50d"
        else:
            mr_note = f"{stretch:+.1f}% vs 50d (in band)"
    else:
        mr_note = "no SMA yet"
    mr = max(-1.0, min(1.0, mr))

    # Volume flow.
    v = report.volume
    if v.ratio >= 1.5 and report.price_action.change_pct > 0:
        vol_score = 0.6
        vol_note = f"buyers in ({v.ratio:.1f}× avg)"
    elif v.ratio >= 1.5 and report.price_action.change_pct < 0:
        vol_score = -0.6
        vol_note = f"sellers in ({v.ratio:.1f}× avg)"
    elif v.ratio <= 0.6:
        vol_score = -0.1
        vol_note = f"quiet ({v.ratio:.1f}× avg)"
    elif v.trending_up:
        vol_score = 0.2
        vol_note = f"volume trending up ({v.ratio:.1f}× avg)"
    else:
        vol_score = 0.0
        vol_note = f"normal ({v.ratio:.1f}× avg)"

    return [
        FactorOut(name="Trend",          score=round(trend_score, 2), note=trend_note),
        FactorOut(name="Momentum",       score=round(mom, 2),         note=mom_note),
        FactorOut(name="Mean reversion", score=round(mr, 2),          note=mr_note),
        FactorOut(name="Volume flow",    score=round(vol_score, 2),   note=vol_note),
    ]


__all__ = [
    "regime_envelope",
    "screener_rows",
    "ticker_detail",
]
