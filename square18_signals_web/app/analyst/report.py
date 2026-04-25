"""Analyst report composer.

Takes OHLCV in, emits a fully-composed ``ReportOut`` with narrative,
indicator states, and an options suggestion routed through the existing
``square18_signals`` recommender so the strategy metrics are real.

The "agent" framing from the user request is implemented here as a
deterministic, rule-based analyst — no LLM calls, reproducible, and
cheap. Every claim in the narrative is grounded in one of the indicator
states computed in the same pass.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from square18_signals.pricing import black_scholes_price

from .constants import DEFAULT_IV, ETF_SIGNAL_TICKERS, TICKER_MAP, TICKERS, Timeframe
from .data import OHLCV, get_ohlcv
from .indicators import (
    atr as _atr,
    macd as _macd,
    rolling_std,
    rsi as _rsi,
    sma,
    support_resistance,
)
from .models import (
    ChartPayload,
    IndicatorATR,
    IndicatorMACD,
    IndicatorRSI,
    IndicatorSMA,
    OptionsSuggestion,
    OverviewRow,
    PriceAction,
    ReportOut,
    TradePlan,
    VolumeStats,
)


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def build_report(
    symbol: str,
    timeframe: Timeframe,
    meta_override: dict | None = None,
) -> ReportOut:
    """Build a full analyst report for a ticker.

    If ``symbol`` is in the curated ``TICKER_MAP`` its metadata is used
    directly. Otherwise the caller may pass ``meta_override`` (e.g. from
    a live yfinance lookup) to supply ``name``/``sector``/``bias`` — this
    is how the /api/search endpoint supports arbitrary tickers.
    """
    sym = symbol.upper()
    meta = meta_override or TICKER_MAP.get(sym)
    if meta is None:
        raise ValueError(f"unknown symbol: {symbol}")

    data = get_ohlcv(sym, timeframe)
    if len(data) < 60:
        raise ValueError(f"insufficient history for {sym} at {timeframe}")

    closes = data.close
    highs = data.high
    lows = data.low
    volumes = data.volume

    sma50_series = sma(closes, 50)
    sma200_series = sma(closes, 200) if len(closes) >= 200 else [None] * len(closes)
    rsi_series = _rsi(closes, 14)
    macd_line, signal_line, hist_line = _macd(closes)
    atr_series = _atr(highs, lows, closes, 14)

    price_action = _price_action(closes, highs, lows, sma50_series)
    volume_stats = _volume_stats(volumes)
    sma_block = _sma_block(closes, sma50_series, sma200_series)
    rsi_block = _rsi_block(rsi_series)
    macd_block = _macd_block(macd_line, signal_line, hist_line)
    atr_block = _atr_block(atr_series, closes)

    composite, verdict, conviction, headline = _score_and_verdict(
        sym, timeframe, price_action, volume_stats, sma_block, rsi_block, macd_block
    )

    narrative = _compose_narrative(
        meta=meta,
        timeframe=timeframe,
        data=data,
        price_action=price_action,
        volume_stats=volume_stats,
        sma_block=sma_block,
        rsi_block=rsi_block,
        macd_block=macd_block,
        atr_block=atr_block,
        verdict=verdict,
        conviction=conviction,
    )

    market_ctx = _market_context_text(sym, meta["sector"], data.source)
    options = _build_options_suggestion(
        sym=sym,
        spot=closes[-1],
        closes=closes,
        atr_block=atr_block,
        price_action=price_action,
        verdict=verdict,
        conviction=conviction,
        composite_score=composite,
        timeframe=timeframe,
    )

    chart = ChartPayload(
        timestamps=data.timestamps,
        close=closes,
        sma50=sma50_series,
        sma200=sma200_series,
    )

    return ReportOut(
        symbol=sym,
        name=meta["name"],
        sector=meta["sector"],
        timeframe=timeframe,
        as_of=data.timestamps[-1] if data.timestamps else datetime.now(timezone.utc).isoformat(),
        source=data.source,
        verdict=verdict,
        conviction=round(conviction, 3),
        composite_score=round(composite, 3),
        headline=headline,
        narrative=narrative,
        price_action=price_action,
        volume=volume_stats,
        sma=sma_block,
        rsi=rsi_block,
        macd=macd_block,
        atr=atr_block,
        market_context=market_ctx,
        options=options,
        chart=chart,
    )


def overview_rows(
    timeframe: Timeframe = "daily",
    *,
    metas: list[dict] | None = None,
) -> list[OverviewRow]:
    """Compact verdict + recommendation for every entry in *metas* (default: ``TICKERS``)."""
    universe = metas if metas is not None else TICKERS
    rows: list[OverviewRow] = []
    for meta in universe:
        try:
            rpt = build_report(meta["symbol"], timeframe)
        except Exception:
            continue
        tp = rpt.options.trade_plan
        rows.append(
            OverviewRow(
                symbol=rpt.symbol,
                name=rpt.name,
                sector=rpt.sector,
                last=round(rpt.price_action.last, 2),
                change_pct=round(rpt.price_action.change_pct, 2),
                verdict=rpt.verdict,
                conviction=rpt.conviction,
                composite_score=rpt.composite_score,
                rsi=round(rpt.rsi.value, 1) if rpt.rsi.value is not None else None,
                trend=rpt.price_action.trend,
                source=rpt.source,
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
        )
    return rows


def etf_overview_rows(timeframe: Timeframe = "daily") -> list[OverviewRow]:
    """Verdicts for the dedicated ETF watchlist (``ETF_SIGNAL_TICKERS``)."""
    return overview_rows(timeframe, metas=ETF_SIGNAL_TICKERS)


# ---------------------------------------------------------------------------
# Indicator → block helpers
# ---------------------------------------------------------------------------


def _price_action(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    sma50_series: list[Optional[float]],
) -> PriceAction:
    last = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else last
    change_pct = (last / prev - 1) * 100 if prev else 0.0
    period_start = closes[0]
    change_pct_period = (last / period_start - 1) * 100 if period_start else 0.0

    supports, resistances = support_resistance(highs, lows, last)

    # Trend detection: compare 20-bar close to 50-bar close and SMA50 slope.
    lookback_short = min(20, len(closes) - 1)
    mean_recent = sum(closes[-lookback_short:]) / lookback_short
    mean_older = sum(closes[-2 * lookback_short : -lookback_short]) / max(
        1, len(closes[-2 * lookback_short : -lookback_short])
    )
    sma50_now = sma50_series[-1]
    sma50_then = sma50_series[-lookback_short] if len(sma50_series) > lookback_short else sma50_now
    sma50_slope = (
        (sma50_now - sma50_then) / sma50_then if sma50_now and sma50_then else 0.0
    )

    if mean_recent > mean_older * 1.01 and sma50_slope > 0.005:
        trend = "uptrend"
    elif mean_recent < mean_older * 0.99 and sma50_slope < -0.005:
        trend = "downtrend"
    else:
        trend = "range"

    patterns: list[str] = []
    if trend == "uptrend":
        patterns.append("higher highs / higher lows")
    elif trend == "downtrend":
        patterns.append("lower highs / lower lows")
    if resistances and abs(last - resistances[0]) / last < 0.015:
        patterns.append(f"testing resistance at ${resistances[0]:.2f}")
    if supports and abs(last - supports[0]) / last < 0.015:
        patterns.append(f"holding support at ${supports[0]:.2f}")

    # Cheap double-top / double-bottom check.
    if len(resistances) >= 2 and abs(resistances[0] - resistances[1]) / resistances[0] < 0.01:
        patterns.append("potential double-top near "
                       f"${resistances[0]:.2f}")
    if len(supports) >= 2 and abs(supports[0] - supports[1]) / supports[0] < 0.01:
        patterns.append("potential double-bottom near "
                       f"${supports[0]:.2f}")

    return PriceAction(
        last=round(last, 4),
        change_pct=round(change_pct, 3),
        change_pct_period=round(change_pct_period, 2),
        supports=[round(x, 2) for x in supports],
        resistances=[round(x, 2) for x in resistances],
        trend=trend,
        patterns=patterns,
    )


def _volume_stats(volumes: list[float]) -> VolumeStats:
    if not volumes:
        return VolumeStats(latest=0, avg_20=0, ratio=0, unusual=False, trending_up=False)
    latest = volumes[-1]
    window = volumes[-20:] if len(volumes) >= 20 else volumes
    avg_20 = sum(window) / len(window)
    ratio = latest / avg_20 if avg_20 else 0.0
    recent5 = sum(volumes[-5:]) / min(5, len(volumes))
    trending_up = recent5 > avg_20 * 1.05
    unusual = ratio > 1.5 or ratio < 0.6
    return VolumeStats(
        latest=round(latest, 0),
        avg_20=round(avg_20, 0),
        ratio=round(ratio, 2),
        unusual=unusual,
        trending_up=trending_up,
    )


def _sma_block(
    closes: list[float],
    sma50_series: list[Optional[float]],
    sma200_series: list[Optional[float]],
) -> IndicatorSMA:
    last = closes[-1]
    s50 = sma50_series[-1]
    s200 = sma200_series[-1]
    pct50 = ((last / s50 - 1) * 100) if s50 else None
    pct200 = ((last / s200 - 1) * 100) if s200 else None
    stacked_bull = bool(s50 and s200 and last > s50 > s200)
    stacked_bear = bool(s50 and s200 and last < s50 < s200)

    # Look for a cross in the trailing 20 bars.
    golden = _cross_recent(sma50_series, sma200_series, direction="up", lookback=20)
    death = _cross_recent(sma50_series, sma200_series, direction="down", lookback=20)

    return IndicatorSMA(
        sma50=round(s50, 2) if s50 else None,
        sma200=round(s200, 2) if s200 else None,
        price_vs_sma50_pct=round(pct50, 2) if pct50 is not None else None,
        price_vs_sma200_pct=round(pct200, 2) if pct200 is not None else None,
        stacked_bullish=stacked_bull,
        stacked_bearish=stacked_bear,
        golden_cross_recent=golden,
        death_cross_recent=death,
    )


def _cross_recent(
    fast: list[Optional[float]],
    slow: list[Optional[float]],
    direction: str,
    lookback: int,
) -> bool:
    n = len(fast)
    start = max(1, n - lookback)
    for i in range(start, n):
        f0, f1 = fast[i - 1], fast[i]
        s0, s1 = slow[i - 1], slow[i]
        if None in (f0, f1, s0, s1):
            continue
        if direction == "up" and f0 <= s0 and f1 > s1:
            return True
        if direction == "down" and f0 >= s0 and f1 < s1:
            return True
    return False


def _rsi_block(series: list[Optional[float]]) -> IndicatorRSI:
    val = series[-1]
    if val is None:
        return IndicatorRSI(value=None, state="unknown")
    if val >= 70:
        state = "overbought"
    elif val <= 30:
        state = "oversold"
    elif val >= 55:
        state = "bullish"
    elif val <= 45:
        state = "bearish"
    else:
        state = "neutral"
    return IndicatorRSI(value=round(val, 2), state=state)


def _macd_block(
    macd_line: list[Optional[float]],
    signal_line: list[Optional[float]],
    hist: list[Optional[float]],
) -> IndicatorMACD:
    m = macd_line[-1]
    s = signal_line[-1]
    h = hist[-1]
    bull_cross = _cross_recent(macd_line, signal_line, "up", lookback=5)
    bear_cross = _cross_recent(macd_line, signal_line, "down", lookback=5)

    direction = "unknown"
    recent_hist = [x for x in hist[-5:] if x is not None]
    if len(recent_hist) >= 3:
        if recent_hist[-1] > recent_hist[0] + 1e-6:
            direction = "rising"
        elif recent_hist[-1] < recent_hist[0] - 1e-6:
            direction = "falling"
        else:
            direction = "flat"

    return IndicatorMACD(
        macd=round(m, 3) if m is not None else None,
        signal=round(s, 3) if s is not None else None,
        histogram=round(h, 3) if h is not None else None,
        bullish_cross_recent=bull_cross,
        bearish_cross_recent=bear_cross,
        histogram_direction=direction,
    )


def _atr_block(
    atr_series: list[Optional[float]], closes: list[float]
) -> IndicatorATR:
    v = atr_series[-1]
    if v is None:
        return IndicatorATR(value=None, pct_of_price=None)
    return IndicatorATR(
        value=round(v, 3),
        pct_of_price=round(v / closes[-1] * 100, 2),
    )


# ---------------------------------------------------------------------------
# Composite score + verdict
# ---------------------------------------------------------------------------


def _score_and_verdict(
    symbol: str,
    timeframe: Timeframe,
    pa: PriceAction,
    vs: VolumeStats,
    sb: IndicatorSMA,
    rsi_b: IndicatorRSI,
    macd_b: IndicatorMACD,
) -> tuple[float, str, float, str]:
    score = 0.0
    # Trend (weight 0.35)
    if pa.trend == "uptrend":
        score += 0.35
    elif pa.trend == "downtrend":
        score -= 0.35

    # SMA stack (weight 0.20)
    if sb.stacked_bullish:
        score += 0.20
    elif sb.stacked_bearish:
        score -= 0.20
    if sb.golden_cross_recent:
        score += 0.08
    if sb.death_cross_recent:
        score -= 0.08

    # RSI (weight 0.10)
    if rsi_b.state == "bullish":
        score += 0.08
    elif rsi_b.state == "bearish":
        score -= 0.08
    elif rsi_b.state == "overbought":
        score -= 0.05  # contrarian tilt — overbought rarely persists cleanly
    elif rsi_b.state == "oversold":
        score += 0.05

    # MACD (weight 0.20)
    if macd_b.bullish_cross_recent:
        score += 0.15
    if macd_b.bearish_cross_recent:
        score -= 0.15
    if macd_b.histogram_direction == "rising":
        score += 0.05
    elif macd_b.histogram_direction == "falling":
        score -= 0.05

    # Volume (weight 0.10)
    if vs.trending_up and pa.change_pct > 0:
        score += 0.06
    if vs.trending_up and pa.change_pct < 0:
        score -= 0.06
    if vs.unusual and vs.ratio > 1.5 and pa.change_pct > 0:
        score += 0.04

    score = max(-1.0, min(1.0, score))

    if score >= 0.3:
        verdict = "BULLISH"
    elif score <= -0.3:
        verdict = "BEARISH"
    else:
        verdict = "NEUTRAL"

    conviction = min(1.0, abs(score) * 1.25 + 0.25)
    headline = _headline(symbol, timeframe, verdict, pa, sb, macd_b, rsi_b)
    return score, verdict, conviction, headline


def _headline(
    symbol: str,
    timeframe: Timeframe,
    verdict: str,
    pa: PriceAction,
    sb: IndicatorSMA,
    macd_b: IndicatorMACD,
    rsi_b: IndicatorRSI,
) -> str:
    tf_word = {"1h": "intraday", "4h": "swing", "daily": "daily", "weekly": "weekly"}[timeframe]
    direction = {"BULLISH": "Bullish", "BEARISH": "Bearish", "NEUTRAL": "Range-bound"}[verdict]
    tags: list[str] = []
    if sb.stacked_bullish:
        tags.append("stacked 20/50/200 SMAs")
    elif sb.stacked_bearish:
        tags.append("inverted SMA stack")
    if macd_b.bullish_cross_recent:
        tags.append("fresh MACD bull cross")
    elif macd_b.bearish_cross_recent:
        tags.append("fresh MACD bear cross")
    if rsi_b.state in ("overbought", "oversold"):
        tags.append(f"RSI {rsi_b.state}")
    tag_s = f" — {', '.join(tags)}" if tags else ""
    return f"{symbol} {tf_word} view: {direction}{tag_s}."


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------


def _compose_narrative(
    *,
    meta: dict,
    timeframe: Timeframe,
    data: OHLCV,
    price_action: PriceAction,
    volume_stats: VolumeStats,
    sma_block: IndicatorSMA,
    rsi_block: IndicatorRSI,
    macd_block: IndicatorMACD,
    atr_block: IndicatorATR,
    verdict: str,
    conviction: float,
) -> str:
    s = meta["symbol"]
    lines: list[str] = []
    tf_human = {"1h": "1-hour", "4h": "4-hour", "daily": "daily", "weekly": "weekly"}[timeframe]

    # Paragraph 1 — setup + trend.
    p1 = (
        f"On the {tf_human} timeframe, {s} is trading at "
        f"${price_action.last:,.2f} ({price_action.change_pct:+.2f}% on the last bar, "
        f"{price_action.change_pct_period:+.2f}% across the displayed window). "
    )
    if price_action.trend == "uptrend":
        p1 += "Structure is constructive — higher highs and higher lows. "
    elif price_action.trend == "downtrend":
        p1 += "Structure is bearish — lower highs and lower lows. "
    else:
        p1 += "Price is ranging; no clear trend dominates. "
    if price_action.patterns:
        p1 += "Notable pattern cues: " + "; ".join(price_action.patterns) + ". "
    lines.append(p1)

    # Paragraph 2 — support/resistance.
    s_txt = (
        "Support: " + ", ".join(f"${x:.2f}" for x in price_action.supports)
        if price_action.supports else "Support: none identified in range"
    )
    r_txt = (
        "Resistance: " + ", ".join(f"${x:.2f}" for x in price_action.resistances)
        if price_action.resistances else "Resistance: none identified in range"
    )
    lines.append(f"{s_txt}. {r_txt}.")

    # Paragraph 3 — volume.
    vol_line = (
        f"Volume is {volume_stats.ratio:.2f}× the 20-bar average "
        f"({volume_stats.latest:,.0f} vs {volume_stats.avg_20:,.0f}). "
    )
    if volume_stats.unusual and volume_stats.ratio > 1.5 and price_action.change_pct > 0:
        vol_line += "Elevated volume is confirming the up-move."
    elif volume_stats.unusual and volume_stats.ratio > 1.5 and price_action.change_pct < 0:
        vol_line += "Elevated volume on down bars — distributive."
    elif volume_stats.trending_up:
        vol_line += "Volume is trending above average — participation improving."
    elif volume_stats.ratio < 0.6:
        vol_line += "Volume is thin — moves here are less reliable."
    else:
        vol_line += "Volume is unremarkable."
    lines.append(vol_line)

    # Paragraph 4 — SMAs.
    if sma_block.sma50 and sma_block.sma200:
        stack_txt = (
            "stacked bullishly (price > 50 > 200)" if sma_block.stacked_bullish
            else "inverted (price < 50 < 200)" if sma_block.stacked_bearish
            else "mixed"
        )
        cross_txt = (
            " A recent golden cross (50 crossing 200 from below) adds weight to the bull case."
            if sma_block.golden_cross_recent
            else " A recent death cross (50 crossing 200 from above) reinforces bearish bias."
            if sma_block.death_cross_recent
            else ""
        )
        lines.append(
            f"Moving averages are {stack_txt}: SMA50 ${sma_block.sma50:.2f} "
            f"({_fmt_pct(sma_block.price_vs_sma50_pct)} vs price), "
            f"SMA200 ${sma_block.sma200:.2f} "
            f"({_fmt_pct(sma_block.price_vs_sma200_pct)} vs price).{cross_txt}"
        )
    elif sma_block.sma50:
        lines.append(
            f"SMA50 is ${sma_block.sma50:.2f} ({_fmt_pct(sma_block.price_vs_sma50_pct)} vs price); "
            f"SMA200 lookback not yet available on this timeframe."
        )

    # Paragraph 5 — momentum (RSI + MACD).
    mo: list[str] = []
    if rsi_block.value is not None:
        mo.append(f"RSI(14) is {rsi_block.value:.1f} — {rsi_block.state}")
    if macd_block.macd is not None and macd_block.signal is not None:
        cross = (
            " with a fresh bullish cross"
            if macd_block.bullish_cross_recent
            else " with a fresh bearish cross"
            if macd_block.bearish_cross_recent
            else ""
        )
        mo.append(
            f"MACD {macd_block.macd:.3f} vs signal {macd_block.signal:.3f} "
            f"(histogram {macd_block.histogram_direction}{cross})"
        )
    if mo:
        lines.append("Momentum: " + "; ".join(mo) + ".")

    # Paragraph 6 — volatility / sizing.
    if atr_block.value is not None and atr_block.pct_of_price is not None:
        lines.append(
            f"ATR(14) is ${atr_block.value:.2f} ({atr_block.pct_of_price:.2f}% of spot) — "
            f"useful for stop placement and position sizing."
        )

    # Paragraph 7 — verdict.
    verdict_sentences = {
        "BULLISH": (
            f"Net view: BULLISH on the {tf_human} timeframe, conviction "
            f"{int(conviction*100)}%. Buyers remain in control and the indicator "
            "stack is confirming. Recommended expression: a clean long call, "
            "strike and expiry in the ticket below."
        ),
        "BEARISH": (
            f"Net view: BEARISH on the {tf_human} timeframe, conviction "
            f"{int(conviction*100)}%. Sellers are in control. Recommended "
            "expression: a clean long put, strike and expiry in the ticket below."
        ),
        "NEUTRAL": (
            f"Net view: NEUTRAL on the {tf_human} timeframe, conviction "
            f"{int(conviction*100)}%. Signals are mixed — the recommended "
            "directional call/put below is a lower-conviction play sized "
            "accordingly (wait for cleaner structure before adding size)."
        ),
    }
    lines.append(verdict_sentences[verdict])

    return "\n\n".join(lines)


def _fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.2f}%"


# ---------------------------------------------------------------------------
# Market / sector context
# ---------------------------------------------------------------------------


def _market_context_text(symbol: str, sector: str, source: str) -> str:
    src_note = (
        "Context below is a static characterization; the live VIX, breadth, "
        "and sector ETFs would be injected by the data layer in production."
        if source == "synthetic"
        else "Broad-market reads should be sourced from the ^VIX, ^SPX, and "
        "sector ETFs in live mode; the text below is a static characterization."
    )
    sector_map = {
        "Semiconductors / AI": (
            "AI-infrastructure demand is the dominant narrative. Semis led the broader "
            "tape higher on the 2024-2025 run; every pullback gets bid while capex "
            "revisions hold."
        ),
        "Software / AI": (
            "Software is bifurcated — AI-first names extend while legacy SaaS compresses. "
            "Earnings revisions and stock-based-comp dilution are the key screens."
        ),
        "Software / Cloud": (
            "Cloud growth has re-accelerated. Hyperscaler capex is flowing into AI "
            "infra; watch Azure/AWS/GCP growth prints."
        ),
        "Communication Services": (
            "Ad-sensitive; follow consumer spend and AI-monetization commentary. "
            "Mega-caps command most of the sector weight."
        ),
        "Autos / EV": (
            "EV demand softened through 2024; margins remain the story. China "
            "competition and Model refresh cycles drive sentiment spikes."
        ),
        "Consumer Electronics": (
            "iPhone unit cycle dominates. Services margins are the quiet tailwind. "
            "China exposure is the recurring risk."
        ),
        "Consumer / Cloud": (
            "Retail plus AWS — margin mix matters more than GMV. Advertising is "
            "the third leg."
        ),
        "Cybersecurity": (
            "Platform consolidation favors incumbents. Ongoing breach cadence "
            "keeps secular budget growth intact."
        ),
        "Index": (
            "SPY is the broad-market reference. Watch VIX, 10-year yields, "
            "and breadth (% of constituents above 50d) for regime cues."
        ),
        "Nuclear / Energy": (
            "Nuclear / SMR has re-rated on AI-driven power demand. Thinly "
            "traded names carry high IV — size small and prefer defined-risk."
        ),
        "Quantum": (
            "Highly speculative and headline-driven. IV is elevated; expect "
            "fast, wide moves. Treat as venture-style position sizing."
        ),
    }
    sector_text = sector_map.get(sector, "No specific sector note.")
    broad = (
        "Broad market: SPY trend remains the dominant gravity. A weakening SPY "
        "plus rising VIX compounds downside in single names; a firm SPY plus "
        "falling VIX accelerates upside in leadership pockets."
    )
    return f"{broad} {sector_text} {src_note}"


# ---------------------------------------------------------------------------
# Options suggestion — clean directional calls / puts only
# ---------------------------------------------------------------------------


def _build_options_suggestion(
    *,
    sym: str,
    spot: float,
    closes: list[float],
    atr_block: IndicatorATR,
    price_action: PriceAction,
    verdict: str,
    conviction: float,
    composite_score: float,
    timeframe: Timeframe,
) -> OptionsSuggestion:
    """Build a single directional ticket.

    Per product spec, multi-leg structures (spreads / straddles / condors)
    are intentionally excluded — every ticker gets a clean long call if the
    composite score is non-negative, or a long put if negative. Strike is
    chosen to be slightly OTM, nudged to the nearest structural level when
    one sits within the expected move.
    """
    rv = _annualized_vol(closes)
    atr_annual = (atr_block.pct_of_price or 0) / 100 * math.sqrt(252)
    iv_est = max(0.12, (rv + atr_annual) / 2) if atr_annual else max(0.15, rv)
    # Clamp IV to a plausible ceiling scaled by ticker default. VIX-style
    # extremes are still possible (default IV 1.10) but not arbitrary.
    iv_est = min(iv_est, DEFAULT_IV.get(sym, 0.40) * 1.6)
    iv_est = min(iv_est, 1.50)  # hard ceiling — 150% annualized

    dte_default = {"1h": 10, "4h": 21, "daily": 35, "weekly": 60}[timeframe]

    contract_type: str = "call" if composite_score >= 0 else "put"

    strike = _pick_strike(
        contract_type=contract_type,
        spot=spot,
        iv=iv_est,
        dte=dte_default,
        conviction=conviction,
        price_action=price_action,
    )

    headline = (
        f"BUY {contract_type.upper()} — "
        f"${_fmt_strike(strike)} strike, ~{dte_default} DTE"
    )

    trade_plan = _build_trade_plan(
        sym=sym,
        spot=spot,
        iv=iv_est,
        dte=dte_default,
        contract_type=contract_type,
        strike=strike,
        atr_block=atr_block,
        price_action=price_action,
        headline=headline,
        conviction=conviction,
        composite_score=composite_score,
        verdict=verdict,
    )

    direction_word = "bullish" if contract_type == "call" else "bearish"
    rationale = (
        f"Clean directional {contract_type} aligned with the {direction_word} "
        f"read (composite {composite_score:+.2f}, verdict {verdict}, "
        f"conviction {int(conviction*100)}%). IV estimate "
        f"{int(iv_est*100)}% over {dte_default}D."
    )

    return OptionsSuggestion(
        headline=headline,
        contract_type=contract_type,  # type: ignore[arg-type]
        strike=strike,
        expiry_dte=dte_default,
        rationale=rationale,
        trade_plan=trade_plan,
        recommendations=[],
    )


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------


def _pick_strike(
    *,
    contract_type: str,
    spot: float,
    iv: float,
    dte: int,
    conviction: float,
    price_action: PriceAction,
) -> float:
    """Pick a sensible single-leg strike — slightly OTM, snap to structural.

    - Base OTM fraction scales with conviction: low-conviction trades start
      near-ATM, high-conviction moves ~3% OTM for more leverage.
    - If a structural level (resistance for calls, support for puts) sits
      within ~0.8 × expected-move and within ~6% of spot, we bias the strike
      there so the option is priced against a real technical magnet.
    """
    T = max(1, dte) / 365.0
    one_sigma = spot * iv * math.sqrt(T)

    conv = max(0.0, min(1.0, conviction))
    # 0.25 conviction → 0.5% OTM, 1.0 conviction → 3% OTM.
    otm_frac = 0.005 + max(0.0, (conv - 0.25)) / 0.75 * 0.025

    if contract_type == "call":
        base = spot * (1 + otm_frac)
        upper_limit = min(spot + 0.8 * one_sigma, spot * 1.06)
        for r in price_action.resistances:
            if spot * 1.002 < r <= upper_limit:
                base = r
                break
    else:  # put
        base = spot * (1 - otm_frac)
        lower_limit = max(spot - 0.8 * one_sigma, spot * 0.94)
        for s in price_action.supports:
            if lower_limit <= s < spot * 0.998:
                base = s
                break

    return _round_strike(base)


def _round_strike(price: float) -> float:
    """Round to a standard-listed-strike increment."""
    if price >= 100:
        return float(round(price))          # $1 strikes
    if price >= 25:
        return float(round(price))          # $1 strikes
    if price >= 5:
        return round(price * 2) / 2         # $0.50 strikes
    return round(price * 4) / 4             # $0.25 strikes


def _fmt_strike(k: float) -> str:
    """Display helper that drops decimals when they aren't useful."""
    if k >= 100 or abs(k - round(k)) < 1e-6:
        return f"{k:.0f}"
    return f"{k:.2f}"


# ---------------------------------------------------------------------------
# TradePlan — the concrete "buy this" trade ticket
# ---------------------------------------------------------------------------


def _build_trade_plan(
    *,
    sym: str,
    spot: float,
    iv: float,
    dte: int,
    contract_type: str,
    strike: float,
    atr_block: IndicatorATR,
    price_action: PriceAction,
    headline: str,
    conviction: float,
    composite_score: float,
    verdict: str,
) -> TradePlan:
    T = max(1, dte) / 365.0
    one_sigma_usd = round(spot * iv * math.sqrt(T), 2)
    one_sigma_pct = round(iv * math.sqrt(T) * 100, 2)

    # Absolute expiry date — roll forward to the next Friday after ``dte`` days.
    expiry_dt = date.today() + timedelta(days=dte)
    while expiry_dt.weekday() != 4:  # 4 == Friday
        expiry_dt += timedelta(days=1)
    expiry_date_iso = expiry_dt.isoformat()

    # Black-Scholes mid as an estimated premium.
    try:
        premium: Optional[float] = round(
            black_scholes_price(
                S=spot, K=strike, T=T, r=0.045, sigma=iv, q=0.0,
                option_type=contract_type,
            ),
            2,
        )
    except Exception:
        premium = None
    cost = round(premium * 100, 2) if premium is not None else None
    break_even: Optional[float] = None
    if premium is not None:
        break_even = round(
            strike + premium if contract_type == "call" else strike - premium,
            2,
        )

    # ---- Target price ------------------------------------------------------
    # Preferred: first structural level in the direction of the trade that's
    # within ~1.5σ of spot (so we don't anchor to a distant resistance that
    # would inflate RR). Otherwise: spot ± 1σ.
    max_target_distance = 1.5 * one_sigma_usd
    if contract_type == "call":
        above = [
            lvl for lvl in price_action.resistances
            if spot * 1.005 < lvl <= spot + max_target_distance
        ]
        target_price = above[0] if above else round(spot + one_sigma_usd, 2)
    else:
        below = [
            lvl for lvl in price_action.supports
            if spot - max_target_distance <= lvl < spot * 0.995
        ]
        target_price = below[0] if below else round(spot - one_sigma_usd, 2)

    # ---- Stop loss (thesis invalidation on the underlying) -----------------
    # Enforce a minimum stop distance (max of 1×ATR or 1.5% of spot) so
    # vanishingly-close structural levels don't produce absurd RR numbers.
    atr_val = atr_block.value or (spot * 0.02)
    min_stop_distance = max(atr_val, spot * 0.015)
    if contract_type == "call":
        atr_stop = spot - 2 * atr_val
        structural = max(price_action.supports) if price_action.supports else None
        raw_stop = max(atr_stop, structural) if structural is not None else atr_stop
        # Push the stop at least min_stop_distance below spot.
        stop_loss = round(min(raw_stop, spot - min_stop_distance), 2)
    else:
        atr_stop = spot + 2 * atr_val
        structural = min(price_action.resistances) if price_action.resistances else None
        raw_stop = min(atr_stop, structural) if structural is not None else atr_stop
        # Push the stop at least min_stop_distance above spot.
        stop_loss = round(max(raw_stop, spot + min_stop_distance), 2)

    # ---- Risk / reward on the underlying ----------------------------------
    risk_reward: Optional[float] = None
    if contract_type == "call" and spot > stop_loss and target_price > spot:
        risk_reward = round((target_price - spot) / (spot - stop_loss), 2)
    elif contract_type == "put" and spot < stop_loss and target_price < spot:
        risk_reward = round((spot - target_price) / (stop_loss - spot), 2)

    # ---- Rationale ---------------------------------------------------------
    prem_txt = f"${premium:.2f}/sh (${cost:.0f}/contract)" if premium else "premium pending"
    be_txt = f"BE ${break_even:.2f}" if break_even is not None else "BE pending"
    tgt_txt = f"target ${target_price:.2f}" if target_price is not None else "target 1σ"
    stop_txt = f"invalidate ${stop_loss:.2f}" if stop_loss is not None else "2×ATR stop"
    rr_txt = f" · RR {risk_reward:.2f}×" if risk_reward else ""

    special_note = _special_ticker_note(sym)
    rationale = (
        f"{headline}. Expected move over {dte}D ±${one_sigma_usd:.2f} "
        f"({one_sigma_pct:.2f}%). Est. premium {prem_txt}, {be_txt}. "
        f"Underlying {tgt_txt}, {stop_txt}{rr_txt}. "
        f"Score {composite_score:+.2f}, verdict {verdict}, conviction "
        f"{int(conviction*100)}%.{special_note}"
    )

    return TradePlan(
        contract_type=contract_type,  # type: ignore[arg-type]
        strike=round(strike, 2),
        expiry_date=expiry_date_iso,
        expiry_dte=dte,
        estimated_premium=premium,
        cost_per_contract=cost,
        spot_at_entry=round(spot, 2),
        break_even=break_even,
        target_price=round(target_price, 2) if target_price is not None else None,
        stop_loss=stop_loss,
        one_sigma_move_usd=one_sigma_usd,
        one_sigma_move_pct=one_sigma_pct,
        risk_reward=risk_reward,
        rationale=rationale,
    )


def _special_ticker_note(sym: str) -> str:
    """Caveats for instruments that don't behave like ordinary equities."""
    if sym == "VIX":
        return (
            " Note: VIX options are cash-settled on /VX futures (not spot), "
            "so retail pricing/Greeks differ from this BS estimate — treat "
            "the ticket as directional guidance, not an executable quote."
        )
    if sym in ("SPY", "QQQ"):
        return " SPY/QQQ are deeply liquid; tighter spreads, assume near-mid fills."
    return ""


# ---------------------------------------------------------------------------
# Vol helpers
# ---------------------------------------------------------------------------


def _annualized_vol(closes: list[float]) -> float:
    if len(closes) < 30:
        return 0.25
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    tail = rets[-60:]
    mean = sum(tail) / len(tail)
    var = sum((x - mean) ** 2 for x in tail) / (len(tail) - 1)
    return math.sqrt(var) * math.sqrt(252)


def _rolling_annualized_vol(closes: list[float], window: int) -> list[float]:
    if len(closes) < window + 2:
        return []
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    out: list[float] = []
    for i in range(window, len(rets)):
        w = rets[i - window : i]
        mean = sum(w) / window
        var = sum((x - mean) ** 2 for x in w) / (window - 1)
        out.append(math.sqrt(var) * math.sqrt(252))
    return out
