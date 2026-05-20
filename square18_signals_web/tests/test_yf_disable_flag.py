"""Strict mode tests for disabling Yahoo Finance fallbacks."""
from __future__ import annotations

import sys
from pathlib import Path

# Make the web app + signals package importable without install.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.analyst import data as data_mod  # noqa: E402
from app.analyst import options_flow as flow_mod  # noqa: E402
from app.analyst import search as search_mod  # noqa: E402
from app.analyst.constants import yfinance_disabled  # noqa: E402
from app.analyst.yahoo_quotes import _run_yf  # noqa: E402


def test_yfinance_disabled_flag_truthy(monkeypatch):
    monkeypatch.setenv("SQUARE18_DISABLE_YF", "1")
    assert yfinance_disabled() is True


def test_run_yf_short_circuits_when_disabled(monkeypatch):
    monkeypatch.setenv("SQUARE18_DISABLE_YF", "1")
    assert _run_yf(lambda: 123, timeout=0.01) is None


def test_search_yf_lookup_disabled(monkeypatch):
    monkeypatch.setenv("SQUARE18_DISABLE_YF", "1")
    assert search_mod._yfinance_search("tesla") == []  # noqa: SLF001


def test_ohlcv_yf_fallback_disabled(monkeypatch):
    monkeypatch.setenv("SQUARE18_DISABLE_YF", "1")
    out = data_mod._fetch_yfinance("AAPL", "daily")  # noqa: SLF001
    assert out is None


def test_options_flow_yf_fallback_disabled(monkeypatch):
    monkeypatch.setenv("SQUARE18_DISABLE_YF", "1")
    monkeypatch.delenv("TRADIER_API_KEY", raising=False)
    snap = flow_mod._fetch_chain("AAPL", 100.0, bypass_cache=True)  # noqa: SLF001
    assert snap.source == "unavailable"
