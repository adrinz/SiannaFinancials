"""Shared constants for the analyst module."""
from __future__ import annotations

from typing import Literal

Timeframe = Literal["1h", "4h", "daily", "weekly"]

TIMEFRAMES: tuple[Timeframe, ...] = ("1h", "4h", "daily", "weekly")


# The user-requested coverage set. Order drives UI display order.
# ``yfinance_symbol`` overrides ``symbol`` when present (used for indices
# and other non-standard identifiers, e.g. ^VIX).
TICKERS: list[dict] = [
    {"symbol": "AMZN",  "name": "Amazon.com",              "sector": "Consumer / Cloud",        "bias":  0.06},
    {"symbol": "TSLA",  "name": "Tesla",                   "sector": "Autos / EV",              "bias": -0.08},
    {"symbol": "META",  "name": "Meta Platforms",          "sector": "Communication Services",  "bias":  0.05},
    {"symbol": "NVDA",  "name": "NVIDIA",                  "sector": "Semiconductors / AI",     "bias":  0.12},
    {"symbol": "AAPL",  "name": "Apple",                   "sector": "Consumer Electronics",    "bias":  0.03},
    {"symbol": "PANW",  "name": "Palo Alto Networks",      "sector": "Cybersecurity",           "bias":  0.04},
    {"symbol": "SPY",   "name": "S&P 500 ETF",             "sector": "Index / Broad Market",    "bias":  0.02},
    {"symbol": "QQQ",   "name": "Invesco QQQ Trust",       "sector": "Index / Tech-heavy",      "bias":  0.04},
    {"symbol": "NFLX",  "name": "Netflix",                 "sector": "Communication Services",  "bias":  0.05},
    {"symbol": "GOOGL", "name": "Alphabet Class A",        "sector": "Communication Services",  "bias":  0.04},
    {"symbol": "MSFT",  "name": "Microsoft",               "sector": "Software / Cloud",        "bias":  0.05},
    {"symbol": "PLTR",  "name": "Palantir Technologies",   "sector": "Software / AI",           "bias":  0.10},
    {"symbol": "COIN",  "name": "Coinbase Global",         "sector": "Financials / Crypto",     "bias":  0.05},
    {"symbol": "XOM",   "name": "Exxon Mobil",             "sector": "Energy / Integrated Oil", "bias":  0.01},
    {"symbol": "QUBT",  "name": "Quantum Computing Inc.",  "sector": "Quantum",                 "bias":  0.04},
    {"symbol": "QBTS",  "name": "D-Wave Quantum",          "sector": "Quantum",                 "bias":  0.02},
    {"symbol": "SMR",   "name": "NuScale Power",           "sector": "Nuclear / Energy",        "bias":  0.08},
    {"symbol": "OKLO",  "name": "Oklo Inc.",               "sector": "Nuclear / Energy",        "bias":  0.09},
    {"symbol": "VIX",   "name": "CBOE Volatility Index",   "sector": "Volatility / Index",      "bias": -0.02,
        "yfinance_symbol": "^VIX"},
]

TICKER_MAP: dict[str, dict] = {t["symbol"]: t for t in TICKERS}


# Per-ticker approximate baseline annualized vol for synthetic generation.
# Only used when yfinance is unavailable.
DEFAULT_IV: dict[str, float] = {
    "AMZN": 0.28,  "TSLA": 0.58, "META": 0.32, "NVDA": 0.44,
    "AAPL": 0.24,  "PANW": 0.36, "SPY":  0.15, "QQQ":  0.18,
    "NFLX": 0.36,  "GOOGL": 0.28, "MSFT": 0.22, "PLTR": 0.55,
    "COIN": 0.80,  "XOM":  0.22,
    "QUBT": 1.20,  "QBTS": 1.05, "SMR":  0.95, "OKLO": 1.10,
    # VIX is itself a vol measure; its vol-of-vol is very high.
    "VIX":  1.10,
}

# Approximate anchor prices used by the synthetic generator so charts land
# in a recognizable range. Real data overrides these.
ANCHOR_PRICES: dict[str, float] = {
    "AMZN": 205.0, "TSLA": 240.0, "META": 485.0, "NVDA": 412.0,
    "AAPL": 187.0, "PANW": 328.0, "SPY":  518.0, "QQQ":  445.0,
    "NFLX": 640.0, "GOOGL": 162.0, "MSFT": 420.0, "PLTR":  34.0,
    "COIN": 220.0, "XOM":  112.0,
    "QUBT":  12.0, "QBTS":   6.0, "SMR":   18.0, "OKLO":  22.0,
    "VIX":   17.0,
}


# Typical market hours (ET) — 6.5 hours = 6 full 1h bars + a short 0.5h bar
# we collapse into the preceding bar to keep things tidy.
HOURS_PER_SESSION = 7  # 09:30-10:30, 10:30-11:30, ..., 15:30-16:00 rounded up
