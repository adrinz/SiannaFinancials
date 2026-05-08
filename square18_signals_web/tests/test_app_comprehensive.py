"""Broad API + HTML surface tests for every user-facing tab and major route.

These complement ``test_e2e_app.py`` and ``test_screener.py``:

* **Shell**: all tab buttons and view sections (Dashboard, Detail, Search,
  Screener, ETF, Stocks, Analyst) plus anchors the JS relies on.
* **Contracts**: response shape, query clamping, and validation (400/404/422)
  without duplicating every happy-path assertion from the slimmer E2E suite.
* **OpenAPI**: each documented path exists for drift detection.

Many happy paths still need OHLCV (network). Those tests :func:`pytest.skip`
when the dashboard screen is empty so air-gapped CI stays green; run locally
with network for full coverage.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.analyst.constants import TICKERS  # noqa: E402
from app.analyst.models import ReportOut  # noqa: E402
from app.analyst import copy_trade as ct  # noqa: E402
from app.main import app  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def network_has_screen(client: TestClient) -> bool:
    r = client.get("/api/screen?filter=all")
    return r.status_code == 200 and bool(r.json())


def _j(client: TestClient, url: str, code: int = 200):
    r = client.get(url)
    assert r.status_code == code, (url, r.status_code, r.text[:300])
    if code == 204:
        return {}
    return r.json()


# ---------------------------------------------------------------------------
# HTML — every tab + critical mount points
# ---------------------------------------------------------------------------


def test_index_html_exposes_every_tab_and_stock_screener_anchors(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    h = r.text

    for view in (
        "dashboard",
        "detail",
        "search",
        "screener",
        "etf",
        "stocks",
        "analyst",
    ):
        assert f'data-view="{view}"' in h
        assert f'class="view view-{view}' in h

    # Dashboard
    for eid in (
        "regime-banner",
        "screen-tbody",
        "movers-gainers",
        "movers-losers",
        "crypto-grid",
        "news-list",
        "opts-calls",
        "opts-puts",
    ):
        assert f'id="{eid}"' in h

    # Detail
    assert 'id="detail-hero"' in h
    assert 'id="symbol-chips"' in h
    assert 'id="factors-tbody"' in h

    # Search
    assert 'id="search-input"' in h
    assert 'id="search-result"' in h

    # Screener tab
    for eid in (
        "screener-jumps",
        "screener-dips",
        "screener-earnings",
        "screener-scope",
        "screener-movers-hint",
    ):
        assert f'id="{eid}"' in h

    # ETF signals tab
    assert 'id="etf-signals-tbody"' in h

    # Stocks tab (mirrors analyst data wiring)
    for eid in (
        "stocks-source",
        "stocks-ticker-strip",
        "stocks-overview-timeframe",
        "stocks-overview-list",
        "stocks-report",
    ):
        assert f'id="{eid}"' in h

    # Analyst tab
    for eid in (
        "analyst-ticker-strip",
        "overview-list",
        "analyst-report",
        "all-recs-tbody",
        "brief-card",
    ):
        assert f'id="{eid}"' in h

    # Static bundles referenced
    assert "/static/app.js" in h
    assert "/static/styles.css" in h


def test_static_assets_served(client: TestClient):
    for path in ("/static/app.js", "/static/styles.css"):
        r = client.get(path)
        assert r.status_code == 200
        assert len(r.content) > 100


def test_health_ok(client: TestClient):
    body = _j(client, "/api/health")
    assert body == {"status": "ok"}


# ---------------------------------------------------------------------------
# OpenAPI surface
# ---------------------------------------------------------------------------


def test_openapi_includes_all_primary_routes(client: TestClient):
    schema = app.openapi()
    paths = set(schema.get("paths", {}).keys())
    expected = {
        "/api/health",
        "/api/regime",
        "/api/screen",
        "/api/ticker/{symbol}",
        "/api/analyst/tickers",
        "/api/analyst/overview",
        "/api/etf/signals",
        "/api/analyst/report/{symbol}",
        "/api/report/signals",
        "/api/report/signals.md",
        "/api/report/signals.txt",
        "/api/search",
        "/api/search/suggest",
        "/api/market/pulse",
        "/api/options/highlights",
        "/api/crypto/snapshot",
        "/api/news",
        "/api/screener/movers",
        "/api/screener/jumps",
        "/api/screener/dips",
        "/api/screener/earnings",
        "/api/copy-trade/creators",
        "/api/copy-trade/holdings/{creator_id}",
        "/api/copy-trade/signals",
        "/api/analyst/llm-config",
        "/api/analyst/polish/{symbol}",
        "/api/analyst/brief",
        "/api/analyst/explain/{symbol}",
    }
    missing = expected - paths
    assert not missing, f"OpenAPI missing paths: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Analyst universe parity
# ---------------------------------------------------------------------------


def test_analyst_tickers_matches_constants_list(client: TestClient):
    rows = _j(client, "/api/analyst/tickers")
    api_syms = {r["symbol"] for r in rows}
    const_syms = {t["symbol"] for t in TICKERS}
    assert api_syms == const_syms
    for r in rows:
        assert set(r.keys()) == {"symbol", "name", "sector"}
        assert r["symbol"].isupper()


# ---------------------------------------------------------------------------
# Dashboard — timeframes & news clamping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tf", ["1h", "daily", "weekly"])
def test_market_pulse_and_options_all_timeframes(client: TestClient, tf: str):
    p = _j(client, f"/api/market/pulse?timeframe={tf}")
    assert p["timeframe"] == tf
    for k in (
        "top_gainers",
        "top_losers",
        "sector_heatmap",
        "breadth_pct_up",
        "tickers_covered",
    ):
        assert k in p

    o = _j(client, f"/api/options/highlights?timeframe={tf}")
    assert o["timeframe"] == tf
    assert "top_calls" in o and "top_puts" in o


def test_news_limit_clamped(client: TestClient):
    small = _j(client, "/api/news?limit=1")
    assert isinstance(small["items"], list) and len(small["items"]) <= 1

    big = _j(client, "/api/news?limit=500")
    assert isinstance(big["items"], list) and len(big["items"]) <= 40


# ---------------------------------------------------------------------------
# Ticker detail — chart ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rng", ["1d", "1m", "ytd"])
def test_ticker_detail_aapl_each_chart_range(
    client: TestClient, network_has_screen: bool, rng: str,
):
    if not network_has_screen:
        pytest.skip("No market data for screen; assume offline")
    d = _j(client, f"/api/ticker/AAPL?range={rng}")
    assert d["row"]["symbol"] == "AAPL"
    assert d["chart"]["range_key"] == rng
    assert isinstance(d["chart"]["bars"], list)


# ---------------------------------------------------------------------------
# Search edge cases
# ---------------------------------------------------------------------------


def test_search_suggest_empty_query_returns_empty_list(client: TestClient):
    assert _j(client, "/api/search/suggest?q=") == []
    # ``q`` is required by FastAPI; omitting it is a validation error.
    r = client.get("/api/search/suggest?limit=12")
    assert r.status_code == 422


def test_search_whitespace_only_query_bad_request(client: TestClient):
    r = client.get("/api/search?q=%20%20%20&timeframe=daily")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Screener — bounds (business logic in routes)
# ---------------------------------------------------------------------------


def test_screener_movers_quick_out_of_range_unprocessable(client: TestClient):
    r = client.get("/api/screener/movers?quick=2")
    assert r.status_code == 422


def test_screener_movers_limit_capped(client: TestClient):
    r = client.get("/api/screener/movers?quick=1&limit=99")
    assert r.status_code == 200
    body = r.json()
    assert len(body["jumps"]["rows"]) <= 25
    assert len(body["dips"]["rows"]) <= 25


def test_screener_earnings_window_capped(client: TestClient):
    r = client.get("/api/screener/earnings?window_days=999&limit=999")
    assert r.status_code == 200
    b = r.json()
    assert b["window_days"] <= 60
    assert len(b["rows"]) <= 200


# ---------------------------------------------------------------------------
# Copy trade — not-found + signal limit
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_copy_state():
    ct.reset_copy_trade_state()
    yield
    ct.reset_copy_trade_state()


def test_copy_trade_holdings_unknown_creator_404(client: TestClient):
    r = client.get("/api/copy-trade/holdings/does-not-exist")
    assert r.status_code == 404


def test_copy_trade_signals_limit_capped(client: TestClient):
    j = _j(client, "/api/copy-trade/signals?limit=9999")
    assert isinstance(j["rows"], list) and len(j["rows"]) <= 200


# ---------------------------------------------------------------------------
# Bulk report export — all timeframes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tf", ["4h", "daily", "weekly"])
def test_signal_report_json_all_timeframes(client: TestClient, tf: str):
    rep = _j(client, f"/api/report/signals?timeframe={tf}")
    assert rep["timeframe"] == tf
    assert "tickers" in rep and "regime" in rep


# ---------------------------------------------------------------------------
# Analyst — ReportOut field completeness (when data available)
# ---------------------------------------------------------------------------


def test_analyst_report_includes_model_fields(
    client: TestClient, network_has_screen: bool,
):
    if not network_has_screen:
        pytest.skip("No market data; offline skip")
    sym = TICKERS[0]["symbol"]
    if sym == "VIX":
        sym = TICKERS[1]["symbol"]
    rep = _j(client, f"/api/analyst/report/{sym}?timeframe=daily&fresh_quotes=0")
    keys = set(rep.keys())
    required = {k for k, f in ReportOut.model_fields.items() if f.is_required()}
    missing = required - keys
    assert not missing, f"Report JSON missing required keys: {sorted(missing)}"
    assert rep["symbol"] == sym
    assert "trade_plan" in rep["options"]


def test_analyst_overview_non_empty_when_dashboard_populated(
    client: TestClient, network_has_screen: bool,
):
    if not network_has_screen:
        pytest.skip("No market data; offline skip")
    ov = _j(client, "/api/analyst/overview?timeframe=daily")
    assert isinstance(ov, list) and len(ov) > 0
    row = ov[0]
    for k in (
        "verdict",
        "conviction",
        "composite_score",
        "rec_contract_type",
        "rec_strike",
        "rec_expiry_dte",
    ):
        assert k in row


def test_regime_envelope_matches_screen_counts(client: TestClient, network_has_screen: bool):
    if not network_has_screen:
        pytest.skip("No market data; offline skip")
    regime = _j(client, "/api/regime")
    rows = _j(client, "/api/screen?filter=all")
    c = regime["counts"]
    assert c["longs"] + c["shorts"] + c["holds"] == len(rows)


# ---------------------------------------------------------------------------
# LLM routes — validation when enabled
# ---------------------------------------------------------------------------


def test_analyst_polish_invalid_timeframe(client: TestClient):
    cfg = _j(client, "/api/analyst/llm-config")
    if not cfg.get("enabled"):
        pytest.skip("LLM disabled")
    r = client.get("/api/analyst/polish/AAPL?timeframe=invalid")
    assert r.status_code == 400


def test_analyst_brief_invalid_timeframe(client: TestClient):
    cfg = _j(client, "/api/analyst/llm-config")
    if not cfg.get("enabled"):
        pytest.skip("LLM disabled")
    r = client.get("/api/analyst/brief?timeframe=invalid")
    assert r.status_code == 400
