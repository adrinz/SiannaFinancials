"""Detailed signal analysis report generator.

Produces a multi-section report summarising every signal the analyst
engine is currently generating, plus the evidence and trade plan that
backs each one. The same underlying data is served as both a
structured JSON payload (for the UI / external tools) and a
human-readable Markdown document (for download / email / Slack).

Nothing here invents facts — every value flows from the deterministic
analyst pipeline (``app.analyst.report.build_report``), the market
regime computed in ``app.services``, and — when present — historical
reliability stats from ``backtest_verdict.json``.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Optional

from .analyst.constants import TICKER_MAP, TICKERS, Timeframe
from .analyst.data import get_ohlcv
from .analyst.models import ReportOut
from .analyst.report import build_report, overview_rows
from .services import regime_envelope


# ---------------------------------------------------------------------------
# Dataclasses describing the report payload
# ---------------------------------------------------------------------------


@dataclass
class RegimeBlock:
    label: str
    vix: float
    vix_change: float
    breadth_pct_above_50d: float
    put_call_ratio: float
    trend_score: float
    longs: int
    shorts: int
    holds: int
    universe_size: int


@dataclass
class IndicatorBlock:
    trend: str                   # uptrend | downtrend | range
    sma50: Optional[float]
    sma200: Optional[float]
    price_vs_sma50_pct: Optional[float]
    price_vs_sma200_pct: Optional[float]
    sma_stacked_bullish: bool
    sma_stacked_bearish: bool
    golden_cross_recent: bool
    death_cross_recent: bool
    rsi: Optional[float]
    rsi_state: str
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_hist: Optional[float]
    macd_hist_direction: str
    macd_bull_cross: bool
    macd_bear_cross: bool
    atr: Optional[float]
    atr_pct: Optional[float]
    vol_ratio: float
    vol_trending_up: bool
    vol_unusual: bool


@dataclass
class TradePlanBlock:
    contract_type: str           # call | put
    strike: float
    expiry_date: str
    expiry_dte: int
    estimated_premium: Optional[float]
    cost_per_contract: Optional[float]
    break_even: Optional[float]
    target_price: Optional[float]
    stop_loss: Optional[float]
    one_sigma_usd: Optional[float]
    one_sigma_pct: Optional[float]
    risk_reward: Optional[float]
    rationale: str


@dataclass
class ReliabilityBlock:
    """Historical hit-rate for this verdict, from walk-forward backtest."""
    bucket: str                  # BULLISH | BEARISH | NEUTRAL
    n: int
    hit_rate_pct: float
    avg_return_pct: float
    profit_factor: float


@dataclass
class TickerBlock:
    symbol: str
    name: str
    sector: str
    timeframe: Timeframe
    as_of: str
    source: str                  # yfinance | synthetic
    # Verdict + components
    verdict: str
    signal: str                  # Buy | Sell | Hold
    composite_score: float
    conviction: float
    headline: str
    narrative: str
    # Price action
    last: float
    change_pct: float
    change_pct_period: float
    supports: list[float]
    resistances: list[float]
    patterns: list[str]
    # Indicators
    indicators: IndicatorBlock
    # Trade plan
    trade_plan: TradePlanBlock
    # Reliability (optional — only if backtest exists)
    reliability: Optional[ReliabilityBlock] = None


@dataclass
class AggregateBlock:
    verdict_counts: dict[str, int]
    sector_tilts: list[dict[str, Any]]
    top_longs: list[dict[str, Any]]
    top_shorts: list[dict[str, Any]]
    best_risk_reward: list[dict[str, Any]]


@dataclass
class SignalReport:
    generated_at_utc: str
    generated_at_est: str
    timeframe: Timeframe
    app: str
    powered_by: str
    regime: RegimeBlock
    aggregate: AggregateBlock
    tickers: list[TickerBlock]
    reliability_overall: dict[str, ReliabilityBlock] = field(default_factory=dict)
    methodology: str = ""
    caveats: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reliability loader (optional)
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKTEST_JSON = _REPO_ROOT / "backtest_verdict.json"


def _load_backtest() -> Optional[dict[str, Any]]:
    if not _BACKTEST_JSON.exists():
        return None
    try:
        return json.loads(_BACKTEST_JSON.read_text())
    except Exception:
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _reliability_for(
    backtest: Optional[dict[str, Any]], symbol: str, verdict: str,
) -> Optional[ReliabilityBlock]:
    if backtest is None:
        return None
    for row in backtest.get("per_symbol", []):
        if row.get("symbol") == symbol:
            bucket = row.get("buckets", {}).get(verdict)
            if not bucket:
                return None
            return ReliabilityBlock(
                bucket=verdict,
                n=int(bucket.get("n", 0)),
                hit_rate_pct=_num(bucket.get("hit_rate"), 0.0),
                avg_return_pct=_num(bucket.get("avg_return_pct"), 0.0),
                profit_factor=_num(bucket.get("profit_factor"), 0.0),
            )
    return None


def _reliability_overall(
    backtest: Optional[dict[str, Any]],
) -> dict[str, ReliabilityBlock]:
    if backtest is None:
        return {}
    out: dict[str, ReliabilityBlock] = {}
    agg = backtest.get("aggregate") or {}
    for bucket_name in ("BULLISH", "BEARISH", "NEUTRAL"):
        b = agg.get(bucket_name)
        if not b:
            continue
        out[bucket_name] = ReliabilityBlock(
            bucket=bucket_name,
            n=int(b.get("n", 0)),
            hit_rate_pct=_num(b.get("hit_rate"), 0.0),
            avg_return_pct=_num(b.get("avg_return_pct"), 0.0),
            profit_factor=_num(b.get("profit_factor"), 0.0),
        )
    return out


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


_METHODOLOGY = (
    "Signals are generated by a deterministic rule-based engine — no LLM "
    "is involved in the decision. The composite score in [-1, +1] is a "
    "weighted combination of:\n"
    "  * Trend direction from price action (weight 0.35)\n"
    "  * SMA stack 50/200 and recent cross events (weight 0.28)\n"
    "  * RSI posture (weight 0.10)\n"
    "  * MACD cross + histogram direction (weight 0.20)\n"
    "  * Volume flow confirmation (weight 0.10)\n"
    "Verdicts: score ≥ +0.30 → BULLISH, ≤ −0.30 → BEARISH, else NEUTRAL. "
    "Conviction = min(1.0, |score| × 1.25 + 0.25)."
)

_CAVEATS = [
    "Free yfinance data is typically delayed 15-20 minutes during US market hours.",
    "IV shown on the screener is a realised-vol proxy (20-bar annualised), "
    "not option-chain implied vol.",
    "Put/Call ratio in the regime block is a placeholder — no free live feed wired.",
    "Reliability stats are from a walk-forward backtest with no look-ahead bias, "
    "but past behaviour does not guarantee future returns.",
    "Recommendations are analytical, not financial advice.",
]

_DISCLAIMER_HEADER = (
    "**⚠ NOT FINANCIAL ADVICE.** Sianna Financials is an educational "
    "research prototype. Every verdict, price target, stop level, and "
    "option trade plan in this document was produced by software models "
    "from delayed public data and may be inaccurate. Nothing here is an "
    "offer, solicitation, or recommendation to buy or sell any security. "
    "Options can lose 100% of their premium — and leveraged structures "
    "can lose more. Do your own research and consult a licensed advisor "
    "before trading."
)

_DISCLAIMER_FOOTER = (
    "## Disclaimer\n\n"
    "This report is provided \"AS IS\" for informational and educational "
    "purposes only. The operators and authors of Sianna Financials are "
    "not registered investment advisers, broker-dealers, or fiduciaries "
    "to any reader of this document. No content in this report is "
    "personalised advice. Data may be delayed, incomplete, or wrong. "
    "The authors disclaim any liability for trading losses, data errors, "
    "or decisions made based on output from this tool. Past backtest "
    "performance is not a reliable indicator of future returns. Before "
    "trading any option or leveraged product, read the OCC publication "
    "\"Characteristics and Risks of Standardized Options\" and consult a "
    "licensed financial professional who understands your personal "
    "circumstances."
)


def _pct_change_period(closes: list[float]) -> float:
    if len(closes) < 2 or closes[0] == 0:
        return 0.0
    return (closes[-1] / closes[0] - 1) * 100.0


def _ticker_block(report: ReportOut, reliability: Optional[ReliabilityBlock]) -> TickerBlock:
    pa = report.price_action
    v = report.volume
    sma = report.sma
    rsi = report.rsi
    macd = report.macd
    atr = report.atr
    tp = report.options.trade_plan

    signal = {"BULLISH": "Buy", "BEARISH": "Sell"}.get(report.verdict, "Hold")

    indicators = IndicatorBlock(
        trend=pa.trend,
        sma50=sma.sma50,
        sma200=sma.sma200,
        price_vs_sma50_pct=sma.price_vs_sma50_pct,
        price_vs_sma200_pct=sma.price_vs_sma200_pct,
        sma_stacked_bullish=sma.stacked_bullish,
        sma_stacked_bearish=sma.stacked_bearish,
        golden_cross_recent=sma.golden_cross_recent,
        death_cross_recent=sma.death_cross_recent,
        rsi=rsi.value,
        rsi_state=rsi.state,
        macd=macd.macd,
        macd_signal=macd.signal,
        macd_hist=macd.histogram,
        macd_hist_direction=macd.histogram_direction,
        macd_bull_cross=macd.bullish_cross_recent,
        macd_bear_cross=macd.bearish_cross_recent,
        atr=atr.value,
        atr_pct=atr.pct_of_price,
        vol_ratio=v.ratio,
        vol_trending_up=v.trending_up,
        vol_unusual=v.unusual,
    )
    plan = TradePlanBlock(
        contract_type=tp.contract_type,
        strike=tp.strike,
        expiry_date=tp.expiry_date,
        expiry_dte=tp.expiry_dte,
        estimated_premium=tp.estimated_premium,
        cost_per_contract=tp.cost_per_contract,
        break_even=tp.break_even,
        target_price=tp.target_price,
        stop_loss=tp.stop_loss,
        one_sigma_usd=tp.one_sigma_move_usd,
        one_sigma_pct=tp.one_sigma_move_pct,
        risk_reward=tp.risk_reward,
        rationale=tp.rationale,
    )
    return TickerBlock(
        symbol=report.symbol,
        name=report.name,
        sector=report.sector,
        timeframe=report.timeframe,
        as_of=report.as_of,
        source=report.source,
        verdict=report.verdict,
        signal=signal,
        composite_score=round(report.composite_score, 3),
        conviction=round(report.conviction, 3),
        headline=report.headline,
        narrative=report.narrative,
        last=round(pa.last, 2),
        change_pct=round(pa.change_pct, 2),
        change_pct_period=round(pa.change_pct_period, 2),
        supports=[round(s, 2) for s in pa.supports],
        resistances=[round(r, 2) for r in pa.resistances],
        patterns=list(pa.patterns),
        indicators=indicators,
        trade_plan=plan,
        reliability=reliability,
    )


def _aggregates(blocks: list[TickerBlock]) -> AggregateBlock:
    counts = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
    for b in blocks:
        counts[b.verdict] = counts.get(b.verdict, 0) + 1

    # Sector tilts — composite-weighted.
    by_sector: dict[str, list[TickerBlock]] = {}
    for b in blocks:
        by_sector.setdefault(b.sector, []).append(b)
    sector_tilts = []
    for sector, items in by_sector.items():
        sector_tilts.append({
            "sector": sector,
            "n": len(items),
            "avg_composite": round(mean([i.composite_score for i in items]), 3),
            "avg_change_pct": round(mean([i.change_pct for i in items]), 2),
            "bullish": sum(1 for i in items if i.verdict == "BULLISH"),
            "bearish": sum(1 for i in items if i.verdict == "BEARISH"),
            "neutral": sum(1 for i in items if i.verdict == "NEUTRAL"),
            "tickers": [i.symbol for i in items],
        })
    sector_tilts.sort(key=lambda s: s["avg_composite"], reverse=True)

    longs = sorted(
        [b for b in blocks if b.verdict == "BULLISH"],
        key=lambda b: (b.composite_score * b.conviction),
        reverse=True,
    )[:5]
    shorts = sorted(
        [b for b in blocks if b.verdict == "BEARISH"],
        key=lambda b: (b.composite_score * b.conviction),
    )[:5]

    def _rr_score(b: TickerBlock) -> float:
        rr = b.trade_plan.risk_reward or 0.0
        return rr * max(0.0, b.conviction)

    best_rr = sorted(blocks, key=_rr_score, reverse=True)[:5]

    def _summary(b: TickerBlock) -> dict[str, Any]:
        return {
            "symbol": b.symbol,
            "name": b.name,
            "sector": b.sector,
            "last": b.last,
            "change_pct": b.change_pct,
            "verdict": b.verdict,
            "composite_score": b.composite_score,
            "conviction": b.conviction,
            "contract": b.trade_plan.contract_type,
            "strike": b.trade_plan.strike,
            "expiry_dte": b.trade_plan.expiry_dte,
            "risk_reward": b.trade_plan.risk_reward,
            "target": b.trade_plan.target_price,
            "stop": b.trade_plan.stop_loss,
        }

    return AggregateBlock(
        verdict_counts=counts,
        sector_tilts=sector_tilts,
        top_longs=[_summary(b) for b in longs],
        top_shorts=[_summary(b) for b in shorts],
        best_risk_reward=[_summary(b) for b in best_rr],
    )


def build_signal_report(timeframe: Timeframe = "daily") -> SignalReport:
    now_utc = datetime.now(timezone.utc)
    # EST is UTC-5, EDT is UTC-4. We don't want to pull pytz in; use a
    # fixed -5 offset which is what the rest of the app does for 'EST'.
    now_est = now_utc.astimezone(timezone(timedelta(hours=-5)))

    env = regime_envelope(now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"), timeframe=timeframe)
    regime = RegimeBlock(
        label=env.regime.label,
        vix=env.regime.vix,
        vix_change=env.regime.vix_change,
        breadth_pct_above_50d=env.regime.breadth_pct_above_50d,
        put_call_ratio=env.regime.put_call_ratio,
        trend_score=env.regime.trend_score,
        longs=env.counts.longs,
        shorts=env.counts.shorts,
        holds=env.counts.holds,
        universe_size=env.counts.universe_size,
    )

    backtest = _load_backtest()

    blocks: list[TickerBlock] = []
    for meta in TICKERS:
        try:
            rpt = build_report(meta["symbol"], timeframe)
        except Exception:
            continue
        rel = _reliability_for(backtest, rpt.symbol, rpt.verdict)
        blocks.append(_ticker_block(rpt, rel))

    # Sort: BULLISH/BEARISH by |composite|, NEUTRAL last.
    blocks.sort(key=lambda b: (b.verdict == "NEUTRAL", -abs(b.composite_score)))

    aggregate = _aggregates(blocks)

    # Detect LLM status so the report header is honest about what's in
    # the mix. Core analysis is always deterministic; the LLM only
    # polishes narrative when enabled.
    powered_by = "Deterministic analyst engine"
    try:
        from .analyst import llm as _llm
        cfg = _llm.config()
        if cfg.enabled:
            powered_by = f"Deterministic analyst engine + Claude ({cfg.model})"
    except Exception:
        pass

    return SignalReport(
        generated_at_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        generated_at_est=now_est.strftime("%Y-%m-%d %H:%M:%S EST"),
        timeframe=timeframe,
        app="Sianna Financials",
        powered_by=powered_by,
        regime=regime,
        aggregate=aggregate,
        tickers=blocks,
        reliability_overall=_reliability_overall(backtest),
        methodology=_METHODOLOGY,
        caveats=list(_CAVEATS),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _fmt_num(x: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if x is None:
        return "—"
    try:
        return f"{x:.{digits}f}{suffix}"
    except Exception:
        return str(x)


def _fmt_signed(x: Optional[float], digits: int = 2, suffix: str = "") -> str:
    if x is None:
        return "—"
    return f"{x:+.{digits}f}{suffix}"


def _bullet(b: bool, yes: str = "yes", no: str = "no") -> str:
    return yes if b else no


def render_markdown(report: SignalReport) -> str:
    r = report
    lines: list[str] = []

    # Header
    lines.append(f"# {r.app} — Signal Analysis Report")
    lines.append("")
    lines.append(f"- **Generated**: {r.generated_at_est} ({r.generated_at_utc})")
    lines.append(f"- **Timeframe**: `{r.timeframe}`")
    lines.append(f"- **Powered by**: {r.powered_by}")
    lines.append("")
    lines.append("> " + _DISCLAIMER_HEADER.replace("\n", "\n> "))
    lines.append("")

    # Executive summary
    counts = r.aggregate.verdict_counts
    lines.append("## Executive summary")
    lines.append("")
    lines.append(
        f"Scanned **{r.regime.universe_size}** names on the `{r.timeframe}` "
        f"timeframe. The verdict split is "
        f"**{counts.get('BULLISH', 0)} BULLISH / "
        f"{counts.get('BEARISH', 0)} BEARISH / "
        f"{counts.get('NEUTRAL', 0)} NEUTRAL**."
    )
    lines.append("")
    lines.append(
        f"**Market regime**: *{r.regime.label}* · "
        f"VIX {r.regime.vix:.2f} ({_fmt_signed(r.regime.vix_change)}), "
        f"breadth {_fmt_num(r.regime.breadth_pct_above_50d, 1)}% above 50d, "
        f"trend score {_fmt_signed(r.regime.trend_score, 3)}, "
        f"put/call {_fmt_num(r.regime.put_call_ratio)}."
    )
    lines.append("")

    if r.aggregate.top_longs:
        lines.append("### Top long ideas (by score × conviction)")
        lines.append("")
        lines.append("| Symbol | Sector | Last | Δ% | Score | Conv | Contract | Strike | DTE | R:R | Target | Stop |")
        lines.append("|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|")
        for t in r.aggregate.top_longs:
            lines.append(
                f"| **{t['symbol']}** | {t['sector']} | {_fmt_num(t['last'])} | "
                f"{_fmt_signed(t['change_pct'])} | {_fmt_signed(t['composite_score'], 2)} | "
                f"{_fmt_num(t['conviction'], 2)} | {t.get('contract', '—').upper()} | "
                f"{_fmt_num(t.get('strike'))} | {t.get('expiry_dte', '—')} | "
                f"{_fmt_num(t.get('risk_reward'), 2, 'x')} | "
                f"{_fmt_num(t.get('target'))} | {_fmt_num(t.get('stop'))} |"
            )
        lines.append("")

    if r.aggregate.top_shorts:
        lines.append("### Top short ideas (by score × conviction)")
        lines.append("")
        lines.append("| Symbol | Sector | Last | Δ% | Score | Conv | Contract | Strike | DTE | R:R | Target | Stop |")
        lines.append("|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|")
        for t in r.aggregate.top_shorts:
            lines.append(
                f"| **{t['symbol']}** | {t['sector']} | {_fmt_num(t['last'])} | "
                f"{_fmt_signed(t['change_pct'])} | {_fmt_signed(t['composite_score'], 2)} | "
                f"{_fmt_num(t['conviction'], 2)} | {t.get('contract', '—').upper()} | "
                f"{_fmt_num(t.get('strike'))} | {t.get('expiry_dte', '—')} | "
                f"{_fmt_num(t.get('risk_reward'), 2, 'x')} | "
                f"{_fmt_num(t.get('target'))} | {_fmt_num(t.get('stop'))} |"
            )
        lines.append("")

    # Sector tilts
    if r.aggregate.sector_tilts:
        lines.append("### Sector tilts")
        lines.append("")
        lines.append("| Sector | N | Avg score | Avg Δ% | Bull | Bear | Neutral | Tickers |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
        for s in r.aggregate.sector_tilts:
            lines.append(
                f"| {s['sector']} | {s['n']} | "
                f"{_fmt_signed(s['avg_composite'], 3)} | "
                f"{_fmt_signed(s['avg_change_pct'])} | "
                f"{s['bullish']} | {s['bearish']} | {s['neutral']} | "
                f"{', '.join(s['tickers'])} |"
            )
        lines.append("")

    # Methodology
    lines.append("## Methodology")
    lines.append("")
    lines.append(r.methodology)
    lines.append("")

    # Historical reliability (if present)
    if r.reliability_overall:
        lines.append("## Historical reliability (walk-forward backtest)")
        lines.append("")
        lines.append("Aggregate hit-rate of verdicts across the tracked universe at the default 5-bar horizon:")
        lines.append("")
        lines.append("| Verdict | Samples | Hit-rate | Avg return | Profit factor |")
        lines.append("|---|---:|---:|---:|---:|")
        for bucket in ("BULLISH", "BEARISH", "NEUTRAL"):
            rel = r.reliability_overall.get(bucket)
            if not rel:
                continue
            lines.append(
                f"| {bucket} | {rel.n} | {_fmt_num(rel.hit_rate_pct, 1)}% | "
                f"{_fmt_signed(rel.avg_return_pct, 2)}% | "
                f"{_fmt_num(rel.profit_factor, 2)} |"
            )
        lines.append("")

    # Per-ticker detail
    lines.append("## Per-ticker detail")
    lines.append("")

    for b in r.tickers:
        lines.append(f"### {b.symbol} — {b.name} · *{b.sector}*")
        lines.append("")
        tag = {"Buy": "🟢 BUY", "Sell": "🔴 SELL", "Hold": "⚪ HOLD"}[b.signal]
        lines.append(
            f"**{tag}** — verdict **{b.verdict}**, "
            f"composite **{_fmt_signed(b.composite_score, 3)}**, "
            f"conviction **{_fmt_num(b.conviction, 2)}**"
        )
        lines.append("")
        lines.append(f"> {b.headline}")
        lines.append("")

        # Price action
        lines.append(
            f"- **Price**: `{_fmt_num(b.last)}` · "
            f"Δ (bar) {_fmt_signed(b.change_pct)}% · "
            f"Δ (window) {_fmt_signed(b.change_pct_period)}% · "
            f"trend **{b.indicators.trend}**"
        )
        if b.supports or b.resistances:
            lines.append(
                f"- **Structure**: supports "
                f"{', '.join(_fmt_num(x) for x in b.supports) or '—'} · "
                f"resistances "
                f"{', '.join(_fmt_num(x) for x in b.resistances) or '—'}"
            )
        if b.patterns:
            lines.append(f"- **Patterns**: {', '.join(b.patterns)}")

        # Indicator block
        ind = b.indicators
        lines.append(
            f"- **Trend/SMA**: 50d `{_fmt_num(ind.sma50)}` "
            f"({_fmt_signed(ind.price_vs_sma50_pct)}%) · "
            f"200d `{_fmt_num(ind.sma200)}` "
            f"({_fmt_signed(ind.price_vs_sma200_pct)}%) · "
            f"stack {'bull' if ind.sma_stacked_bullish else 'bear' if ind.sma_stacked_bearish else 'mixed'}"
            + (", golden cross recent" if ind.golden_cross_recent else "")
            + (", death cross recent" if ind.death_cross_recent else "")
        )
        lines.append(
            f"- **Momentum**: RSI `{_fmt_num(ind.rsi, 1)}` ({ind.rsi_state}) · "
            f"MACD `{_fmt_num(ind.macd, 3)}` / signal `{_fmt_num(ind.macd_signal, 3)}` "
            f"(hist {_fmt_signed(ind.macd_hist, 3)}, {ind.macd_hist_direction})"
            + (", bull cross recent" if ind.macd_bull_cross else "")
            + (", bear cross recent" if ind.macd_bear_cross else "")
        )
        lines.append(
            f"- **Volatility / flow**: ATR `{_fmt_num(ind.atr, 2)}` "
            f"({_fmt_num(ind.atr_pct, 2)}% of price) · "
            f"volume `{_fmt_num(ind.vol_ratio, 2)}×` avg"
            + (", trending up" if ind.vol_trending_up else "")
            + (", unusual" if ind.vol_unusual else "")
        )

        # Trade plan
        p = b.trade_plan
        lines.append("")
        lines.append(
            f"**Trade plan** — **{p.contract_type.upper()} "
            f"${_fmt_num(p.strike)}** exp. `{p.expiry_date}` "
            f"({p.expiry_dte} DTE)"
        )
        lines.append(
            f"- Est. premium `${_fmt_num(p.estimated_premium, 2)}` "
            f"→ cost `${_fmt_num(p.cost_per_contract, 2)}` / contract · "
            f"break-even `${_fmt_num(p.break_even, 2)}`"
        )
        lines.append(
            f"- Target `${_fmt_num(p.target_price, 2)}` · "
            f"stop `${_fmt_num(p.stop_loss, 2)}` · "
            f"R:R `{_fmt_num(p.risk_reward, 2, 'x')}` · "
            f"1σ move ±`${_fmt_num(p.one_sigma_usd, 2)}` "
            f"(`{_fmt_num(p.one_sigma_pct, 2)}%`)"
        )
        if p.rationale:
            lines.append(f"- *{p.rationale}*")

        # Reliability for this bucket (optional)
        if b.reliability:
            rel = b.reliability
            lines.append(
                f"- **Historical** ({rel.bucket}, n={rel.n}): hit-rate "
                f"`{_fmt_num(rel.hit_rate_pct, 1)}%`, avg return "
                f"`{_fmt_signed(rel.avg_return_pct, 2)}%`, profit factor "
                f"`{_fmt_num(rel.profit_factor, 2)}`"
            )

        # Narrative
        if b.narrative:
            lines.append("")
            lines.append(b.narrative.strip())

        lines.append("")
        lines.append(f"*Data source: {b.source} · bar as of {b.as_of}*")
        lines.append("")
        lines.append("---")
        lines.append("")

    # Caveats
    if r.caveats:
        lines.append("## Caveats & notes")
        lines.append("")
        for c in r.caveats:
            lines.append(f"- {c}")
        lines.append("")

    # Full disclaimer
    lines.append(_DISCLAIMER_FOOTER)
    lines.append("")

    return "\n".join(lines)


def render_text(report: SignalReport) -> str:
    """Lightweight plain-text variant for terminals / email / Slack."""
    # Strip the Markdown decorations from render_markdown rather than
    # maintaining a second template. The Markdown is already very close
    # to readable plaintext.
    md = render_markdown(report)
    out = []
    for line in md.splitlines():
        if line.startswith("| ") or line.startswith("|---"):
            out.append(line)  # keep tables as-is — still readable
        elif line.startswith("#### "):
            out.append(line[5:])
        elif line.startswith("### "):
            out.append(line[4:])
        elif line.startswith("## "):
            out.append(line[3:])
            out.append("-" * len(line[3:]))
        elif line.startswith("# "):
            out.append(line[2:])
            out.append("=" * len(line[2:]))
        else:
            out.append(line.replace("**", "").replace("`", ""))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Convenience: JSON-serializable dict for FastAPI
# ---------------------------------------------------------------------------


def to_dict(report: SignalReport) -> dict[str, Any]:
    return asdict(report)


__all__ = [
    "SignalReport",
    "build_signal_report",
    "render_markdown",
    "render_text",
    "to_dict",
]
