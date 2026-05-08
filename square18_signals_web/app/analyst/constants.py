"""Shared constants for the analyst module."""
from __future__ import annotations

from typing import Literal

Timeframe = Literal["1h", "4h", "daily", "weekly"]

TIMEFRAMES: tuple[Timeframe, ...] = ("1h", "4h", "daily", "weekly")

# Screener: upcoming earnings card — Nasdaq / yfinance look-ahead
SCREENER_EARNINGS_WINDOW_DAYS: int = 7


# The user-requested coverage set. Order drives UI display order.
# ``yfinance_symbol`` overrides ``symbol`` when present (used for indices
# and other non-standard identifiers, e.g. ^VIX).
TICKERS: list[dict] = [
    {"symbol": "AMZN",  "name": "Amazon.com",              "sector": "Consumer / Cloud",        "bias":  0.06},
    {"symbol": "TSLA",  "name": "Tesla",                   "sector": "Autos / EV",              "bias": -0.08},
    {"symbol": "META",  "name": "Meta Platforms",          "sector": "Communication Services",  "bias":  0.05},
    {"symbol": "NVDA",  "name": "NVIDIA",                  "sector": "Semiconductors / AI",     "bias":  0.12},
    {"symbol": "AVGO",  "name": "Broadcom",                "sector": "Semiconductors",          "bias":  0.08},
    {"symbol": "AMD",   "name": "Advanced Micro Devices",  "sector": "Semiconductors",          "bias":  0.07},
    {"symbol": "TSM",   "name": "Taiwan Semiconductor",    "sector": "Semiconductors",          "bias":  0.06},
    {"symbol": "QCOM",  "name": "Qualcomm",                "sector": "Semiconductors",          "bias":  0.05},
    {"symbol": "INTC",  "name": "Intel",                   "sector": "Semiconductors",          "bias":  0.02},
    {"symbol": "MU",    "name": "Micron Technology",       "sector": "Semiconductors",          "bias":  0.05},
    {"symbol": "AMAT",  "name": "Applied Materials",       "sector": "Semiconductors / Equip.", "bias":  0.05},
    {"symbol": "ASML",  "name": "ASML Holding",            "sector": "Semiconductors / Equip.", "bias":  0.06},
    {"symbol": "MRVL",  "name": "Marvell Technology",      "sector": "Semiconductors",          "bias":  0.05},
    {"symbol": "SNDK",  "name": "SanDisk Corp.",           "sector": "Semiconductors / Storage", "bias":  0.03},
    {"symbol": "AAPL",  "name": "Apple",                   "sector": "Consumer Electronics",    "bias":  0.03},
    {"symbol": "PANW",  "name": "Palo Alto Networks",      "sector": "Cybersecurity",           "bias":  0.04},
    {"symbol": "SPY",   "name": "S&P 500 ETF",             "sector": "Index / Broad Market",    "bias":  0.02},
    {"symbol": "QQQ",   "name": "Invesco QQQ Trust",       "sector": "Index / Tech-heavy",      "bias":  0.04},
    {"symbol": "NFLX",  "name": "Netflix",                 "sector": "Communication Services",  "bias":  0.05},
    {"symbol": "GOOGL", "name": "Alphabet Class A",        "sector": "Communication Services",  "bias":  0.04},
    {"symbol": "MSFT",  "name": "Microsoft",               "sector": "Software / Cloud",        "bias":  0.05},
    {"symbol": "ORCL",  "name": "Oracle",                  "sector": "Software / Cloud",        "bias":  0.04},
    {"symbol": "CRM",   "name": "Salesforce",              "sector": "Software / Cloud",        "bias":  0.04},
    {"symbol": "PLTR",  "name": "Palantir Technologies",   "sector": "Software / AI",           "bias":  0.10},
    {"symbol": "HOOD",  "name": "Robinhood Markets",       "sector": "Financials / Brokerage",    "bias":  0.05},
    {"symbol": "GME",   "name": "GameStop",                "sector": "Consumer / Retail",         "bias":  0.02},
    {"symbol": "BABA",  "name": "Alibaba Group (ADR)",     "sector": "Consumer / E-commerce",     "bias":  0.04},
    {"symbol": "JPM",   "name": "JPMorgan Chase",          "sector": "Financials / Banks",      "bias":  0.03},
    {"symbol": "COIN",  "name": "Coinbase Global",         "sector": "Financials / Crypto",     "bias":  0.05},
    {"symbol": "XOM",   "name": "Exxon Mobil",             "sector": "Energy / Integrated Oil", "bias":  0.01},
    {"symbol": "BYDDY", "name": "BYD Co. (ADR)",           "sector": "Autos / EV",              "bias":  0.04},
    {"symbol": "QUBT",  "name": "Quantum Computing Inc.",  "sector": "Quantum",                 "bias":  0.04},
    {"symbol": "QBTS",  "name": "D-Wave Quantum",          "sector": "Quantum",                 "bias":  0.02},
    {"symbol": "SMR",   "name": "NuScale Power",           "sector": "Nuclear / Energy",        "bias":  0.08},
    {"symbol": "OKLO",  "name": "Oklo Inc.",               "sector": "Nuclear / Energy",        "bias":  0.09},
    {"symbol": "VIX",   "name": "CBOE Volatility Index",   "sector": "Volatility / Index",      "bias": -0.02,
        "yfinance_symbol": "^VIX"},
]

# liquid ETFs for the "ETF signals" tab (order = UI). Overlap with
# ``TICKERS`` (e.g. SPY, QQQ) does not override existing ``TICKER_MAP``;
# only new symbols are merged in.
ETF_SIGNAL_TICKERS: list[dict] = [
    {"symbol": "SPY",  "name": "S&P 500 ETF",             "sector": "ETF / US broad",   "bias": 0.02},
    {"symbol": "QQQ",  "name": "Invesco QQQ Trust",        "sector": "ETF / US growth",  "bias": 0.04},
    {"symbol": "IWM",  "name": "iShares Russell 2000",     "sector": "ETF / US small-cap", "bias": 0.03},
    {"symbol": "DIA",  "name": "SPDR Dow Jones",           "sector": "ETF / US blue-chip", "bias": 0.02},
    {"symbol": "VTI",  "name": "Vanguard Total Stock Mkt", "sector": "ETF / US total",   "bias": 0.02},
    {"symbol": "VOO",  "name": "Vanguard S&P 500",         "sector": "ETF / US large-cap", "bias": 0.02},
    {"symbol": "EFA",  "name": "iShares MSCI EAFE",         "sector": "ETF / intl dev",  "bias": 0.02},
    {"symbol": "EEM",  "name": "iShares MSCI Emerging Mkts", "sector": "ETF / em",       "bias": 0.04},
    {"symbol": "IEFA", "name": "iShares Core MSCI EAFE",     "sector": "ETF / intl dev",  "bias": 0.02},
    {"symbol": "VUG",  "name": "Vanguard Growth",           "sector": "ETF / US growth",  "bias": 0.04},
    {"symbol": "GLD",  "name": "SPDR Gold",                 "sector": "ETF / commodity",  "bias": 0.01},
    {"symbol": "TLT",  "name": "iShares 20+ Year Treasury", "sector": "ETF / rates",      "bias": 0.01},
    {"symbol": "HYG",  "name": "iShares iBoxx High Yield",  "sector": "ETF / credit",     "bias": 0.02},
    {"symbol": "XLF",  "name": "Financial Select Sector",   "sector": "ETF / sector (fin)", "bias": 0.02},
    {"symbol": "XLE",  "name": "Energy Select Sector",      "sector": "ETF / sector (ene)", "bias": 0.02},
    {"symbol": "XLK",  "name": "Technology Select Sector",  "sector": "ETF / sector (tec)", "bias": 0.04},
    {"symbol": "SMH",  "name": "VanEck Semiconductor",      "sector": "ETF / semis",    "bias": 0.06},
    {"symbol": "ARKK", "name": "ARK Innovation",            "sector": "ETF / thematic", "bias": 0.08},
]

_ticker_map: dict[str, dict] = {t["symbol"]: t for t in TICKERS}
for _m in ETF_SIGNAL_TICKERS:
    _ticker_map.setdefault(_m["symbol"], _m)
TICKER_MAP: dict[str, dict] = _ticker_map


# Per-ticker approximate baseline annualized vol for synthetic generation.
# Only used when yfinance is unavailable.
DEFAULT_IV: dict[str, float] = {
    "AMZN": 0.28,  "TSLA": 0.58, "META": 0.32, "NVDA": 0.44,
    "AVGO": 0.35,  "AMD": 0.45, "TSM": 0.32, "QCOM": 0.30,
    "INTC": 0.40,  "MU": 0.45, "AMAT": 0.35,
    "ASML": 0.38,  "MRVL": 0.42, "SNDK": 0.55,
    "AAPL": 0.24,  "PANW": 0.36, "SPY":  0.15, "QQQ":  0.18,
    "NFLX": 0.36,  "GOOGL": 0.28, "MSFT": 0.22, "ORCL": 0.28, "CRM": 0.30,
    "PLTR": 0.55,  "HOOD": 0.58, "GME": 0.95, "BABA": 0.42,
    "JPM": 0.22,
    "COIN": 0.80,  "XOM":  0.22, "BYDDY": 0.50,
    "QUBT": 1.20,  "QBTS": 1.05, "SMR":  0.95, "OKLO": 1.10,
    # VIX is itself a vol measure; its vol-of-vol is very high.
    "VIX":  1.10,
    # ETF tab-only symbols (broad/sector/ bond proxies).
    "IWM": 0.24,  "DIA": 0.16,  "VTI": 0.16,  "VOO": 0.16,
    "EFA": 0.18,  "EEM": 0.24,  "IEFA": 0.16, "VUG": 0.22,
    "GLD": 0.16,  "TLT": 0.12,  "HYG": 0.08,  "XLF": 0.20,
    "XLE": 0.24,  "XLK": 0.20,  "SMH": 0.30,  "ARKK": 0.45,
}

# Approximate anchor prices used by the synthetic generator so charts land
# in a recognizable range. Real data overrides these.
ANCHOR_PRICES: dict[str, float] = {
    "AMZN": 205.0, "TSLA": 240.0, "META": 485.0, "NVDA": 412.0,
    "AVGO": 185.0, "AMD": 120.0, "TSM": 180.0, "QCOM": 160.0,
    "INTC": 45.0, "MU": 90.0, "AMAT": 180.0,
    "ASML": 780.0, "MRVL": 85.0, "SNDK": 55.0,
    "AAPL": 187.0, "PANW": 328.0, "SPY":  518.0, "QQQ":  445.0,
    "NFLX": 640.0, "GOOGL": 162.0, "MSFT": 420.0, "ORCL": 140.0, "CRM": 260.0,
    "PLTR":  34.0, "HOOD": 105.0, "GME":  22.0, "BABA": 125.0,
    "JPM": 200.0,
    "COIN": 220.0, "XOM":  112.0, "BYDDY": 12.0,
    "QUBT":  12.0, "QBTS":   6.0, "SMR":   18.0, "OKLO":  22.0,
    "VIX":   17.0,
    "IWM": 220.0, "DIA":  420.0, "VTI":  280.0, "VOO":  520.0,
    "EFA":  78.0, "EEM":   45.0, "IEFA":  72.0, "VUG":  420.0,
    "GLD": 240.0, "TLT":   90.0, "HYG":   80.0, "XLF":   50.0,
    "XLE":  90.0, "XLK":  250.0, "SMH":  280.0, "ARKK":  55.0,
}


# Typical market hours (ET) — 6.5 hours = 6 full 1h bars + a short 0.5h bar
# we collapse into the preceding bar to keep things tidy.
HOURS_PER_SESSION = 7  # 09:30-10:30, 10:30-11:30, ..., 15:30-16:00 rounded up
