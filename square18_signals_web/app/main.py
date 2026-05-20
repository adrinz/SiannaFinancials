"""FastAPI entry point for the Sianna Financials web app."""
from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make the sibling `square18_signals` package importable without install.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_SIGNALS_SRC = _REPO / "square18_signals" / "src"
if str(_SIGNALS_SRC) not in sys.path:
    sys.path.insert(0, str(_SIGNALS_SRC))

# Load local secrets from square18_signals_web/.env (gitignored).
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None
if load_dotenv is not None:
    load_dotenv(_HERE.parent / ".env")

from .analyst import TICKERS as ANALYST_TICKERS  # noqa: E402
from .analyst.constants import SCREENER_EARNINGS_WINDOW_DAYS  # noqa: E402
from .analyst import build_report, overview_rows  # noqa: E402
from .analyst.report import etf_overview_rows  # noqa: E402
from .analyst import earnings as _earnings  # noqa: E402
from .analyst import llm as _llm  # noqa: E402
from .analyst import market as _market  # noqa: E402
from .analyst import movers as _movers  # noqa: E402
from .analyst import copy_trade as _copy_trade  # noqa: E402
from .analyst import search as _search  # noqa: E402
from .analyst.models import OverviewRow, ReportOut, TickerMeta  # noqa: E402
from .models import RegimeEnvelope, TickerDetailOut, TickerRowOut  # noqa: E402
from .services import (  # noqa: E402
    regime_envelope,
    screener_rows,
    ticker_detail,
)
from .signal_report import (  # noqa: E402
    build_signal_report,
    render_markdown as render_signal_report_md,
    render_text as render_signal_report_txt,
    to_dict as signal_report_to_dict,
)

app = FastAPI(
    title="Sianna Financials",
    description=(
        "Stocks & options analyzer — daily screener with buy/sell signals "
        "and a real options strategy recommender."
    ),
    version="0.1.0",
)

_STATIC_DIR = _HERE.parent / "static"
_log = logging.getLogger(__name__)


def _warm_analyst_overview_cache() -> None:
    """Pre-build daily overview so Analyst tab is not empty on first open."""
    try:
        overview_rows("daily")  # type: ignore[arg-type]
        _log.info("Warmed analyst overview cache (daily)")
    except Exception as exc:
        _log.warning("Analyst overview warm-up skipped: %s", exc)


@app.on_event("startup")
def _startup_warm_overview() -> None:
    threading.Thread(
        target=_warm_analyst_overview_cache,
        name="overview-warm-daily",
        daemon=True,
    ).start()


@app.get("/api/regime", response_model=RegimeEnvelope, tags=["dashboard"])
def get_regime() -> RegimeEnvelope:
    return regime_envelope(datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"))


@app.get("/api/screen", response_model=list[TickerRowOut], tags=["dashboard"])
def get_screen(filter: str = "all") -> list[TickerRowOut]:
    allowed = {"all", "buy", "sell", "hold"}
    f = filter.lower()
    if f not in allowed:
        raise HTTPException(400, f"filter must be one of {sorted(allowed)}")
    return screener_rows(f)


_ALLOWED_CHART_RANGES = frozenset({"1d", "5d", "1m", "6m", "1y", "ytd"})


@app.get("/api/ticker/{symbol}", response_model=TickerDetailOut, tags=["detail"])
def get_ticker(
    symbol: str,
    chart_range: str = Query(
        "1d",
        description="Price chart window: 1d, 5d, 1m, 6m, 1y, ytd (default 1d = hourly intraday)",
        alias="range",
    ),
) -> TickerDetailOut:
    rng = chart_range.lower().strip()
    if rng not in _ALLOWED_CHART_RANGES:
        raise HTTPException(400, f"range must be one of {sorted(_ALLOWED_CHART_RANGES)}")
    detail = ticker_detail(symbol, price_range=rng)
    if detail is None:
        raise HTTPException(404, f"unknown symbol: {symbol}")
    return detail


_ALLOWED_TIMEFRAMES = {"1h", "4h", "daily", "weekly"}


@app.get("/api/analyst/tickers", response_model=list[TickerMeta], tags=["analyst"])
def analyst_tickers() -> list[TickerMeta]:
    return [
        TickerMeta(symbol=t["symbol"], name=t["name"], sector=t["sector"])
        for t in ANALYST_TICKERS
    ]


@app.get("/api/analyst/overview", response_model=list[OverviewRow], tags=["analyst"])
def analyst_overview(timeframe: str = "daily") -> list[OverviewRow]:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    return overview_rows(timeframe)  # type: ignore[arg-type]


@app.get("/api/etf/signals", response_model=list[OverviewRow], tags=["etf"])
def etf_signals(timeframe: str = "daily") -> list[OverviewRow]:
    """Verdict + trade-plan summary for the ETF watchlist (``ETF_SIGNAL_TICKERS``)."""
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    return etf_overview_rows(timeframe)  # type: ignore[arg-type]


@app.get(
    "/api/analyst/report/{symbol}",
    response_model=ReportOut,
    tags=["analyst"],
)
def analyst_report(
    symbol: str,
    timeframe: str = "daily",
    fresh_quotes: int = Query(
        1,
        ge=0,
        le=1,
        description=(
            "1 = pull live Yahoo spot + option chain mids for this request (bypass in-app quote cache); "
            "0 = reuse short TTL cache where available (lighter on Yahoo)."
        ),
    ),
) -> ReportOut:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    try:
        return build_report(  # type: ignore[arg-type]
            symbol, timeframe, fresh_quotes=bool(fresh_quotes)
        )
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/report/signals", tags=["report"])
def get_signal_report_json(timeframe: str = "daily") -> dict:
    """Comprehensive signal analysis report in structured JSON.

    Contains: market regime, verdict counts, top long/short ideas,
    sector tilts, methodology notes, full per-ticker breakdown (price
    action + indicators + trade plan), and historical reliability
    stats when ``backtest_verdict.json`` is present.
    """
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    rpt = build_signal_report(timeframe)  # type: ignore[arg-type]
    return signal_report_to_dict(rpt)


@app.get("/api/report/signals.md", tags=["report"])
def get_signal_report_markdown(timeframe: str = "daily", download: bool = False) -> Response:
    """Same report rendered as Markdown — safe to save / email / paste.

    Pass ``?download=true`` to force a file download with a dated filename.
    """
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    rpt = build_signal_report(timeframe)  # type: ignore[arg-type]
    md = render_signal_report_md(rpt)
    headers: dict[str, str] = {}
    if download:
        stamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M")
        headers["Content-Disposition"] = (
            f'attachment; filename="sianna_signal_report_{timeframe}_{stamp}.md"'
        )
    return Response(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers=headers,
    )


@app.get("/api/report/signals.txt", response_class=PlainTextResponse, tags=["report"])
def get_signal_report_text(timeframe: str = "daily") -> str:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    rpt = build_signal_report(timeframe)  # type: ignore[arg-type]
    return render_signal_report_txt(rpt)


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Free-form stock search — Buy / Sell / Hold with entry + exit levels.
# ---------------------------------------------------------------------------


class ResolvedSymbolOut(BaseModel):
    symbol: str
    name: str
    sector: str
    currency: str = "USD"
    exchange: str = ""


class StockPlanOut(BaseModel):
    action: str  # Buy | Sell | Hold
    confidence: float
    entry_price: float
    entry_zone_low: float
    entry_zone_high: float
    target_price: float
    stop_loss: float
    risk_reward: float | None
    expected_move_pct: float
    time_horizon: str
    rationale: str
    caveats: list[str]


class SearchResultOut(BaseModel):
    resolved: ResolvedSymbolOut
    report: ReportOut
    stock_plan: StockPlanOut


@app.get("/api/search", response_model=SearchResultOut, tags=["search"])
def search_stock(q: str, timeframe: str = "daily") -> SearchResultOut:
    if not (q or "").strip():
        raise HTTPException(400, "query `q` is required")
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    try:
        result = _search.search(q, timeframe)  # type: ignore[arg-type]
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"search failed: {e}")

    return SearchResultOut(
        resolved=ResolvedSymbolOut(
            symbol=result.resolved.symbol,
            name=result.resolved.name,
            sector=result.resolved.sector,
            currency=result.resolved.currency,
            exchange=result.resolved.exchange,
        ),
        report=result.report,
        stock_plan=StockPlanOut(**result.stock_plan.__dict__),
    )


@app.get("/api/search/suggest", response_model=list[ResolvedSymbolOut], tags=["search"])
def search_suggest(q: str, limit: int = 6) -> list[ResolvedSymbolOut]:
    if not (q or "").strip():
        return []
    try:
        rows = _search.suggest(q, limit=max(1, min(limit, 12)))
    except Exception:
        rows = []
    return [
        ResolvedSymbolOut(
            symbol=r.symbol,
            name=r.name,
            sector=r.sector,
            currency=r.currency,
            exchange=r.exchange,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Dashboard: market pulse, options highlights, crypto, news.
# Thin aggregators on top of the analyst pipeline and yfinance — each card
# degrades to an empty list rather than 5xx so the page always renders.
# ---------------------------------------------------------------------------


class MoverRowOut(BaseModel):
    symbol: str
    name: str
    sector: str
    last: float
    change_pct: float
    verdict: str


class SectorRowOut(BaseModel):
    sector: str
    avg_change_pct: float
    count: int
    bullish: int
    bearish: int
    neutral: int
    tickers: list[str]


class MarketPulseOut(BaseModel):
    timeframe: str
    top_gainers: list[MoverRowOut]
    top_losers: list[MoverRowOut]
    sector_heatmap: list[SectorRowOut]
    breadth_pct_up: float
    tickers_covered: int


class OptionRecOut(BaseModel):
    symbol: str
    name: str
    sector: str
    contract_type: str
    strike: float | None
    expiry_date: str | None
    expiry_dte: int | None
    cost_per_contract: float | None
    break_even: float | None
    target_price: float | None
    risk_reward: float | None
    verdict: str
    conviction: float
    change_pct: float
    last: float


class OptionsHighlightsOut(BaseModel):
    timeframe: str
    top_calls: list[OptionRecOut]
    top_puts: list[OptionRecOut]


class CryptoRowOut(BaseModel):
    symbol: str
    name: str
    last: float
    change_pct_24h: float
    change_pct_7d: float
    spark: list[float]


class CryptoSnapshotOut(BaseModel):
    rows: list[CryptoRowOut]
    source: str


class NewsItemOut(BaseModel):
    title: str
    publisher: str
    url: str
    related: str
    published_at: str
    summary: str = ""


class NewsFeedOut(BaseModel):
    items: list[NewsItemOut]
    source: str


@app.get("/api/market/pulse", response_model=MarketPulseOut, tags=["dashboard"])
def get_market_pulse(timeframe: str = "daily") -> MarketPulseOut:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    p = _market.market_pulse(timeframe)  # type: ignore[arg-type]
    return MarketPulseOut(
        timeframe=p.timeframe,
        top_gainers=[MoverRowOut(**m.__dict__) for m in p.top_gainers],
        top_losers=[MoverRowOut(**m.__dict__) for m in p.top_losers],
        sector_heatmap=[SectorRowOut(**s.__dict__) for s in p.sector_heatmap],
        breadth_pct_up=p.breadth_pct_up,
        tickers_covered=p.tickers_covered,
    )


@app.get(
    "/api/options/highlights",
    response_model=OptionsHighlightsOut,
    tags=["dashboard"],
)
def get_options_highlights(timeframe: str = "daily") -> OptionsHighlightsOut:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    h = _market.options_highlights(timeframe)  # type: ignore[arg-type]
    return OptionsHighlightsOut(
        timeframe=h.timeframe,
        top_calls=[OptionRecOut(**o.__dict__) for o in h.top_calls],
        top_puts=[OptionRecOut(**o.__dict__) for o in h.top_puts],
    )


@app.get("/api/crypto/snapshot", response_model=CryptoSnapshotOut, tags=["dashboard"])
def get_crypto_snapshot() -> CryptoSnapshotOut:
    snap = _market.crypto_snapshot()
    return CryptoSnapshotOut(
        rows=[CryptoRowOut(**r.__dict__) for r in snap.rows],
        source=snap.source,
    )


@app.get("/api/news", response_model=NewsFeedOut, tags=["dashboard"])
def get_news(limit: int = 12) -> NewsFeedOut:
    feed = _market.news_feed(limit=max(1, min(limit, 40)))
    return NewsFeedOut(
        items=[NewsItemOut(**n.__dict__) for n in feed.items],
        source=feed.source,
    )


# ---------------------------------------------------------------------------
# Screener tab — daily price jumps / dips and upcoming earnings.
# All three views derive from the shared analyst pipeline so they stay
# consistent with the dashboard verdicts. The earnings helper is
# best-effort and degrades to an empty list when yfinance is offline.
# ---------------------------------------------------------------------------


class ScreenerMoverOut(BaseModel):
    symbol: str
    name: str
    sector: str
    last: float
    change_pct: float
    verdict: str | None = None
    composite_score: float | None = None
    rsi: float | None = None


class ScreenerEarningsOut(BaseModel):
    symbol: str
    name: str
    sector: str
    earnings_date: str
    days_until: int
    last: float | None
    change_pct: float | None
    verdict: str | None


class ScreenerMoversListOut(BaseModel):
    timeframe: str
    source: str  # "sp500" when broad universe served the data, "curated" on fallback
    rows: list[ScreenerMoverOut]


class ScreenerMoversSideOut(BaseModel):
    """Jumps or dips block inside the combined movers response."""

    source: str
    rows: list[ScreenerMoverOut]


class ScreenerMoversPairOut(BaseModel):
    """Jumps + dips in one round-trip. Use ``?quick=1`` for instant curated (first paint)."""

    timeframe: str
    jumps: ScreenerMoversSideOut
    dips: ScreenerMoversSideOut


class ScreenerEarningsListOut(BaseModel):
    window_days: int
    source: str  # "sp500" | "curated" | "unavailable"
    rows: list[ScreenerEarningsOut]


class CopyTradeCreatorOut(BaseModel):
    id: str
    name: str
    type: str
    description: str = ""


class CopyTradeHoldingOut(BaseModel):
    name: str
    cusip: str
    symbol: str | None = None
    value_000s: int
    value_usd: float
    weight_pct: float
    shares: float | None = None


class CopyTradeHoldingsOut(BaseModel):
    creator_id: str
    source: str
    as_of: str
    accession: str = ""
    message: str
    rows: list[CopyTradeHoldingOut]


class CopyTradeSignalOut(BaseModel):
    creator_id: str
    kind: str
    as_of: str
    message: str
    symbol: str | None = None
    cusip: str | None = None
    detail: str = ""
    ts: float = 0.0


class CopyTradeSignalsListOut(BaseModel):
    rows: list[CopyTradeSignalOut]


@app.get(
    "/api/screener/movers",
    response_model=ScreenerMoversPairOut,
    tags=["screener"],
)
def screener_movers(
    timeframe: str = "daily",
    limit: int = 10,
    quick: int = Query(0, ge=0, le=1, description="1 = instant tracked-list movers (first paint)"),
) -> ScreenerMoversPairOut:
    """Combined jumps + dips. Set ``quick=1`` for curated only (no S&P 500 yfinance)."""
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    n = max(1, min(limit, 25))
    if quick == 1:
        j, d, js, ds = _movers.movers_pair_curated_only(n)
    else:
        j, d, js, ds = _movers.movers_pair_with_fallback(n)
    return ScreenerMoversPairOut(
        timeframe=timeframe,
        jumps=ScreenerMoversSideOut(
            source=js, rows=[ScreenerMoverOut(**m.__dict__) for m in j]
        ),
        dips=ScreenerMoversSideOut(
            source=ds, rows=[ScreenerMoverOut(**m.__dict__) for m in d]
        ),
    )


@app.get(
    "/api/screener/jumps",
    response_model=ScreenerMoversListOut,
    tags=["screener"],
)
def screener_jumps(timeframe: str = "daily", limit: int = 10) -> ScreenerMoversListOut:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    n = max(1, min(limit, 25))
    items, source = _movers.movers_with_fallback("jumps", n)
    return ScreenerMoversListOut(
        timeframe=timeframe,
        source=source,
        rows=[ScreenerMoverOut(**m.__dict__) for m in items],
    )


@app.get(
    "/api/screener/dips",
    response_model=ScreenerMoversListOut,
    tags=["screener"],
)
def screener_dips(timeframe: str = "daily", limit: int = 10) -> ScreenerMoversListOut:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    n = max(1, min(limit, 25))
    items, source = _movers.movers_with_fallback("dips", n)
    return ScreenerMoversListOut(
        timeframe=timeframe,
        source=source,
        rows=[ScreenerMoverOut(**m.__dict__) for m in items],
    )


@app.get(
    "/api/screener/earnings",
    response_model=ScreenerEarningsListOut,
    tags=["screener"],
)
def screener_earnings(
    window_days: int = SCREENER_EARNINGS_WINDOW_DAYS, limit: int = 50
) -> ScreenerEarningsListOut:
    days = max(1, min(window_days, 60))
    n = max(1, min(limit, 200))
    try:
        rows, source = _earnings.upcoming_earnings_with_source(window_days=days)
    except Exception:
        rows, source = [], "unavailable"
    rows = rows[:n]
    return ScreenerEarningsListOut(
        window_days=days,
        source=source,
        rows=[
            ScreenerEarningsOut(
                symbol=r.symbol,
                name=r.name,
                sector=r.sector,
                earnings_date=r.earnings_date,
                days_until=r.days_until,
                last=r.last,
                change_pct=r.change_pct,
                verdict=r.verdict,
            )
            for r in rows
        ],
    )


# ---------------------------------------------------------------------------
# Copy trade (research; no orders) — 13F + static themed baskets
# ---------------------------------------------------------------------------


@app.get(
    "/api/copy-trade/creators",
    response_model=list[CopyTradeCreatorOut],
    tags=["copy-trade"],
)
def copy_trade_creators() -> list[CopyTradeCreatorOut]:
    out: list[CopyTradeCreatorOut] = []
    for c in _copy_trade.list_creators():
        out.append(
            CopyTradeCreatorOut(
                id=c["id"],
                name=c["name"],
                type=c.get("type", ""),
                description=c.get("description", "") or "",
            )
        )
    return out


@app.get(
    "/api/copy-trade/holdings/{creator_id}",
    response_model=CopyTradeHoldingsOut,
    tags=["copy-trade"],
)
def copy_trade_holdings(
    creator_id: str,
    refresh: int = Query(1, ge=0, le=1, description="1 = refetch from SEC (13F) or recompute; 0 = last saved snapshot if any"),
) -> CopyTradeHoldingsOut:
    if not _copy_trade.get_creator_by_id(creator_id):
        raise HTTPException(404, "Unknown copy-trade creator_id")
    if refresh == 0:
        g = _copy_trade.get_stored_snapshot(creator_id)
        if g:
            rows, meta = g
            as_of = (meta or {}).get("as_of") or (meta or {}).get("filing") or ""
            acc = (meta or {}).get("accession") or ""
            return CopyTradeHoldingsOut(
                creator_id=creator_id,
                source="cached",
                as_of=as_of,
                accession=acc,
                message="Last saved snapshot. Use ?refresh=1 to refetch (SEC may be slow on first run).",
                rows=[
                    CopyTradeHoldingOut(
                        name=r.name,
                        cusip=r.cusip,
                        symbol=r.symbol,
                        value_000s=r.value_000s,
                        value_usd=r.value_usd,
                        weight_pct=r.weight_pct,
                        shares=r.shares,
                    )
                    for r in rows
                ],
            )
    res = _copy_trade.refresh_creator_and_signals(creator_id)
    rows, _, src, as_of, acc, err, _ = res
    msg = err or f"source={src}"
    return CopyTradeHoldingsOut(
        creator_id=creator_id,
        source=src,
        as_of=as_of,
        accession=acc,
        message=msg,
        rows=[
            CopyTradeHoldingOut(
                name=r.name,
                cusip=r.cusip,
                symbol=r.symbol,
                value_000s=r.value_000s,
                value_usd=r.value_usd,
                weight_pct=r.weight_pct,
                shares=r.shares,
            )
            for r in rows
        ],
    )


@app.get(
    "/api/copy-trade/signals",
    response_model=CopyTradeSignalsListOut,
    tags=["copy-trade"],
)
def copy_trade_signals(
    creator_id: str | None = None, limit: int = 30
) -> CopyTradeSignalsListOut:
    n = max(1, min(limit, 200))
    raw = _copy_trade.get_signals(creator_id, n)
    return CopyTradeSignalsListOut(
        rows=[CopyTradeSignalOut(**r) for r in raw]
    )


# ---------------------------------------------------------------------------
# LLM enrichment (Claude Sonnet 4.5) — fail-open; no-ops when ANTHROPIC_API_KEY
# is missing. The deterministic /report and /overview endpoints above remain
# the source of truth; these endpoints produce optional polished prose only.
# ---------------------------------------------------------------------------


class LLMConfigOut(BaseModel):
    enabled: bool
    model: str
    last_error: str | None = None


class LLMTextOut(BaseModel):
    text: str
    model: str
    symbol: str | None = None
    timeframe: str | None = None


class ExplainIn(BaseModel):
    question: str
    timeframe: str = "daily"


@app.get("/api/analyst/llm-config", response_model=LLMConfigOut, tags=["analyst"])
def llm_config() -> LLMConfigOut:
    cfg = _llm.config()
    return LLMConfigOut(enabled=cfg.enabled, model=cfg.model, last_error=_llm.last_error())


def _deterministic_brief(rows: list[dict], timeframe: str, llm_err: str | None = None) -> str:
    bull = [r for r in rows if r.get("verdict") == "BULLISH"]
    bear = [r for r in rows if r.get("verdict") == "BEARISH"]
    neutral = [r for r in rows if r.get("verdict") == "NEUTRAL"]
    top_bull = sorted(bull, key=lambda r: float(r.get("conviction") or 0), reverse=True)[:3]
    top_bear = sorted(bear, key=lambda r: float(r.get("conviction") or 0), reverse=True)[:3]
    sectors: dict[str, dict[str, int]] = {}
    for r in rows:
        sec = str(r.get("sector") or "Unknown")
        verdict = str(r.get("verdict") or "NEUTRAL")
        if sec not in sectors:
            sectors[sec] = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
        if verdict in sectors[sec]:
            sectors[sec][verdict] += 1
    themes = sorted(
        sectors.items(),
        key=lambda kv: max(kv[1]["BULLISH"], kv[1]["BEARISH"]),
        reverse=True,
    )[:3]
    lines: list[str] = []
    lines.append(
        f"{timeframe.upper()} setup: {len(bull)} bullish, {len(bear)} bearish, {len(neutral)} neutral signals."
    )
    if llm_err:
        lines.append("")
        lines.append(f"_LLM temporarily unavailable ({llm_err}). Showing deterministic fallback._")
    lines.append("")
    lines.append("### Bullish setups")
    if top_bull:
        for r in top_bull:
            lines.append(
                f"- `{r.get('symbol')}` {r.get('verdict')} | conviction {int(float(r.get('conviction') or 0)*100)}% | "
                f"{r.get('rec_contract_type', 'call')} {r.get('rec_strike', '—')}"
            )
    else:
        lines.append("- No strong bullish setups in the current sample.")
    lines.append("")
    lines.append("### Bearish setups")
    if top_bear:
        for r in top_bear:
            lines.append(
                f"- `{r.get('symbol')}` {r.get('verdict')} | conviction {int(float(r.get('conviction') or 0)*100)}% | "
                f"{r.get('rec_contract_type', 'put')} {r.get('rec_strike', '—')}"
            )
    else:
        lines.append("- No strong bearish setups in the current sample.")
    lines.append("")
    lines.append("### Cross-ticker themes")
    if themes:
        for sec, counts in themes:
            dom = "bullish" if counts["BULLISH"] >= counts["BEARISH"] else "bearish"
            dom_n = max(counts["BULLISH"], counts["BEARISH"])
            lines.append(
                f"- {sec}: {dom_n} names lean {dom} "
                f"(bull {counts['BULLISH']} / bear {counts['BEARISH']} / neutral {counts['NEUTRAL']})."
            )
    else:
        lines.append("- Sector alignment is mixed with no dominant theme.")
    return "\n".join(lines)


def _deterministic_explain(report: dict, question: str, llm_err: str | None = None) -> str:
    sym = report.get("symbol", "Ticker")
    verdict = report.get("verdict", "NEUTRAL")
    score = report.get("composite_score", 0)
    conv = int(float(report.get("conviction") or 0) * 100)
    opt = report.get("options", {}) or {}
    tp = opt.get("trade_plan", {}) or {}
    lines = [
        f"LLM is currently unavailable{f' ({llm_err})' if llm_err else ''}, so this is a deterministic summary.",
        f"{sym} is {verdict} with composite {score:+.2f} and conviction {conv}%.",
    ]
    if tp:
        lines.append(
            f"Current ticket context: {tp.get('contract_type', 'option')} strike {tp.get('strike', '—')}, "
            f"expiry ~{tp.get('expiry_dte', '—')}D, target {tp.get('target_price', '—')}, "
            f"stop {tp.get('stop_loss', '—')}."
        )
    lines.append(
        f'Question received: "{question.strip()}". Please retry when network access is restored for a full natural-language explanation.'
    )
    return "\n\n".join(lines)


@app.get(
    "/api/analyst/polish/{symbol}",
    response_model=LLMTextOut,
    tags=["analyst"],
)
def analyst_polish(symbol: str, timeframe: str = "daily") -> LLMTextOut:
    if not _llm.config().enabled:
        raise HTTPException(503, "LLM layer disabled — set ANTHROPIC_API_KEY")
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    try:
        rpt = build_report(symbol, timeframe, fresh_quotes=True)  # type: ignore[arg-type]
    except ValueError as e:
        raise HTTPException(404, str(e))
    text = _llm.polish_narrative(rpt.model_dump())
    if not text:
        text = rpt.narrative
        return LLMTextOut(
            text=text,
            model="deterministic-fallback",
            symbol=rpt.symbol,
            timeframe=timeframe,
        )
    return LLMTextOut(
        text=text, model=_llm.config().model,
        symbol=rpt.symbol, timeframe=timeframe,
    )


@app.get("/api/analyst/brief", response_model=LLMTextOut, tags=["analyst"])
def analyst_brief(timeframe: str = "daily") -> LLMTextOut:
    if not _llm.config().enabled:
        raise HTTPException(503, "LLM layer disabled — set ANTHROPIC_API_KEY")
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    rows = overview_rows(timeframe)  # type: ignore[arg-type]
    if not rows:
        text = (
            "Market brief is temporarily unavailable because live upstream data is blocked.\n\n"
            "### Bullish setups\n"
            "- No actionable bullish setups yet.\n\n"
            "### Bearish setups\n"
            "- No actionable bearish setups yet.\n\n"
            "### Cross-ticker themes\n"
            "- Using fallback mode until market data connectivity recovers."
        )
        return LLMTextOut(text=text, model="deterministic-fallback", timeframe=timeframe)
    payload = [r.model_dump() for r in rows]
    text = _llm.market_brief(payload)
    if not text:
        text = _deterministic_brief(payload, timeframe, _llm.last_error())
        return LLMTextOut(text=text, model="deterministic-fallback", timeframe=timeframe)
    return LLMTextOut(text=text, model=_llm.config().model, timeframe=timeframe)


@app.post(
    "/api/analyst/explain/{symbol}",
    response_model=LLMTextOut,
    tags=["analyst"],
)
def analyst_explain(symbol: str, body: ExplainIn) -> LLMTextOut:
    if not _llm.config().enabled:
        raise HTTPException(503, "LLM layer disabled — set ANTHROPIC_API_KEY")
    if body.timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    if not body.question or not body.question.strip():
        raise HTTPException(400, "question is required")
    try:
        rpt = build_report(symbol, body.timeframe, fresh_quotes=True)  # type: ignore[arg-type]
    except ValueError as e:
        raise HTTPException(404, str(e))
    payload = rpt.model_dump()
    text = _llm.explain_ticket(payload, body.question)
    if not text:
        text = _deterministic_explain(payload, body.question, _llm.last_error())
        return LLMTextOut(
            text=text,
            model="deterministic-fallback",
            symbol=rpt.symbol,
            timeframe=body.timeframe,
        )
    return LLMTextOut(
        text=text, model=_llm.config().model,
        symbol=rpt.symbol, timeframe=body.timeframe,
    )


# --- Static frontend (served last so /api takes precedence) ---

if _STATIC_DIR.exists():
    # Serve index.html at "/" so deep links (including empty path) work.
    @app.get("/", include_in_schema=False)
    def root() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )
