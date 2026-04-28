"""Business logic layer — turns the live analyst pipeline into API payloads.

This module is the bridge between the deterministic analyst engine
(``app.analyst.*``) and the web UI's contracts (``app.models``). Everything
here now derives from real OHLCV data fetched through ``get_ohlcv`` —
no hardcoded ticker fixtures are used anymore.

Flow per request:
    /api/screen  -> overview_rows(daily)  -> TickerRowOut[]
    /api/regime  -> overview_rows + breadth + VIX quote -> RegimeEnvelope
    /api/tickers/{sym} -> build_report(daily) -> TickerDetailOut

The synthetic TickerSnapshot fixtures in ``app/data.py`` remain in the
codebase for backward compatibility / test seeding, but are no longer
consumed by any production endpoint.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Optional

from square18_signals import (
    MarketContext,
    recommend_strategies,
)

from .analyst.constants import DEFAULT_IV, TICKER_MAP, Timeframe
from .analyst.factors import derive_factors_from_report
from .analyst.data import get_ohlcv, get_ohlcv_1d_intraday, OHLCV
from .analyst.market import news_for_ticker
from .analyst.models import OverviewRow, ReportOut
from .analyst.report import build_report, overview_rows
from .models import (
    CountsOut,
    ExpectedMoveOut,
    FactorOut,
    LegOut,
    MetricsOut,
    RecommendationOut,
    RegimeEnvelope,
    RegimeOut,
    StrategyOut,
    TickerChartBarOut,
    TickerChartBundleOut,
    TickerChartContextOut,
    TickerDetailOut,
    TickerNewsOut,
    TickerRowOut,
    TickerSignalLineOut,
    clamp_inf,
)


# ---------------------------------------------------------------------------
# Verdict -> signal mapping
# ---------------------------------------------------------------------------


def _signal_for(verdict: str) -> str:
    return {"BULLISH": "Buy", "BEARISH": "Sell"}.get(verdict, "Hold")


def _direction_for(verdict: str) -> str:
    return {"BULLISH": "bull", "BEARISH": "bear"}.get(verdict, "neutral")


# ---------------------------------------------------------------------------
# IV proxy from realised close-to-close volatility
#
# The free data tier doesn't give us option-chain IV. We compute a
# statistically-meaningful proxy: trailing 20-bar realised vol annualised,
# and rank it against the last year of the same series. This drives the
# iv / iv_rank / iv_percentile columns on the dashboard and feeds the
# options recommender with a defensible vol input.
# ---------------------------------------------------------------------------


def _annualised_rv(closes: list[float], window: int = 20) -> Optional[float]:
    if len(closes) < window + 2:
        return None
    rets: list[float] = []
    for a, b in zip(closes[-(window + 1):-1], closes[-window:]):
        if a <= 0 or b <= 0:
            continue
        rets.append(math.log(b / a))
    if len(rets) < 2:
        return None
    sd = statistics.pstdev(rets)
    return sd * math.sqrt(252)


def _rv_rank_pct(closes: list[float], window: int = 20, lookback: int = 252) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (current_rv, rank_0_100, percentile_0_100)."""
    current = _annualised_rv(closes, window=window)
    if current is None:
        return None, None, None
    series: list[float] = []
    for end in range(window + 1, min(len(closes), lookback + window + 1) + 1):
        rv = _annualised_rv(closes[:end], window=window)
        if rv is not None:
            series.append(rv)
    if len(series) < 10:
        return current, 50.0, 50.0
    lo, hi = min(series), max(series)
    rank = 0.0 if hi == lo else (current - lo) / (hi - lo) * 100.0
    below = sum(1 for v in series if v <= current)
    pct = below / len(series) * 100.0
    return current, max(0.0, min(100.0, rank)), max(0.0, min(100.0, pct))


def _dte_pref_for(rv_rank: Optional[float]) -> int:
    """High IV rank → closer DTE (premium-rich), low → further out."""
    if rv_rank is None:
        return 35
    if rv_rank >= 75:
        return 21
    if rv_rank >= 50:
        return 30
    if rv_rank >= 25:
        return 45
    return 60


# ---------------------------------------------------------------------------
# Row / detail builders
# ---------------------------------------------------------------------------


def _row_from_overview(r: OverviewRow, closes: list[float]) -> TickerRowOut:
    rv, rank, pct = _rv_rank_pct(closes)
    iv = rv if rv is not None else DEFAULT_IV.get(r.symbol, 0.30)
    signal = _signal_for(r.verdict)
    direction = _direction_for(r.verdict)
    return TickerRowOut(
        symbol=r.symbol,
        name=r.name,
        sector=r.sector,
        price=r.last,
        change_pct=r.change_pct,
        signal=signal,          # type: ignore[arg-type]
        direction=direction,    # type: ignore[arg-type]
        composite_score=round(r.composite_score, 3),
        confidence=round(r.conviction, 3),
        rsi=round(r.rsi, 1) if r.rsi is not None else 50.0,
        iv=round(iv, 3),
        iv_rank=round(rank, 1) if rank is not None else 50.0,
        iv_percentile=round(pct, 1) if pct is not None else 50.0,
        dte_pref=_dte_pref_for(rank),
        earnings_in_window=False,
    )


def regime_envelope(last_scan_iso: str, timeframe: Timeframe = "daily") -> RegimeEnvelope:
    """Build the dashboard regime banner + counts from live analyst data.

    * VIX level + 1-day change come from the real VIX OHLCV series.
    * Breadth (% above 50d SMA) is computed across the tracked universe.
    * Trend score = average composite_score across the universe, which
      already rolls up MA stack, MACD, RSI posture, and price action.
    * Put/call ratio is still a fixture (needs an options-chain feed).
    * Label is derived from the pair (vix_level, trend_score).
    """
    rows = overview_rows(timeframe)

    # Trend score: universe-wide mean composite.
    if rows:
        trend_score = sum(r.composite_score for r in rows) / len(rows)
    else:
        trend_score = 0.0

    # Breadth: % of tracked tickers trading above their 50d SMA.
    breadth_pct = _breadth_above_50d(timeframe)

    # VIX spot + change.
    vix_last, vix_change = _vix_quote()

    # Put/call remains a placeholder — no free feed. Could be wired
    # to CBOE CSV later.
    put_call = 0.92

    label = _regime_label(vix_last, trend_score, breadth_pct)

    longs = sum(1 for r in rows if r.verdict == "BULLISH")
    shorts = sum(1 for r in rows if r.verdict == "BEARISH")
    holds = sum(1 for r in rows if r.verdict == "NEUTRAL")

    return RegimeEnvelope(
        regime=RegimeOut(
            label=label,
            vix=round(vix_last, 2),
            vix_change=round(vix_change, 2),
            breadth_pct_above_50d=round(breadth_pct, 1),
            put_call_ratio=round(put_call, 2),
            trend_score=round(trend_score, 3),
        ),
        counts=CountsOut(
            universe_size=len(TICKER_MAP),
            scanned=len(rows),
            longs=longs,
            shorts=shorts,
            holds=holds,
        ),
        last_scan_iso=last_scan_iso,
    )


_breadth_lock = threading.Lock()
_breadth_cache: dict[Timeframe, tuple[float, float]] = {}
BREADTH_CACHE_TTL_SEC = 300.0


def _vix_quote() -> tuple[float, float]:
    """Return (vix_last, vix_1d_change). Falls back to a sensible default."""
    try:
        series = get_ohlcv("VIX", "daily")
        if len(series) >= 2:
            last = float(series.close[-1])
            prev = float(series.close[-2])
            chg = last - prev
            return last, chg
        if len(series) == 1:
            return float(series.close[-1]), 0.0
    except Exception:
        pass
    return 17.0, 0.0


def _compute_breadth_pct_above_50d(timeframe: Timeframe) -> float:
    """% of tracked equities trading above their 50-bar SMA (uncached)."""
    from .analyst.indicators import sma

    # Equities only — VIX above its own 50d means more fear, which
    # inverts the usual "bullish" reading, so we exclude it.
    symbols = [s for s in TICKER_MAP.keys() if s != "VIX"]
    above = 0
    total = 0
    for sym in symbols:
        try:
            series = get_ohlcv(sym, timeframe)
        except Exception:
            continue
        closes = series.close
        if len(closes) < 50:
            continue
        s50 = sma(closes, 50)
        if not s50 or s50[-1] is None:
            continue
        total += 1
        if closes[-1] > s50[-1]:
            above += 1
    if total == 0:
        return 50.0
    return above / total * 100.0


def _breadth_above_50d(timeframe: Timeframe) -> float:
    """% above 50d SMA — one expensive yfinance pass per *timeframe*; TTL-cached."""
    now = time.time()
    with _breadth_lock:
        hit = _breadth_cache.get(timeframe)
        if hit and (now - hit[0]) < BREADTH_CACHE_TTL_SEC:
            return hit[1]

    pct = _compute_breadth_pct_above_50d(timeframe)

    with _breadth_lock:
        now2 = time.time()
        hit2 = _breadth_cache.get(timeframe)
        if hit2 and (now2 - hit2[0]) < BREADTH_CACHE_TTL_SEC:
            return hit2[1]
        _breadth_cache[timeframe] = (time.time(), pct)
    return pct


def _regime_label(vix: float, trend_score: float, breadth_pct: float) -> str:
    """Human-readable market posture."""
    if vix >= 28:
        return "High-vol defensive"
    if vix >= 22:
        base = "Choppy, elevated vol"
    elif vix <= 13:
        base = "Low-vol drift"
    else:
        base = "Balanced"
    if trend_score >= 0.35 and breadth_pct >= 60:
        return f"{base} — risk-on bias"
    if trend_score <= -0.35 and breadth_pct <= 40:
        return f"{base} — risk-off bias"
    if trend_score >= 0.15:
        return f"{base} — leaning bullish"
    if trend_score <= -0.15:
        return f"{base} — leaning bearish"
    return f"{base} — neutral"


def screener_rows(signal_filter: str = "all", timeframe: Timeframe = "daily") -> list[TickerRowOut]:
    """Live signals table, driven by ``overview_rows(timeframe)``."""
    rows_live = overview_rows(timeframe)
    out: list[TickerRowOut] = []
    want = signal_filter.lower()
    for r in rows_live:
        signal = _signal_for(r.verdict)
        if want != "all" and signal.lower() != want:
            continue
        closes: list[float] = []
        try:
            series = get_ohlcv(r.symbol, timeframe)
            closes = series.close
        except Exception:
            closes = []
        out.append(_row_from_overview(r, closes))
    # Buy/Sell by |composite|, Holds last.
    out.sort(key=lambda x: (x.signal == "Hold", -abs(x.composite_score)))
    return out


_ALLOWED_CHART_RANGES = frozenset({"1d", "5d", "1m", "6m", "1y", "ytd"})


def _iso_to_date(ts: str) -> date | None:
    if not ts:
        return None
    try:
        s = str(ts).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _ohlcv_to_bar_list(data: OHLCV, start: int, end: int) -> list[TickerChartBarOut]:
    start = max(0, int(start))
    end = min(len(data), int(end))
    return [
        TickerChartBarOut(
            t=data.timestamps[i],
            o=round(float(data.open[i]), 4),
            h=round(float(data.high[i]), 4),
            l=round(float(data.low[i]), 4),
            c=round(float(data.close[i]), 4),
            v=round(float(data.volume[i]), 0) if i < len(data.volume) else 0.0,
        )
        for i in range(start, end)
    ]


def _chart_bundle_for_range(symbol: str, range_key: str) -> TickerChartBundleOut:
    """Slice OHLCV to match Yahoo-style ranges: 1D = minute intraday (smooth), else daily bars."""
    rk = range_key.lower()
    if rk not in _ALLOWED_CHART_RANGES:
        rk = "1d"
    sym = symbol.upper()
    bars: list[TickerChartBarOut] = []
    x_g: str = "day"

    if rk == "1d":
        # Dense 1m/2m/5m (or interpolated) session — Yahoo-like smooth 1D chart + full day axis
        idata: OHLCV | None = None
        try:
            idata = get_ohlcv_1d_intraday(sym)
        except Exception:
            idata = None
        if idata and len(idata) >= 2:
            bars = _ohlcv_to_bar_list(idata, 0, len(idata))
            x_g = "session"
        else:
            h = get_ohlcv(sym, "1h")
            if len(h) >= 2:
                n = min(16, len(h))
                bars = _ohlcv_to_bar_list(h, len(h) - n, len(h))
                x_g = "hour"
            else:
                d = get_ohlcv(sym, "daily")
                n = min(5, len(d))
                bars = _ohlcv_to_bar_list(d, max(0, len(d) - n), len(d)) if d else []
    elif rk == "5d":
        d = get_ohlcv(sym, "daily")
        n = min(5, len(d))
        bars = _ohlcv_to_bar_list(d, max(0, len(d) - n), len(d)) if d else []
    elif rk == "1m":
        d = get_ohlcv(sym, "daily")
        n = min(22, len(d))
        bars = _ohlcv_to_bar_list(d, max(0, len(d) - n), len(d)) if d else []
    elif rk == "6m":
        d = get_ohlcv(sym, "daily")
        n = min(128, len(d))
        bars = _ohlcv_to_bar_list(d, max(0, len(d) - n), len(d)) if d else []
    elif rk == "1y":
        d = get_ohlcv(sym, "daily")
        n = min(252, len(d))
        bars = _ohlcv_to_bar_list(d, max(0, len(d) - n), len(d)) if d else []
    else:  # ytd
        d = get_ohlcv(sym, "daily")
        y = datetime.now(timezone.utc).year
        jan1 = date(y, 1, 1)
        start_i: int | None = None
        for i, ts in enumerate(d.timestamps):
            di = _iso_to_date(ts)
            if di is not None and di >= jan1:
                start_i = i
                break
        if start_i is None:
            start_i = max(0, len(d) - 64)
        bars = _ohlcv_to_bar_list(d, start_i, len(d))
        if len(bars) < 2:
            n = min(64, len(d))
            bars = _ohlcv_to_bar_list(d, max(0, len(d) - n), len(d))

    if len(bars) < 1:
        d = get_ohlcv(sym, "daily")
        n = min(5, len(d))
        if d:
            bars = _ohlcv_to_bar_list(d, max(0, len(d) - n), len(d))
            x_g = "day"

    return TickerChartBundleOut(range_key=rk, x_granularity=x_g, bars=bars)


def _narrative_excerpt(narrative: str, max_len: int = 720) -> str:
    t = (narrative or "").strip()
    if not t:
        return ""
    if len(t) <= max_len:
        return t
    cut = t[:max_len]
    if " " in cut:
        return cut.rsplit(" ", 1)[0] + "…"
    return cut + "…"


def _technical_bullets_from_report(report: ReportOut) -> list[str]:
    out: list[str] = []
    s = report.sma
    if s.price_vs_sma50_pct is not None:
        extra = ""
        if s.golden_cross_recent:
            extra = " (recent golden cross)"
        elif s.death_cross_recent:
            extra = " (recent death cross)"
        out.append(f"SMA: price {s.price_vs_sma50_pct:+.1f}% vs 50d{extra}")
    pa = report.price_action
    out.append(f"Price action: {pa.trend}, change {pa.change_pct:+.2f}%")
    r = report.rsi
    if r.value is not None:
        out.append(f"RSI (14): {r.value:.1f} — {r.state}")
    st = report.stochastic
    if st.pct_k is not None and st.pct_d is not None:
        out.append(
            f"Stochastic (14/3/3): %K={st.pct_k:.1f}, %D={st.pct_d:.1f} — {st.state}"
        )
    m = report.macd
    if m.histogram is not None:
        out.append(f"MACD: histogram {m.histogram:+.3f}, slope {m.histogram_direction}")
    else:
        out.append("MACD: not enough data for a clean read on this window")
    if report.atr.value is not None and report.atr.pct_of_price is not None:
        out.append(
            f"ATR (14): ${report.atr.value:.2f} (~{report.atr.pct_of_price:.2f}% of price)"
        )
    ad = report.adx
    if ad.value is not None:
        out.append(
            f"ADX (14): {ad.value:.1f} (+/− DI bias {ad.directional_bias}; "
            f"trend strength {ad.trend_strength})"
        )
    bb = report.bollinger
    if bb.middle is not None and bb.pct_b is not None:
        out.append(
            f"Bollinger (20): %B≈{bb.pct_b:.2f}, position={bb.position}"
        )
    v = report.volume
    out.append(
        f"Volume vs 20d avg: {v.ratio:.2f}× ({'rising' if v.trending_up else 'flat/soft'} vs its series)"
    )
    return out[:10]


def ticker_detail(
    symbol: str,
    timeframe: Timeframe = "daily",
    *,
    price_range: str = "1d",
) -> TickerDetailOut | None:
    """Detail payload for a single ticker, sourced from the analyst pipeline."""
    sym = symbol.upper()
    meta = TICKER_MAP.get(sym)
    if meta is None:
        return None

    try:
        report = build_report(sym, timeframe)
    except Exception:
        return None

    try:
        series = get_ohlcv(sym, timeframe)
        closes = series.close
    except Exception:
        closes = report.chart.close

    # Equivalent OverviewRow so we can reuse _row_from_overview.
    tp = report.options.trade_plan
    synthetic_row = OverviewRow(
        symbol=report.symbol,
        name=report.name,
        sector=report.sector,
        last=round(report.price_action.last, 2),
        change_pct=round(report.price_action.change_pct, 2),
        verdict=report.verdict,
        conviction=report.conviction,
        composite_score=report.composite_score,
        rsi=round(report.rsi.value, 1) if report.rsi.value is not None else None,
        trend=report.price_action.trend,
        source=report.source,
        rec_contract_type=tp.contract_type,
        rec_strike=tp.strike,
        rec_expiry_date=tp.expiry_date,
        rec_expiry_dte=tp.expiry_dte,
        rec_premium=tp.estimated_premium,
        rec_cost_per_contract=tp.cost_per_contract,
        rec_break_even=tp.break_even,
        rec_target=tp.target_price,
        rec_stop=tp.stop_loss,
        rec_risk_reward=tp.risk_reward,
    )
    row = _row_from_overview(synthetic_row, closes)

    # 30-bar price window for the sparkline / chart.
    price_30 = [round(float(c), 2) for c in closes[-30:]] if closes else report.chart.close[-30:]

    # Expected move: use the recommender's trade-plan one-sigma when present,
    # otherwise derive from realised vol.
    if tp.one_sigma_move_usd is not None and tp.one_sigma_move_pct is not None:
        expected = ExpectedMoveOut(
            one_sigma_usd=round(float(tp.one_sigma_move_usd), 2),
            one_sigma_pct=round(float(tp.one_sigma_move_pct), 2),
        )
    else:
        iv_used = row.iv
        dte = max(1, row.dte_pref)
        one_sigma_usd = row.price * iv_used * math.sqrt(dte / 365.0)
        expected = ExpectedMoveOut(
            one_sigma_usd=round(one_sigma_usd, 2),
            one_sigma_pct=round(iv_used * math.sqrt(dte / 365.0) * 100.0, 2),
        )

    ctx = MarketContext(
        symbol=row.symbol,
        spot=row.price,
        iv=row.iv,
        iv_rank=row.iv_rank,
        direction=row.direction,
        conviction=row.confidence,
        dte=row.dte_pref,
        risk_free_rate=0.045,
        dividend_yield=0.0,
        earnings_in_window=row.earnings_in_window,
    )
    try:
        recs = recommend_strategies(ctx, max_results=4)
    except Exception:
        recs = []

    factors = derive_factors_from_report(report)

    pr = price_range.lower().strip()
    if pr not in _ALLOWED_CHART_RANGES:
        pr = "1d"

    try:
        chart_bundle = _chart_bundle_for_range(sym, pr)
    except Exception:
        chart_bundle = TickerChartBundleOut(range_key=pr, x_granularity="day", bars=[])

    chart_closes = [b.c for b in chart_bundle.bars]
    if chart_closes:
        price_series = [round(float(c), 4) for c in chart_closes]
    elif closes:
        price_series = [round(float(c), 4) for c in closes[-200:]]
    else:
        price_series = [round(float(c), 4) for c in report.chart.close[-200:]]

    chart_context = _build_ticker_chart_context(
        report=report,
        row=row,
        factors=factors,
        closes=closes if closes else list(report.chart.close),
    )

    try:
        news_items = [
            TickerNewsOut(
                title=n.title,
                publisher=n.publisher,
                url=n.url,
                published_at=n.published_at,
                summary=n.summary or "",
            )
            for n in news_for_ticker(sym, 8)
        ]
    except Exception:
        news_items = []

    signal_detail = (
        f"{report.headline} — Verdict {report.verdict}, signal {row.signal} "
        f"at {int(row.confidence * 100)}% confidence; composite {row.composite_score:+.2f}."
    )
    narrative_summary = _narrative_excerpt(report.narrative)
    technical_bullets = _technical_bullets_from_report(report)

    return TickerDetailOut(
        row=row,
        factors=factors,
        price_30d=price_30,
        price_series=price_series,
        chart=chart_bundle,
        chart_context=chart_context,
        expected_move=expected,
        news=news_items,
        signal_detail=signal_detail,
        narrative_summary=narrative_summary,
        technical_bullets=technical_bullets,
        recommendations=[
            RecommendationOut(
                strategy=StrategyOut(
                    name=r.strategy.name,
                    legs=[
                        LegOut(
                            kind=leg.kind,
                            side=leg.side,
                            strike=leg.strike,
                            premium=round(leg.premium, 3),
                            quantity=leg.quantity,
                        )
                        for leg in r.strategy.legs
                    ],
                ),
                metrics=MetricsOut(
                    net_debit=round(r.metrics.net_debit, 2),
                    max_gain=clamp_inf(r.metrics.max_gain),
                    max_loss=clamp_inf(r.metrics.max_loss),
                    breakevens=[round(b, 2) for b in r.metrics.breakevens],
                    probability_of_profit=round(r.metrics.probability_of_profit, 4),
                ),
                rationale=r.rationale,
                fit_score=round(r.fit_score, 3),
                tags=list(r.tags),
            )
            for r in recs
        ],
    )


def _bollinger_interpretation_for_detail(closes: list[float]) -> str:
    """Short English read of Bollinger(20, 2σ) on the trailing window."""
    if not closes:
        return "No price history for Bollinger bands."
    tail = closes[-200:]
    n = 20
    if len(tail) < n:
        if len(tail) < 3:
            return "Not enough bars for a 20-period Bollinger on this window."
        n = len(tail)
    window = tail[-n:]
    m = float(statistics.fmean(window))
    if len(window) < 2:
        return f"Mean of last {n} closes ≈ ${m:.2f}."
    st = float(statistics.pstdev(window))
    upper = m + 2.0 * st
    lower = m - 2.0 * st
    last = float(tail[-1])
    if st < 1e-12 * max(abs(m), 1.0):
        return f"20-period mean ≈ ${m:.2f}; volatility band is very tight on this window."
    span = upper - lower
    pct_b = (last - lower) / span * 100.0 if span > 0 else 50.0
    if last >= upper:
        pos = (
            "at or above the upper band — price is stretched vs recent 20-bar "
            "volatility (watch for mean reversion risk in the model's view)."
        )
    elif last <= lower:
        pos = (
            "at or below the lower band — recent window is weak vs its own "
            "volatility (often read as oversold until structure improves)."
        )
    elif last > m:
        pos = f"above the midline, in the upper portion of the envelope (%B ≈ {pct_b:.0f}%)."
    else:
        pos = f"below the midline, in the lower portion of the envelope (%B ≈ {pct_b:.0f}%)."
    return (
        f"On the last {n} closes: mid {m:.2f}, band {lower:.2f} – {upper:.2f} "
        f"(Bollinger 20, 2σ). Last close {last:.2f} is {pos} "
        f"The shaded band in the chart is that envelope; midline is the 20-SMA of close."
    )


def _build_ticker_chart_context(
    *,
    report: ReportOut,
    row: TickerRowOut,
    factors: list[FactorOut],
    closes: list[float],
) -> TickerChartContextOut:
    """Explain RSI/MACD/ATR/BB and factor scores for the detail enlarged view."""
    lines: list[TickerSignalLineOut] = []

    lines.append(
        TickerSignalLineOut(
            label="Model headline",
            detail=report.headline,
        )
    )
    lines.append(
        TickerSignalLineOut(
            label="Signal (screener)",
            detail=(
                f"{row.signal} is derived from the same composite as the analyst: "
                f"verdict {report.verdict} with confidence {int(row.confidence * 100)}%."
            ),
        )
    )

    rsi = report.rsi
    if rsi.value is not None:
        st = getattr(rsi, "state", "unknown")
        lines.append(
            TickerSignalLineOut(
                label="RSI (14)",
                detail=(
                    f"Reading {rsi.value:.1f} — {st} posture. The model uses RSI "
                    "50/70/30 style zones for bull/bear and overbought/oversold tilts, "
                    "not as a stand-alone buy/sell rule."
                ),
            )
        )

    m = report.macd
    m_parts: list[str] = []
    if m.histogram is not None:
        m_parts.append(
            f"histogram {m.histogram:+.3f}, slope {m.histogram_direction}"
        )
    if m.bullish_cross_recent:
        m_parts.append("fresh bullish MACD/signal cross (recent bars)")
    if m.bearish_cross_recent:
        m_parts.append("fresh bearish MACD/signal cross (recent bars)")
    lines.append(
        TickerSignalLineOut(
            label="MACD (12,26,9)",
            detail=(
                "; ".join(m_parts)
                if m_parts
                else "No strong MACD state — momentum treated as mixed."
            ),
        )
    )

    atr = report.atr
    if atr.pct_of_price is not None and atr.value is not None:
        lines.append(
            TickerSignalLineOut(
                label="ATR (14) / volatility",
                detail=(
                    f"ATR ${atr.value:.2f} is about {atr.pct_of_price:.2f}% of last price — "
                    "informs option strike distance and how wide a typical session move is."
                ),
            )
        )

    lines.append(
        TickerSignalLineOut(
            label="Bollinger (20, 2σ) on chart",
            detail=_bollinger_interpretation_for_detail(closes),
        )
    )

    for f in factors:
        lines.append(
            TickerSignalLineOut(
                label=f"Factor — {f.name}",
                detail=f"Score {f.score:+.2f} — {f.note}",
            )
        )

    return TickerChartContextOut(
        headline=report.headline,
        verdict=report.verdict,
        signal=row.signal,
        lines=lines,
    )


__all__ = [
    "regime_envelope",
    "screener_rows",
    "ticker_detail",
]
