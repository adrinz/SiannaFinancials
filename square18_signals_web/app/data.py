"""In-memory mock data provider for the web app.

Gives the UI realistic enough data to exercise every feature of the
`square18_signals` package — including IV histories deep enough that
`iv_rank` / `iv_percentile` return meaningful numbers. In production,
this module is replaced by a live provider (Polygon / Tradier / yfinance).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Literal

Signal = Literal["Buy", "Sell", "Hold"]
Direction = Literal["bull", "bear", "neutral"]


@dataclass(frozen=True)
class FactorScore:
    name: str
    score: float  # -1..+1
    note: str


@dataclass(frozen=True)
class TickerSnapshot:
    symbol: str
    name: str
    sector: str
    price: float
    change_pct: float
    signal: Signal
    direction: Direction
    composite_score: float  # -1..+1
    confidence: float  # 0..1
    rsi: float
    dte_pref: int
    iv: float  # current ATM IV (annualized)
    iv_history: list[float]  # 252 trailing-day observations
    factors: list[FactorScore]
    price_30d: list[float]
    earnings_in_window: bool = False


@dataclass(frozen=True)
class MarketRegime:
    label: str
    vix: float
    vix_change: float
    breadth_pct_above_50d: float
    put_call_ratio: float
    trend_score: float  # -1..+1


# ---------------------------------------------------------------------------
# Synthetic IV-history generator — mean-reverting around a base level with
# seasonal/earnings bumps. Seeded per-symbol so results are stable.
# ---------------------------------------------------------------------------


def _iv_history(symbol: str, base: float, seed_salt: int = 0) -> list[float]:
    rng = random.Random(hash(symbol) % (2**32) + seed_salt)
    out: list[float] = []
    iv = base
    for i in range(252):
        shock = rng.gauss(0, base * 0.06)
        iv = 0.9 * iv + 0.1 * base + shock
        # Quarterly earnings bumps
        if i % 63 == 0 and i > 0:
            iv += base * 0.4
        iv = max(0.08, iv)
        out.append(iv)
    return out


def _synthesize_price_30d(last: float, direction: Direction) -> list[float]:
    rng = random.Random(int(last * 1000))
    trend = {"bull": 0.0015, "bear": -0.0018, "neutral": 0.0}[direction]
    price = last / (1 + trend * 30)
    out: list[float] = []
    for _ in range(30):
        step = rng.gauss(trend, 0.012)
        price = price * (1 + step)
        out.append(round(price, 2))
    out[-1] = last
    return out


# ---------------------------------------------------------------------------
# Hand-curated "signals today" — realistic enough to demo every code path
# ---------------------------------------------------------------------------


def _build_universe() -> list[TickerSnapshot]:
    specs: list[dict] = [
        dict(
            symbol="NVDA", name="NVIDIA Corp", sector="Semiconductors",
            price=412.83, change_pct=2.14, signal="Buy", direction="bull",
            composite_score=0.78, confidence=0.82, rsi=63.0, dte_pref=35,
            iv=0.32, iv_base=0.36, earnings_in_window=False,
            factors=[
                ("Trend", 0.90, "Price above 20/50/200 SMA, stacked bullishly"),
                ("Momentum", 0.72, "MACD hist rising, RSI 63 (not overbought)"),
                ("Mean reversion", -0.10, "Slight overextension vs VWAP"),
                ("Volume flow", 0.65, "OBV rising, unusual call volume at 420"),
                ("Fundamentals", 0.55, "FCF yield improving, earnings revisions positive"),
                ("Event risk", -0.20, "Earnings in 41d (outside window, OK)"),
            ],
        ),
        dict(
            symbol="AAPL", name="Apple Inc", sector="Consumer Electronics",
            price=187.42, change_pct=0.38, signal="Buy", direction="bull",
            composite_score=0.58, confidence=0.66, rsi=54.0, dte_pref=45,
            iv=0.22, iv_base=0.26, earnings_in_window=False,
            factors=[
                ("Trend", 0.70, "Above 50 & 200 SMA, 20 reclaimed 3d ago"),
                ("Momentum", 0.40, "MACD flat, RSI mid-range"),
                ("Mean reversion", 0.30, "Pulled back to 20 SMA — entry zone"),
                ("Volume flow", 0.25, "Neutral OBV, steady institutional bid"),
                ("Fundamentals", 0.60, "Services margin expanding"),
                ("Event risk", 0.0, "No near-term catalysts"),
            ],
        ),
        dict(
            symbol="TSLA", name="Tesla Inc", sector="Autos",
            price=241.07, change_pct=-1.82, signal="Sell", direction="bear",
            composite_score=-0.61, confidence=0.70, rsi=38.0, dte_pref=30,
            iv=0.55, iv_base=0.48, earnings_in_window=False,
            factors=[
                ("Trend", -0.75, "Below 20/50/200, death cross intact"),
                ("Momentum", -0.55, "MACD hist negative, expanding"),
                ("Mean reversion", 0.10, "RSI 38 — not extreme yet"),
                ("Volume flow", -0.60, "OBV breaking down, distribution pattern"),
                ("Fundamentals", -0.50, "Margin compression, delivery miss"),
                ("Event risk", 0.0, "No earnings in window"),
            ],
        ),
        dict(
            symbol="SPY", name="S&P 500 ETF", sector="Index",
            price=518.72, change_pct=0.21, signal="Hold", direction="neutral",
            composite_score=0.12, confidence=0.55, rsi=55.0, dte_pref=45,
            iv=0.16, iv_base=0.15, earnings_in_window=False,
            factors=[
                ("Trend", 0.30, "Above 200 SMA, chop near 20 SMA"),
                ("Momentum", 0.05, "MACD near zero line"),
                ("Mean reversion", 0.20, "Inside Bollinger middle band"),
                ("Volume flow", -0.05, "Breadth narrowing"),
                ("Fundamentals", 0.15, "Mixed earnings season"),
                ("Event risk", -0.30, "FOMC in 9 days"),
            ],
        ),
        dict(
            symbol="META", name="Meta Platforms", sector="Social",
            price=488.15, change_pct=1.07, signal="Buy", direction="bull",
            composite_score=0.51, confidence=0.60, rsi=58.0, dte_pref=35,
            iv=0.30, iv_base=0.33, earnings_in_window=True,
            factors=[
                ("Trend", 0.60, "Stacked SMAs, pullback bought"),
                ("Momentum", 0.45, "MACD crossing up"),
                ("Mean reversion", 0.20, "Near 20 SMA"),
                ("Volume flow", 0.35, "Steady accumulation"),
                ("Fundamentals", 0.55, "Ad revenue accelerating"),
                ("Event risk", -0.20, "Earnings in 18d (risk)"),
            ],
        ),
        dict(
            symbol="XOM", name="Exxon Mobil", sector="Energy",
            price=104.28, change_pct=-0.62, signal="Hold", direction="neutral",
            composite_score=-0.18, confidence=0.48, rsi=46.0, dte_pref=45,
            iv=0.21, iv_base=0.24, earnings_in_window=False,
            factors=[
                ("Trend", -0.20, "Flat 50 SMA, below 20"),
                ("Momentum", -0.15, "MACD drift lower"),
                ("Mean reversion", 0.30, "Oversold intraday, bouncing"),
                ("Volume flow", -0.10, "Mild distribution"),
                ("Fundamentals", 0.30, "FCF strong, dividend covered"),
                ("Event risk", 0.0, "Clear window"),
            ],
        ),
        dict(
            symbol="AMD", name="Advanced Micro Devices", sector="Semiconductors",
            price=156.40, change_pct=3.12, signal="Buy", direction="bull",
            composite_score=0.64, confidence=0.71, rsi=66.0, dte_pref=30,
            iv=0.42, iv_base=0.40, earnings_in_window=False,
            factors=[
                ("Trend", 0.75, "Strong stacked trend"),
                ("Momentum", 0.60, "Breakout on volume"),
                ("Mean reversion", -0.20, "Slightly extended"),
                ("Volume flow", 0.55, "Heavy call sweeps"),
                ("Fundamentals", 0.40, "AI accelerator wins"),
                ("Event risk", -0.10, "Clear"),
            ],
        ),
        dict(
            symbol="NFLX", name="Netflix", sector="Streaming",
            price=612.50, change_pct=-0.84, signal="Sell", direction="bear",
            composite_score=-0.42, confidence=0.58, rsi=43.0, dte_pref=35,
            iv=0.40, iv_base=0.35, earnings_in_window=False,
            factors=[
                ("Trend", -0.40, "Lost 20 SMA"),
                ("Momentum", -0.45, "MACD down"),
                ("Mean reversion", 0.15, "Near lower BB"),
                ("Volume flow", -0.35, "Distribution"),
                ("Fundamentals", -0.30, "Sub growth decel"),
                ("Event risk", -0.10, "Clear"),
            ],
        ),
    ]

    out: list[TickerSnapshot] = []
    for spec in specs:
        iv_hist = _iv_history(spec["symbol"], spec["iv_base"])
        factors = [FactorScore(n, s, note) for n, s, note in spec["factors"]]
        price_30d = _synthesize_price_30d(spec["price"], spec["direction"])
        out.append(
            TickerSnapshot(
                symbol=spec["symbol"],
                name=spec["name"],
                sector=spec["sector"],
                price=spec["price"],
                change_pct=spec["change_pct"],
                signal=spec["signal"],
                direction=spec["direction"],
                composite_score=spec["composite_score"],
                confidence=spec["confidence"],
                rsi=spec["rsi"],
                dte_pref=spec["dte_pref"],
                iv=spec["iv"],
                iv_history=iv_hist,
                factors=factors,
                price_30d=price_30d,
                earnings_in_window=spec["earnings_in_window"],
            )
        )
    return out


UNIVERSE: list[TickerSnapshot] = _build_universe()
BY_SYMBOL: dict[str, TickerSnapshot] = {t.symbol: t for t in UNIVERSE}


MARKET_REGIME = MarketRegime(
    label="Risk-On — narrow",
    vix=14.8,
    vix_change=-0.4,
    breadth_pct_above_50d=58.0,
    put_call_ratio=0.78,
    trend_score=0.42,
)


def counts() -> dict[str, int]:
    universe_size = 487  # pretend we scanned a larger universe
    longs = sum(1 for t in UNIVERSE if t.signal == "Buy")
    shorts = sum(1 for t in UNIVERSE if t.signal == "Sell")
    return {
        "universe_size": universe_size,
        "scanned": universe_size,
        "longs": max(longs, 34),
        "shorts": max(shorts, 21),
        "holds": universe_size - max(longs, 34) - max(shorts, 21),
    }
