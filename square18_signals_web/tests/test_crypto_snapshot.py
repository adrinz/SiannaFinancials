"""Unit tests for dashboard crypto snapshot fallback behavior."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

# Make the web app + signals package importable without install.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.analyst import market  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_crypto_snapshot_uses_coinmarketcap_when_yfinance_disabled(monkeypatch):
    monkeypatch.setenv("SQUARE18_DISABLE_YF", "1")
    payload = {
        "data": {
            "cryptoCurrencyList": [
                {
                    "symbol": "BTC",
                    "quotes": [
                        {
                            "price": 68000.12,
                            "percentChange24h": 1.23,
                            "percentChange7d": 4.56,
                            "high24h": 69000.0,
                            "low24h": 67000.0,
                        }
                    ],
                },
                {
                    "symbol": "ETH",
                    "quotes": [
                        {
                            "price": 3100.5,
                            "percentChange24h": -0.8,
                            "percentChange7d": 2.1,
                            "high24h": 3200.0,
                            "low24h": 3000.0,
                        }
                    ],
                },
            ]
        }
    }
    monkeypatch.setattr(
        market.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Resp(json.dumps(payload).encode("utf-8")),
    )

    snap = market.crypto_snapshot()
    assert snap.source == "www.coinmarketcap.com"
    assert len(snap.rows) >= 2
    assert snap.rows[0].symbol in {"BTC-USD", "ETH-USD"}


def test_crypto_snapshot_unavailable_when_all_feeds_fail(monkeypatch):
    monkeypatch.setenv("SQUARE18_DISABLE_YF", "1")
    monkeypatch.setattr(
        market.urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    snap = market.crypto_snapshot()
    assert snap.source == "unavailable"
    assert snap.rows == []
