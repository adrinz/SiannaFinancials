"""Analyst report composer.

Takes OHLCV in, emits a fully-composed ``ReportOut`` with narrative,
indicator states, and an options suggestion routed through the existing
``square18_signals`` recommender so the strategy metrics are real.

The "agent" framing from the user request is implemented here as a
deterministic, rule-based analyst — no LLM calls, reproducible, and
cheap. Every claim in the narrative is grounded in one of the indicator
states computed in the same pass.
"""
from __future__ import annotations

import concurrent.futures
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from square18_signals.pricing import black_scholes_price

from .constants import (
    DEFAULT_IV,
    ETF_SIGNAL_TICKERS,
    SCREENER_EARNINGS_WINDOW_DAYS,
    TICKER_MAP,
    TICKERS,
    Timeframe,
)
from .data import OHLCV, get_ohlcv
from .factors import (
    bull_bear_balance_percent,
    derive_factor_breakdown,
    derive_factors_from_report,
    equity_direction_reason,
)
from .options_flow import get_options_flow, option_liquidity_at_strike
from .regime import breadth_above_50d, vix_quote
from .signal_config import (
    bearish_threshold,
    bullish_threshold,
    probability_for_signal,
    load_signal_config,
    probability_for_verdict,
)
from .yahoo_quotes import yf_last_price, yf_option_mid_per_share
from .indicators import (
    adx as _adx,
    atr as _atr,
    bollinger as _bollinger,
    macd as _macd,
    rolling_std,
    rsi as _rsi,
    rsi_divergence as _rsi_divergence,
    sma,
    stochastic as _stochastic,
    support_resistance,
)
from .models import (
    ChartPayload,
    EarningsSoonOut,
    IndicatorADX,
    IndicatorATR,
    IndicatorBollinger,
    IndicatorMACD,
    IndicatorRSI,
    IndicatorSMA,
    IndicatorStochastic,
    OptionsFlowOut,
    OptionsSuggestion,
    OverviewRow,
    PriceAction,
    ReportOut,
    StockEntryOut,
    StockStrategyOut,
    TradePlan,
    VolumeStats,
)


# ---------------------------------------------------------------------------
# MTF quick-score in-process cache
# ---------------------------------------------------------------------------

_mtf_cache: dict[str, tuple[float, float]] = {}  # "SYM|tf" -> (ts, score)
_MTF_CACHE_TTL = 120.0  # seconds

# In-process cache so concurrent screener calls (earnings + movers + enrich) do
# not each fan out a full build_report for the whole universe.
_overview_lock = threading.Lock()
_build_locks_guard = threading.Lock()
_overview_build_locks: dict[str, threading.Lock] = {}
_overview_rows_cache: dict[str, tuple[float, list[OverviewRow]]] = {}
OVERVIEW_ROWS_CACHE_TTL_SEC = 90.0
OVERVIEW_MAX_WORKERS = 8
OVERVIEW_TOTAL_TIMEOUT_SEC = 12.0


def _lock_for_overview_key(key: str) -> threading.Lock:
    with _build_locks_guard:
        if key not in _overview_build_locks:
            _overview_build_locks[key] = threading.Lock()
        return _overview_build_locks[key]


def _overview_cache_key(
    timeframe: Timeframe, metas: list[dict] | None
) -> str:
    if metas is None:
        return f"{timeframe}::default"
    syms = "|".join(sorted(m["symbol"] for m in metas))
    return f"{timeframe}::{syms}"


def _safe_build_report(symbol: str, timeframe: Timeframe) -> ReportOut | None:
    try:
        return build_report(symbol, timeframe)
    except Exception:
        return None


def reset_overview_rows_cache() -> None:
    """Test helper: clear the optional overview row cache."""
    with _overview_lock:
        _overview_rows_cache.clear()


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------


def build_report(
    symbol: str,
    timeframe: Timeframe,
    meta_override: dict | None = None,
    *,
    fresh_quotes: bool = False,
) -> ReportOut:
    """Build a full analyst report for a ticker.

    If ``symbol`` is in the curated ``TICKER_MAP`` its metadata is used
    directly. Otherwise the caller may pass ``meta_override`` (e.g. from
    a live yfinance lookup) to supply ``name``/``sector``/``bias`` — this
    is how the /api/search endpoint supports arbitrary tickers.

    Pass ``fresh_quotes=True`` to bypass in-process Yahoo spot/option quote
    caches so trade-plan cost reflects the latest chain pull on this request
    (still subject to Yahoo’s own delay tier, not brokerage tick-by-tick data).
    """
    sym = symbol.upper()
    meta = meta_override or TICKER_MAP.get(sym)
    if meta is None:
        raise ValueError(f"unknown symbol: {symbol}")

    data = get_ohlcv(sym, timeframe)
    if len(data) < 60:
        raise ValueError(f"insufficient history for {sym} at {timeframe}")

    closes = data.close
    y_spot = yf_last_price(sym, bypass_cache=fresh_quotes)
    spot_for_options = float(y_spot) if (y_spot is not None and y_spot > 0) else float(closes[-1])
    highs = data.high
    lows = data.low
    volumes = data.volume

    sma50_series = sma(closes, 50)
    sma200_series = sma(closes, 200) if len(closes) >= 200 else [None] * len(closes)
    rsi_series = _rsi(closes, 14)
    macd_line, signal_line, hist_line = _macd(closes)
    atr_series = _atr(highs, lows, closes, 14)
    adx_series, plus_di_series, minus_di_series = _adx(highs, lows, closes, 14)
    bb_mid, bb_upper, bb_lower = _bollinger(closes, 20, 2.0)
    stoch_k, stoch_d = _stochastic(highs, lows, closes, 14, 3, 3)
    rsi_div = _rsi_divergence(closes, rsi_series)

    # ADX slope — detect declining strong trend (trend losing steam)
    _adx_clean = [x for x in adx_series if x is not None]
    adx_slope_note: str = ""
    if len(_adx_clean) >= 7:
        _adx_now = _adx_clean[-1]
        _adx_5ago = _adx_clean[-6]
        if _adx_now is not None and _adx_5ago is not None and _adx_now > 22 and _adx_5ago > _adx_now + 5:
            adx_slope_note = (
                f"ADX declining: {_adx_now:.0f} vs {_adx_5ago:.0f} five bars ago — "
                "trend is losing momentum even though it's still active. "
                "Conviction in trend continuation should be reduced."
            )

    _is_index = meta.get("sector", "") in ("Volatility / Index", "Index / Broad Market", "Index / Tech-heavy")
    price_action = _price_action(closes, highs, lows, sma50_series)
    volume_stats = _volume_stats(volumes, is_index=_is_index)
    sma_block = _sma_block(closes, sma50_series, sma200_series)
    rsi_block = _rsi_block(rsi_series)
    macd_block = _macd_block(macd_line, signal_line, hist_line)
    atr_block = _atr_block(atr_series, closes)
    adx_block = _adx_block(adx_series, plus_di_series, minus_di_series)
    bb_block = _bollinger_block(closes, bb_mid, bb_upper, bb_lower)
    stoch_block = _stochastic_block(stoch_k, stoch_d)

    # --- Tier-1 enhanced scoring pipeline -----------------------------------
    cfg = load_signal_config()

    # Step 1: raw deterministic score (now includes Stochastic + Bollinger)
    raw_score = _compute_raw_score(
        price_action, volume_stats, sma_block, rsi_block, macd_block, adx_block,
        stoch_b=stoch_block, bb_b=bb_block,
    )

    # Step 2: multi-timeframe confluence bonus / veto
    mtf_score, mtf_note = _apply_mtf(raw_score, sym, timeframe, cfg)

    # Step 3a: triple mean-reversion override (RSI + Stoch + Bollinger all extreme)
    tri_score, tri_note = _apply_triple_mean_reversion_gate(mtf_score, rsi_block, stoch_block, bb_block)

    # Step 3b: RSI + Bollinger dual mean-reversion gate
    mr_score, mr_note = _apply_mean_reversion_gate(tri_score, rsi_block, bb_block)

    # Step 3c: earnings proximity gate
    earnings_score, earnings_note, _ew = _apply_earnings_gate(mr_score, sym, meta)

    # Step 3d: regime gate (VIX + market breadth — use vix_change for spike detection)
    regime_data_fallback = False
    try:
        vix_val, vix_change_today = vix_quote()
        breadth_val = breadth_above_50d(timeframe)
    except Exception:
        regime_data_fallback = True
        vix_val, vix_change_today, breadth_val = 17.0, 0.0, 50.0
    if not isinstance(breadth_val, float):
        breadth_val = 50.0
    final_score, regime_note = _apply_regime_gate(earnings_score, vix_val, breadth_val, cfg)

    # Step 4: apply configurable thresholds → verdict + conviction
    verdict, conviction = _score_to_verdict(final_score, cfg)
    composite = round(max(-1.0, min(1.0, final_score)), 3)
    headline = _headline(sym, timeframe, verdict, price_action, sma_block,
                         macd_block, rsi_block, adx_block)

    # Step 5: Tier-2 options intelligence (UOA · term-structure · skew)
    # Fetched after the verdict so UOA/skew adj is applied to the pre-verdict score
    # and re-applied below, keeping the scoring pipeline transparent.
    try:
        flow = get_options_flow(sym, spot_for_options, verdict, bypass_cache=fresh_quotes)
        flow_out = OptionsFlowOut(
            uoa_bull=flow.uoa_bull,
            uoa_bear=flow.uoa_bear,
            uoa_note=flow.uoa_note,
            term_slope=flow.term_slope,
            front_iv=flow.front_iv,
            back_iv=flow.back_iv,
            term_note=flow.term_note,
            skew=flow.skew,
            skew_note=flow.skew_note,
            atm_iv=flow.atm_iv,
            iv_baseline_ratio=flow.iv_baseline_ratio,
            implied_move_30d_pct=flow.implied_move_30d_pct,
            flow_score_adj=flow.flow_score_adj,
            source=flow.source,
        )
        # Apply flow adjustment and re-derive verdict if it tips over a threshold
        if flow.flow_score_adj != 0.0:
            adj_composite = max(-1.0, min(1.0, composite + flow.flow_score_adj))
            new_verdict, new_conviction = _score_to_verdict(adj_composite, cfg)
            composite = round(adj_composite, 3)
            verdict = new_verdict
            conviction = new_conviction
            headline = _headline(sym, timeframe, verdict, price_action, sma_block,
                                 macd_block, rsi_block, adx_block)
    except Exception:
        flow_out = OptionsFlowOut(source="unavailable")
    # -------------------------------------------------------------------------

    # Step 6: calibrated historical probability from backtest artifact.
    # Resolve after all score adjustments so the probability matches final verdict.
    try:
        _bt_path = __file__[:__file__.index("app" + __import__("os").sep)] if False else None
        import json as _json
        from pathlib import Path as _Path
        _bt_file = _Path(__file__).resolve().parent.parent.parent / "backtest_verdict.json"
        if _bt_file.exists():
            _bt = _json.loads(_bt_file.read_text())
        else:
            _bt = None
    except Exception:
        _bt = None

    signal_probability: Optional[float] = None
    signal_probability_scope: Optional[str] = None
    if _bt is not None:
        for _row in _bt.get("per_symbol", []):
            if _row.get("symbol") == sym:
                _bkt = _row.get("buckets", {}).get(verdict, {})
                if int(_bkt.get("n", 0)) >= 20:
                    signal_probability = float(_bkt["hit_rate"])
                    signal_probability_scope = "symbol"
                break
        if signal_probability is None:
            _agg = _bt.get("aggregate", {}).get(verdict, {})
            if _agg.get("n"):
                signal_probability = float(_agg["hit_rate"])
                signal_probability_scope = "aggregate"
    if signal_probability is None:
        signal_probability = probability_for_signal(verdict, composite)
        if signal_probability is not None:
            signal_probability_scope = "config"
    if signal_probability is None:
        signal_probability = probability_for_verdict(verdict)
        if signal_probability is not None:
            signal_probability_scope = "config"

    narrative = _compose_narrative(
        meta=meta,
        timeframe=timeframe,
        data=data,
        price_action=price_action,
        volume_stats=volume_stats,
        sma_block=sma_block,
        rsi_block=rsi_block,
        macd_block=macd_block,
        atr_block=atr_block,
        adx_block=adx_block,
        bollinger_block=bb_block,
        stoch_block=stoch_block,
        verdict=verdict,
        conviction=conviction,
    )

    market_ctx = _market_context_text(sym, meta["sector"], data.source)
    options = _build_options_suggestion(
        sym=sym,
        spot=spot_for_options,
        closes=closes,
        atr_block=atr_block,
        price_action=price_action,
        verdict=verdict,
        conviction=conviction,
        composite_score=composite,
        timeframe=timeframe,
        allow_directional_trade=(data.source != "synthetic"),
        fresh_quotes=fresh_quotes,
    )

    chart = ChartPayload(
        timestamps=data.timestamps,
        close=closes,
        sma50=sma50_series,
        sma200=sma200_series,
    )

    # earnings_soon: use the result already fetched in the earnings gate above;
    # fall back to a fresh lookup with the screener window if the gate returned None.
    if _ew is not None:
        earnings_soon = EarningsSoonOut(earnings_date=_ew[0], days_until=_ew[1])
    else:
        from .earnings import earnings_within_window_days as _eww
        _ew2 = _eww(sym, meta, window_days=SCREENER_EARNINGS_WINDOW_DAYS)
        earnings_soon = EarningsSoonOut(earnings_date=_ew2[0], days_until=_ew2[1]) if _ew2 else None

    # ---- Collect all signal quality warnings --------------------------------
    signal_warnings: list[str] = []

    # 0. Triple mean-reversion override note (from gate above)
    if tri_note:
        signal_warnings.append(tri_note)

    # 1. Synthetic data (critical)
    if data.source == "synthetic":
        signal_warnings.append(
            "⚠ SYNTHETIC DATA — live market connection failed; this signal uses "
            "simulated (GBM) price history. DO NOT trade on this."
        )
    if regime_data_fallback:
        signal_warnings.append(
            "Regime data unavailable (VIX/breadth). Using neutral fallback assumptions "
            "for market regime — treat conviction as lower confidence."
        )

    # 2. Earnings proximity (from gate above)
    if earnings_note:
        signal_warnings.append(earnings_note)

    # 3. RSI + Bollinger mean-reversion stretch (from gate above)
    if mr_note:
        signal_warnings.append(mr_note)

    # 4. ADX weak / absent — ranging market
    if adx_block.trend_strength in ("absent", "weak") and abs(composite) < 0.70:
        signal_warnings.append(
            f"ADX {adx_block.value:.0f} ({adx_block.trend_strength}) — no clear trend detected. "
            "Trend-following indicators (SMA stack, MACD cross) are unreliable in ranging/sideways "
            "markets. Wait for a confirmed directional breakout before committing new risk."
        )

    # 5. Price extension from 50d SMA
    ext = sma_block.price_vs_sma50_pct
    if ext is not None:
        if ext > 12 and composite > 0:
            signal_warnings.append(
                f"Stock is {ext:.1f}% above its 50-day SMA — historically stretched. "
                "Chasing bull signals at these extensions carries elevated mean-reversion risk."
            )
        elif ext < -12 and composite < 0:
            signal_warnings.append(
                f"Stock is {abs(ext):.1f}% below its 50-day SMA — oversold stretch. "
                "Bear signals at these levels have higher reversal risk."
            )

    # 6. All major factors aligned (correlation saturation)
    bull_factors = sum([
        price_action.trend == "uptrend",
        sma_block.stacked_bullish,
        macd_block.bullish_cross_recent,
        macd_block.histogram_direction == "rising",
        volume_stats.trending_up and price_action.change_pct > 0,
    ])
    bear_factors = sum([
        price_action.trend == "downtrend",
        sma_block.stacked_bearish,
        macd_block.bearish_cross_recent,
        macd_block.histogram_direction == "falling",
        volume_stats.trending_up and price_action.change_pct < 0,
    ])
    if bull_factors >= 4 and composite > 0.65:
        signal_warnings.append(
            f"{bull_factors}/5 trend-following factors simultaneously bullish — "
            "this can happen late in a move, not at the start. "
            "You may be chasing an extended trend. Check RSI/MACD divergence before entering."
        )
    if bear_factors >= 4 and composite < -0.65:
        signal_warnings.append(
            f"{bear_factors}/5 trend-following factors simultaneously bearish — "
            "extreme alignment can mark capitulation / oversold lows, not trend continuation."
        )

    # 7. Bollinger squeeze (direction undetermined)
    if bb_block.bandwidth_pct is not None and bb_block.bandwidth_pct < 4.5:
        signal_warnings.append(
            f"Bollinger Bands very tight (bandwidth {bb_block.bandwidth_pct:.1f}% of price) — "
            "a volatility squeeze. A breakout may come soon, but direction is unclear. "
            "Avoid large positions until price breaks out with volume."
        )

    # 8. Borderline score — just crossed threshold
    bull_tau_cfg = float(load_signal_config()["thresholds"]["BULLISH"]["min_score"])
    bear_tau_cfg = float(load_signal_config()["thresholds"]["BEARISH"]["max_score"])
    if composite >= bull_tau_cfg and composite < bull_tau_cfg + 0.12:
        signal_warnings.append(
            f"Score {composite:+.2f} is just above the BULLISH threshold ({bull_tau_cfg:+.2f}). "
            "Borderline signals have lower historical reliability — "
            "consider waiting for a stronger reading before opening a position."
        )
    elif composite > 0 and composite < bull_tau_cfg:
        signal_warnings.append(
            f"Score {composite:+.2f} is below the BULLISH threshold ({bull_tau_cfg:+.2f}) "
            "and remains in the neutral zone. Treat upside bias as weak until confirmation."
        )
    elif composite <= bear_tau_cfg and composite > bear_tau_cfg - 0.12:
        signal_warnings.append(
            f"Score {composite:+.2f} is just below the BEARISH threshold ({bear_tau_cfg:+.2f}). "
            "Borderline bear signals have the weakest historical edge — extra confirmation recommended."
        )
    elif composite < 0 and composite > bear_tau_cfg:
        signal_warnings.append(
            f"Score {composite:+.2f} is above the BEARISH threshold ({bear_tau_cfg:+.2f}) "
            "and remains in the neutral zone. Treat downside bias as weak until confirmation."
        )

    # 9. RSI divergence (price and RSI disagree at recent pivots)
    if rsi_div == "bearish" and composite > 0:
        signal_warnings.append(
            "RSI bearish divergence detected — price made a higher high but RSI made a "
            "lower high. Momentum is weakening while price rises, which raises reversal risk. "
            "Bull signals are less reliable under this pattern."
        )
    elif rsi_div == "bullish" and composite < 0:
        signal_warnings.append(
            "RSI bullish divergence detected — price made a lower low but RSI made a "
            "higher low. Downside momentum is weakening, which raises bounce risk. "
            "Bear signals are less reliable under this pattern."
        )

    # 10. Overnight / intra-session gap detection
    if len(closes) >= 3 and len(data.open) >= 2:
        try:
            last_open = float(data.open[-1])
            prev_close = float(closes[-2])
            if prev_close > 0:
                gap_pct = (last_open / prev_close - 1) * 100
                if abs(gap_pct) >= 3.0:
                    direction = "up" if gap_pct > 0 else "down"
                    signal_warnings.append(
                        f"Large {direction} gap detected: last bar opened "
                        f"{gap_pct:+.1f}% vs prior close. This often means news or event risk. "
                        "Indicators still reflect pre-gap bars, so signal quality is lower right now."
                    )
        except Exception:
            pass

    # 11. Sector ETF headwind/tailwind
    _SECTOR_ETF: dict[str, str] = {
        "Semiconductors / AI": "SMH", "Semiconductors": "SMH",
        "Semiconductors / Equip.": "SMH",
        "Consumer Electronics": "XLK", "Software / Cloud": "XLK",
        "Software / AI": "XLK", "Communication Services": "XLK",
        "Cybersecurity": "XLK",
        "Financials / Banks": "XLF", "Financials / Crypto": "XLF",
        "Energy / Integrated Oil": "XLE",
        "Consumer / Cloud": "XLK",
        "Autos / EV": "XLK",
    }
    _sector = meta.get("sector", "")
    _etf_sym = _SECTOR_ETF.get(_sector)
    if _etf_sym:
        try:
            _etf_score = _quick_score(_etf_sym, timeframe)
            if _etf_score is not None:
                if composite > 0 and _etf_score < -0.20:
                    signal_warnings.append(
                        f"Sector headwind: {_sector} ETF ({_etf_sym}) is bearish "
                        f"(score {_etf_score:+.2f}) while this ticker is bullish. "
                        "Trading against sector trend lowers odds of follow-through. "
                        "Consider waiting for better sector alignment."
                    )
                elif composite < 0 and _etf_score > 0.20:
                    signal_warnings.append(
                        f"Sector tailwind: {_sector} ETF ({_etf_sym}) is bullish "
                        f"(score {_etf_score:+.2f}) but this ticker is bearish. "
                        "Shorting a stock in a rising sector is higher risk."
                    )
        except Exception:
            pass

    # 12. Symbol-class specific risks
    _sym_class_notes = {
        "QUBT": "Speculative quantum-computing micro-cap with implied vol ~120%/yr. "
                "Technical patterns are dominated by news and short-squeeze dynamics — TA edge is minimal.",
        "QBTS": "Speculative quantum-computing small-cap (~105% IV). News/narrative driven; "
                "technical signals have low predictive value.",
        "SMR": "Pre-revenue nuclear SMR company (~95% IV). Regulatory news overrides all technicals.",
        "OKLO": "Early-stage nuclear (~110% IV). Highly speculative; chart patterns unreliable.",
        "COIN": "Crypto-sector correlated: price often moves with Bitcoin regardless of technical setup. "
                "Check BTC trend before acting on this signal.",
        "BYDDY": "ADR (American Depositary Receipt): subject to FX risk (CNY/USD), Hong Kong session gaps, "
                "and limited US options liquidity. Options bid-ask spreads may be very wide.",
    }
    if sym in _sym_class_notes:
        signal_warnings.append(_sym_class_notes[sym])

    # 10. Options chain liquidity at recommended strike
    try:
        _tp = options.trade_plan
        _liq = option_liquidity_at_strike(
            sym, _tp.strike, _tp.contract_type == "call",
        )
        if _liq:
            _oi = _liq.get("oi", 0)
            _sp = _liq.get("spread_pct")
            if _oi < 100:
                signal_warnings.append(
                    f"Low open interest at ${_tp.strike} {_tp.contract_type}: "
                    f"OI = {_oi} contracts. Liquidity is thin, so fills may be poor "
                    "and exits may be harder."
                )
            if _sp is not None and _sp > 10:
                signal_warnings.append(
                    f"Wide bid-ask spread at ${_tp.strike} {_tp.contract_type}: "
                    f"{_sp:.0f}% of mid-price. You lose about {_sp/2:.0f}% on entry "
                    "from spread alone. Prefer tighter markets."
                )
                # Penalty for wide spread
                conviction = round(max(0.10, conviction - 0.15), 3)
    except Exception:
        pass

    # 10b. Options cost-effectiveness (only if trade plan is populated)
    try:
        _tp = options.trade_plan
        if (_tp.cost_per_contract is not None and _tp.one_sigma_move_usd is not None
                and _tp.one_sigma_move_usd > 0):
            _sigma_dollars = _tp.one_sigma_move_usd * 100  # convert per-share → per contract
            _cost_pct_of_sigma = _tp.cost_per_contract / _sigma_dollars
            if _cost_pct_of_sigma > 0.55:
                signal_warnings.append(
                    f"Premium ${_tp.cost_per_contract:.0f}/contract is "
                    f"{_cost_pct_of_sigma*100:.0f}% of a full 1σ expected move "
                    f"(±${_tp.one_sigma_move_usd:.2f}/share). This option is expensive "
                    "for the expected move and may need a bigger move than usual to pay off."
                )
    except Exception:
        pass

    # 11. ADX slope — declining strong trend
    if adx_slope_note:
        signal_warnings.append(adx_slope_note)

    # 12. MACD histogram deceleration
    if macd_block.histogram_direction == "decelerating_bull" and composite > 0:
        signal_warnings.append(
            "MACD histogram decelerating: still positive but shrinking — "
            "bullish momentum is fading. Consider tighter risk control or wait "
            "for momentum to re-accelerate."
        )
    elif macd_block.histogram_direction == "decelerating_bear" and composite < 0:
        signal_warnings.append(
            "MACD histogram decelerating: still negative but shrinking — "
            "bearish momentum is fading. A bounce risk is increasing."
        )

    # 13. MACD cross quality — RSI below midline reduces reliability
    _rsi_val = rsi_block.value
    if macd_block.bullish_cross_recent and _rsi_val is not None and _rsi_val < 50:
        signal_warnings.append(
            f"MACD bull cross with RSI {_rsi_val:.0f} below 50 — "
            "this is a weaker setup than a bull cross with RSI above 50."
        )
    elif macd_block.bearish_cross_recent and _rsi_val is not None and _rsi_val > 50:
        signal_warnings.append(
            f"MACD bear cross with RSI {_rsi_val:.0f} above 50 — "
            "this is a weaker bearish setup. Waiting for RSI below 50 gives better confirmation."
        )

    # 14. Dual Stochastic + RSI overbought / oversold
    if rsi_block.state == "overbought" and stoch_block.state == "overbought":
        signal_warnings.append(
            f"Both RSI ({rsi_block.value:.0f}) and Stochastic ({stoch_block.pct_k:.0f}/{stoch_block.pct_d:.0f}) "
            "are overbought at the same time. Reversal risk is higher here for new bull entries."
        )
    elif rsi_block.state == "oversold" and stoch_block.state == "oversold":
        signal_warnings.append(
            f"Both RSI ({rsi_block.value:.0f}) and Stochastic ({stoch_block.pct_k:.0f}/{stoch_block.pct_d:.0f}) "
            "are oversold at the same time. Bounce risk is higher here for new bear entries."
        )

    # 15. IV crush risk (backwardation + earnings proximity)
    try:
        _of_source = flow_out.source if flow_out else "unavailable"
        _of_term = flow_out.term_slope if flow_out else None
        _has_earnings = earnings_soon is not None and earnings_soon.days_until <= 7
        if _of_term is not None and _of_term > 1.08 and _has_earnings:
            signal_warnings.append(
                f"⚠ IV crush risk: options are pricing elevated near-term volatility "
                f"({_of_term:.2f}× term slope = backwardation) ahead of earnings in "
                f"{earnings_soon.days_until}d. Option prices can drop hard after earnings, "
                "so even a correct direction can still lose money if entry premium is high."
            )
        elif _of_term is not None and _of_term > 1.10 and not _has_earnings:
            signal_warnings.append(
                f"Term structure in backwardation ({_of_term:.2f}×): front-month IV elevated "
                "vs back-month. Near-term event risk may be priced in. "
                "Long options can be expensive and may lose value if volatility drops."
            )
    except Exception:
        pass

    # 18. Stale-bar warning for intraday timeframes
    if timeframe in ("1h", "4h"):
        from datetime import datetime as _dt, timezone as _tz
        try:
            last_bar_iso = data.timestamps[-1]
            last_bar_dt = _dt.fromisoformat(last_bar_iso.replace("Z", "+00:00"))
            bar_age_min = (_dt.now(_tz.utc) - last_bar_dt).total_seconds() / 60
            bar_size_min = 60 if timeframe == "1h" else 240
            if bar_age_min < bar_size_min:
                signal_warnings.append(
                    f"Current {timeframe.upper()} bar is still open — "
                    f"indicators use the last CLOSED bar ({int(bar_age_min)}m ago). "
                    "Intraday reversals inside the open bar are not yet reflected in this signal."
                )
        except Exception:
            pass

    # 17. Earnings vs option expiry alignment
    try:
        _tp_dte = options.trade_plan.expiry_dte
        if earnings_soon is not None and earnings_soon.days_until > 0:
            _earn_days = earnings_soon.days_until
            if _earn_days > _tp_dte:
                signal_warnings.append(
                    f"Option expires BEFORE earnings: your {_tp_dte}D contract expires "
                    f"before earnings in {_earn_days}d. You may miss the main catalyst. "
                    f"Consider longer days to expiry beyond {earnings_soon.earnings_date}."
                )
            else:
                signal_warnings.append(
                    f"Earnings in {_earn_days}d fall within your option's {_tp_dte}D window. "
                    "Event volatility is already priced in, so option value can fall after "
                    "earnings even if direction is right."
                )
    except Exception:
        pass

    # 17b. Short interest squeeze risk
    try:
        from .yahoo_quotes import yf_short_interest_pct as _yf_si
        _si = _yf_si(sym)
        if _si is not None:
            if composite < 0 and _si >= 0.20:
                signal_warnings.append(
                    f"⚠ High short interest: {_si*100:.0f}% of float is sold short. "
                    "Squeeze risk is high, so bearish options can be hit by sharp upside moves."
                )
            elif composite > 0 and _si >= 0.30:
                signal_warnings.append(
                    f"High short interest: {_si*100:.0f}% of float is sold short (>30%). "
                    "Upside squeeze is possible, but volatility is also higher than normal."
                )
    except Exception:
        pass

    # 17c. Weekend theta warning (Thu/Fri entry with short DTE)
    try:
        from datetime import datetime as _dt_now
        _dow = _dt_now.now().weekday()   # 0=Mon … 4=Fri
        _tp_dte2 = options.trade_plan.expiry_dte
        if _dow in (3, 4) and _tp_dte2 <= 21 and options.trade_plan.theta_per_day is not None:
            _weekend_cost = round(abs(options.trade_plan.theta_per_day) * 2 * 100, 2)
            _day_name = "Thursday" if _dow == 3 else "Friday"
            signal_warnings.append(
                f"Weekend theta: entering on {_day_name} with {_tp_dte2} DTE means "
                f"paying ~${_weekend_cost:.2f}/contract over Sat+Sun while the market "
                "is closed (time decay only). Monday or Tuesday entries may be more efficient."
            )
    except Exception:
        pass

    # 17d. Low DTE gamma explosion warning
    try:
        _tp_dte3 = options.trade_plan.expiry_dte
        if _tp_dte3 <= 14:
            signal_warnings.append(
                f"Very short DTE: {_tp_dte3} days to expiry. Gamma is very high — a $1 "
                "underlying move can swing option value quickly. Gains can be fast, "
                "but losses can be just as fast."
            )
    except Exception:
        pass

    # 17e. VIX intraday spike — regime may have changed since last OHLCV bar
    if vix_change_today >= 4.0 and composite > 0:
        signal_warnings.append(
            f"VIX spiked +{vix_change_today:.1f} points today (now {vix_val:.1f}). "
            "Market fear jumped sharply — risk-off conditions may be developing that "
            "are not yet reflected in indicator values (which use yesterday's close). "
            "Bull signals formed on prior-day data carry higher uncertainty today."
        )
    elif vix_change_today <= -4.0 and composite < 0:
        signal_warnings.append(
            f"VIX dropped {vix_change_today:.1f} points today (now {vix_val:.1f}). "
            "Fear is rapidly leaving the market — bear signals may be premature as "
            "risk appetite recovers."
        )

    # 17f. Options IV context — expensive premium / move already priced
    try:
        _ivr = flow_out.iv_baseline_ratio if flow_out else None
        _iv30 = flow_out.implied_move_30d_pct if flow_out else None
        if _ivr is not None and _ivr >= 1.45:
            signal_warnings.append(
                f"Elevated IV regime: ATM IV is ~{_ivr:.2f}× the symbol's baseline. "
                "Options are expensive right now, and prices can drop after a news event "
                "even if your market direction is correct."
            )
        if (
            _iv30 is not None
            and options.trade_plan.target_price is not None
            and spot_for_options > 0
        ):
            _target_move = abs((options.trade_plan.target_price - spot_for_options) / spot_for_options) * 100
            if _target_move > (_iv30 * 1.15):
                signal_warnings.append(
                    f"Target may be ambitious vs implied move: 30D implied move is ±{_iv30:.1f}% "
                    f"while target needs ~{_target_move:.1f}%. Consider smaller size or more time to expiry."
                )
            elif _target_move < (_iv30 * 0.45):
                signal_warnings.append(
                    f"Market is pricing a larger move than your target (±{_iv30:.1f}% implied vs "
                    f"~{_target_move:.1f}% target). News-driven volatility may move this faster than expected."
                )
    except Exception:
        pass

    # 18. Extreme volume spike (meme / squeeze / news event)
    if not _is_index and volume_stats.ratio > 5.0:
        signal_warnings.append(
            f"Extreme volume: {volume_stats.ratio:.0f}× normal 20-day average. "
            "This often means major news or squeeze risk. Indicator reliability is lower "
            "when flows are this extreme."
        )

    # 19. Low average daily volume (thin liquidity)
    if not _is_index and volume_stats.avg_20 > 0 and volume_stats.avg_20 < 500_000:
        signal_warnings.append(
            f"Low average daily volume: {volume_stats.avg_20/1e6:.2f}M shares/day (20-day avg). "
            "Thin markets can move fast on small orders, and option spreads are usually wider."
        )

    # 20. Near-term price deterioration within broader uptrend (poor entry timing)
    if len(closes) >= 5 and price_action.trend == "uptrend" and composite > 0:
        _recent_high = max(closes[-5:])
        if _recent_high > 0:
            _pullback_pct = (closes[-1] - _recent_high) / _recent_high * 100
            if _pullback_pct < -4.0:
                signal_warnings.append(
                    f"Near-term weakness in uptrend: price is {abs(_pullback_pct):.1f}% below "
                    f"its 5-bar high (${_recent_high:.2f}). Adding long stock while the "
                    "stock is pulling back risks buying a dip that keeps dipping — "
                    f"consider waiting for price to reclaim ${_recent_high:.2f}."
                )

    _eq_factors = derive_factor_breakdown(
        sma_block, price_action, adx_block, rsi_block, macd_block, volume_stats
    )
    _dir_sum, _dir_bullets = equity_direction_reason(
        verdict, _eq_factors, headline, composite, conviction
    )
    stock_strategy = _build_stock_strategy(
        sym=sym,
        spot=spot_for_options,
        closes=closes,
        atr_block=atr_block,
        price_action=price_action,
        verdict=verdict,
        conviction=conviction,
        composite_score=composite,
        timeframe=timeframe,
        rsi_block=rsi_block,
        direction_summary=_dir_sum,
        direction_bullets=_dir_bullets,
    )
    equity_signal_warnings = _derive_equity_signal_warnings(
        signal_warnings,
        earnings_soon=earnings_soon,
        stock_swing_days=_stock_swing_calendar_days(timeframe),
        flow_out=flow_out,
    )

    bull_pct, bear_pct = bull_bear_balance_percent(composite)
    report = ReportOut(
        symbol=sym,
        name=meta["name"],
        sector=meta["sector"],
        timeframe=timeframe,
        as_of=data.timestamps[-1] if data.timestamps else datetime.now(timezone.utc).isoformat(),
        source=data.source,
        verdict=verdict,
        conviction=round(conviction, 3),
        composite_score=composite,
        bull_pct=bull_pct,
        bear_pct=bear_pct,
        verdict_factors=[],
        headline=headline,
        narrative=narrative,
        price_action=price_action,
        volume=volume_stats,
        sma=sma_block,
        rsi=rsi_block,
        macd=macd_block,
        atr=atr_block,
        adx=adx_block,
        bollinger=bb_block,
        stochastic=stoch_block,
        market_context=market_ctx,
        options=options,
        chart=chart,
        earnings_soon=earnings_soon,
        signal_probability=round(signal_probability, 1) if signal_probability is not None else None,
        signal_probability_scope=signal_probability_scope,  # type: ignore[arg-type]
        mtf_confluence=mtf_note or None,
        regime_gate=regime_note or None,
        signal_warnings=signal_warnings,
        equity_signal_warnings=equity_signal_warnings,
        stock_strategy=stock_strategy,
        options_flow=flow_out,
    )
    return report.model_copy(
        update={"verdict_factors": derive_factors_from_report(report)}
    )


def overview_rows(
    timeframe: Timeframe = "daily",
    *,
    metas: list[dict] | None = None,
) -> list[OverviewRow]:
    """Compact verdict + recommendation for every entry in *metas* (default: ``TICKERS``)."""
    key = _overview_cache_key(timeframe, metas)
    now = time.time()
    with _overview_lock:
        hit = _overview_rows_cache.get(key)
        if (
            hit
            and (now - hit[0]) < OVERVIEW_ROWS_CACHE_TTL_SEC
        ):
            return list(hit[1])

    b_lock = _lock_for_overview_key(key)
    with b_lock:
        now2 = time.time()
        with _overview_lock:
            hit2 = _overview_rows_cache.get(key)
            if (
                hit2
                and (now2 - hit2[0]) < OVERVIEW_ROWS_CACHE_TTL_SEC
            ):
                return list(hit2[1])

        universe = metas if metas is not None else TICKERS
        rows: list[OverviewRow] = []
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=OVERVIEW_MAX_WORKERS)
        fut_to_meta = {
            ex.submit(_safe_build_report, meta["symbol"], timeframe): meta
            for meta in universe
        }
        done, not_done = concurrent.futures.wait(
            set(fut_to_meta.keys()),
            timeout=OVERVIEW_TOTAL_TIMEOUT_SEC,
            return_when=concurrent.futures.ALL_COMPLETED,
        )
        for fut in done:
            rpt = fut.result()
            if rpt is None:
                continue
            tp = rpt.options.trade_plan
            rows.append(
                OverviewRow(
                    symbol=rpt.symbol,
                    name=rpt.name,
                    sector=rpt.sector,
                    last=round(rpt.options.trade_plan.spot_at_entry, 2),
                    change_pct=round(rpt.price_action.change_pct, 2),
                    verdict=rpt.verdict,
                    conviction=rpt.conviction,
                    composite_score=rpt.composite_score,
                    rsi=round(rpt.rsi.value, 1) if rpt.rsi.value is not None else None,
                    trend=rpt.price_action.trend,
                    source=rpt.source,
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
            )
        for fut in not_done:
            fut.cancel()
        ex.shutdown(wait=False, cancel_futures=True)
        rows.sort(key=lambda r: r.symbol)
        ts = time.time()
        with _overview_lock:
            _overview_rows_cache[key] = (ts, rows)
    return rows


def etf_overview_rows(timeframe: Timeframe = "daily") -> list[OverviewRow]:
    """Verdicts for the dedicated ETF watchlist (``ETF_SIGNAL_TICKERS``)."""
    return overview_rows(timeframe, metas=ETF_SIGNAL_TICKERS)


# ---------------------------------------------------------------------------
# Indicator → block helpers
# ---------------------------------------------------------------------------


def _swing_structure_patterns(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    trend: str,
    last: float,
) -> list[str]:
    """Lightweight chart-structure hints (heuristic; not formal pattern recognition)."""
    out: list[str] = []
    n = len(closes)
    if n < 30:
        return out
    try:
        prior_hi = max(highs[-30:-8])
        recent_hi = max(highs[-8:])
        if prior_hi > 0 and recent_hi > prior_hi * 1.002 and last < prior_hi * 0.999:
            out.append(
                "Possible bull trap — probe above prior swing highs, close back below (failed breakout)"
            )
    except (ValueError, IndexError):
        pass
    try:
        prior_lo = min(lows[-30:-8])
        recent_lo = min(lows[-8:])
        if prior_lo > 0 and recent_lo < prior_lo * 0.998 and last > prior_lo * 1.001:
            out.append(
                "Possible bear trap — dip under prior swing low, reclaim (spring / false breakdown)"
            )
    except (ValueError, IndexError):
        pass
    third = max(n // 3, 8)
    if n >= third * 3:
        left_hi = max(highs[:third])
        right_hi = max(highs[-third:])
        rim = max(left_hi, right_hi)
        if rim > 0 and abs(left_hi - right_hi) / rim < 0.03:
            mid_lo = min(lows[third : n - third])
            if (rim - mid_lo) / rim > 0.035 and last >= rim * 0.965 and last <= rim * 1.025:
                out.append(
                    "Possible cup-and-handle-style base (heuristic) — rounded dip between similar rim highs"
                )
    if n >= 25 and trend == "uptrend":
        leg_hi = max(highs[-25:-10])
        leg_lo = min(lows[-25:-10])
        leg_range = leg_hi - leg_lo
        if leg_range > 0 and leg_lo > 0 and (leg_hi / leg_lo - 1) > 0.05:
            pullback_hi = max(highs[-10:])
            pullback_lo = min(lows[-10:])
            pb_range = pullback_hi - pullback_lo
            if pb_range < leg_range * 0.45 and last > pullback_lo:
                out.append("Possible bull flag — strong impulse leg, then shallow drift / pullback")
    if n >= 25 and trend == "downtrend":
        leg_hi = max(highs[-25:-10])
        leg_lo = min(lows[-25:-10])
        leg_range = leg_hi - leg_lo
        if leg_range > 0 and leg_lo > 0 and (leg_hi / leg_lo - 1) > 0.05:
            pullback_hi = max(highs[-10:])
            pullback_lo = min(lows[-10:])
            pb_range = pullback_hi - pullback_lo
            if pb_range < leg_range * 0.45 and last < pullback_hi:
                out.append("Possible bear flag — downside impulse, then shallow bounce")
    hi15 = highs[-15:]
    lo15 = lows[-15:]
    if len(hi15) >= 15 and len(lo15) >= 15:
        band_hi = max(hi15) - min(hi15)
        band_lo = max(lo15) - min(lo15)
        avg_hi = sum(hi15) / len(hi15)
        avg_lo = sum(lo15) / len(lo15)
        if avg_hi > 0 and band_hi < avg_hi * 0.012 and avg_lo > 0 and (avg_lo - min(lo15[:7]) > max(lo15[7:]) - avg_lo):
            if trend in ("uptrend", "range"):
                out.append("Ascending triangle bias (heuristic) — higher lows into tight resistance band")
        if avg_lo > 0 and band_lo < avg_lo * 0.012 and avg_hi > 0 and (max(hi15[:7]) - avg_hi > avg_hi - min(hi15[7:])):
            if trend in ("downtrend", "range"):
                out.append("Descending triangle bias (heuristic) — lower highs into tight support band")
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _price_action(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    sma50_series: list[Optional[float]],
) -> PriceAction:
    last = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else last
    change_pct = (last / prev - 1) * 100 if prev else 0.0
    period_start = closes[0]
    change_pct_period = (last / period_start - 1) * 100 if period_start else 0.0

    supports, resistances = support_resistance(highs, lows, last)

    # Trend detection: compare 20-bar close to 50-bar close and SMA50 slope.
    lookback_short = min(20, len(closes) - 1)
    mean_recent = sum(closes[-lookback_short:]) / lookback_short
    mean_older = sum(closes[-2 * lookback_short : -lookback_short]) / max(
        1, len(closes[-2 * lookback_short : -lookback_short])
    )
    sma50_now = sma50_series[-1]
    sma50_then = sma50_series[-lookback_short] if len(sma50_series) > lookback_short else sma50_now
    sma50_slope = (
        (sma50_now - sma50_then) / sma50_then if sma50_now and sma50_then else 0.0
    )

    if mean_recent > mean_older * 1.01 and sma50_slope > 0.005:
        trend = "uptrend"
    elif mean_recent < mean_older * 0.99 and sma50_slope < -0.005:
        trend = "downtrend"
    else:
        trend = "range"

    patterns: list[str] = []
    if trend == "uptrend":
        patterns.append("higher highs / higher lows")
    elif trend == "downtrend":
        patterns.append("lower highs / lower lows")
    if resistances and abs(last - resistances[0]) / last < 0.015:
        patterns.append(f"testing resistance at ${resistances[0]:.2f}")
    if supports and abs(last - supports[0]) / last < 0.015:
        patterns.append(f"holding support at ${supports[0]:.2f}")

    # Cheap double-top / double-bottom check.
    if len(resistances) >= 2 and abs(resistances[0] - resistances[1]) / resistances[0] < 0.01:
        patterns.append("potential double-top near "
                       f"${resistances[0]:.2f}")
    if len(supports) >= 2 and abs(supports[0] - supports[1]) / supports[0] < 0.01:
        patterns.append("potential double-bottom near "
                       f"${supports[0]:.2f}")

    patterns.extend(_swing_structure_patterns(closes, highs, lows, trend, last))
    seen_p: set[str] = set()
    deduped: list[str] = []
    for p in patterns:
        if p not in seen_p:
            seen_p.add(p)
            deduped.append(p)
    patterns = deduped

    return PriceAction(
        last=round(last, 4),
        change_pct=round(change_pct, 3),
        change_pct_period=round(change_pct_period, 2),
        supports=[round(x, 2) for x in supports],
        resistances=[round(x, 2) for x in resistances],
        trend=trend,
        patterns=patterns,
    )


def _volume_stats(volumes: list[float], is_index: bool = False) -> VolumeStats:
    if not volumes:
        return VolumeStats(latest=0, avg_20=0, ratio=0, unusual=False, trending_up=False)
    latest = volumes[-1]
    window = volumes[-20:] if len(volumes) >= 20 else volumes
    avg_20 = sum(window) / len(window)
    ratio = latest / avg_20 if avg_20 else 0.0
    recent5 = sum(volumes[-5:]) / min(5, len(volumes))
    trending_up = recent5 > avg_20 * 1.05
    unusual = ratio > 1.5 or ratio < 0.6
    # Indices (VIX, etc.) report zero volume — signals derived from volume are meaningless.
    if is_index or avg_20 < 1:
        return VolumeStats(latest=0, avg_20=0, ratio=1.0, unusual=False, trending_up=False)
    return VolumeStats(
        latest=round(latest, 0),
        avg_20=round(avg_20, 0),
        ratio=round(ratio, 2),
        unusual=unusual,
        trending_up=trending_up,
    )


def _sma_block(
    closes: list[float],
    sma50_series: list[Optional[float]],
    sma200_series: list[Optional[float]],
) -> IndicatorSMA:
    last = closes[-1]
    s50 = sma50_series[-1]
    s200 = sma200_series[-1]
    pct50 = ((last / s50 - 1) * 100) if s50 else None
    pct200 = ((last / s200 - 1) * 100) if s200 else None
    stacked_bull = bool(s50 and s200 and last > s50 > s200)
    stacked_bear = bool(s50 and s200 and last < s50 < s200)

    # Look for a cross in the trailing 20 bars.
    golden = _cross_recent(sma50_series, sma200_series, direction="up", lookback=20)
    death = _cross_recent(sma50_series, sma200_series, direction="down", lookback=20)

    stack = (
        "stacked bullish"
        if stacked_bull
        else "stacked bearish"
        if stacked_bear
        else "mixed"
    )

    return IndicatorSMA(
        sma50=round(s50, 2) if s50 else None,
        sma200=round(s200, 2) if s200 else None,
        price_vs_sma50_pct=round(pct50, 2) if pct50 is not None else None,
        price_vs_sma200_pct=round(pct200, 2) if pct200 is not None else None,
        stacked_bullish=stacked_bull,
        stacked_bearish=stacked_bear,
        golden_cross_recent=golden,
        death_cross_recent=death,
        stack=stack,
    )


def _cross_recent(
    fast: list[Optional[float]],
    slow: list[Optional[float]],
    direction: str,
    lookback: int,
) -> bool:
    n = len(fast)
    start = max(1, n - lookback)
    for i in range(start, n):
        f0, f1 = fast[i - 1], fast[i]
        s0, s1 = slow[i - 1], slow[i]
        if None in (f0, f1, s0, s1):
            continue
        if direction == "up" and f0 <= s0 and f1 > s1:
            return True
        if direction == "down" and f0 >= s0 and f1 < s1:
            return True
    return False


def _rsi_block(series: list[Optional[float]]) -> IndicatorRSI:
    val = series[-1]
    if val is None:
        return IndicatorRSI(value=None, state="unknown")
    if val >= 70:
        state = "overbought"
    elif val <= 30:
        state = "oversold"
    elif val >= 55:
        state = "bullish"
    elif val <= 45:
        state = "bearish"
    else:
        state = "neutral"
    return IndicatorRSI(value=round(val, 2), state=state)


def _macd_block(
    macd_line: list[Optional[float]],
    signal_line: list[Optional[float]],
    hist: list[Optional[float]],
) -> IndicatorMACD:
    m = macd_line[-1]
    s = signal_line[-1]
    h = hist[-1]
    bull_cross = _cross_recent(macd_line, signal_line, "up", lookback=5)
    bear_cross = _cross_recent(macd_line, signal_line, "down", lookback=5)

    direction = "unknown"
    recent_hist = [x for x in hist[-5:] if x is not None]
    if len(recent_hist) >= 3:
        first, last = recent_hist[0], recent_hist[-1]
        mid = recent_hist[len(recent_hist) // 2]
        if last > first + 1e-6:
            direction = "rising"
        elif last < first - 1e-6:
            direction = "falling"
        else:
            direction = "flat"
        # Deceleration: histogram moved in one direction then reversed.
        # e.g. positive but peak was earlier and now shrinking.
        if direction == "flat" or (direction == "rising" and mid > last):
            if all(v is not None and v > 0 for v in recent_hist):
                direction = "decelerating_bull"   # still positive, momentum fading
        elif direction == "flat" or (direction == "falling" and mid < last):
            if all(v is not None and v < 0 for v in recent_hist):
                direction = "decelerating_bear"   # still negative, selling losing steam

    state_parts: list[str] = [f"histogram {direction}"]
    if bull_cross:
        state_parts.append("bull cross")
    elif bear_cross:
        state_parts.append("bear cross")

    return IndicatorMACD(
        macd=round(m, 3) if m is not None else None,
        signal=round(s, 3) if s is not None else None,
        histogram=round(h, 3) if h is not None else None,
        bullish_cross_recent=bull_cross,
        bearish_cross_recent=bear_cross,
        histogram_direction=direction,
        state=" · ".join(state_parts),
    )


def _atr_regime(pct: float) -> str:
    if pct >= 6.0:
        return "very high vs spot"
    if pct >= 4.0:
        return "elevated volatility"
    if pct >= 2.0:
        return "moderate volatility"
    return "quiet vs spot"


def _atr_block(
    atr_series: list[Optional[float]], closes: list[float]
) -> IndicatorATR:
    v = atr_series[-1]
    if v is None:
        return IndicatorATR(value=None, pct_of_price=None, regime="unknown")
    pct = round(v / closes[-1] * 100, 2)
    return IndicatorATR(
        value=round(v, 3),
        pct_of_price=pct,
        regime=_atr_regime(float(pct)),
    )


StrengthT = Literal["unknown", "absent", "weak", "moderate", "strong"]


def _adx_trend_strength(val: Optional[float]) -> StrengthT:
    if val is None:
        return "unknown"
    if val < 20.0:
        return "absent"
    if val < 25.0:
        return "weak"
    if val < 40.0:
        return "moderate"
    return "strong"


def _adx_directional_bias(
    plus_di: Optional[float], minus_di: Optional[float]
) -> Literal["bullish", "bearish", "neutral", "unknown"]:
    if plus_di is None or minus_di is None:
        return "unknown"
    if plus_di > minus_di * 1.05:
        return "bullish"
    if minus_di > plus_di * 1.05:
        return "bearish"
    return "neutral"


def _adx_block(
    adx_series: list[Optional[float]],
    plus_di_series: list[Optional[float]],
    minus_di_series: list[Optional[float]],
) -> IndicatorADX:
    av = adx_series[-1] if adx_series else None
    pd = plus_di_series[-1] if plus_di_series else None
    md = minus_di_series[-1] if minus_di_series else None
    return IndicatorADX(
        value=round(av, 3) if av is not None else None,
        plus_di=round(pd, 3) if pd is not None else None,
        minus_di=round(md, 3) if md is not None else None,
        trend_strength=_adx_trend_strength(av),
        directional_bias=_adx_directional_bias(pd, md),
    )


BBPos = Literal[
    "above_upper",
    "near_upper",
    "mid",
    "near_lower",
    "below_lower",
    "unknown",
]


def _bb_position(pct_b: Optional[float]) -> BBPos:
    if pct_b is None:
        return "unknown"
    if pct_b > 1.0:
        return "above_upper"
    if pct_b < 0.0:
        return "below_lower"
    if pct_b >= 0.8:
        return "near_upper"
    if pct_b <= 0.2:
        return "near_lower"
    return "mid"


def _bollinger_block(
    closes: list[float],
    middle: list[Optional[float]],
    upper: list[Optional[float]],
    lower: list[Optional[float]],
) -> IndicatorBollinger:
    m = middle[-1] if middle else None
    u = upper[-1] if upper else None
    l = lower[-1] if lower else None
    lc = closes[-1]
    if m is None or u is None or l is None:
        return IndicatorBollinger(
            middle=None,
            upper=None,
            lower=None,
            bandwidth_pct=None,
            pct_b=None,
            position="unknown",
        )
    bw = u - l
    bandwidth_pct = round((bw / m) * 100.0, 3) if m else None
    pct_b: Optional[float]
    if bw > 1e-12:
        pct_b = round((lc - l) / bw, 4)
    else:
        pct_b = 0.5
    return IndicatorBollinger(
        middle=round(m, 4),
        upper=round(u, 4),
        lower=round(l, 4),
        bandwidth_pct=bandwidth_pct,
        pct_b=pct_b,
        position=_bb_position(pct_b),
    )


def _stochastic_block(
    k_series: list[Optional[float]],
    d_series: list[Optional[float]],
) -> IndicatorStochastic:
    k = k_series[-1] if k_series else None
    d = d_series[-1] if d_series else None
    if k is None or d is None:
        return IndicatorStochastic(
            pct_k=None,
            pct_d=None,
            state="unknown",
            bullish_cross_recent=False,
            bearish_cross_recent=False,
        )

    kv = round(float(k), 2)
    dv = round(float(d), 2)

    if kv >= 80:
        state = "overbought"
    elif kv <= 20:
        state = "oversold"
    elif kv >= 55 and kv > dv:
        state = "bullish"
    elif kv <= 45 and kv < dv:
        state = "bearish"
    else:
        state = "neutral"

    bull = _cross_recent(k_series, d_series, "up", lookback=5)
    bear = _cross_recent(k_series, d_series, "down", lookback=5)

    return IndicatorStochastic(
        pct_k=kv,
        pct_d=dv,
        state=state,
        bullish_cross_recent=bull,
        bearish_cross_recent=bear,
    )


# ---------------------------------------------------------------------------
# Composite score + verdict
# ---------------------------------------------------------------------------

_TF_HIGHER: dict[str, str] = {"1h": "4h", "4h": "daily", "daily": "weekly"}


def _compute_raw_score(
    pa: PriceAction,
    vs: VolumeStats,
    sb: IndicatorSMA,
    rsi_b: IndicatorRSI,
    macd_b: IndicatorMACD,
    adx_b: IndicatorADX,
    stoch_b: Optional[IndicatorStochastic] = None,
    bb_b: Optional[IndicatorBollinger] = None,
) -> float:
    """Pure scoring function — no side effects, no thresholds.

    Called both from the live pipeline (full args) and from the backtest tool
    (stoch_b / bb_b optional for backward compat).
    All Tier-1 enhancements (MTF, regime gate) are applied *after* this
    in ``build_report`` so that the backtest remains self-consistent.
    """
    score = 0.0

    # Trend (weight 0.35)
    if pa.trend == "uptrend":
        score += 0.35
    elif pa.trend == "downtrend":
        score -= 0.35

    # SMA stack (weight 0.20 + cross events)
    if sb.stacked_bullish:
        score += 0.20
    elif sb.stacked_bearish:
        score -= 0.20
    if sb.golden_cross_recent:
        score += 0.08
    if sb.death_cross_recent:
        score -= 0.08

    # RSI (weight 0.10) — contrarian tilt on extremes (strengthened: overbought is a
    # real mean-reversion risk, not a minor nuisance)
    if rsi_b.state == "bullish":
        score += 0.08
    elif rsi_b.state == "bearish":
        score -= 0.08
    elif rsi_b.state == "overbought":
        score -= 0.12   # raised from -0.05: chasing overbought momentum has poor EV
    elif rsi_b.state == "oversold":
        score += 0.05

    # MACD (weight 0.20) — RSI zero-line filter applied to crosses.
    # Research: MACD bull cross when RSI > 50 → 58% win rate;
    # same cross when RSI < 50 → 42% win rate (PF 1.72 vs 1.21).
    # Halve the bonus when RSI is on the wrong side of the midline.
    _rsi_above_mid = rsi_b.value is not None and rsi_b.value >= 50
    _rsi_below_mid = rsi_b.value is not None and rsi_b.value < 50
    if macd_b.bullish_cross_recent:
        score += 0.15 if _rsi_above_mid else 0.07
    if macd_b.bearish_cross_recent:
        score -= 0.15 if _rsi_below_mid else 0.07
    if macd_b.histogram_direction == "rising":
        score += 0.05
    elif macd_b.histogram_direction == "falling":
        score -= 0.05
    elif macd_b.histogram_direction == "decelerating_bull":
        # Histogram still positive but shrinking — early reversal warning
        score -= 0.02
    elif macd_b.histogram_direction == "decelerating_bear":
        # Histogram still negative but shrinking — early recovery signal
        score += 0.02

    # MACD line vs zero — confirms overall momentum direction independently
    # of the signal-line cross. Being above zero means net positive momentum
    # over the 12/26 EMA window; below zero = net negative.
    if macd_b.macd is not None:
        if macd_b.macd > 0:
            score += 0.03   # MACD above zero: net bullish momentum
        elif macd_b.macd < 0:
            score -= 0.03   # MACD below zero: net bearish momentum

    # Volume (weight 0.10)
    if vs.trending_up and pa.change_pct > 0:
        score += 0.06
    if vs.trending_up and pa.change_pct < 0:
        score -= 0.06
    if vs.unusual and vs.ratio > 1.5 and pa.change_pct > 0:
        score += 0.04
    if vs.unusual and vs.ratio > 1.5 and pa.change_pct < 0:
        score -= 0.04   # Fix: distribution day — high volume on down bar

    # ADX directional bias
    if adx_b.trend_strength in ("moderate", "strong"):
        if pa.trend == "uptrend" and adx_b.directional_bias == "bullish":
            score += 0.04
        elif pa.trend == "downtrend" and adx_b.directional_bias == "bearish":
            score += 0.04
        elif pa.trend == "uptrend" and adx_b.directional_bias == "bearish":
            score -= 0.03
        elif pa.trend == "downtrend" and adx_b.directional_bias == "bullish":
            score -= 0.03

    # Stochastic (weight 0.08) — secondary momentum confirmation
    # Computed but previously ignored in scoring. Now provides a small
    # confirmation/contrarian kicker consistent with the RSI treatment.
    if stoch_b is not None:
        if stoch_b.state == "overbought":
            score -= 0.05
        elif stoch_b.state == "oversold":
            score += 0.05
        if stoch_b.bullish_cross_recent:
            score += 0.03
        if stoch_b.bearish_cross_recent:
            score -= 0.03

    # Bollinger band position (weight 0.04) — extreme extension adds
    # mean-reversion pressure; not a strong signal on its own but
    # complements RSI at extremes.
    if bb_b is not None and bb_b.pct_b is not None:
        if bb_b.pct_b > 0.95:
            score -= 0.04   # at or above upper band
        elif bb_b.pct_b < 0.05:
            score += 0.04   # at or below lower band

    # Chart patterns (weight 0.06–0.08)
    # These are computed and displayed but previously had zero effect on score.
    pattern_str = " ".join(pa.patterns).lower()
    if "double-top" in pattern_str and score > 0:
        score -= 0.08   # reversal pattern at resistance — penalise bull
    if "double-bottom" in pattern_str and score < 0:
        score += 0.08   # reversal pattern at support — penalise bear
    if "testing resistance" in pattern_str and score > 0:
        score -= 0.04   # price at resistance = higher barrier for bulls
    if "holding support" in pattern_str and score < 0:
        score += 0.04   # price bouncing off support = lower barrier for bears

    return max(-1.0, min(1.0, score))


def _quick_score(sym: str, timeframe: Timeframe) -> Optional[float]:
    """Lightweight score for a symbol+timeframe (used for MTF confluence).

    Results are in-process cached for _MTF_CACHE_TTL seconds to avoid
    doubling the yfinance fan-out when the higher TF is also in the universe.
    Calls _compute_raw_score (not the full build_report) to avoid recursion.
    Includes Stochastic and Bollinger for consistency with the live pipeline.
    """
    key = f"{sym}|{timeframe}"
    now = time.time()
    cached = _mtf_cache.get(key)
    if cached and (now - cached[0]) < _MTF_CACHE_TTL:
        return cached[1]
    try:
        data = get_ohlcv(sym, timeframe)
        if len(data) < 60:
            return None
        closes, highs, lows, volumes = data.close, data.high, data.low, data.volume
        s50 = sma(closes, 50)
        s200 = sma(closes, 200) if len(closes) >= 200 else [None] * len(closes)
        pa = _price_action(closes, highs, lows, s50)
        vs = _volume_stats(volumes)
        sb = _sma_block(closes, s50, s200)
        rb = _rsi_block(_rsi(closes, 14))
        ml, sl_, hl = _macd(closes)
        mb = _macd_block(ml, sl_, hl)
        adx_s, pdi_s, mdi_s = _adx(highs, lows, closes, 14)
        ab = _adx_block(adx_s, pdi_s, mdi_s)
        bb_mid, bb_upper, bb_lower = _bollinger(closes, 20, 2.0)
        stoch_k, stoch_d = _stochastic(highs, lows, closes, 14, 3, 3)
        bbb = _bollinger_block(closes, bb_mid, bb_upper, bb_lower)
        stb = _stochastic_block(stoch_k, stoch_d)
        score = _compute_raw_score(pa, vs, sb, rb, mb, ab, stoch_b=stb, bb_b=bbb)
        _mtf_cache[key] = (now, score)
        return score
    except Exception:
        return None


def _apply_mtf(raw_score: float, sym: str, timeframe: Timeframe, cfg: dict) -> tuple[float, str]:
    """Multi-timeframe confluence: bonus if higher TF agrees, veto if it strongly disagrees.

    Returns (adjusted_score, human-readable note).
    """
    mtf_cfg = cfg.get("mtf", {})
    if not mtf_cfg.get("enabled", True):
        return raw_score, ""
    higher_tf = _TF_HIGHER.get(timeframe)
    if higher_tf is None:
        return raw_score, ""  # weekly has no higher TF
    htf_score = _quick_score(sym, higher_tf)  # type: ignore[arg-type]
    if htf_score is None:
        return raw_score, ""

    bonus = float(mtf_cfg.get("confluence_bonus", 0.06))
    penalty = float(mtf_cfg.get("conflict_penalty", 0.06))
    veto_t = float(mtf_cfg.get("strong_veto_threshold", 0.20))
    htf_dir = "↑" if htf_score > 0.05 else ("↓" if htf_score < -0.05 else "→")

    # Strong veto: higher TF is decisively opposite
    if raw_score > 0 and htf_score < -veto_t:
        adj = min(raw_score * 0.5, bullish_threshold() - 0.01)
        return adj, f"{higher_tf} strongly bearish ({htf_score:+.2f}) → bull vetoed"
    if raw_score < 0 and htf_score > veto_t:
        adj = max(raw_score * 0.5, bearish_threshold() + 0.01)
        return adj, f"{higher_tf} strongly bullish ({htf_score:+.2f}) → bear vetoed"

    # Confluence bonus: same direction
    if raw_score > 0 and htf_score > 0:
        return min(1.0, raw_score + bonus), f"{higher_tf} confirms {htf_dir} ({htf_score:+.2f})"
    if raw_score < 0 and htf_score < 0:
        return max(-1.0, raw_score - bonus), f"{higher_tf} confirms {htf_dir} ({htf_score:+.2f})"

    # Mild conflict: dampen
    if raw_score > 0 and htf_score <= 0:
        return raw_score - penalty, f"{higher_tf} neutral/bear {htf_dir} ({htf_score:+.2f}) dampens bull"
    if raw_score < 0 and htf_score >= 0:
        return raw_score + penalty, f"{higher_tf} neutral/bull {htf_dir} ({htf_score:+.2f}) dampens bear"

    return raw_score, ""


def _apply_triple_mean_reversion_gate(
    score: float,
    rsi_b: IndicatorRSI,
    stoch_b: IndicatorStochastic,
    bb_b: IndicatorBollinger,
) -> tuple[float, str]:
    """Hard cap when ALL THREE mean-reversion indicators are simultaneously extreme.

    Research: when RSI >70 + Stochastic >80 + Bollinger %B >0.90 fire together,
    multi-indicator systems show high-probability mean-reversion setups.
    A single bullish trend bias cannot override triple confirmation of overextension.

    Applies symmetrically to bearish extremes (oversold triple).
    """
    if score > 0:
        rsi_extreme = rsi_b.state == "overbought" and (rsi_b.value or 0) >= 70
        stoch_extreme = stoch_b.state == "overbought" and (stoch_b.pct_k or 0) >= 80
        bb_extreme = bb_b.pct_b is not None and bb_b.pct_b >= 0.90
        if rsi_extreme and stoch_extreme and bb_extreme:
            old = score
            adj = round(min(score * 0.30, 0.25), 3)  # push firmly below bull threshold
            return adj, (
                f"Triple mean-reversion extreme: RSI {rsi_b.value:.0f} + "
                f"Stoch {stoch_b.pct_k:.0f} + Bollinger %B {bb_b.pct_b:.2f} — "
                f"all three simultaneously overbought. Score dampened {old:+.2f}→{adj:+.2f}. "
                "This pattern has high reversal probability; do not chase."
            )
    elif score < 0:
        rsi_extreme = rsi_b.state == "oversold" and (rsi_b.value or 100) <= 30
        stoch_extreme = stoch_b.state == "oversold" and (stoch_b.pct_k or 100) <= 20
        bb_extreme = bb_b.pct_b is not None and bb_b.pct_b <= 0.10
        if rsi_extreme and stoch_extreme and bb_extreme:
            old = score
            adj = round(max(score * 0.30, -0.25), 3)
            return adj, (
                f"Triple mean-reversion extreme: RSI {rsi_b.value:.0f} + "
                f"Stoch {stoch_b.pct_k:.0f} + Bollinger %B {bb_b.pct_b:.2f} — "
                f"all three simultaneously oversold. Score lifted {old:+.2f}→{adj:+.2f}. "
                "Potential bounce; avoid new puts at this exhaustion level."
            )
    return score, ""


def _apply_mean_reversion_gate(
    score: float,
    rsi_b: IndicatorRSI,
    bb_block: IndicatorBollinger,
) -> tuple[float, str]:
    """Penalise a bull score when RSI AND Bollinger both signal overextension.

    A single overbought indicator is already handled in _compute_raw_score.
    When BOTH extremes fire simultaneously, the mean-reversion risk is much
    higher — e.g. a stock that just gapped up on earnings with RSI 75 and
    price at 92% of the Bollinger band.
    """
    if score <= 0:
        return score, ""

    rsi_extreme = rsi_b.state == "overbought" and (rsi_b.value or 0) >= 70
    bb_stretched = bb_block.pct_b is not None and bb_block.pct_b >= 0.85

    if rsi_extreme and bb_stretched:
        pct_b = bb_block.pct_b or 0
        rsi_v = rsi_b.value or 70
        strength = min(1.0, ((pct_b - 0.85) / 0.15) * 0.5 + ((rsi_v - 70) / 10) * 0.5)
        penalty = round(0.08 + strength * 0.10, 3)
        adj = round(max(-1.0, score - penalty), 3)
        return adj, (
            f"RSI {rsi_v:.0f} (overbought) + Bollinger %B {pct_b:.2f} "
            f"(stretched) → mean-reversion risk, bull dampened {penalty:.2f}"
        )
    return score, ""


def _apply_earnings_gate(
    score: float,
    sym: str,
    meta: dict,
) -> tuple[float, str]:
    """Suppress directional signals around earnings — the most reliable source of
    post-signal reversals.  Technical indicators built on pre-earnings bars have
    very low predictive power once the report is out.

    - days_until < 0 : earnings was very recently (within 2 days) → high suppression
    - days_until == 0 : earnings today → maximum suppression (force near-NEUTRAL)
    - days_until == 1 : earnings tomorrow → strong suppression
    - days_until <= 3 : upcoming → mild caution
    """
    from .earnings import earnings_within_window_days
    try:
        ew = earnings_within_window_days(sym, meta, window_days=7)
    except Exception:
        ew = None

    # Also check if earnings was in the PAST 2 days (yfinance may not list it going forward)
    from datetime import date, timedelta
    import json as _j
    from pathlib import Path as _p
    # Heuristic: if a recent earning was missed by the forward calendar, look at OHLCV for gaps
    if ew is None:
        return score, "", None

    days = ew[1]
    earnings_iso = ew[0]

    if days == 0:
        # Earnings today — technical trend is pre-report noise
        adj = round(score * 0.25, 3)  # dampen 75%
        return adj, f"⚠ Earnings TODAY ({earnings_iso}) — technicals are pre-report; signal unreliable", ew
    if days == 1:
        adj = round(score * 0.50, 3)  # dampen 50%
        return adj, f"⚠ Earnings TOMORROW ({earnings_iso}) — signal dampened 50%", ew
    if days <= 3:
        adj = round(score * 0.75, 3)  # dampen 25%
        return adj, f"Earnings in {days}d ({earnings_iso}) — signal dampened 25%", ew
    return score, "", ew


def _apply_regime_gate(score: float, vix: float, breadth: float, cfg: dict) -> tuple[float, str]:
    """Suppress signals in high-volatility or low-breadth regimes.

    Returns (adjusted_score, human-readable note).
    """
    reg = cfg.get("regime", {})
    if not reg.get("enabled", True):
        return score, ""

    vix_extreme = float(reg.get("vix_extreme_suppress_all", 35.0))
    vix_high = float(reg.get("vix_high_suppress_bull", 28.0))
    breadth_low = float(reg.get("breadth_low_suppress_bull", 35.0))

    note_parts: list[str] = []

    if vix >= vix_extreme:
        if abs(score) < 0.55:
            score = score * 0.40
            note_parts.append(f"VIX {vix:.0f} (extreme) → signal dampened 60%")
        else:
            note_parts.append(f"VIX {vix:.0f} (extreme, strong signal retained)")
    elif vix >= vix_high and score > 0:
        if score < 0.50:
            score = score * 0.60
            note_parts.append(f"VIX {vix:.0f} elevated → bull dampened 40%")
        else:
            note_parts.append(f"VIX {vix:.0f} elevated (strong bull retained)")

    if breadth < breadth_low and score > 0:
        if score < 0.45:
            score = score * 0.70
            note_parts.append(f"Breadth {breadth:.0f}% < {breadth_low:.0f}% → bull dampened 30%")
        else:
            note_parts.append(f"Breadth {breadth:.0f}% weak (strong bull retained)")

    return max(-1.0, min(1.0, score)), " · ".join(note_parts)


def _score_to_verdict(score: float, cfg: dict) -> tuple[str, float]:
    """Convert a composite score to (verdict, conviction) using config thresholds.

    Conviction is now proportional to the absolute score, capped at 0.85 to
    prevent the algorithm from ever claiming 100% certainty.  Previously the
    formula reached 1.0 at a score of only 0.60, which was dangerously
    overconfident for a borderline directional reading.
    """
    bull_tau = float(cfg["thresholds"]["BULLISH"]["min_score"])
    bear_tau = float(cfg["thresholds"]["BEARISH"]["max_score"])
    score = max(-1.0, min(1.0, score))
    if score >= bull_tau:
        verdict = "BULLISH"
    elif score <= bear_tau:
        verdict = "BEARISH"
    else:
        verdict = "NEUTRAL"
    # Proportional conviction: the composite score IS the signal strength (0→1).
    # Cap at 0.85 — a deterministic rule-based engine cannot be 100% certain.
    conviction = round(min(0.85, max(0.30, abs(score))), 3)
    return verdict, conviction


def _score_and_verdict(
    symbol: str,
    timeframe: Timeframe,
    pa: PriceAction,
    vs: VolumeStats,
    sb: IndicatorSMA,
    rsi_b: IndicatorRSI,
    macd_b: IndicatorMACD,
    adx_b: IndicatorADX,
) -> tuple[float, str, float, str]:
    """Used by the backtest tool and ETF/screener quick-paths.

    MTF and regime enhancements are NOT applied here (they require external
    network calls that would make the backtest loop impractically slow).
    The full live pipeline via ``build_report`` applies all enhancements.
    """
    score = _compute_raw_score(pa, vs, sb, rsi_b, macd_b, adx_b)
    cfg = load_signal_config()
    verdict, conviction = _score_to_verdict(score, cfg)
    headline = _headline(symbol, timeframe, verdict, pa, sb, macd_b, rsi_b, adx_b)
    return score, verdict, conviction, headline


def _headline(
    symbol: str,
    timeframe: Timeframe,
    verdict: str,
    pa: PriceAction,
    sb: IndicatorSMA,
    macd_b: IndicatorMACD,
    rsi_b: IndicatorRSI,
    adx_b: IndicatorADX,
) -> str:
    tf_word = {"1h": "intraday", "4h": "swing", "daily": "daily", "weekly": "weekly"}[timeframe]
    direction = {"BULLISH": "Bullish", "BEARISH": "Bearish", "NEUTRAL": "Range-bound"}[verdict]
    tags: list[str] = []
    if sb.stacked_bullish:
        tags.append("stacked 20/50/200 SMAs")
    elif sb.stacked_bearish:
        tags.append("inverted SMA stack")
    if macd_b.bullish_cross_recent:
        tags.append("fresh MACD bull cross")
    elif macd_b.bearish_cross_recent:
        tags.append("fresh MACD bear cross")
    if rsi_b.state in ("overbought", "oversold"):
        tags.append(f"RSI {rsi_b.state}")
    if adx_b.value is not None and adx_b.trend_strength in ("moderate", "strong"):
        tags.append(f"ADX {adx_b.value:.0f}")
    tag_s = f" — {', '.join(tags)}" if tags else ""
    return f"{symbol} {tf_word} view: {direction}{tag_s}."


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------


def _compose_narrative(
    *,
    meta: dict,
    timeframe: Timeframe,
    data: OHLCV,
    price_action: PriceAction,
    volume_stats: VolumeStats,
    sma_block: IndicatorSMA,
    rsi_block: IndicatorRSI,
    macd_block: IndicatorMACD,
    atr_block: IndicatorATR,
    adx_block: IndicatorADX,
    bollinger_block: IndicatorBollinger,
    stoch_block: IndicatorStochastic,
    verdict: str,
    conviction: float,
) -> str:
    s = meta["symbol"]
    lines: list[str] = []
    tf_human = {"1h": "1-hour", "4h": "4-hour", "daily": "daily", "weekly": "weekly"}[timeframe]

    # Paragraph 1 — setup + trend.
    p1 = (
        f"On the {tf_human} timeframe, {s} is trading at "
        f"${price_action.last:,.2f} ({price_action.change_pct:+.2f}% on the last bar, "
        f"{price_action.change_pct_period:+.2f}% across the displayed window). "
    )
    if price_action.trend == "uptrend":
        p1 += "Structure is constructive — higher highs and higher lows. "
    elif price_action.trend == "downtrend":
        p1 += "Structure is bearish — lower highs and lower lows. "
    else:
        p1 += "Price is ranging; no clear trend dominates. "
    if price_action.patterns:
        p1 += "Notable pattern cues: " + "; ".join(price_action.patterns) + ". "
    lines.append(p1)

    # Paragraph 2 — support/resistance.
    s_txt = (
        "Support: " + ", ".join(f"${x:.2f}" for x in price_action.supports)
        if price_action.supports else "Support: none identified in range"
    )
    r_txt = (
        "Resistance: " + ", ".join(f"${x:.2f}" for x in price_action.resistances)
        if price_action.resistances else "Resistance: none identified in range"
    )
    lines.append(f"{s_txt}. {r_txt}.")

    # Paragraph 3 — volume.
    vol_line = (
        f"Volume is {volume_stats.ratio:.2f}× the 20-bar average "
        f"({volume_stats.latest:,.0f} vs {volume_stats.avg_20:,.0f}). "
    )
    if volume_stats.unusual and volume_stats.ratio > 1.5 and price_action.change_pct > 0:
        vol_line += "Elevated volume is confirming the up-move."
    elif volume_stats.unusual and volume_stats.ratio > 1.5 and price_action.change_pct < 0:
        vol_line += "Elevated volume on down bars — distributive."
    elif volume_stats.trending_up:
        vol_line += "Volume is trending above average — participation improving."
    elif volume_stats.ratio < 0.6:
        vol_line += "Volume is thin — moves here are less reliable."
    else:
        vol_line += "Volume is unremarkable."
    lines.append(vol_line)

    # Paragraph 4 — SMAs.
    if sma_block.sma50 and sma_block.sma200:
        stack_txt = (
            "stacked bullishly (price > 50 > 200)" if sma_block.stacked_bullish
            else "inverted (price < 50 < 200)" if sma_block.stacked_bearish
            else "mixed"
        )
        cross_txt = (
            " A recent golden cross (50 crossing 200 from below) adds weight to the bull case."
            if sma_block.golden_cross_recent
            else " A recent death cross (50 crossing 200 from above) reinforces bearish bias."
            if sma_block.death_cross_recent
            else ""
        )
        lines.append(
            f"Moving averages are {stack_txt}: SMA50 ${sma_block.sma50:.2f} "
            f"({_fmt_pct(sma_block.price_vs_sma50_pct)} vs price), "
            f"SMA200 ${sma_block.sma200:.2f} "
            f"({_fmt_pct(sma_block.price_vs_sma200_pct)} vs price).{cross_txt}"
        )
    elif sma_block.sma50:
        lines.append(
            f"SMA50 is ${sma_block.sma50:.2f} ({_fmt_pct(sma_block.price_vs_sma50_pct)} vs price); "
            f"SMA200 lookback not yet available on this timeframe."
        )

    # Paragraph 5 — momentum (RSI + MACD).
    mo: list[str] = []
    if rsi_block.value is not None:
        mo.append(f"RSI(14) is {rsi_block.value:.1f} — {rsi_block.state}")
    if macd_block.macd is not None and macd_block.signal is not None:
        cross = (
            " with a fresh bullish cross"
            if macd_block.bullish_cross_recent
            else " with a fresh bearish cross"
            if macd_block.bearish_cross_recent
            else ""
        )
        mo.append(
            f"MACD {macd_block.macd:.3f} vs signal {macd_block.signal:.3f} "
            f"(histogram {macd_block.histogram_direction}{cross})"
        )
    if stoch_block.pct_k is not None and stoch_block.pct_d is not None:
        scross = ""
        if stoch_block.bullish_cross_recent:
            scross = ", fresh %K cross above %D"
        elif stoch_block.bearish_cross_recent:
            scross = ", fresh %K cross below %D"
        mo.append(
            f"Stochastic(14/3/3) %K/%D {stoch_block.pct_k:.1f}"
            f"/{stoch_block.pct_d:.1f} — {stoch_block.state}{scross}"
        )
    if mo:
        lines.append("Momentum: " + "; ".join(mo) + ".")

    # ADX — trend conviction (distinct from RSI momentum).
    if adx_block.value is not None:
        dpi = adx_block.plus_di
        dmi = adx_block.minus_di
        di_txt = ""
        if dpi is not None and dmi is not None:
            di_txt = f", +DI {dpi:.2f} vs −DI {dmi:.2f}"
        lines.append(
            f"ADX(14) reads {adx_block.value:.2f}{di_txt}"
            f" — trend strength is {adx_block.trend_strength.replace('_', ' ')}, "
            f"directional bias {adx_block.directional_bias} vs the last bar."
        )

    # Bollinger Bands — mean-reversion context vs band walk.
    if (
        bollinger_block.upper is not None
        and bollinger_block.lower is not None
        and bollinger_block.middle is not None
    ):
        pct_b_txt = ""
        if bollinger_block.pct_b is not None:
            pct_b_txt = f"Williams %B (position inside bands): {bollinger_block.pct_b:.2f}; "
        lines.append(
            f"Bollinger(20, 2σ) mid ${bollinger_block.middle:.2f}"
            f", band ${bollinger_block.lower:.2f}"
            f"–${bollinger_block.upper:.2f}"
            + (
                f" (bandwidth {bollinger_block.bandwidth_pct:.2f}% of mid)."
                if bollinger_block.bandwidth_pct is not None
                else "."
            )
            + f" {pct_b_txt}Price is labeled as {bollinger_block.position.replace('_', ' ')}."
        )

    # Paragraph — volatility / sizing (ATR).
    if atr_block.value is not None and atr_block.pct_of_price is not None:
        lines.append(
            f"ATR(14) is ${atr_block.value:.2f} ({atr_block.pct_of_price:.2f}% of spot) — "
            f"useful for stop placement and position sizing."
        )

    # Paragraph 7 — verdict.
    verdict_sentences = {
        "BULLISH": (
            f"Net view: BULLISH on the {tf_human} timeframe, conviction "
            f"{int(conviction*100)}%. Buyers remain in control and the indicator "
            "stack is confirming. Recommended expression: a clean long call, "
            "strike and expiry in the ticket below."
        ),
        "BEARISH": (
            f"Net view: BEARISH on the {tf_human} timeframe, conviction "
            f"{int(conviction*100)}%. Sellers are in control. Recommended "
            "expression: a clean long put, strike and expiry in the ticket below."
        ),
        "NEUTRAL": (
            f"Net view: NEUTRAL on the {tf_human} timeframe, conviction "
            f"{int(conviction*100)}%. Signals are mixed — the recommended "
            "directional call/put below is a lower-conviction play sized "
            "accordingly (wait for cleaner structure before adding size)."
        ),
    }
    lines.append(verdict_sentences[verdict])

    return "\n\n".join(lines)


def _fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.2f}%"


# ---------------------------------------------------------------------------
# Market / sector context
# ---------------------------------------------------------------------------


def _market_context_text(symbol: str, sector: str, source: str) -> str:
    src_note = (
        "Context below is a static characterization; the live VIX, breadth, "
        "and sector ETFs would be injected by the data layer in production."
        if source == "synthetic"
        else "Broad-market reads should be sourced from the ^VIX, ^SPX, and "
        "sector ETFs in live mode; the text below is a static characterization."
    )
    sector_map = {
        "Semiconductors / AI": (
            "AI-infrastructure demand is the dominant narrative. Semis led the broader "
            "tape higher on the 2024-2025 run; every pullback gets bid while capex "
            "revisions hold."
        ),
        "Software / AI": (
            "Software is bifurcated — AI-first names extend while legacy SaaS compresses. "
            "Earnings revisions and stock-based-comp dilution are the key screens."
        ),
        "Software / Cloud": (
            "Cloud growth has re-accelerated. Hyperscaler capex is flowing into AI "
            "infra; watch Azure/AWS/GCP growth prints."
        ),
        "Communication Services": (
            "Ad-sensitive; follow consumer spend and AI-monetization commentary. "
            "Mega-caps command most of the sector weight."
        ),
        "Autos / EV": (
            "EV demand softened through 2024; margins remain the story. China "
            "competition and Model refresh cycles drive sentiment spikes."
        ),
        "Consumer Electronics": (
            "iPhone unit cycle dominates. Services margins are the quiet tailwind. "
            "China exposure is the recurring risk."
        ),
        "Consumer / Cloud": (
            "Retail plus AWS — margin mix matters more than GMV. Advertising is "
            "the third leg."
        ),
        "Cybersecurity": (
            "Platform consolidation favors incumbents. Ongoing breach cadence "
            "keeps secular budget growth intact."
        ),
        "Index": (
            "SPY is the broad-market reference. Watch VIX, 10-year yields, "
            "and breadth (% of constituents above 50d) for regime cues."
        ),
        "Nuclear / Energy": (
            "Nuclear / SMR has re-rated on AI-driven power demand. Thinly "
            "traded names carry high IV — size small and prefer defined-risk."
        ),
        "Quantum": (
            "Highly speculative and headline-driven. IV is elevated; expect "
            "fast, wide moves. Treat as venture-style position sizing."
        ),
    }
    sector_text = sector_map.get(sector, "No specific sector note.")
    broad = (
        "Broad market: SPY trend remains the dominant gravity. A weakening SPY "
        "plus rising VIX compounds downside in single names; a firm SPY plus "
        "falling VIX accelerates upside in leadership pockets."
    )
    return f"{broad} {sector_text} {src_note}"


# ---------------------------------------------------------------------------
# Options suggestion — clean directional calls / puts only
# ---------------------------------------------------------------------------


def _build_options_suggestion(
    *,
    sym: str,
    spot: float,
    closes: list[float],
    atr_block: IndicatorATR,
    price_action: PriceAction,
    verdict: str,
    conviction: float,
    composite_score: float,
    timeframe: Timeframe,
    allow_directional_trade: bool = True,
    fresh_quotes: bool = False,
) -> OptionsSuggestion:
    """Build a single directional ticket.

    Per product spec, multi-leg structures (spreads / straddles / condors)
    are intentionally excluded — every ticker gets a clean long call if the
    composite score is non-negative, or a long put if negative. Strike is
    chosen to be slightly OTM, nudged to the nearest structural level when
    one sits within the expected move.
    """
    rv = _annualized_vol(closes)
    atr_annual = (atr_block.pct_of_price or 0) / 100 * math.sqrt(252)
    iv_est = max(0.12, (rv + atr_annual) / 2) if atr_annual else max(0.15, rv)
    # Clamp IV to a plausible ceiling scaled by ticker default. VIX-style
    # extremes are still possible (default IV 1.10) but not arbitrary.
    iv_est = min(iv_est, DEFAULT_IV.get(sym, 0.40) * 1.6)
    iv_est = min(iv_est, 1.50)  # hard ceiling — 150% annualized

    _dte_map = {"1h": 10, "4h": 21, "daily": 35, "weekly": 60}
    dte_default = _dte_map[timeframe]
    # High-IV names: shorten DTE to reduce theta cost and avoid paying
    # for volatility that is unlikely to be realised directionally.
    # iv_est > 0.80 (80% annualized) → cut DTE by 14 days; > 1.00 → cut by 21 days.
    if iv_est > 1.00:
        dte_default = max(_dte_map["1h"], dte_default - 21)
    elif iv_est > 0.80:
        dte_default = max(_dte_map["1h"], dte_default - 14)

    contract_type: str = "call" if composite_score >= 0 else "put"
    actionable_directional = allow_directional_trade and verdict != "NEUTRAL" and conviction >= 0.40

    strike = _pick_strike(
        contract_type=contract_type,
        spot=spot,
        iv=iv_est,
        dte=dte_default,
        conviction=conviction,
        price_action=price_action,
    )

    if actionable_directional:
        headline = (
            f"BUY {contract_type.upper()} — "
            f"${_fmt_strike(strike)} strike, ~{dte_default} DTE"
        )
    else:
        headline = "NO TRADE — wait for stronger directional confirmation"

    trade_plan = _build_trade_plan(
        sym=sym,
        spot=spot,
        iv=iv_est,
        dte=dte_default,
        contract_type=contract_type,
        strike=strike,
        atr_block=atr_block,
        price_action=price_action,
        headline=headline,
        conviction=conviction,
        composite_score=composite_score,
        verdict=verdict,
        fresh_quotes=fresh_quotes,
    )

    if actionable_directional:
        direction_word = "bullish" if contract_type == "call" else "bearish"
        rationale = (
            f"Clean directional {contract_type} aligned with the {direction_word} "
            f"read (composite {composite_score:+.2f}, verdict {verdict}, "
            f"conviction {int(conviction*100)}%). IV estimate "
            f"{int(iv_est*100)}% over {dte_default}D."
        )
    else:
        rationale = (
            f"Directional option entry is suppressed for now (verdict {verdict}, "
            f"conviction {int(conviction*100)}%). Wait for a stronger setup before "
            "opening a directional call/put."
        )

    return OptionsSuggestion(
        headline=headline,
        contract_type=contract_type,  # type: ignore[arg-type]
        strike=strike,
        expiry_dte=dte_default,
        rationale=rationale,
        trade_plan=trade_plan,
        recommendations=[],
    )


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------


def _pick_strike(
    *,
    contract_type: str,
    spot: float,
    iv: float,
    dte: int,
    conviction: float,
    price_action: PriceAction,
) -> float:
    """Pick a sensible single-leg strike — slightly OTM, snap to structural.

    - Base OTM fraction scales with conviction: low-conviction trades start
      near-ATM, high-conviction moves ~3% OTM for more leverage.
    - If a structural level (resistance for calls, support for puts) sits
      within ~0.8 × expected-move and within ~6% of spot, we bias the strike
      there so the option is priced against a real technical magnet.
    """
    T = max(1, dte) / 365.0
    one_sigma = spot * iv * math.sqrt(T)

    conv = max(0.0, min(1.0, conviction))
    # 0.25 conviction → 0.5% OTM, 1.0 conviction → 3% OTM.
    otm_frac = 0.005 + max(0.0, (conv - 0.25)) / 0.75 * 0.025

    if contract_type == "call":
        base = spot * (1 + otm_frac)
        upper_limit = min(spot + 0.8 * one_sigma, spot * 1.06)
        for r in price_action.resistances:
            if spot * 1.002 < r <= upper_limit:
                base = r
                break
    else:  # put
        base = spot * (1 - otm_frac)
        lower_limit = max(spot - 0.8 * one_sigma, spot * 0.94)
        for s in price_action.supports:
            if lower_limit <= s < spot * 0.998:
                base = s
                break

    return _round_strike(base)


def _round_strike(price: float) -> float:
    """Round to a standard-listed-strike increment."""
    if price >= 100:
        return float(round(price))          # $1 strikes
    if price >= 25:
        return float(round(price))          # $1 strikes
    if price >= 5:
        return round(price * 2) / 2         # $0.50 strikes
    return round(price * 4) / 4             # $0.25 strikes


def _fmt_strike(k: float) -> str:
    """Display helper that drops decimals when they aren't useful."""
    if k >= 100 or abs(k - round(k)) < 1e-6:
        return f"{k:.0f}"
    return f"{k:.2f}"


def _stock_swing_calendar_days(timeframe: Timeframe) -> int:
    """Calendar days for swing expected-move sizing (shorter than options DTE)."""
    return {"1h": 5, "4h": 8, "daily": 12, "weekly": 15}[timeframe]


def _hold_horizon_label(timeframe: Timeframe) -> str:
    return {
        "1h": "Hours to a few sessions (intraday chart)",
        "4h": "Several sessions to about one week",
        "daily": "Several days to a couple of weeks (swing trade)",
        "weekly": "Multi-week swing — still tactical, not long-term allocation",
    }[timeframe]


def _underlying_target_stop_rr(
    *,
    spot: float,
    atr_block: IndicatorATR,
    price_action: PriceAction,
    direction_long: bool,
    one_sigma_usd: float,
) -> tuple[float, float, Optional[float]]:
    max_target_distance = 1.5 * one_sigma_usd
    if direction_long:
        above = [
            lvl for lvl in price_action.resistances
            if spot * 1.005 < lvl <= spot + max_target_distance
        ]
        target_price = above[0] if above else round(spot + one_sigma_usd, 2)
    else:
        below = [
            lvl for lvl in price_action.supports
            if spot - max_target_distance <= lvl < spot * 0.995
        ]
        target_price = below[0] if below else round(spot - one_sigma_usd, 2)
    atr_val = atr_block.value or (spot * 0.02)
    min_stop_distance = max(atr_val, spot * 0.015)
    if direction_long:
        atr_stop = spot - 2 * atr_val
        structural = max(price_action.supports) if price_action.supports else None
        raw_stop = max(atr_stop, structural) if structural is not None else atr_stop
        stop_loss = round(min(raw_stop, spot - min_stop_distance), 2)
    else:
        atr_stop = spot + 2 * atr_val
        structural = min(price_action.resistances) if price_action.resistances else None
        raw_stop = min(atr_stop, structural) if structural is not None else atr_stop
        stop_loss = round(max(raw_stop, spot + min_stop_distance), 2)
    risk_reward: Optional[float] = None
    if direction_long and spot > stop_loss and target_price > spot:
        risk_reward = round((target_price - spot) / (spot - stop_loss), 2)
    elif not direction_long and spot < stop_loss and target_price < spot:
        risk_reward = round((spot - target_price) / (stop_loss - spot), 2)
    return round(target_price, 2), stop_loss, risk_reward


def _derive_equity_signal_warnings(
    signal_warnings: list[str],
    *,
    earnings_soon: Optional[EarningsSoonOut],
    stock_swing_days: int,
    flow_out: Optional[OptionsFlowOut],
) -> list[str]:
    """Stock-tab warnings: drop options-only jargon; localize shared risks for cash equity."""
    out: list[str] = []
    skip_substrings = (
        "Weekend theta:",
        "Very short DTE:",
        "expires BEFORE earnings",
        "Option expires",
        "within your option's",
        "Low open interest at",
        "hard to fill at mid-price",
        "Wide bid-ask spread at $",
        "Premium $",
        "/contract is ",
        "long option positions",
        "debit spread",
        "Use spreads to reduce vega",
    )
    for w in signal_warnings:
        if any(s in w for s in skip_substrings):
            continue
        if "IV crush risk:" in w or ("backwardation" in w and "Long option premium" in w):
            _term = flow_out.term_slope if flow_out else None
            if earnings_soon and _term is not None and _term > 1.08:
                out.append(
                    f"⚠ Event risk: volatility term structure is stressed (~{_term:.2f}×) "
                    f"and earnings are near. Stocks can gap through stops around the print — "
                    "size accordingly."
                )
            elif _term is not None and _term > 1.08:
                out.append(
                    f"Near-term event risk: IV term slope {_term:.2f}× (backwardation). "
                    "Cash equities can still gap sharply when the catalyst resolves — expect "
                    "possible slippage versus your stop level."
                )
            continue
        if "Term structure in backwardation" in w and "Long option premium" in w:
            _term = flow_out.term_slope if flow_out else None
            if _term is not None:
                out.append(
                    f"Term structure in backwardation ({_term:.2f}×): near-term uncertainty is elevated. "
                    "Stock stops are not guaranteed fill prices if the name gaps on news."
                )
            continue
        w2 = w.replace(
            "Wait for a confirmed directional breakout before committing new risk.",
            "Wait for a confirmed directional breakout before adding stock.",
        )
        w2 = w2.replace(
            "Research defines Unusual Options Activity as 5x+ normal volume — this level ",
            "Unusual equity volume (5×+ normal) often means ",
        )
        w2 = w2.replace("Options bid-ask spreads are likely very wide.", "Use limit orders; spreads may be wide.")
        w2 = w2.replace("entering a directional stock trade.", "adding stock.")
        w2 = w2.replace("before committing a new stock position", "before adding stock exposure")
        out.append(w2)

    # Dedupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)

    if (
        earnings_soon is not None
        and earnings_soon.days_until > 0
        and earnings_soon.days_until <= stock_swing_days
    ):
        uniq.append(
            f"Earnings in {earnings_soon.days_until}d falls inside this ~{stock_swing_days}d swing window — "
            "the tape can reset overnight. Consider smaller size or waiting until after the report."
        )
    if stock_swing_days <= 15:
        dow = datetime.now().weekday()
        if dow in (3, 4):
            dayn = "Thursday" if dow == 3 else "Friday"
            uniq.append(
                f"{dayn} entry: markets are closed Sat–Sun — headline and gap risk can move the stock "
                "away from your levels. Stops do not guarantee execution prices."
            )
    if flow_out is not None:
        if flow_out.iv_baseline_ratio is not None and flow_out.iv_baseline_ratio >= 1.45:
            uniq.append(
                f"Elevated volatility regime: ATM IV is ~{flow_out.iv_baseline_ratio:.2f}× baseline. "
                "Price jumps and slippage risk are higher than usual; use smaller size."
            )
        if flow_out.implied_move_30d_pct is not None and flow_out.implied_move_30d_pct >= 12.0:
            uniq.append(
                f"High implied move context: options market prices ~±{flow_out.implied_move_30d_pct:.1f}% "
                "30D move, signaling a choppy market with bigger overnight move risk."
            )
    return uniq


def _build_stock_strategy(
    *,
    sym: str,
    spot: float,
    closes: list[float],
    atr_block: IndicatorATR,
    price_action: PriceAction,
    verdict: str,
    conviction: float,
    composite_score: float,
    timeframe: Timeframe,
    rsi_block: IndicatorRSI,
    direction_summary: str = "",
    direction_bullets: Optional[list[str]] = None,
) -> StockStrategyOut:
    """Verdict-driven swing stock plan (not options contract selection)."""
    dir_bullets = list(direction_bullets) if direction_bullets is not None else []
    rv = _annualized_vol(closes)
    atr_annual = (atr_block.pct_of_price or 0) / 100 * math.sqrt(252) if atr_block.pct_of_price else 0.0
    iv_est = max(0.12, (rv + atr_annual) / 2) if atr_annual else max(0.15, rv)
    iv_est = min(iv_est, DEFAULT_IV.get(sym, 0.40) * 1.6)
    iv_est = min(iv_est, 1.50)
    swing_d = _stock_swing_calendar_days(timeframe)
    T = max(1, swing_d) / 365.0
    one_sigma_usd = round(spot * iv_est * math.sqrt(T), 2)
    hold_h = _hold_horizon_label(timeframe)
    rsi_v = rsi_block.value

    if verdict == "NEUTRAL":
        sup = min(price_action.supports) if price_action.supports else None
        res = max(price_action.resistances) if price_action.resistances else None
        parts = []
        if sup is not None:
            parts.append(f"support near ${sup:.2f}")
        if res is not None:
            parts.append(f"resistance near ${res:.2f}")
        range_note = " · ".join(parts) if parts else "Wait for a directional breakout with volume."
        return StockStrategyOut(
            action="hold_wait",
            action_display="WAIT",
            headline="No directional edge — stand aside or wait for breakout",
            entry=StockEntryOut(
                mode="market",
                price=round(spot, 2),
                note="Reference only; do not chase without a fresh setup.",
            ),
            take_profit=None,
            stop_loss=None,
            risk_reward=None,
            buy_price=None,
            short_entry_price=None,
            sell_take_profit_price=None,
            sell_stop_price=None,
            chart_patterns=list(price_action.patterns),
            direction_summary=direction_summary,
            direction_bullets=dir_bullets,
            hold_horizon=hold_h,
            rationale=(
                f"Composite {composite_score:+.2f} is NEUTRAL — no swing edge. "
                f"Range: {range_note}"
            ),
            range_note=range_note,
        )

    direction_long = verdict == "BULLISH"
    tgt, stp, rr = _underlying_target_stop_rr(
        spot=spot,
        atr_block=atr_block,
        price_action=price_action,
        direction_long=direction_long,
        one_sigma_usd=one_sigma_usd,
    )

    if direction_long:
        action = "buy"
        limits = [s for s in price_action.supports if s < spot * 0.998 and s > stp]
        limit_px = max(limits) if limits else round(spot, 2)
        use_limit = limit_px < spot * 0.995
        entry = StockEntryOut(
            mode="limit" if use_limit else "market",
            price=round(limit_px, 2),
            note=(
                f"Consider limit near ${limit_px:.2f} on a pullback; else reference ~${spot:.2f}."
                if use_limit
                else f"Stock near ${spot:.2f} — tactical swing long toward ${tgt:.2f}."
            ),
        )
        headline = f"Swing long — book profits near ${tgt:.2f}, stop ${stp:.2f}"
        rationale = (
            f"Bullish swing: take-profit near ${tgt:.2f}; stop near ${stp:.2f} to protect capital."
        )
        if rsi_v is not None:
            rationale += f" Thesis weakens on a close below ${stp:.2f} or if RSI loses 50."
    else:
        action = "sell_short"
        limits = [r for r in price_action.resistances if r > spot * 1.002 and r < stp]
        limit_px = min(limits) if limits else round(spot, 2)
        use_limit = limit_px > spot * 1.005
        entry = StockEntryOut(
            mode="limit" if use_limit else "market",
            price=round(limit_px, 2),
            note=(
                f"Consider limit near ${limit_px:.2f} on a relief rally; else reference ~${spot:.2f}."
                if use_limit
                else f"Stock near ${spot:.2f} — tactical short / trim / inverse-ETF swing toward ${tgt:.2f}."
            ),
        )
        headline = f"Swing bearish — target ${tgt:.2f}, stop ${stp:.2f} (short / hedge / inverse ETF)"
        rationale = (
            f"Bearish swing: target lower near ${tgt:.2f}; stop near ${stp:.2f} if the breakdown fails."
        )
        if rsi_v is not None:
            rationale += f" Thesis weakens on a close above ${stp:.2f} or if RSI regains 50."

    rationale += f" Conviction {int(conviction * 100)}% (score {composite_score:+.2f})."
    ch_pat = list(price_action.patterns)
    if direction_long:
        return StockStrategyOut(
            action=action,
            action_display="BUY",
            headline=headline,
            entry=entry,
            take_profit=tgt,
            stop_loss=stp,
            risk_reward=rr,
            buy_price=round(entry.price, 2),
            short_entry_price=None,
            sell_take_profit_price=tgt,
            sell_stop_price=stp,
            chart_patterns=ch_pat,
            direction_summary=direction_summary,
            direction_bullets=dir_bullets,
            hold_horizon=hold_h,
            rationale=rationale,
        )
    return StockStrategyOut(
        action=action,
        action_display="SHORT",
        headline=headline,
        entry=entry,
        take_profit=tgt,
        stop_loss=stp,
        risk_reward=rr,
        buy_price=None,
        short_entry_price=round(entry.price, 2),
        sell_take_profit_price=tgt,
        sell_stop_price=stp,
        chart_patterns=ch_pat,
        direction_summary=direction_summary,
        direction_bullets=dir_bullets,
        hold_horizon=hold_h,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# TradePlan — the concrete "buy this" trade ticket
# ---------------------------------------------------------------------------


def _build_trade_plan(
    *,
    sym: str,
    spot: float,
    iv: float,
    dte: int,
    contract_type: str,
    strike: float,
    atr_block: IndicatorATR,
    price_action: PriceAction,
    headline: str,
    conviction: float,
    composite_score: float,
    verdict: str,
    fresh_quotes: bool = False,
) -> TradePlan:
    T = max(1, dte) / 365.0
    one_sigma_usd = round(spot * iv * math.sqrt(T), 2)
    one_sigma_pct = round(iv * math.sqrt(T) * 100, 2)

    # Absolute expiry date — roll forward to the next Friday after ``dte`` days.
    expiry_dt = date.today() + timedelta(days=dte)
    while expiry_dt.weekday() != 4:  # 4 == Friday
        expiry_dt += timedelta(days=1)
    expiry_date_iso = expiry_dt.isoformat()

    # Premium: Yahoo option chain (bid+ask)/2 when a listed expiry/strike exists,
    # else Black–Scholes so the ticket still renders offline.
    premium: Optional[float] = None
    premium_source = "model"
    chain_mid = yf_option_mid_per_share(
        sym,
        strike,
        contract_type == "call",
        expiry_dt,
        bypass_cache=fresh_quotes,
    )
    if chain_mid is not None and chain_mid > 0:
        premium = round(chain_mid, 2)
        premium_source = "yahoo_chain"
    if premium is None:
        try:
            premium = round(
                black_scholes_price(
                    S=spot, K=strike, T=T, r=0.045, sigma=iv, q=0.0,
                    option_type=contract_type,
                ),
                2,
            )
            premium_source = "model"
        except Exception:
            premium = None
    cost = round(premium * 100, 2) if premium is not None else None
    break_even: Optional[float] = None
    if premium is not None:
        break_even = round(
            strike + premium if contract_type == "call" else strike - premium,
            2,
        )

    # ---- Target / stop / R:R on underlying (shared with stock_strategy) ----
    target_price, stop_loss, risk_reward = _underlying_target_stop_rr(
        spot=spot,
        atr_block=atr_block,
        price_action=price_action,
        direction_long=(contract_type == "call"),
        one_sigma_usd=one_sigma_usd,
    )

    # ---- Fetch Live Greeks & Liquidity --------------------------------------
    from .options_flow import option_liquidity_at_strike
    liq = option_liquidity_at_strike(sym, strike, contract_type == "call")
    
    # ---- Black-Scholes Greeks (analytical fallback) -------------------------
    delta_val: Optional[float] = liq.get("delta") if liq else None
    theta_day: Optional[float] = liq.get("theta") if liq else None
    vega_pct: Optional[float] = liq.get("vega") if liq else None
    
    if delta_val is None:
        try:
            from square18_signals.pricing import greeks as _greeks
            _g = _greeks(
                S=spot, K=float(strike), T=T, r=0.045, sigma=iv,
                option_type=contract_type, q=0.0,
            )
            delta_val = round(_g.delta, 3)
            theta_day = round(_g.theta_per_day, 4)   # $ per share per calendar day
            vega_pct = round(_g.vega_per_pct, 4)     # $ per share per +1% IV
        except Exception:
            pass

    # ---- Rationale ---------------------------------------------------------
    if premium is not None:
        _src = "Yahoo chain mid" if premium_source == "yahoo_chain" else "BS model"
        prem_txt = f"${premium:.2f}/sh ({_src}; ${cost:.0f}/contract)"
    else:
        prem_txt = "premium pending"
    be_txt = f"BE ${break_even:.2f}" if break_even is not None else "BE pending"
    tgt_txt = f"target ${target_price:.2f}" if target_price is not None else "target 1σ"
    stop_txt = f"invalidate ${stop_loss:.2f}" if stop_loss is not None else "2×ATR stop"
    rr_txt = f" · RR {risk_reward:.2f}×" if risk_reward else ""

    special_note = _special_ticker_note(sym)
    # Signal validity window: tell the user when the thesis is invalidated
    if contract_type == "call":
        validity_note = (
            f" Signal valid while price stays above ${stop_loss:.2f} "
            "and RSI remains above 50. Exit if either is breached."
        )
    else:
        validity_note = (
            f" Signal valid while price stays below ${stop_loss:.2f} "
            "and RSI remains below 50. Exit if either is breached."
        )
    rationale = (
        f"{headline}. Expected move over {dte}D ±${one_sigma_usd:.2f} "
        f"({one_sigma_pct:.2f}%). Est. premium {prem_txt}, {be_txt}. "
        f"Underlying {tgt_txt}, {stop_txt}{rr_txt}. "
        f"Score {composite_score:+.2f}, verdict {verdict}, conviction "
        f"{int(conviction*100)}%.{validity_note}{special_note}"
    )

    # ---- P&L scenario estimates (BS repricing at key price levels) ----------
    scenario_target: Optional[float] = None
    scenario_flat14: Optional[float] = None
    scenario_stop: Optional[float] = None
    if premium is not None and target_price is not None and stop_loss is not None:
        try:
            from square18_signals.pricing import black_scholes_price as _bsp
            T_target = max(1 / 365.0, T - T / 3.0)      # assume target in 1/3 of DTE
            T_flat = max(1 / 365.0, T - 14 / 365.0)     # 14 days elapsed, flat
            T_stop = max(1 / 365.0, T - T / 6.0)        # assume stop hit quickly
            p_at_tgt = _bsp(S=target_price, K=float(strike), T=T_target, r=0.045,
                            sigma=iv, option_type=contract_type, q=0.0)
            p_flat = _bsp(S=spot, K=float(strike), T=T_flat, r=0.045,
                          sigma=iv, option_type=contract_type, q=0.0)
            p_at_stop = _bsp(S=stop_loss, K=float(strike), T=T_stop, r=0.045,
                             sigma=iv, option_type=contract_type, q=0.0)
            scenario_target = round((p_at_tgt - premium) * 100, 0)
            scenario_flat14 = round((p_flat - premium) * 100, 0)
            scenario_stop = round((p_at_stop - premium) * 100, 0)
        except Exception:
            pass

    return TradePlan(
        contract_type=contract_type,  # type: ignore[arg-type]
        strike=round(strike, 2),
        expiry_date=expiry_date_iso,
        expiry_dte=dte,
        estimated_premium=premium,
        cost_per_contract=cost,
        spot_at_entry=round(spot, 2),
        break_even=break_even,
        target_price=round(target_price, 2) if target_price is not None else None,
        stop_loss=stop_loss,
        one_sigma_move_usd=one_sigma_usd,
        one_sigma_move_pct=one_sigma_pct,
        risk_reward=risk_reward,
        delta=delta_val,
        theta_per_day=theta_day,
        vega_per_pct=vega_pct,
        scenario_at_target=scenario_target,
        scenario_flat_14d=scenario_flat14,
        scenario_at_stop=scenario_stop,
        rationale=rationale,
    )


def _special_ticker_note(sym: str) -> str:
    """Caveats for instruments that don't behave like ordinary equities."""
    if sym == "VIX":
        return (
            " Note: VIX options are cash-settled on /VX futures (not spot), "
            "so retail pricing/Greeks differ from this BS estimate — treat "
            "the ticket as directional guidance, not an executable quote."
        )
    if sym in ("SPY", "QQQ"):
        return " SPY/QQQ are deeply liquid; tighter spreads, assume near-mid fills."
    return ""


# ---------------------------------------------------------------------------
# Vol helpers
# ---------------------------------------------------------------------------


def _annualized_vol(closes: list[float]) -> float:
    if len(closes) < 30:
        return 0.25
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    tail = rets[-60:]
    mean = sum(tail) / len(tail)
    var = sum((x - mean) ** 2 for x in tail) / (len(tail) - 1)
    return math.sqrt(var) * math.sqrt(252)


def _rolling_annualized_vol(closes: list[float], window: int) -> list[float]:
    if len(closes) < window + 2:
        return []
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    out: list[float] = []
    for i in range(window, len(rets)):
        w = rets[i - window : i]
        mean = sum(w) / window
        var = sum((x - mean) ** 2 for x in w) / (window - 1)
        out.append(math.sqrt(var) * math.sqrt(252))
    return out
