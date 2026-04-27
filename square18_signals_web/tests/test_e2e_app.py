"""End-to-end app tests for Dashboard, Detail, Search, Screener, ETF, Copy trade, Analyst.

These tests exercise the app through FastAPI's TestClient as a browser/API
consumer would:
  - verify page shell and per-view containers exist in the served HTML
  - hit every major endpoint used by the UI
  - validate happy-path behavior plus key validation/error paths
  - cross-check related endpoints for data consistency

The suite intentionally avoids brittle value assertions because market data is
live (or pseudo-live) and changes continuously.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make the web app + signals package importable without install.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def _json(client: TestClient, url: str, expected: int = 200) -> dict | list:
    r = client.get(url)
    assert r.status_code == expected, (url, r.status_code, r.text[:400])
    if expected == 204:
        return {}
    return r.json()


# ---------------------------------------------------------------------------
# App shell / page containers
# ---------------------------------------------------------------------------


def test_root_serves_all_page_containers(client: TestClient):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text

    # Tab buttons for all user-facing pages.
    assert 'data-view="dashboard"' in html
    assert 'data-view="detail"' in html
    assert 'data-view="search"' in html
    assert 'data-view="analyst"' in html
    assert 'data-view="etf"' in html
    assert 'data-view="copy-trade"' in html

    # Core view containers.
    assert 'class="view view-dashboard' in html
    assert 'class="view view-detail' in html
    assert 'class="view view-search' in html
    assert 'class="view view-etf' in html
    assert 'class="view view-copy-trade' in html
    assert 'class="view view-analyst' in html

    # Key per-page anchors used by app.js.
    assert 'id="regime-banner"' in html
    assert 'id="screen-tbody"' in html
    assert 'id="detail-hero"' in html
    assert 'id="search-input"' in html
    assert 'id="search-result"' in html
    assert 'id="overview-list"' in html
    assert 'id="analyst-report"' in html
    assert 'id="all-recs-tbody"' in html
    assert 'id="etf-signals-tbody"' in html
    assert 'id="copytrade-holdings-tbody"' in html
    assert 'id="copytrade-select"' in html


# ---------------------------------------------------------------------------
# Dashboard flow
# ---------------------------------------------------------------------------


def test_copy_trade_creators_endpoint(client: TestClient):
    rows = _json(client, "/api/copy-trade/creators")
    assert isinstance(rows, list) and len(rows) >= 1
    a = rows[0]
    for k in ("id", "name", "type", "description"):
        assert k in a


def test_copy_trade_signals_endpoint(client: TestClient):
    out = _json(client, "/api/copy-trade/signals?limit=3")
    assert "rows" in out
    assert isinstance(out["rows"], list)



def test_dashboard_endpoints_happy_path(client: TestClient):
    regime = _json(client, "/api/regime")
    assert set(regime.keys()) == {"regime", "counts", "last_scan_iso"}
    assert "label" in regime["regime"]
    assert "vix" in regime["regime"]
    assert "longs" in regime["counts"]
    assert "shorts" in regime["counts"]
    assert "holds" in regime["counts"]

    rows = _json(client, "/api/screen?filter=all")
    assert isinstance(rows, list)
    assert len(rows) > 0
    sample = rows[0]
    for k in (
        "symbol",
        "name",
        "sector",
        "price",
        "change_pct",
        "signal",
        "direction",
        "composite_score",
        "confidence",
        "rsi",
        "iv",
        "iv_rank",
        "iv_percentile",
        "dte_pref",
        "earnings_in_window",
    ):
        assert k in sample

    pulse = _json(client, "/api/market/pulse?timeframe=daily")
    assert pulse["timeframe"] == "daily"
    assert "top_gainers" in pulse and "top_losers" in pulse
    assert "sector_heatmap" in pulse
    assert "breadth_pct_up" in pulse
    assert "tickers_covered" in pulse

    opts = _json(client, "/api/options/highlights?timeframe=daily")
    assert opts["timeframe"] == "daily"
    assert "top_calls" in opts and "top_puts" in opts

    crypto = _json(client, "/api/crypto/snapshot")
    assert "rows" in crypto
    assert "source" in crypto
    if crypto["rows"]:
        c = crypto["rows"][0]
        for k in ("symbol", "name", "last", "change_pct_24h", "change_pct_7d", "spark"):
            assert k in c

    news = _json(client, "/api/news?limit=8")
    assert "items" in news and "source" in news
    if news["items"]:
        n = news["items"][0]
        for k in ("title", "publisher", "url", "related", "published_at", "summary"):
            assert k in n


def test_dashboard_screen_filter_validation_and_behavior(client: TestClient):
    bad = client.get("/api/screen?filter=invalid")
    assert bad.status_code == 400

    all_rows = _json(client, "/api/screen?filter=all")
    buy_rows = _json(client, "/api/screen?filter=buy")
    sell_rows = _json(client, "/api/screen?filter=sell")
    hold_rows = _json(client, "/api/screen?filter=hold")

    # Filtered sets should be subsets of "all".
    all_symbols = {r["symbol"] for r in all_rows}
    for subset in (buy_rows, sell_rows, hold_rows):
        assert {r["symbol"] for r in subset}.issubset(all_symbols)

    # Semantic check: each filter returns matching signal labels only.
    assert all(r["signal"] == "Buy" for r in buy_rows)
    assert all(r["signal"] == "Sell" for r in sell_rows)
    assert all(r["signal"] == "Hold" for r in hold_rows)


@pytest.mark.parametrize("endpoint", ["/api/market/pulse", "/api/options/highlights"])
def test_dashboard_timeframe_validation(client: TestClient, endpoint: str):
    bad = client.get(f"{endpoint}?timeframe=monthly")
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# Ticker detail flow
# ---------------------------------------------------------------------------


def test_ticker_detail_matches_dashboard_symbol(client: TestClient):
    rows = _json(client, "/api/screen?filter=all")
    symbol = rows[0]["symbol"]

    detail = _json(client, f"/api/ticker/{symbol}")
    assert detail["row"]["symbol"] == symbol
    assert isinstance(detail["factors"], list)
    assert isinstance(detail["price_30d"], list)
    assert len(detail["price_30d"]) > 0
    assert isinstance(detail["price_series"], list)
    assert len(detail["price_series"]) >= 2
    assert isinstance(detail["chart"], dict)
    assert detail["chart"]["range_key"] == "1d"
    assert detail["chart"]["x_granularity"] == "session"
    assert len(detail["chart"]["bars"]) == len(detail["price_series"])
    assert isinstance(detail.get("news"), list)
    assert "signal_detail" in detail
    assert "narrative_summary" in detail
    assert isinstance(detail.get("technical_bullets"), list)
    cc = detail["chart_context"]
    assert cc["headline"]
    assert cc["verdict"] in ("BULLISH", "BEARISH", "NEUTRAL")
    assert cc["signal"] in ("Buy", "Sell", "Hold")
    assert isinstance(cc["lines"], list)
    assert len(cc["lines"]) >= 1
    assert "one_sigma_usd" in detail["expected_move"]
    assert "one_sigma_pct" in detail["expected_move"]
    assert isinstance(detail["recommendations"], list)


def test_ticker_detail_unknown_symbol_returns_404(client: TestClient):
    r = client.get("/api/ticker/THIS_DOES_NOT_EXIST")
    assert r.status_code == 404


def test_ticker_detail_invalid_range_returns_400(client: TestClient):
    r = client.get("/api/ticker/AAPL?range=10y")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Search flow
# ---------------------------------------------------------------------------


def test_search_suggest_and_search_happy_path(client: TestClient):
    suggestions = _json(client, "/api/search/suggest?q=tesla&limit=5")
    assert isinstance(suggestions, list)

    # Use a stable, known ticker for deterministic behavior.
    result = _json(client, "/api/search?q=AAPL&timeframe=daily")
    assert "resolved" in result
    assert "report" in result
    assert "stock_plan" in result

    assert result["resolved"]["symbol"] == result["report"]["symbol"]
    plan = result["stock_plan"]
    for k in (
        "action",
        "confidence",
        "entry_price",
        "entry_zone_low",
        "entry_zone_high",
        "target_price",
        "stop_loss",
        "expected_move_pct",
        "time_horizon",
        "rationale",
        "caveats",
    ):
        assert k in plan


def test_search_validation_and_not_found(client: TestClient):
    r = client.get("/api/search")
    # Missing required query param `q` is validated by FastAPI itself.
    assert r.status_code == 422

    r = client.get("/api/search?q=AAPL&timeframe=monthly")
    assert r.status_code == 400

    r = client.get("/api/search?q=zzzzzzzzzzzz")
    assert r.status_code in {404, 502}


# ---------------------------------------------------------------------------
# Analyst flow
# ---------------------------------------------------------------------------


def test_etf_signals_overview_happy_path(client: TestClient):
    """ETF watchlist returns the same OverviewRow shape as /api/analyst/overview."""
    rows = _json(client, "/api/etf/signals?timeframe=daily")
    assert isinstance(rows, list) and len(rows) > 0
    row = rows[0]
    for k in (
        "symbol",
        "name",
        "sector",
        "last",
        "change_pct",
        "verdict",
        "conviction",
        "composite_score",
        "trend",
        "source",
        "rec_contract_type",
        "rec_strike",
        "rec_expiry_date",
        "rec_expiry_dte",
    ):
        assert k in row


def test_etf_signals_timeframe_validation(client: TestClient):
    r = client.get("/api/etf/signals?timeframe=monthly")
    assert r.status_code == 400


@pytest.mark.parametrize("tf", ["1h", "4h", "daily", "weekly"])
def test_etf_signals_supports_all_timeframes(client: TestClient, tf: str):
    rows = _json(client, f"/api/etf/signals?timeframe={tf}")
    assert isinstance(rows, list)


def test_analyst_tickers_overview_report_flow(client: TestClient):
    tickers = _json(client, "/api/analyst/tickers")
    assert isinstance(tickers, list) and len(tickers) > 0
    symbol = tickers[0]["symbol"]

    overview = _json(client, "/api/analyst/overview?timeframe=daily")
    assert isinstance(overview, list) and len(overview) > 0
    ov = overview[0]
    for k in (
        "symbol",
        "name",
        "sector",
        "last",
        "change_pct",
        "verdict",
        "conviction",
        "composite_score",
        "trend",
        "source",
        "rec_contract_type",
        "rec_strike",
        "rec_expiry_date",
        "rec_expiry_dte",
    ):
        assert k in ov

    report = _json(client, f"/api/analyst/report/{symbol}?timeframe=daily")
    assert report["symbol"] == symbol
    assert report["timeframe"] == "daily"
    for k in (
        "headline",
        "narrative",
        "price_action",
        "volume",
        "sma",
        "rsi",
        "macd",
        "atr",
        "market_context",
        "options",
        "chart",
    ):
        assert k in report
    assert report["options"]["contract_type"] in {"call", "put"}


@pytest.mark.parametrize("tf", ["1h", "4h", "daily", "weekly"])
def test_analyst_overview_supports_all_timeframes(client: TestClient, tf: str):
    rows = _json(client, f"/api/analyst/overview?timeframe={tf}")
    assert isinstance(rows, list)


def test_analyst_endpoint_validation(client: TestClient):
    r = client.get("/api/analyst/overview?timeframe=monthly")
    assert r.status_code == 400

    r = client.get("/api/analyst/report/NOPE?timeframe=daily")
    assert r.status_code == 404


def test_analyst_llm_endpoints_contract(client: TestClient):
    cfg = _json(client, "/api/analyst/llm-config")
    assert set(cfg.keys()) == {"enabled", "model", "last_error"}

    if not cfg["enabled"]:
        assert client.get("/api/analyst/polish/AAPL?timeframe=daily").status_code == 503
        assert client.get("/api/analyst/brief?timeframe=daily").status_code == 503
        assert client.post(
            "/api/analyst/explain/AAPL",
            json={"question": "Why this signal?", "timeframe": "daily"},
        ).status_code == 503
    else:
        # Enabled path can still fail transiently (provider/network/rate limit);
        # contract is that it should not 404/405 when payload is valid.
        assert client.get("/api/analyst/polish/AAPL?timeframe=daily").status_code in {200, 502}
        assert client.get("/api/analyst/brief?timeframe=daily").status_code in {200, 500, 502}
        assert client.post(
            "/api/analyst/explain/AAPL",
            json={"question": "Why this signal?", "timeframe": "daily"},
        ).status_code in {200, 502}


def test_analyst_explain_validation(client: TestClient):
    cfg = _json(client, "/api/analyst/llm-config")
    if not cfg["enabled"]:
        pytest.skip("LLM disabled; validation path is gated by 503 check first")

    # When enabled, invalid payload should be validated before remote call.
    r = client.post("/api/analyst/explain/AAPL", json={"question": "", "timeframe": "daily"})
    assert r.status_code == 400

    r = client.post(
        "/api/analyst/explain/AAPL",
        json={"question": "Explain please", "timeframe": "monthly"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Signal report flow
# ---------------------------------------------------------------------------


def test_signal_report_json_markdown_text(client: TestClient):
    report_json = _json(client, "/api/report/signals?timeframe=daily")
    for k in (
        "generated_at_utc",
        "generated_at_est",
        "timeframe",
        "app",
        "powered_by",
        "regime",
        "aggregate",
        "tickers",
        "methodology",
        "caveats",
    ):
        assert k in report_json
    assert report_json["timeframe"] == "daily"
    assert isinstance(report_json["tickers"], list)

    md = client.get("/api/report/signals.md?timeframe=daily")
    assert md.status_code == 200
    assert "text/markdown" in md.headers.get("content-type", "")
    assert "# Sianna Financials — Signal Analysis Report" in md.text

    md_dl = client.get("/api/report/signals.md?timeframe=daily&download=true")
    assert md_dl.status_code == 200
    cd = md_dl.headers.get("content-disposition", "")
    assert "attachment;" in cd
    assert "sianna_signal_report_daily_" in cd

    txt = client.get("/api/report/signals.txt?timeframe=daily")
    assert txt.status_code == 200
    assert "text/plain" in txt.headers.get("content-type", "")
    assert "Signal Analysis Report" in txt.text


@pytest.mark.parametrize(
    "url",
    [
        "/api/report/signals?timeframe=monthly",
        "/api/report/signals.md?timeframe=monthly",
        "/api/report/signals.txt?timeframe=monthly",
    ],
)
def test_signal_report_timeframe_validation(client: TestClient, url: str):
    r = client.get(url)
    assert r.status_code == 400

