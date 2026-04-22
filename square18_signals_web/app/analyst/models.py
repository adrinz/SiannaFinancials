"""Pydantic response models for the analyst endpoints."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from ..models import RecommendationOut  # re-use existing recommender payload


Verdict = Literal["BULLISH", "BEARISH", "NEUTRAL"]


class IndicatorSMA(BaseModel):
    sma50: Optional[float]
    sma200: Optional[float]
    price_vs_sma50_pct: Optional[float]
    price_vs_sma200_pct: Optional[float]
    stacked_bullish: bool
    stacked_bearish: bool
    golden_cross_recent: bool
    death_cross_recent: bool


class IndicatorRSI(BaseModel):
    value: Optional[float]
    state: Literal["overbought", "oversold", "bullish", "bearish", "neutral", "unknown"]


class IndicatorMACD(BaseModel):
    macd: Optional[float]
    signal: Optional[float]
    histogram: Optional[float]
    bullish_cross_recent: bool
    bearish_cross_recent: bool
    histogram_direction: Literal["rising", "falling", "flat", "unknown"]


class IndicatorATR(BaseModel):
    value: Optional[float]
    pct_of_price: Optional[float]


class VolumeStats(BaseModel):
    latest: float
    avg_20: float
    ratio: float  # latest / avg_20
    unusual: bool  # ratio > 1.5 or < 0.6
    trending_up: bool  # 5-bar mean > 20-bar mean


class PriceAction(BaseModel):
    last: float
    change_pct: float
    change_pct_period: float  # since start of displayed window
    supports: list[float]
    resistances: list[float]
    trend: Literal["uptrend", "downtrend", "range"]
    patterns: list[str]


class TradePlan(BaseModel):
    """Ready-to-read trade ticket for the primary recommended contract.

    This is the deliverable the user asked for — a concrete call/put with
    strike, expiry date, estimated premium, and explicit target / stop
    levels derived from the same technicals used to form the verdict.
    """

    contract_type: Literal["call", "put"]
    strike: float
    expiry_date: str  # ISO date, e.g. "2026-05-22"
    expiry_dte: int
    estimated_premium: Optional[float]        # per-share mid (Black-Scholes estimate)
    cost_per_contract: Optional[float]        # 100 × premium
    spot_at_entry: float
    break_even: Optional[float]
    target_price: Optional[float]             # technical price objective for the underlying
    stop_loss: Optional[float]                # underlying stop level (for "thesis invalidated")
    one_sigma_move_usd: Optional[float]       # expected move over DTE
    one_sigma_move_pct: Optional[float]
    risk_reward: Optional[float]              # (target - spot) / (spot - stop) for calls; mirrored for puts
    rationale: str                            # 1-2 sentence plain-English "why"


class OptionsSuggestion(BaseModel):
    headline: str  # e.g. "BUY CALL — $420 strike, ~35 DTE"
    contract_type: Literal["call", "put"]
    strike: float
    expiry_dte: int
    rationale: str
    trade_plan: TradePlan
    # Retained for API compatibility; now always empty — per user spec we
    # only recommend clean single-leg directional calls or puts.
    recommendations: list[RecommendationOut] = []


class ChartPayload(BaseModel):
    timestamps: list[str]
    close: list[float]
    sma50: list[Optional[float]]
    sma200: list[Optional[float]]


class ReportOut(BaseModel):
    symbol: str
    name: str
    sector: str
    timeframe: Literal["1h", "4h", "daily", "weekly"]
    as_of: str
    source: str  # yfinance | synthetic
    verdict: Verdict
    conviction: float  # 0..1
    composite_score: float  # -1..+1
    headline: str  # one-line summary
    narrative: str  # multi-paragraph written analysis

    price_action: PriceAction
    volume: VolumeStats
    sma: IndicatorSMA
    rsi: IndicatorRSI
    macd: IndicatorMACD
    atr: IndicatorATR

    market_context: str
    options: OptionsSuggestion
    chart: ChartPayload


class OverviewRow(BaseModel):
    symbol: str
    name: str
    sector: str
    last: float
    change_pct: float
    verdict: Verdict
    conviction: float
    composite_score: float  # -1..+1 from _score_and_verdict
    rsi: Optional[float]
    trend: Literal["uptrend", "downtrend", "range"]
    source: str

    # Headline directional recommendation — always populated (call or put).
    rec_contract_type: Literal["call", "put"]
    rec_strike: float
    rec_expiry_date: str
    rec_expiry_dte: int
    rec_premium: Optional[float]
    rec_cost_per_contract: Optional[float]
    rec_break_even: Optional[float]
    rec_target: Optional[float]
    rec_stop: Optional[float]
    rec_risk_reward: Optional[float]


class TickerMeta(BaseModel):
    symbol: str
    name: str
    sector: str
