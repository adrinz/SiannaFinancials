"""Verify trade-ticket math: cost, break-even, target/stop, and underlying R:R."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.analyst.models import IndicatorATR, PriceAction  # noqa: E402
from app.analyst.report import _build_trade_plan  # noqa: E402


@patch("app.analyst.report.yf_option_mid_per_share", return_value=5.25)
def test_call_cost_and_break_even_from_option_mid(_mock_mid: object) -> None:
    """Listed US equity options: 1 contract = 100 shares × premium per share."""
    tp = _build_trade_plan(
        sym="TEST",
        spot=100.0,
        iv=0.28,
        dte=35,
        contract_type="call",
        strike=105.0,
        atr_block=IndicatorATR(value=3.0, pct_of_price=3.0, regime="normal"),
        price_action=PriceAction(
            last=100.0,
            change_pct=0.0,
            change_pct_period=0.0,
            supports=[94.0],
            resistances=[108.0],
            trend="uptrend",
            patterns=[],
        ),
        headline="head",
        conviction=0.5,
        composite_score=0.2,
        verdict="Buy",
        fresh_quotes=False,
    )
    assert tp.estimated_premium == 5.25
    assert tp.cost_per_contract == 525.0
    assert tp.break_even == 110.25  # K + premium (long call)


@patch("app.analyst.report.yf_option_mid_per_share", return_value=5.25)
def test_put_break_even(_mock_mid: object) -> None:
    tp = _build_trade_plan(
        sym="TEST",
        spot=100.0,
        iv=0.28,
        dte=35,
        contract_type="put",
        strike=95.0,
        atr_block=IndicatorATR(value=3.0, pct_of_price=3.0, regime="normal"),
        price_action=PriceAction(
            last=100.0,
            change_pct=0.0,
            change_pct_period=0.0,
            supports=[93.0],
            resistances=[102.0],
            trend="downtrend",
            patterns=[],
        ),
        headline="head",
        conviction=0.5,
        composite_score=-0.2,
        verdict="Sell",
        fresh_quotes=False,
    )
    assert tp.break_even == 89.75  # K − premium (long put)


@patch("app.analyst.report.yf_option_mid_per_share", return_value=5.25)
def test_call_underlying_risk_reward_matches_formula(_mock_mid: object) -> None:
    """R:R is on the stock path: (Tgt−S)/(S−stop). Not option premium P&L."""
    spot = 100.0
    # Target: first resistance in (100.5, spot+1.5σ] → 102
    # Stop: ATR path — atr_val=5 → atr_stop=90; min(raw_stop, 95)=90
    tp = _build_trade_plan(
        sym="TEST",
        spot=spot,
        iv=0.28,
        dte=35,
        contract_type="call",
        strike=105.0,
        atr_block=IndicatorATR(value=5.0, pct_of_price=5.0, regime="elevated"),
        price_action=PriceAction(
            last=spot,
            change_pct=0.0,
            change_pct_period=0.0,
            supports=[80.0],
            resistances=[102.0],
            trend="uptrend",
            patterns=[],
        ),
        headline="head",
        conviction=0.5,
        composite_score=0.2,
        verdict="Buy",
        fresh_quotes=False,
    )
    assert tp.target_price == 102.0
    assert tp.stop_loss == 90.0
    assert tp.risk_reward == 0.2  # (102-100)/(100-90)
