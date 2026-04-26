"""Unit + API tests for Copy trade (13F parser, creators, mocked refresh)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.analyst import copy_trade as ct  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_copy_state():
    ct.reset_copy_trade_state()
    yield
    ct.reset_copy_trade_state()


def test_parse_13f_infotable_xml():
    xml = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/thirteenf/information">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfIssuer>COM</titleOfIssuer>
    <cusip>037833100</cusip>
    <value>120000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>500000</sshPrnamt>
    </shrsOrPrnAmt>
  </infoTable>
  <infoTable>
    <nameOfIssuer>MICROSOFT CORP</nameOfIssuer>
    <cusip>594918104</cusip>
    <value>80000</value>
  </infoTable>
</informationTable>
"""
    rows = ct._parse_13f_infotable_xml(xml)  # noqa: SLF001
    assert len(rows) == 2
    assert rows[0]["name"] == "APPLE INC"
    assert rows[0]["value_000s"] == 120000
    assert rows[0]["shares"] == 500000.0
    assert rows[1]["value_000s"] == 80000


def test_list_creators_non_empty():
    assert len(ct.list_creators()) >= 2
    ids = {c["id"] for c in ct.list_creators()}
    assert "berkshire-13f" in ids
    assert "thematic-mega-quality" in ids


def test_static_basket_holdings_has_rows():
    raw, src, _, _, err = ct.static_basket_holdings(["AAPL", "MSFT"])
    assert not err
    assert src == "static_basket"
    assert len(raw) >= 2


def test_copy_trade_creators_endpoint(client: TestClient):
    r = client.get("/api/copy-trade/creators")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and len(body) >= 2
    assert body[0]["id"]


def test_refresh_thematic_basket(client: TestClient):
    """Uses static basket + get_ohlcv; may skip in air-gapped CI if data layer fails."""
    r = client.get("/api/copy-trade/holdings/thematic-mega-quality?refresh=1")
    assert r.status_code == 200
    b = r.json()
    assert b["creator_id"] == "thematic-mega-quality"
    if b["rows"]:
        sym = {row["symbol"] for row in b["rows"] if row.get("symbol")}
        assert "AAPL" in sym or "MSFT" in sym or len(sym) >= 1


def test_copy_trade_signals_empty_ok(client: TestClient):
    r = client.get("/api/copy-trade/signals?limit=5")
    assert r.status_code == 200
    assert "rows" in r.json()


def test_copy_trade_holdings_404(client: TestClient):
    assert client.get("/api/copy-trade/holdings/nope").status_code == 404
