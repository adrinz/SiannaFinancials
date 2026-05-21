"""Tests for the Screener tab API, broad-universe movers, earnings helper,
and the universe loader."""
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
from app.analyst import movers as movers_mod  # noqa: E402
from app.analyst import report as report_mod  # noqa: E402
from app.analyst import universe as universe_mod  # noqa: E402
from app.analyst.constants import TICKER_MAP, TICKERS  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tracked list (TICKERS)
# ---------------------------------------------------------------------------


def test_tracked_list_includes_requested_symbols():
    for sym in (
        "AVGO",
        "TSM",
        "AMD",
        "QCOM",
        "BYDDY",
        "JPM",
        "ORCL",
        "ASML",
        "MRVL",
        "SNDK",
        "HOOD",
        "GME",
        "BABA",
    ):
        assert sym in TICKER_MAP
    assert len(TICKERS) >= 34


# ---------------------------------------------------------------------------
# Universe loader
# ---------------------------------------------------------------------------


def test_universe_loads_full_sp500_snapshot():
    universe_mod.reset_cache()
    rows = universe_mod.sp500_universe()
    assert len(rows) >= 450, "S&P 500 snapshot should have ~503 rows"
    assert all({"symbol", "name", "sector"} <= r.keys() for r in rows)
    by_symbol = universe_mod.universe_by_symbol()
    assert "AAPL" in by_symbol
    assert "MSFT" in by_symbol
    assert by_symbol["AAPL"]["name"]


def test_universe_uses_remote_csv_when_network_returns_rows(monkeypatch):
    universe_mod.reset_cache()
    fake = universe_mod._normalize_rows(  # noqa: SLF001
        [
            {"symbol": f"Z{i:04d}", "name": f"Co {i}", "sector": "Tech"}
            for i in range(450)
        ]
    )
    monkeypatch.setattr(universe_mod, "_fetch_remote_csv", lambda: fake)
    out = universe_mod.sp500_universe()
    assert len(out) == 450
    assert universe_mod.universe_source() == "remote"


def test_universe_cold_starts_on_bundled_when_remote_fails(monkeypatch):
    universe_mod.reset_cache()
    monkeypatch.setattr(universe_mod, "_fetch_remote_csv", lambda: None)
    rows = universe_mod.sp500_universe()
    assert len(rows) >= 450
    assert universe_mod.universe_source() == "bundle"
    assert "AAPL" in {r["symbol"] for r in rows}


def test_universe_serves_stale_after_remote_succeeds_then_fails(monkeypatch):
    universe_mod.reset_cache()
    fake = universe_mod._normalize_rows(  # noqa: SLF001
        [
            {"symbol": f"Y{i:04d}", "name": f"Q {i}", "sector": "S"}
            for i in range(450)
        ]
    )
    monkeypatch.setattr(universe_mod, "_fetch_remote_csv", lambda: fake)
    assert universe_mod.universe_source() == "remote"
    first = list(universe_mod.sp500_universe())
    # Expire so the next read attempts another fetch.
    universe_mod._state["valid_until"] = 0.0  # noqa: SLF001
    monkeypatch.setattr(universe_mod, "_fetch_remote_csv", lambda: None)
    second = universe_mod.sp500_universe()
    assert second == first
    assert universe_mod.universe_source() == "stale"


# ---------------------------------------------------------------------------
# Movers helper — broad path + curated fallback
# ---------------------------------------------------------------------------


def test_movers_with_fallback_falls_back_to_curated(monkeypatch):
    """When the broad fetch returns nothing, we get curated rows instead."""
    movers_mod.reset_cache()
    monkeypatch.setattr(movers_mod, "_fetch_universe_quotes", lambda: [])
    rows, source = movers_mod.movers_with_fallback("jumps", limit=5)
    assert source == "curated"
    assert len(rows) <= 5
    for r in rows:
        assert r.change_pct > 0


def test_movers_with_fallback_uses_broad_when_available(monkeypatch):
    movers_mod.reset_cache()
    fake_broad = [
        movers_mod.MoverItem(
            symbol="ABCD", name="Acme", sector="Tech",
            last=100.0, change_pct=12.5,
        ),
        movers_mod.MoverItem(
            symbol="ZZZZ", name="Zed", sector="Energy",
            last=10.0, change_pct=-5.0,
        ),
    ]
    monkeypatch.setattr(movers_mod, "_fetch_universe_quotes", lambda: fake_broad)
    rows, source = movers_mod.movers_with_fallback("jumps", limit=5)
    assert source == "sp500"
    assert len(rows) == 1
    assert rows[0].symbol == "ABCD"

    rows, source = movers_mod.movers_with_fallback("dips", limit=5)
    assert source == "sp500"
    assert len(rows) == 1
    assert rows[0].symbol == "ZZZZ"


def test_movers_pair_with_fallback_uses_one_broad_fetch(monkeypatch):
    calls = {"n": 0}

    def _track():
        calls["n"] += 1
        return [
            movers_mod.MoverItem(
                symbol="ABCD", name="A", sector="T", last=1.0, change_pct=2.0,
            ),
            movers_mod.MoverItem(
                symbol="ZETA", name="Z", sector="T", last=1.0, change_pct=-3.0,
            ),
        ]

    movers_mod.reset_cache()
    monkeypatch.setattr(movers_mod, "_fetch_universe_quotes", _track)
    j, d, js, ds = movers_mod.movers_pair_with_fallback(5)
    assert calls["n"] == 1
    assert js == "sp500" and ds == "sp500"
    assert j[0].symbol == "ABCD" and d[0].symbol == "ZETA"


# ---------------------------------------------------------------------------
# Endpoints — happy path + validation
# ---------------------------------------------------------------------------


def test_screener_jumps_returns_only_positive_movers(client: TestClient, monkeypatch):
    """Force broad path off so we exercise the deterministic curated fallback."""
    movers_mod.reset_cache()
    monkeypatch.setattr(movers_mod, "_fetch_universe_quotes", lambda: [])
    r = client.get("/api/screener/jumps?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["timeframe"] == "daily"
    assert body["source"] in {"sp500", "curated"}
    for row in body["rows"]:
        assert {"symbol", "name", "sector", "last", "change_pct"} <= row.keys()
        assert row["change_pct"] > 0
    pcts = [r["change_pct"] for r in body["rows"]]
    assert pcts == sorted(pcts, reverse=True)


def test_screener_dips_returns_only_negative_movers(client: TestClient, monkeypatch):
    movers_mod.reset_cache()
    monkeypatch.setattr(movers_mod, "_fetch_universe_quotes", lambda: [])
    r = client.get("/api/screener/dips?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] in {"sp500", "curated"}
    for row in body["rows"]:
        assert row["change_pct"] < 0
    pcts = [r["change_pct"] for r in body["rows"]]
    assert pcts == sorted(pcts)


def test_screener_jumps_validates_timeframe(client: TestClient):
    r = client.get("/api/screener/jumps?timeframe=bogus")
    assert r.status_code == 400


def test_screener_dips_validates_timeframe(client: TestClient):
    r = client.get("/api/screener/dips?timeframe=bogus")
    assert r.status_code == 400


def test_screener_movers_pair_quick_returns_curated_shape(client: TestClient):
    r = client.get("/api/screener/movers?quick=1&limit=5")
    assert r.status_code == 200
    b = r.json()
    assert b["timeframe"] == "daily"
    assert b["jumps"]["source"] == "curated" and b["dips"]["source"] == "curated"
    for row in b["jumps"]["rows"]:
        assert row["change_pct"] > 0
    for row in b["dips"]["rows"]:
        assert row["change_pct"] < 0


def test_screener_movers_pair_combined_envelope(client: TestClient, monkeypatch):
    movers_mod.reset_cache()
    fake_broad = [
        movers_mod.MoverItem(
            symbol="UP", name="Up Co", sector="T", last=10.0, change_pct=5.0
        ),
        movers_mod.MoverItem(
            symbol="DN", name="Down Co", sector="T", last=10.0, change_pct=-4.0
        ),
    ]
    monkeypatch.setattr(movers_mod, "_fetch_universe_quotes", lambda: fake_broad)
    r = client.get("/api/screener/movers?limit=3")
    assert r.status_code == 200
    b = r.json()
    assert b["jumps"]["source"] == "sp500" and b["dips"]["source"] == "sp500"
    assert b["jumps"]["rows"][0]["symbol"] == "UP"
    assert b["dips"]["rows"][0]["symbol"] == "DN"


def test_screener_movers_validates_timeframe(client: TestClient):
    r = client.get("/api/screener/movers?timeframe=nope")
    assert r.status_code == 400


def test_screener_earnings_envelope_when_helper_returns_empty(client, monkeypatch):
    monkeypatch.setattr(
        earnings_mod,
        "upcoming_earnings_with_source",
        lambda window_days=7: ([], "unavailable"),
    )
    r = client.get("/api/screener/earnings?window_days=7&limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 7
    assert body["source"] == "unavailable"
    assert body["rows"] == []


def test_screener_earnings_payload_shape(client: TestClient, monkeypatch):
    sample = [
        earnings_mod.EarningsRow(
            symbol="NVDA",
            name="NVIDIA Corp",
            sector="Information Technology",
            earnings_date="2026-04-29",
            days_until=5,
            last=412.0,
            change_pct=1.23,
            verdict="BULLISH",
        )
    ]
    monkeypatch.setattr(
        earnings_mod,
        "upcoming_earnings_with_source",
        lambda window_days=7: (sample, "sp500"),
    )
    r = client.get("/api/screener/earnings?window_days=7&limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["window_days"] == 7
    assert body["source"] == "sp500"
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["symbol"] == "NVDA"
    assert row["earnings_date"] == "2026-04-29"
    assert row["days_until"] == 5
    assert row["verdict"] == "BULLISH"


# ---------------------------------------------------------------------------
# Earnings broad path: Nasdaq calendar with S&P 500 filter
# ---------------------------------------------------------------------------


def test_broad_earnings_filters_to_universe(monkeypatch):
    # Ensure S&P 500 is the real bundled set (a prior test may have
    # swapped in a synthetic universe for remote/stale cases).
    universe_mod.reset_cache()
    monkeypatch.setattr(universe_mod, "_fetch_remote_csv", lambda: None)
    earnings_mod.reset_cache()
    today = date.today()
    iso_today = today.isoformat()
    iso_plus3 = (today + timedelta(days=3)).isoformat()

    nasdaq_payload: dict[str, list[dict]] = {
        iso_today: [
            {"symbol": "AAPL", "name": "Apple Inc."},
            {"symbol": "FAKEEXOTIC", "name": "Not in S&P 500"},
        ],
        iso_plus3: [
            {"symbol": "MSFT", "name": "Microsoft Corp"},
        ],
    }

    def _fake_fetch(iso, timeout=6.0):
        return nasdaq_payload.get(iso, [])

    monkeypatch.setattr(earnings_mod, "_fetch_nasdaq_day", _fake_fetch)
    monkeypatch.setattr(earnings_mod, "_broad_universe_rows", lambda: [])

    rows = earnings_mod._broad_earnings(window_days=7)
    symbols = [r.symbol for r in rows]
    assert "AAPL" in symbols
    assert "MSFT" in symbols
    assert "FAKEEXOTIC" not in symbols
    # Sorted by date.
    dates = [r.earnings_date for r in rows]
    assert dates == sorted(dates)


def test_upcoming_earnings_falls_back_to_curated(monkeypatch):
    """When Nasdaq is dead, helper falls back to curated yfinance walk."""
    earnings_mod.reset_cache()
    monkeypatch.delenv("SQUARE18_DISABLE_YF", raising=False)
    today = date.today()

    monkeypatch.setattr(earnings_mod, "_fetch_nasdaq_day", lambda iso, timeout=6.0: [])

    class _FakeTicker:
        def __init__(self, symbol: str):
            self.symbol = symbol

        @property
        def calendar(self):
            if self.symbol == "AAPL":
                return {"Earnings Date": [today + timedelta(days=2)]}
            return {}

    fake_yf = types.SimpleNamespace(Ticker=_FakeTicker)
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    rows, source = earnings_mod.upcoming_earnings_with_source(window_days=7)
    assert source == "curated"
    assert any(r.symbol == "AAPL" for r in rows)


def test_upcoming_earnings_unavailable_when_all_paths_fail(monkeypatch):
    earnings_mod.reset_cache()
    monkeypatch.setattr(earnings_mod, "_fetch_nasdaq_day", lambda iso, timeout=6.0: [])

    class _BoomTicker:
        def __init__(self, symbol: str):
            raise RuntimeError("yfinance offline")

    monkeypatch.setitem(
        sys.modules, "yfinance", types.SimpleNamespace(Ticker=_BoomTicker)
    )
    rows, source = earnings_mod.upcoming_earnings_with_source(window_days=7)
    assert source == "unavailable"
    assert rows == []


def test_upcoming_earnings_cache_short_circuits(monkeypatch):
    """Once the cache is warm, the data sources aren't hit again within TTL."""
    earnings_mod.reset_cache()
    today = date.today()

    sample = [
        earnings_mod.EarningsRow(
            symbol="AAPL", name="Apple Inc.", sector="Information Technology",
            earnings_date=(today + timedelta(days=1)).isoformat(), days_until=1,
            last=180.0, change_pct=0.5, verdict=None,
        )
    ]
    earnings_mod._cache["rows"] = sample
    earnings_mod._cache["ts"] = __import__("time").time()
    earnings_mod._cache["source"] = "sp500"

    def _explode(iso, timeout=6.0):
        raise AssertionError("Nasdaq path should not be hit when cache is warm")

    monkeypatch.setattr(earnings_mod, "_fetch_nasdaq_day", _explode)
    rows, source = earnings_mod.upcoming_earnings_with_source(window_days=7)
    assert source == "sp500"
    assert rows == sample


def test_earnings_within_window_falls_back_to_calendar_when_yf_disabled(monkeypatch):
    earnings_mod.reset_cache()
    monkeypatch.setenv("SQUARE18_DISABLE_YF", "1")
    today = date.today()
    sample = [
        earnings_mod.EarningsRow(
            symbol="NVDA",
            name="NVIDIA Corporation",
            sector="Information Technology",
            earnings_date=(today + timedelta(days=2)).isoformat(),
            days_until=2,
            last=900.0,
            change_pct=1.2,
            verdict="BULLISH",
        )
    ]
    earnings_mod._cache["rows"] = sample
    earnings_mod._cache["ts"] = __import__("time").time()
    earnings_mod._cache["source"] = "sp500"

    out = earnings_mod.earnings_within_window_days("NVDA", {}, window_days=7)
    assert out == (sample[0].earnings_date, 2)


def test_earnings_within_window_falls_back_when_yf_missing_symbol(monkeypatch):
    earnings_mod.reset_cache()
    monkeypatch.delenv("SQUARE18_DISABLE_YF", raising=False)
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=lambda _sym: object()))
    monkeypatch.setattr(earnings_mod, "_next_earnings_date", lambda yf, symbol: None)
    today = date.today()
    sample = [
        earnings_mod.EarningsRow(
            symbol="NVDA",
            name="NVIDIA Corporation",
            sector="Information Technology",
            earnings_date=(today + timedelta(days=1)).isoformat(),
            days_until=1,
            last=900.0,
            change_pct=1.2,
            verdict="BULLISH",
        )
    ]
    earnings_mod._cache["rows"] = sample
    earnings_mod._cache["ts"] = __import__("time").time()
    earnings_mod._cache["source"] = "sp500"

    out = earnings_mod.earnings_within_window_days("NVDA", {}, window_days=7)
    assert out == (sample[0].earnings_date, 1)


def test_overview_rows_cacheable_threshold():
    assert report_mod._overview_rows_cacheable([], 36) is False
    assert report_mod._overview_rows_cacheable([object()] * 17, 36) is False
    assert report_mod._overview_rows_cacheable([object()] * 18, 36) is True


def test_peek_overview_rows_cache_returns_warm_snapshot():
    report_mod.reset_overview_rows_cache()
    sample = [object()] * 20
    key = report_mod._overview_cache_key("daily", None)
    report_mod._overview_rows_cache[key] = (__import__("time").time(), sample)
    hit = report_mod.peek_overview_rows_cache("daily")
    assert hit is not None
    assert len(hit) == 20


def test_overview_rows_does_not_cache_empty(monkeypatch):
    report_mod.reset_overview_rows_cache()
    monkeypatch.setattr(report_mod, "_safe_build_report", lambda _s, _tf: None)
    rows = report_mod.overview_rows("daily")
    assert rows == []
    with report_mod._overview_lock:
        assert not report_mod._overview_rows_cache


# ---------------------------------------------------------------------------
# Frontend wiring smoke test
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
    assert 'id="screener-scope"' in html
    assert 'id="screener-movers-hint"' in html
