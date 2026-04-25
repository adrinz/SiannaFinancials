"""FastAPI entry point for the Sianna Financials web app."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Make the sibling `square18_signals` package importable without install.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_SIGNALS_SRC = _REPO / "square18_signals" / "src"
if str(_SIGNALS_SRC) not in sys.path:
    sys.path.insert(0, str(_SIGNALS_SRC))

from .analyst import TICKERS as ANALYST_TICKERS  # noqa: E402
from .analyst import build_report, overview_rows  # noqa: E402
from .analyst import earnings as _earnings  # noqa: E402
from .analyst import llm as _llm  # noqa: E402
from .analyst import market as _market  # noqa: E402
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


@app.get("/api/ticker/{symbol}", response_model=TickerDetailOut, tags=["detail"])
def get_ticker(symbol: str) -> TickerDetailOut:
    detail = ticker_detail(symbol)
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


@app.get(
    "/api/analyst/report/{symbol}",
    response_model=ReportOut,
    tags=["analyst"],
)
def analyst_report(symbol: str, timeframe: str = "daily") -> ReportOut:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    try:
        return build_report(symbol, timeframe)  # type: ignore[arg-type]
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
    verdict: str
    composite_score: float
    rsi: float | None


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
    rows: list[ScreenerMoverOut]


class ScreenerEarningsListOut(BaseModel):
    window_days: int
    rows: list[ScreenerEarningsOut]


def _movers_from_overview(timeframe: str, *, side: str, limit: int) -> list[ScreenerMoverOut]:
    rows = overview_rows(timeframe)  # type: ignore[arg-type]
    if side == "jumps":
        candidates = [r for r in rows if r.change_pct > 0]
        candidates.sort(key=lambda r: r.change_pct, reverse=True)
    else:  # dips
        candidates = [r for r in rows if r.change_pct < 0]
        candidates.sort(key=lambda r: r.change_pct)
    out: list[ScreenerMoverOut] = []
    for r in candidates[:limit]:
        out.append(
            ScreenerMoverOut(
                symbol=r.symbol,
                name=r.name,
                sector=r.sector,
                last=r.last,
                change_pct=r.change_pct,
                verdict=r.verdict,
                composite_score=r.composite_score,
                rsi=r.rsi,
            )
        )
    return out


@app.get(
    "/api/screener/jumps",
    response_model=ScreenerMoversListOut,
    tags=["screener"],
)
def screener_jumps(timeframe: str = "daily", limit: int = 10) -> ScreenerMoversListOut:
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {sorted(_ALLOWED_TIMEFRAMES)}")
    n = max(1, min(limit, 25))
    return ScreenerMoversListOut(
        timeframe=timeframe,
        rows=_movers_from_overview(timeframe, side="jumps", limit=n),
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
    return ScreenerMoversListOut(
        timeframe=timeframe,
        rows=_movers_from_overview(timeframe, side="dips", limit=n),
    )


@app.get(
    "/api/screener/earnings",
    response_model=ScreenerEarningsListOut,
    tags=["screener"],
)
def screener_earnings(window_days: int = 14, limit: int = 25) -> ScreenerEarningsListOut:
    days = max(1, min(window_days, 60))
    n = max(1, min(limit, 50))
    rows = _earnings.upcoming_earnings(window_days=days)[:n]
    return ScreenerEarningsListOut(
        window_days=days,
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
        rpt = build_report(symbol, timeframe)  # type: ignore[arg-type]
    except ValueError as e:
        raise HTTPException(404, str(e))
    text = _llm.polish_narrative(rpt.model_dump())
    if not text:
        raise HTTPException(502, _llm.last_error() or "LLM call failed")
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
        raise HTTPException(500, "no overview data available")
    text = _llm.market_brief([r.model_dump() for r in rows])
    if not text:
        raise HTTPException(502, _llm.last_error() or "LLM call failed")
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
        rpt = build_report(symbol, body.timeframe)  # type: ignore[arg-type]
    except ValueError as e:
        raise HTTPException(404, str(e))
    text = _llm.explain_ticket(rpt.model_dump(), body.question)
    if not text:
        raise HTTPException(502, _llm.last_error() or "LLM call failed")
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
