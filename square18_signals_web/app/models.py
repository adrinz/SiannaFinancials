"""Pydantic response models for the web API."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field


class RegimeOut(BaseModel):
    label: str
    vix: float
    vix_change: float
    breadth_pct_above_50d: float
    put_call_ratio: float
    trend_score: float


class CountsOut(BaseModel):
    universe_size: int
    scanned: int
    longs: int
    shorts: int
    holds: int


class FactorOut(BaseModel):
    name: str
    score: float
    note: str


class TickerRowOut(BaseModel):
    """Row-sized payload for the dashboard table."""

    symbol: str
    name: str
    sector: str
    price: float
    change_pct: float
    signal: Literal["Buy", "Sell", "Hold"]
    direction: Literal["bull", "bear", "neutral"]
    composite_score: float
    confidence: float
    rsi: float
    iv: float
    iv_rank: float
    iv_percentile: float
    dte_pref: int
    earnings_in_window: bool


class LegOut(BaseModel):
    kind: Literal["call", "put", "stock"]
    side: Literal["long", "short"]
    strike: float | None = None
    premium: float
    quantity: float


class StrategyOut(BaseModel):
    name: str
    legs: list[LegOut]


class MetricsOut(BaseModel):
    net_debit: float  # > 0 debit, < 0 credit
    max_gain: float | None  # null == unbounded
    max_loss: float | None
    breakevens: list[float]
    probability_of_profit: float


class RecommendationOut(BaseModel):
    strategy: StrategyOut
    metrics: MetricsOut
    rationale: str
    fit_score: float
    tags: list[str]


class ExpectedMoveOut(BaseModel):
    one_sigma_usd: float
    one_sigma_pct: float


class TickerDetailOut(BaseModel):
    row: TickerRowOut
    factors: list[FactorOut]
    price_30d: list[float]
    expected_move: ExpectedMoveOut
    recommendations: list[RecommendationOut]


class RegimeEnvelope(BaseModel):
    regime: RegimeOut
    counts: CountsOut
    last_scan_iso: str


def clamp_inf(x: float) -> float | None:
    """JSON can't carry ±inf; map to null so the UI can render '∞'."""
    if math.isinf(x) or math.isnan(x):
        return None
    return x
