"""Unit tests for verdict-driven stock_strategy and equity_signal_warnings."""
from __future__ import annotations

from app.analyst.models import (
    IndicatorATR,
    IndicatorRSI,
    OptionsFlowOut,
    PriceAction,
)
from app.analyst.factors import equity_direction_reason
from app.analyst.report import (
    _build_stock_strategy,
    _derive_equity_signal_warnings,
    _stock_swing_calendar_days,
    _swing_structure_patterns,
    _underlying_target_stop_rr,
)
from app.models import FactorOut


def test_underlying_target_stop_long():
    pa = PriceAction(
        last=100.0,
        change_pct=0.0,
        change_pct_period=0.0,
        supports=[94.0, 97.0],
        resistances=[103.0, 108.0],
        trend="uptrend",
        patterns=[],
    )
    atr = IndicatorATR(value=2.0, pct_of_price=2.0, regime="normal")
    one_sigma = 5.0
    tgt, stp, rr = _underlying_target_stop_rr(
        spot=100.0,
        atr_block=atr,
        price_action=pa,
        direction_long=True,
        one_sigma_usd=one_sigma,
    )
    assert tgt > 100.0
    assert stp < 100.0
    assert rr is not None and rr > 0


def test_stock_strategy_bullish_long_geometry():
    pa = PriceAction(
        last=100.0,
        change_pct=1.0,
        change_pct_period=2.0,
        supports=[96.0, 98.5],
        resistances=[102.0, 105.0],
        trend="uptrend",
        patterns=[],
    )
    atr = IndicatorATR(value=1.5, pct_of_price=1.5, regime="normal")
    rsi = IndicatorRSI(value=55.0, state="bullish")
    closes = [90.0 + i * 0.5 for i in range(80)]
    ss = _build_stock_strategy(
        sym="TEST",
        spot=100.0,
        closes=closes,
        atr_block=atr,
        price_action=pa,
        verdict="BULLISH",
        conviction=0.6,
        composite_score=0.4,
        timeframe="daily",
        rsi_block=rsi,
        direction_summary="Bullish — unit test headline stub.",
        direction_bullets=["Trend — up", "Momentum — supportive"],
    )
    assert ss.action == "buy"
    assert "Bullish" in ss.direction_summary
    assert ss.action_display == "BUY"
    assert ss.buy_price is not None and ss.buy_price == ss.entry.price
    assert ss.sell_take_profit_price == ss.take_profit
    assert ss.sell_stop_price == ss.stop_loss
    assert ss.short_entry_price is None
    assert isinstance(ss.chart_patterns, list)
    assert ss.take_profit is not None and ss.take_profit > 100.0
    assert ss.stop_loss is not None and ss.stop_loss < 100.0


def test_stock_strategy_neutral_no_directional_levels():
    pa = PriceAction(
        last=100.0,
        change_pct=0.0,
        change_pct_period=0.0,
        supports=[98.0],
        resistances=[102.0],
        trend="range",
        patterns=[],
    )
    atr = IndicatorATR(value=1.0, pct_of_price=1.0, regime="normal")
    rsi = IndicatorRSI(value=50.0, state="neutral")
    closes = [95.0] * 80
    ss = _build_stock_strategy(
        sym="TEST",
        spot=100.0,
        closes=closes,
        atr_block=atr,
        price_action=pa,
        verdict="NEUTRAL",
        conviction=0.35,
        composite_score=0.05,
        timeframe="daily",
        rsi_block=rsi,
        direction_summary="Neutral — unit test.",
        direction_bullets=["Trend — range (score +0.00)", "Momentum — quiet (score +0.00)"],
    )
    assert ss.action == "hold_wait"
    assert ss.action_display == "WAIT"
    assert ss.take_profit is None
    assert ss.stop_loss is None
    assert ss.buy_price is None
    assert ss.chart_patterns == pa.patterns


def test_equity_warnings_skip_options_jargon():
    sw = [
        "Weekend theta: entering on Friday means paying decay.",
        "Wait for a confirmed directional breakout before committing new risk.",
    ]
    eq = _derive_equity_signal_warnings(
        sw,
        earnings_soon=None,
        stock_swing_days=12,
        flow_out=OptionsFlowOut(source="unavailable"),
    )
    assert not any("theta" in x.lower() for x in eq)
    assert any("adding stock" in x for x in eq)


def test_swing_days_map():
    assert _stock_swing_calendar_days("1h") < _stock_swing_calendar_days("weekly")


def test_swing_structure_patterns_bull_trap():
    n = 36
    closes = [100.0] * n
    highs = [100.5] * n
    lows = [99.5] * n
    for i in range(n - 30, n - 8):
        highs[i] = 101.0
        closes[i] = 100.0
    highs[-2] = 103.0
    closes[-1] = 100.0
    lows[-1] = 99.0
    pats = _swing_structure_patterns(closes, highs, lows, "range", closes[-1])
    assert any("bull trap" in p.lower() for p in pats)


def test_equity_direction_reason_bull_lists_drivers():
    fac = [
        FactorOut(name="Trend", score=0.7, note="50d above 200d"),
        FactorOut(name="Momentum", score=0.5, note="RSI 55, MACD hist rising"),
        FactorOut(name="Mean reversion", score=-0.1, note="+2.0% vs 50d (in band)"),
        FactorOut(name="Volume flow", score=0.2, note="normal (1.0× avg)"),
    ]
    summary, bullets = equity_direction_reason(
        "BULLISH", fac, "FOO daily view: Bullish — stacked SMAs.", 0.42, 0.62
    )
    assert "Bullish" in summary
    assert "FOO" in summary
    assert len(bullets) >= 2
    assert any(b.startswith("Trend") for b in bullets)
