"""Factor breakdown derived from ``ReportOut`` — shared by API and analyst UI."""

from __future__ import annotations

from ..models import FactorOut


def bull_bear_balance_percent(composite_score: float) -> tuple[float, float]:
    """Map composite score in [-1, 1] to a 100-point bull vs bear split.

    -1 ⇒ 0% bull / 100% bear; +1 ⇒ 100% bull / 0% bear; 0 ⇒ 50%/50%.
    """
    bull = round((composite_score + 1.0) / 2.0 * 100.0, 1)
    bull = max(0.0, min(100.0, bull))
    bear = round(100.0 - bull, 1)
    return bull, bear


def derive_factors_from_report(report: "ReportOut") -> list[FactorOut]:  # noqa: F821
    """Expose the deterministic components behind conviction (ticker-detail parity)."""

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
    adx_o = report.adx
    if (
        adx_o.trend_strength in ("moderate", "strong")
        and adx_o.value is not None
        and adx_o.value >= 25.0
    ):
        if adx_o.directional_bias == "bullish":
            trend_score += 0.15
            trend_notes.append("+DI > −DI (ADX confirms upside trend strength)")
        elif adx_o.directional_bias == "bearish":
            trend_score -= 0.15
            trend_notes.append("−DI > +DI (ADX confirms downside trend strength)")
    trend_score = max(-1.0, min(1.0, trend_score))
    trend_note = ", ".join(trend_notes) if trend_notes else f"price action: {pa_trend}"

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
        mom -= 0.3   # contrarian: overbought = mean-reversion risk, not momentum add
        mom_notes.append(f"RSI overbought ({rsi.value:.0f})")
    elif rsi.state == "oversold":
        mom += 0.3   # contrarian: oversold = bounce potential
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
        FactorOut(name="Trend", score=round(trend_score, 2), note=trend_note),
        FactorOut(name="Momentum", score=round(mom, 2), note=mom_note),
        FactorOut(name="Mean reversion", score=round(mr, 2), note=mr_note),
        FactorOut(name="Volume flow", score=round(vol_score, 2), note=vol_note),
    ]
