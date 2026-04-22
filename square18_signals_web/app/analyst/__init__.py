"""Analyst module — technical analysis + written report composer.

Public surface is intentionally narrow:

    from app.analyst import (
        TICKERS,           # list of supported symbols
        Timeframe,         # "1h" | "4h" | "daily" | "weekly"
        build_report,      # symbol, timeframe -> Report payload
        overview_rows,     # all tickers, compact verdict table
    )

Optional LLM enrichment (Claude Sonnet 4.5) lives in ``app.analyst.llm``
and is fail-open — the app works identically without an API key.
"""
from .constants import TICKERS, Timeframe
from .report import build_report, overview_rows

__all__ = [
    "TICKERS", "Timeframe", "build_report", "overview_rows",
]
