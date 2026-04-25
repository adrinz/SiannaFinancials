"""Unit tests for dashboard market news aggregation."""
from __future__ import annotations

import io
import sys
import types
from pathlib import Path

# Make the web app + signals package importable without install.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parent / "square18_signals" / "src"))

from app.analyst import market  # noqa: E402


def test_fetch_marketwatch_rss_parses_items(monkeypatch):
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss><channel>
  <item>
    <title>Stocks rise on tech strength</title>
    <link>https://example.com/story-1</link>
    <pubDate>Wed, 22 Apr 2026 12:15:00 GMT</pubDate>
    <description><![CDATA[<p>Indexes moved higher.</p>]]></description>
  </item>
  <item>
    <title>Bond yields ease</title>
    <link>https://example.com/story-2</link>
    <pubDate>Wed, 22 Apr 2026 11:05:00 GMT</pubDate>
    <description><![CDATA[<p>Treasuries rallied.</p>]]></description>
  </item>
</channel></rss>
"""

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(market.urllib.request, "urlopen", lambda *args, **kwargs: _Resp(xml))
    items = market._fetch_marketwatch_rss(limit=5)
    assert len(items) >= 2
    assert items[0].title
    assert items[0].publisher == "MarketWatch"
    assert items[0].related == "MKT"
    assert "<p>" not in items[0].summary


def test_news_feed_prefers_cnbc_rss(monkeypatch):
    monkeypatch.setattr(
        market,
        "_fetch_cnbc_rss",
        lambda limit: [
            market.NewsItem(
                title="Headline from CNBC",
                publisher="CNBC",
                url="https://www.cnbc.com/example",
                related="MKT",
                published_at="2026-04-22T12:00:00+00:00",
                summary="",
            )
        ][:limit],
    )
    called_mw: dict[str, int] = {"n": 0}

    def _track_mw(limit: int) -> list:
        called_mw["n"] += 1
        return []

    monkeypatch.setattr(market, "_fetch_marketwatch_rss", _track_mw)
    feed = market.news_feed(limit=3)
    assert feed.source == "cnbc-rss"
    assert len(feed.items) == 1
    assert feed.items[0].title == "Headline from CNBC"
    assert called_mw["n"] == 0


def test_news_feed_uses_marketwatch_when_cnbc_empty(monkeypatch):
    monkeypatch.setattr(market, "_fetch_cnbc_rss", lambda _limit: [])
    monkeypatch.setattr(
        market,
        "_fetch_marketwatch_rss",
        lambda limit: [
            market.NewsItem(
                title="Fallback headline",
                publisher="MarketWatch",
                url="https://example.com/fallback",
                related="MKT",
                published_at="2026-04-22T12:15:00+00:00",
                summary="Fallback summary",
            )
        ][:limit],
    )
    feed = market.news_feed(limit=3)
    assert feed.source == "marketwatch-rss"
    assert len(feed.items) == 1
    assert feed.items[0].title == "Fallback headline"


def test_news_feed_uses_internal_snapshot_when_external_sources_empty(monkeypatch):
    monkeypatch.setattr(market, "_fetch_cnbc_rss", lambda _limit: [])
    monkeypatch.setattr(market, "_fetch_marketwatch_rss", lambda _limit: [])
    monkeypatch.setattr(
        market,
        "overview_rows",
        lambda _timeframe="daily": [
            types.SimpleNamespace(
                symbol="AAPL",
                name="Apple",
                sector="Tech",
                change_pct=1.25,
                verdict="BULLISH",
                conviction=0.78,
            ),
            types.SimpleNamespace(
                symbol="TSLA",
                name="Tesla",
                sector="Autos",
                change_pct=-0.85,
                verdict="NEUTRAL",
                conviction=0.51,
            ),
        ],
    )
    feed = market.news_feed(limit=4)
    assert feed.source == "internal-snapshot"
    assert len(feed.items) >= 3
    assert any("Market breadth update" in i.title for i in feed.items)
