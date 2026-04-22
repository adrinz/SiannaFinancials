"""Market-wide aggregates for the Dashboard view.

These functions compose data from the existing analyst pipeline
(``overview_rows``) plus a handful of additional live calls (crypto via
yfinance, news via yfinance.Ticker.news) and return shapes the Dashboard
UI can render without additional backend calls.

Design
------
* Everything here is a thin aggregator — no new math; just slicing,
  sorting, grouping, and formatting on top of what ``build_report``
  already produces.
* Crypto and news are best-effort. If the network/yfinance path fails
  we return empty lists rather than raising, so a dashboard card can
  degrade gracefully.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from statistics import mean
from typing import Optional
import urllib.request
import xml.etree.ElementTree as ET

from .constants import TICKERS, Timeframe
from .models import OverviewRow
from .report import overview_rows


# ---------------------------------------------------------------------------
# Movers & sector heatmap
# ---------------------------------------------------------------------------


@dataclass
class MoverRow:
    symbol: str
    name: str
    sector: str
    last: float
    change_pct: float
    verdict: str


@dataclass
class SectorRow:
    sector: str
    avg_change_pct: float
    count: int
    bullish: int
    bearish: int
    neutral: int
    tickers: list[str]


@dataclass
class MarketPulse:
    timeframe: Timeframe
    top_gainers: list[MoverRow]
    top_losers: list[MoverRow]
    sector_heatmap: list[SectorRow]
    breadth_pct_up: float
    tickers_covered: int


def market_pulse(timeframe: Timeframe = "daily", top_n: int = 5) -> MarketPulse:
    rows = overview_rows(timeframe)
    movers = [
        MoverRow(
            symbol=r.symbol,
            name=r.name,
            sector=r.sector,
            last=r.last,
            change_pct=r.change_pct,
            verdict=r.verdict,
        )
        for r in rows
    ]
    movers_by_change = sorted(movers, key=lambda m: m.change_pct, reverse=True)
    gainers = [m for m in movers_by_change if m.change_pct > 0][:top_n]
    losers = sorted(
        [m for m in movers_by_change if m.change_pct < 0],
        key=lambda m: m.change_pct,
    )[:top_n]

    # Group by sector with average change + verdict tallies.
    by_sector: dict[str, list[MoverRow]] = {}
    for m in movers:
        by_sector.setdefault(m.sector, []).append(m)
    heatmap: list[SectorRow] = []
    for sector, items in by_sector.items():
        heatmap.append(
            SectorRow(
                sector=sector,
                avg_change_pct=round(mean([i.change_pct for i in items]), 2),
                count=len(items),
                bullish=sum(1 for i in items if i.verdict == "BULLISH"),
                bearish=sum(1 for i in items if i.verdict == "BEARISH"),
                neutral=sum(1 for i in items if i.verdict == "NEUTRAL"),
                tickers=[i.symbol for i in items],
            )
        )
    heatmap.sort(key=lambda s: s.avg_change_pct, reverse=True)

    if movers:
        breadth = sum(1 for m in movers if m.change_pct > 0) / len(movers) * 100
    else:
        breadth = 0.0

    return MarketPulse(
        timeframe=timeframe,
        top_gainers=gainers,
        top_losers=losers,
        sector_heatmap=heatmap,
        breadth_pct_up=round(breadth, 1),
        tickers_covered=len(movers),
    )


# ---------------------------------------------------------------------------
# Options highlights
# ---------------------------------------------------------------------------


@dataclass
class OptionRec:
    symbol: str
    name: str
    sector: str
    contract_type: str       # "call" | "put"
    strike: Optional[float]
    expiry_date: Optional[str]
    expiry_dte: Optional[int]
    cost_per_contract: Optional[float]
    break_even: Optional[float]
    target_price: Optional[float]
    risk_reward: Optional[float]
    verdict: str
    conviction: float
    change_pct: float
    last: float


@dataclass
class OptionsHighlights:
    timeframe: Timeframe
    top_calls: list[OptionRec]
    top_puts: list[OptionRec]


def options_highlights(timeframe: Timeframe = "daily", top_n: int = 5) -> OptionsHighlights:
    rows = overview_rows(timeframe)

    def _rec(r: OverviewRow) -> OptionRec:
        return OptionRec(
            symbol=r.symbol,
            name=r.name,
            sector=r.sector,
            contract_type=r.rec_contract_type or "none",
            strike=r.rec_strike,
            expiry_date=r.rec_expiry_date,
            expiry_dte=r.rec_expiry_dte,
            cost_per_contract=r.rec_cost_per_contract,
            break_even=r.rec_break_even,
            target_price=r.rec_target,
            risk_reward=r.rec_risk_reward,
            verdict=r.verdict,
            conviction=r.conviction,
            change_pct=r.change_pct,
            last=r.last,
        )

    calls = [_rec(r) for r in rows if r.rec_contract_type == "call"]
    puts  = [_rec(r) for r in rows if r.rec_contract_type == "put"]

    # Rank by (risk/reward if present, else 0) * conviction — favours plays
    # that are both high-conviction and have a clean R:R.
    def _score(o: OptionRec) -> float:
        rr = o.risk_reward if o.risk_reward is not None else 0.0
        return rr * max(0.0, o.conviction)

    calls.sort(key=_score, reverse=True)
    puts.sort(key=_score, reverse=True)
    return OptionsHighlights(
        timeframe=timeframe,
        top_calls=calls[:top_n],
        top_puts=puts[:top_n],
    )


# ---------------------------------------------------------------------------
# Crypto snapshot
# ---------------------------------------------------------------------------


CRYPTO_SYMBOLS: list[dict] = [
    {"symbol": "BTC-USD",  "name": "Bitcoin"},
    {"symbol": "ETH-USD",  "name": "Ethereum"},
    {"symbol": "SOL-USD",  "name": "Solana"},
    {"symbol": "XRP-USD",  "name": "XRP"},
    {"symbol": "DOGE-USD", "name": "Dogecoin"},
    {"symbol": "ADA-USD",  "name": "Cardano"},
    {"symbol": "AVAX-USD", "name": "Avalanche"},
    {"symbol": "LINK-USD", "name": "Chainlink"},
]


@dataclass
class CryptoRow:
    symbol: str
    name: str
    last: float
    change_pct_24h: float
    change_pct_7d: float
    spark: list[float]  # last 30 closes


@dataclass
class CryptoSnapshot:
    rows: list[CryptoRow]
    source: str


def crypto_snapshot() -> CryptoSnapshot:
    rows: list[CryptoRow] = []
    source = "yfinance"
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return CryptoSnapshot(rows=[], source="unavailable")

    for meta in CRYPTO_SYMBOLS:
        try:
            t = yf.Ticker(meta["symbol"])
            hist = t.history(period="60d", interval="1d", auto_adjust=True, actions=False)
            if hist is None or hist.empty or "Close" not in hist.columns:
                continue
            hist = hist.dropna(subset=["Close"])
            if hist.empty:
                continue
            closes = [float(x) for x in hist["Close"].tolist()]
            last = closes[-1]
            prev = closes[-2] if len(closes) >= 2 else last
            wk_ago = closes[-8] if len(closes) >= 8 else closes[0]
            change_24h = (last / prev - 1) * 100 if prev else 0.0
            change_7d = (last / wk_ago - 1) * 100 if wk_ago else 0.0
            spark = closes[-30:]
            rows.append(
                CryptoRow(
                    symbol=meta["symbol"],
                    name=meta["name"],
                    last=round(last, 4 if last < 1 else 2),
                    change_pct_24h=round(change_24h, 2),
                    change_pct_7d=round(change_7d, 2),
                    spark=spark,
                )
            )
        except Exception:
            continue

    if not rows:
        source = "unavailable"
    return CryptoSnapshot(rows=rows, source=source)


# ---------------------------------------------------------------------------
# News feed
# ---------------------------------------------------------------------------


# Always include a few broad-market tickers so the feed isn't dominated
# by a single-name story.
NEWS_TICKERS: list[str] = [
    "SPY", "QQQ", "AAPL", "NVDA", "MSFT", "AMZN", "TSLA", "META",
    "GOOGL", "BTC-USD", "ETH-USD",
]


@dataclass
class NewsItem:
    title: str
    publisher: str
    url: str
    related: str     # primary ticker (comma-joined if multiple)
    published_at: str  # ISO-8601 UTC
    summary: str = ""


@dataclass
class NewsFeed:
    items: list[NewsItem]
    source: str


def _parse_yf_news_entry(entry: dict) -> Optional[dict]:
    """Normalise yfinance news entries across versions.

    Recent yfinance returns ``{'content': {...}}``; older versions emit a
    flat dict. This accepts both.
    """
    if not isinstance(entry, dict):
        return None
    content = entry.get("content") if isinstance(entry.get("content"), dict) else entry
    if not isinstance(content, dict):
        return None
    title = content.get("title") or ""
    pub = (
        (content.get("provider") or {}).get("displayName")
        if isinstance(content.get("provider"), dict)
        else content.get("publisher")
    ) or ""
    url = ""
    cu = content.get("canonicalUrl")
    if isinstance(cu, dict):
        url = cu.get("url") or ""
    if not url:
        cl = content.get("clickThroughUrl")
        if isinstance(cl, dict):
            url = cl.get("url") or ""
    if not url:
        url = content.get("link") or entry.get("link") or ""
    pub_date = (
        content.get("pubDate")
        or content.get("displayTime")
        or content.get("providerPublishTime")
        or entry.get("providerPublishTime")
        or ""
    )
    summary = content.get("summary") or content.get("description") or ""
    return {
        "title": title,
        "publisher": pub,
        "url": url,
        "published_at": pub_date,
        "summary": summary,
    }


def _coerce_ts(val) -> str:
    """Return ISO-8601 UTC. Accept int epoch, str, or blank."""
    from datetime import datetime, timezone
    if isinstance(val, (int, float)) and val > 0:
        try:
            return datetime.fromtimestamp(int(val), tz=timezone.utc).isoformat()
        except Exception:
            return ""
    if isinstance(val, str):
        return val
    return ""


def _strip_html(text: str) -> str:
    """Very small HTML-to-text cleaner for RSS summaries."""
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


def _fetch_marketwatch_rss(limit: int) -> list[NewsItem]:
    """Fallback live news source when yfinance news is unavailable."""
    feeds = [
        "https://www.marketwatch.com/rss/topstories",
        "https://www.marketwatch.com/rss/marketpulse",
    ]
    seen_titles: set[str] = set()
    items: list[NewsItem] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    for feed_url in feeds:
        if len(items) >= limit:
            break
        try:
            req = urllib.request.Request(feed_url, headers=headers)
            with urllib.request.urlopen(req, timeout=6) as resp:  # noqa: S310 (public RSS URL)
                payload = resp.read()
            root = ET.fromstring(payload)
        except Exception:
            continue

        for node in root.findall(".//item"):
            if len(items) >= limit:
                break
            title = (node.findtext("title") or "").strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            link = (node.findtext("link") or "").strip()
            pub_date = (node.findtext("pubDate") or "").strip()
            summary = _strip_html(node.findtext("description") or "")
            items.append(
                NewsItem(
                    title=title,
                    publisher="MarketWatch",
                    url=link,
                    related="MKT",
                    published_at=pub_date,
                    summary=summary,
                )
            )
    return items


def _build_internal_snapshot_news(limit: int) -> list[NewsItem]:
    """Generate headline-style updates from the current analyst overview."""
    rows = overview_rows("daily")
    if not rows:
        return []

    now = datetime.now(timezone.utc).isoformat()
    items: list[NewsItem] = []

    up = sum(1 for r in rows if r.change_pct > 0)
    breadth = round((up / len(rows)) * 100, 1) if rows else 0.0
    items.append(
        NewsItem(
            title=f"Market breadth update: {breadth}% of tracked names are up today",
            publisher="Sianna Internal",
            url="",
            related="MKT",
            published_at=now,
            summary=(
                f"Coverage includes {len(rows)} tickers from the analyst universe. "
                "This is an internal snapshot generated when external feeds are unavailable."
            ),
        )
    )

    movers = sorted(rows, key=lambda r: r.change_pct, reverse=True)
    top_gainer = movers[0]
    top_loser = movers[-1]
    items.append(
        NewsItem(
            title=f"{top_gainer.symbol} leads gainers at {top_gainer.change_pct:+.2f}%",
            publisher="Sianna Internal",
            url="",
            related=top_gainer.symbol,
            published_at=now,
            summary=(
                f"{top_gainer.name} ({top_gainer.sector}) currently shows a "
                f"{top_gainer.verdict.lower()} setup with conviction {top_gainer.conviction:.2f}."
            ),
        )
    )
    items.append(
        NewsItem(
            title=f"{top_loser.symbol} trails at {top_loser.change_pct:+.2f}%",
            publisher="Sianna Internal",
            url="",
            related=top_loser.symbol,
            published_at=now,
            summary=(
                f"{top_loser.name} ({top_loser.sector}) is the weakest mover in "
                "the current tracked set."
            ),
        )
    )

    for row in movers[: max(0, limit - len(items))]:
        items.append(
            NewsItem(
                title=f"{row.symbol}: {row.change_pct:+.2f}% on the session",
                publisher="Sianna Internal",
                url="",
                related=row.symbol,
                published_at=now,
                summary=(
                    f"{row.name} in {row.sector}. Verdict: {row.verdict}. "
                    f"Conviction: {row.conviction:.2f}."
                ),
            )
        )
        if len(items) >= limit:
            break

    return items[:limit]


def news_feed(limit: int = 12) -> NewsFeed:
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return NewsFeed(items=[], source="unavailable")

    seen_titles: set[str] = set()
    items: list[NewsItem] = []
    for sym in NEWS_TICKERS:
        if len(items) >= limit * 2:
            break
        try:
            t = yf.Ticker(sym)
            raw = getattr(t, "news", None) or []
        except Exception:
            raw = []
        for entry in raw:
            parsed = _parse_yf_news_entry(entry)
            if not parsed or not parsed["title"]:
                continue
            title = parsed["title"].strip()
            if title in seen_titles:
                continue
            seen_titles.add(title)
            items.append(
                NewsItem(
                    title=title,
                    publisher=parsed["publisher"] or "—",
                    url=parsed["url"] or "",
                    related=sym,
                    published_at=_coerce_ts(parsed["published_at"]),
                    summary=parsed["summary"],
                )
            )

    # Sort newest first when timestamps are comparable, otherwise keep order.
    def _key(n: NewsItem):
        return n.published_at or ""
    items.sort(key=_key, reverse=True)

    if items:
        return NewsFeed(items=items[:limit], source="yfinance")

    fallback_items = _fetch_marketwatch_rss(limit)
    if fallback_items:
        return NewsFeed(items=fallback_items[:limit], source="marketwatch-rss")

    internal_items = _build_internal_snapshot_news(limit)
    if internal_items:
        return NewsFeed(items=internal_items, source="internal-snapshot")

    return NewsFeed(items=[], source="unavailable")
