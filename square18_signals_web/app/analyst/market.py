"""Market-wide aggregates for the Dashboard view.

These functions compose data from the existing analyst pipeline
(``overview_rows``) plus a handful of additional live calls (crypto via
yfinance with CoinGecko fallback, news via CNBC and MarketWatch RSS) and
return shapes the Dashboard UI can render without additional backend calls.

Design
------
* Everything here is a thin aggregator — no new math; just slicing,
  sorting, grouping, and formatting on top of what ``build_report``
  already produces.
* Crypto and news are best-effort. If the network path fails
  we return empty lists rather than raising, so a dashboard card can
  degrade gracefully.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from statistics import mean
from typing import Optional
import urllib.request
import xml.etree.ElementTree as ET

from .constants import TICKERS, Timeframe, yfinance_disabled
from .data import _threaded_with_timeout, _YF_REQUEST_TIMEOUT
from .models import OverviewRow
from .report import overview_rows, overview_rows_fast


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
    rows = overview_rows_fast(timeframe)
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
    rows = overview_rows_fast(timeframe)

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
    {"symbol": "BNB-USD",  "name": "BNB"},
    {"symbol": "TRX-USD",  "name": "TRON"},
    {"symbol": "SUI-USD",  "name": "Sui"},
    {"symbol": "TON-USD",  "name": "Toncoin"},
    {"symbol": "SHIB-USD", "name": "Shiba Inu"},
    {"symbol": "LTC-USD",  "name": "Litecoin"},
    {"symbol": "BCH-USD",  "name": "Bitcoin Cash"},
    {"symbol": "DOT-USD",  "name": "Polkadot"},
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


def _fetch_coinmarketcap_snapshot() -> list[CryptoRow]:
    """Fallback crypto feed using public CoinMarketCap data API."""
    url = (
        "https://api.coinmarketcap.com/data-api/v3/cryptocurrency/listing"
        "?start=1&limit=200&sortBy=market_cap&sortType=desc&convert=USD"
        "&cryptoType=all&tagType=all"
    )
    req = urllib.request.Request(url, headers=_RSS_HTTP_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    rows_raw = (
        (payload.get("data") or {}).get("cryptoCurrencyList")
        if isinstance(payload.get("data"), dict)
        else None
    )
    if not isinstance(rows_raw, list):
        return []

    by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in rows_raw
        if isinstance(item, dict) and item.get("symbol")
    }

    out: list[CryptoRow] = []
    for meta in CRYPTO_SYMBOLS:
        sym = str(meta["symbol"]).replace("-USD", "").upper()
        item = by_symbol.get(sym)
        if not item:
            continue
        quotes = item.get("quotes")
        quote = quotes[0] if isinstance(quotes, list) and quotes else {}
        if not isinstance(quote, dict):
            quote = {}
        try:
            last = float(quote.get("price"))
        except Exception:
            continue
        c24 = quote.get("percentChange24h")
        c7 = quote.get("percentChange7d")
        high24 = quote.get("high24h")
        low24 = quote.get("low24h")
        # Listing endpoint does not provide sparkline; build a bounded proxy
        # from low/high/current so the tile remains informative.
        spark: list[float] = []
        try:
            hi = float(high24)
            lo = float(low24)
            if hi > 0 and lo > 0 and hi >= lo:
                mid = (hi + lo) / 2
                spark = [
                    lo,
                    lo * 1.01,
                    mid * 0.99,
                    mid * 1.01,
                    hi * 0.995,
                    last,
                ]
        except Exception:
            spark = []
        out.append(
            CryptoRow(
                symbol=meta["symbol"],  # keep -USD shape for UI consistency
                name=meta["name"],
                last=round(last, 4 if last < 1 else 2),
                change_pct_24h=round(float(c24 or 0.0), 2),
                change_pct_7d=round(float(c7 or 0.0), 2),
                spark=spark if spark else [last],
            )
        )
    return out


def crypto_snapshot() -> CryptoSnapshot:
    rows: list[CryptoRow] = []
    source = "yfinance"
    if not yfinance_disabled():
        try:
            import yfinance as yf  # type: ignore
        except Exception:
            yf = None

        if yf is not None:
            for meta in CRYPTO_SYMBOLS:
                try:
                    sym = meta["symbol"]

                    def _pull_hist():
                        t = yf.Ticker(sym)
                        return t.history(
                            period="60d", interval="1d", auto_adjust=True, actions=False
                        )

                    # Same hard cap as OHLCV so a stuck Yahoo socket cannot block the dashboard.
                    hist = _threaded_with_timeout(
                        _pull_hist, min(45.0, _YF_REQUEST_TIMEOUT)
                    )
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
        rows = _fetch_coinmarketcap_snapshot()
        if rows:
            source = "www.coinmarketcap.com"
        else:
            source = "unavailable"
    return CryptoSnapshot(rows=rows, source=source)


# ---------------------------------------------------------------------------
# News feed
# ---------------------------------------------------------------------------


# Official CNBC section RSS (two feeds merged + deduped, then newest-first).
CNBC_RSS_URLS: list[str] = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # US Top News and Analysis
    "https://www.cnbc.com/id/10001147/device/rss/rss.html",  # Business News
]

_RSS_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


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


def _strip_html(text: str) -> str:
    """Very small HTML-to-text cleaner for RSS summaries."""
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


def _rss_pubdate_to_iso(pub: str) -> str:
    """Normalise RSS ``pubDate`` to ISO-8601 UTC; fall back to the raw string."""
    if not pub or not str(pub).strip():
        return ""
    try:
        dt = parsedate_to_datetime(str(pub).strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(pub).strip()


def _fetch_rss_from_url(
    feed_url: str,
    *,
    publisher: str,
    related: str,
    max_items: int,
) -> list[NewsItem]:
    """Read up to ``max_items`` unique titles from a single RSS document."""
    try:
        req = urllib.request.Request(feed_url, headers=_RSS_HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            payload = resp.read()
        root = ET.fromstring(payload)
    except Exception:
        return []

    out: list[NewsItem] = []
    for node in root.findall(".//item"):
        if len(out) >= max_items:
            break
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        link = (node.findtext("link") or "").strip()
        pub_raw = (node.findtext("pubDate") or "").strip()
        summary = _strip_html(node.findtext("description") or "")
        out.append(
            NewsItem(
                title=title,
                publisher=publisher,
                url=link,
                related=related,
                published_at=_rss_pubdate_to_iso(pub_raw) or pub_raw,
                summary=summary,
            )
        )
    return out


def _fetch_cnbc_rss(limit: int) -> list[NewsItem]:
    """Primary dashboard news: two CNBC section feeds, deduped, newest first."""
    seen: set[str] = set()
    acc: list[NewsItem] = []
    per = max(8, min(30, limit * 2))
    for url in CNBC_RSS_URLS:
        for it in _fetch_rss_from_url(url, publisher="CNBC", related="MKT", max_items=per):
            if it.title in seen:
                continue
            seen.add(it.title)
            acc.append(it)
    acc.sort(key=lambda n: n.published_at or "", reverse=True)
    return acc[:limit]


def _fetch_marketwatch_rss(limit: int) -> list[NewsItem]:
    """Fallback when CNBC RSS is unavailable or empty."""
    seen_titles: set[str] = set()
    items: list[NewsItem] = []
    for feed_url in (
        "https://www.marketwatch.com/rss/topstories",
        "https://www.marketwatch.com/rss/marketpulse",
    ):
        if len(items) >= limit:
            break
        for it in _fetch_rss_from_url(
            feed_url, publisher="MarketWatch", related="MKT", max_items=limit
        ):
            if it.title in seen_titles:
                continue
            seen_titles.add(it.title)
            items.append(it)
            if len(items) >= limit:
                break
    items.sort(key=lambda n: n.published_at or "", reverse=True)
    return items[:limit]


def _build_internal_snapshot_news(limit: int) -> list[NewsItem]:
    """Generate headline-style updates from the current analyst overview."""
    rows = overview_rows_fast("daily")
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


def news_for_ticker(ticker: str, limit: int = 8) -> list[NewsItem]:
    """Headlines that mention the ticker from live RSS feeds."""
    t = ticker.upper()
    t_word = re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)
    feed = news_feed(50)
    out: list[NewsItem] = []
    seen: set[str] = set()

    for item in feed.items:
        if item.title in seen:
            continue
        rel = (item.related or "").upper()
        if t in rel or t in rel.split() or t_word.search(item.title or "") or t_word.search(
            item.summary or ""
        ):
            seen.add(item.title)
            out.append(item)
        if len(out) >= limit:
            return out

    for item in feed.items:
        if item.title in seen:
            continue
        seen.add(item.title)
        out.append(item)
        if len(out) >= limit:
            break
    return out[:limit]


def news_feed(limit: int = 12) -> NewsFeed:
    # CNBC first. If it does not fully fill the requested limit, top up from
    # MarketWatch, then internal snapshot so the dashboard card remains populated.
    items = _fetch_cnbc_rss(limit)
    seen_titles = {i.title for i in items}
    source_parts: list[str] = ["cnbc-rss"] if items else []

    if len(items) < limit:
        mw_needed = max(0, limit - len(items))
        mw_items = _fetch_marketwatch_rss(mw_needed)
        added = 0
        for it in mw_items:
            if it.title in seen_titles:
                continue
            seen_titles.add(it.title)
            items.append(it)
            added += 1
            if len(items) >= limit:
                break
        if added:
            source_parts.append("marketwatch-rss")

    if len(items) < limit:
        snap_needed = max(0, limit - len(items))
        internal_items = _build_internal_snapshot_news(snap_needed)
        added = 0
        for it in internal_items:
            if it.title in seen_titles:
                continue
            seen_titles.add(it.title)
            items.append(it)
            added += 1
            if len(items) >= limit:
                break
        if added:
            source_parts.append("internal-snapshot")

    if items:
        return NewsFeed(items=items[:limit], source="+".join(source_parts))
    internal_items = _build_internal_snapshot_news(limit)
    if internal_items:
        return NewsFeed(items=internal_items, source="internal-snapshot")
    return NewsFeed(items=[], source="unavailable")
