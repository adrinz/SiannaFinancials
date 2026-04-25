"""Tests for the Screener tab API and the upcoming-earnings helper."""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make the web app + signals package importable without install.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.analyst import earnings as earnings_mod  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Endpoints — happy path + validation
# ---------------------------------------------------------------------------


def test_screener_jumps_returns_only_positive_movers(client: TestClient):
    r = client.get("/api/screener/jumps?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["timeframe"] == "daily"
    assert isinstance(body["rows"], list)
    assert len(body["rows"]) <= 5
    for row in body["rows"]:
        assert {"symbol", "name", "sector", "last", "change_pct", "verdict"} <= row.keys()
        assert row["change_pct"] > 0
    # Sorted descending by change_pct.
    pcts = [r["change_pct"] for r in body["rows"]]
    assert pcts == sorted(pcts, reverse=True)


def test_screener_dips_returns_only_negative_movers(client: TestClient):
    r = client.get("/api/screener/dips?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["rows"], list)
    assert len(body["rows"]) <= 5
    for row in body["rows"]:
        assert row["change_pct"] < 0
    pcts = [r["change_pct"] for r in body["rows"]]
    assert pcts == sorted(pcts)  # ascending = most negative first


def test_screener_jumps_validates_timeframe(client: TestClient):
    r = client.get("/api/screener/jumps?timeframe=bogus")
    assert r.status_code == 400


def test_screener_dips_validates_timeframe(client: TestClient):
    r = client.get("/api/screener/dips?timeframe=bogus")
    assert r.status_code == 400


def test_screener_earnings_responds_with_envelope(client: TestClient, monkeypatch):
    # Make the helper deterministic by short-circuiting yfinance.
    monkeypatch.setattr(earnings_mod, "upcoming_earnings", lambda window_days=14: [])
    r = client.get("/api/screener/earnings?window_days=7&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 7
    assert body["rows"] == []


def test_screener_earnings_payload_shape(client: TestClient, monkeypatch):
    sample = [
        earnings_mod.EarningsRow(
            symbol="NVDA",
            name="NVIDIA",
            sector="Semiconductors / AI",
            earnings_date="2026-04-29",
            days_until=5,
            last=412.0,
            change_pct=1.23,
            verdict="BULLISH",
        )
    ]
    monkeypatch.setattr(earnings_mod, "upcoming_earnings", lambda window_days=14: sample)
    r = client.get("/api/screener/earnings?window_days=14&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["symbol"] == "NVDA"
    assert row["earnings_date"] == "2026-04-29"
    assert row["days_until"] == 5
    assert row["verdict"] == "BULLISH"


# ---------------------------------------------------------------------------
# upcoming_earnings() — helper-level tests with a faked yfinance
# ---------------------------------------------------------------------------


def _make_fake_yfinance(date_by_symbol: dict[str, date | None]) -> types.SimpleNamespace:
    """Build a minimal yfinance stand-in that returns canned calendar dicts."""

    class _FakeTicker:
        def __init__(self, symbol: str):
            self.symbol = symbol

        @property
        def calendar(self):
            d = date_by_symbol.get(self.symbol)
            if d is None:
                return {}
            return {"Earnings Date": [d]}

    return types.SimpleNamespace(Ticker=_FakeTicker)


def test_upcoming_earnings_filters_to_window(monkeypatch):
    earnings_mod.reset_cache()
    today = date.today()
    fake_yf = _make_fake_yfinance(
        {
            "AAPL": today + timedelta(days=3),    # in window
            "TSLA": today + timedelta(days=20),   # outside default 14d window
            "NVDA": today + timedelta(days=14),   # boundary, should be included
            "MSFT": today - timedelta(days=2),    # already reported, skipped
        }
    )
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    rows = earnings_mod.upcoming_earnings(window_days=14)
    symbols = [r.symbol for r in rows]
    assert "AAPL" in symbols
    assert "NVDA" in symbols
    assert "TSLA" not in symbols
    assert "MSFT" not in symbols
    # Sorted ascending by earnings_date.
    dates = [r.earnings_date for r in rows]
    assert dates == sorted(dates)


def test_upcoming_earnings_handles_yfinance_failure(monkeypatch):
    earnings_mod.reset_cache()

    class _BoomTicker:
        def __init__(self, symbol: str):
            raise RuntimeError("network down")

    monkeypatch.setitem(
        sys.modules, "yfinance", types.SimpleNamespace(Ticker=_BoomTicker)
    )
    rows = earnings_mod.upcoming_earnings(window_days=14)
    assert rows == []


def test_upcoming_earnings_skips_indices(monkeypatch):
    """The VIX entry should never appear in the earnings calendar."""
    earnings_mod.reset_cache()
    today = date.today()
    fake_yf = _make_fake_yfinance({"^VIX": today + timedelta(days=2)})
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)
    rows = earnings_mod.upcoming_earnings(window_days=14)
    assert all(r.symbol != "VIX" for r in rows)


# ---------------------------------------------------------------------------
# Frontend wiring smoke test — the screener tab + section must be present.
# ---------------------------------------------------------------------------


def test_index_html_exposes_screener_tab(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'data-view="screener"' in html
    assert 'class="view view-screener' in html
    assert 'id="screener-jumps"' in html
    assert 'id="screener-dips"' in html
    assert 'id="screener-earnings"' in html
