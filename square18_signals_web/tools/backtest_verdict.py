"""Walk-forward reliability check for the Analyst verdict.

Purpose
-------
Answer the question: *"If I had traded the BULLISH / BEARISH verdict
every bar in the past, what would my hit-rate, average return, and
profit factor have been?"*

This is a pure, reproducible **backtest on the same deterministic rules
that power the live dashboard.** For every ticker in the curated
universe and every bar in the available history, it:

1. Takes the window of closes ending at bar ``t``.
2. Rebuilds the indicator blocks (SMA50/200, RSI(14), MACD 12/26/9,
   ATR, volume stats) using the *same* functions the live app uses.
3. Runs ``_score_and_verdict`` to produce a verdict + conviction.
4. Measures the realised forward return from bar ``t`` to bar
   ``t + horizon`` (default: 5 bars).
5. Tallies hit-rate and PnL per verdict bucket.

No look-ahead: at bar ``t`` the forward window ``[t+1 … t+horizon]`` is
only used to score the trade, never to compute the signal.

Usage
-----
    cd square18_signals_web
    python -m tools.backtest_verdict --timeframe daily --horizon 5 --min-bars 260

Output
------
    Per-ticker metrics and an aggregate block printed to stdout.
    A JSON artifact is written to ``./backtest_verdict.json`` so you
    can diff runs or plot them later.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional

# Make the sibling package importable without install.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_SIGNALS_SRC = _REPO / "square18_signals" / "src"
if str(_SIGNALS_SRC) not in sys.path:
    sys.path.insert(0, str(_SIGNALS_SRC))
_WEB_ROOT = _HERE.parent
if str(_WEB_ROOT) not in sys.path:
    sys.path.insert(0, str(_WEB_ROOT))

from app.analyst.constants import TICKERS, Timeframe  # noqa: E402
from app.analyst.data import get_ohlcv  # noqa: E402
from app.analyst.indicators import adx as _adx  # noqa: E402
from app.analyst.indicators import atr as _atr  # noqa: E402
from app.analyst.indicators import bollinger as _bollinger  # noqa: E402
from app.analyst.indicators import macd as _macd  # noqa: E402
from app.analyst.indicators import rsi as _rsi  # noqa: E402
from app.analyst.indicators import sma  # noqa: E402
from app.analyst.indicators import stochastic as _stochastic  # noqa: E402
from app.analyst.report import (  # noqa: E402
    _adx_block,
    _atr_block,
    _bollinger_block,
    _compute_raw_score,
    _macd_block,
    _price_action,
    _rsi_block,
    _sma_block,
    _stochastic_block,
    _volume_stats,
    _score_to_verdict,
)
from app.analyst.signal_config import load_signal_config  # noqa: E402


@dataclass
class BarResult:
    verdict: str
    conviction: float
    score: float
    fwd_return_pct: float
    option_proxy_return_pct: float


@dataclass
class Bucket:
    n: int = 0
    wins: int = 0
    losses: int = 0
    returns: list[float] = None

    def __post_init__(self):
        if self.returns is None:
            self.returns = []

    def add(self, ret: float) -> None:
        self.n += 1
        self.returns.append(ret)
        if ret > 0:
            self.wins += 1
        elif ret < 0:
            self.losses += 1

    def summary(self) -> dict:
        if not self.returns:
            return dict(n=0)
        wins_sum = sum(r for r in self.returns if r > 0)
        losses_sum = -sum(r for r in self.returns if r < 0)
        return dict(
            n=self.n,
            hit_rate=round(self.wins / self.n * 100, 2),
            avg_return_pct=round(statistics.mean(self.returns), 3),
            median_return_pct=round(statistics.median(self.returns), 3),
            stdev_pct=round(statistics.pstdev(self.returns), 3) if self.n > 1 else 0.0,
            profit_factor=round(wins_sum / losses_sum, 2) if losses_sum > 0 else None,
            best_pct=round(max(self.returns), 3),
            worst_pct=round(min(self.returns), 3),
        )


def _score_window(
    closes, highs, lows, volumes,
    bull_tau: float = 0.30, bear_tau: float = -0.40,
) -> tuple[str, float, float]:
    """Rebuild indicator blocks and return (verdict, conviction, score).

    Uses ``_compute_raw_score`` (no MTF / regime side effects) so the
    backtest loop stays reproducible and fast.  Thresholds are passed
    explicitly so ``--search-tau`` can sweep values without touching
    ``signal_thresholds.json``.
    """
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200) if len(closes) >= 200 else [None] * len(closes)
    rsi_series = _rsi(closes, 14)
    macd_line, signal_line, hist_line = _macd(closes)
    atr_series = _atr(highs, lows, closes, 14)
    adx_series, plus_di_series, minus_di_series = _adx(highs, lows, closes, 14)

    pa = _price_action(closes, highs, lows, sma50)
    vs = _volume_stats(volumes)
    sb = _sma_block(closes, sma50, sma200)
    rsi_b = _rsi_block(rsi_series)
    macd_b = _macd_block(macd_line, signal_line, hist_line)
    _atr_block(atr_series, closes)
    adx_b = _adx_block(adx_series, plus_di_series, minus_di_series)
    bb_mid, bb_upper, bb_lower = _bollinger(closes, 20, 2.0)
    stoch_k, stoch_d = _stochastic(highs, lows, closes, 14, 3, 3)
    bbb = _bollinger_block(closes, bb_mid, bb_upper, bb_lower)
    stb = _stochastic_block(stoch_k, stoch_d)

    score = _compute_raw_score(pa, vs, sb, rsi_b, macd_b, adx_b, stoch_b=stb, bb_b=bbb)
    cfg = {
        "thresholds": {
            "BULLISH": {"min_score": bull_tau},
            "BEARISH": {"max_score": bear_tau},
        }
    }
    verdict, conviction = _score_to_verdict(score, cfg)
    return verdict, conviction, score


def _signed_directional_return(verdict: str, fwd_return_pct: float) -> float:
    """Return a positive value when the verdict direction is correct."""
    if verdict == "BEARISH":
        return -fwd_return_pct
    return fwd_return_pct


def _option_proxy_return_pct(
    verdict: str,
    conviction: float,
    fwd_return_pct: float,
    horizon: int,
) -> float:
    """Simple options-edge proxy from directional move, leverage, and theta drag."""
    directional = _signed_directional_return(verdict, fwd_return_pct)
    leverage = 1.2 + max(0.0, min(1.0, conviction))
    theta_drag = 0.35 * max(1, horizon) / 5.0
    proxy = directional * leverage - theta_drag
    return round(max(-100.0, min(300.0, proxy)), 3)


def backtest_symbol(
    symbol: str,
    timeframe: Timeframe,
    horizon: int,
    min_bars: int,
    stride: int = 1,
    hc_filter: float = 0.0,
    bull_tau: float = 0.30,
    bear_tau: float = -0.40,
) -> tuple[dict, list[BarResult]]:
    data = get_ohlcv(symbol, timeframe)
    n = len(data.close)
    if n < min_bars + horizon + 5:
        return {"skipped": True, "reason": f"only {n} bars"}, []

    rows: list[BarResult] = []
    # Walk forward from min_bars to n - horizon.
    for t in range(min_bars, n - horizon, stride):
        closes = data.close[: t + 1]
        highs = data.high[: t + 1]
        lows = data.low[: t + 1]
        volumes = data.volume[: t + 1]
        try:
            verdict, conviction, score = _score_window(
                closes, highs, lows, volumes, bull_tau=bull_tau, bear_tau=bear_tau,
            )
        except Exception:
            continue
        if conviction < hc_filter:
            continue
        spot = closes[-1]
        fwd = data.close[t + horizon]
        fwd_ret = (fwd / spot - 1) * 100
        option_proxy_ret = _option_proxy_return_pct(
            verdict=verdict,
            conviction=conviction,
            fwd_return_pct=fwd_ret,
            horizon=horizon,
        )
        rows.append(
            BarResult(
                verdict=verdict,
                conviction=round(conviction, 3),
                score=round(score, 3),
                fwd_return_pct=round(fwd_ret, 3),
                option_proxy_return_pct=option_proxy_ret,
            )
        )

    buckets: dict[str, Bucket] = {
        "BULLISH": Bucket(),
        "BEARISH": Bucket(),
        "NEUTRAL": Bucket(),
    }
    for r in rows:
        buckets[r.verdict].add(_signed_directional_return(r.verdict, r.fwd_return_pct))

    return (
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": data.source,
            "bars_tested": len(rows),
            "total_bars": n,
            "horizon": horizon,
            "buckets": {k: v.summary() for k, v in buckets.items()},
        },
        rows,
    )


def _option_proxy_summary(rows: list[BarResult]) -> dict:
    vals = [r.option_proxy_return_pct for r in rows]
    if not vals:
        return {"n": 0}
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    wins_sum = sum(wins)
    losses_sum = -sum(losses)
    return {
        "n": len(vals),
        "hit_rate": round(len(wins) / len(vals) * 100, 2),
        "avg_return_pct": round(statistics.mean(vals), 3),
        "median_return_pct": round(statistics.median(vals), 3),
        "stdev_pct": round(statistics.pstdev(vals), 3) if len(vals) > 1 else 0.0,
        "profit_factor": round(wins_sum / losses_sum, 2) if losses_sum > 0 else None,
        "best_pct": round(max(vals), 3),
        "worst_pct": round(min(vals), 3),
    }


def _calibration_bins(rows: list[BarResult], verdict: str) -> list[dict]:
    """Calibrate hit-rate by abs(score) bins for a verdict."""
    bins = [(0.00, 0.20), (0.20, 0.35), (0.35, 0.50), (0.50, 0.70), (0.70, 1.01)]
    out: list[dict] = []
    for lo, hi in bins:
        sub = [
            r for r in rows
            if r.verdict == verdict and lo <= abs(r.score) < hi
        ]
        if not sub:
            continue
        signed = [_signed_directional_return(r.verdict, r.fwd_return_pct) for r in sub]
        wins = [v for v in signed if v > 0]
        out.append({
            "min_score": round(lo, 2),
            "max_score": round(min(1.0, hi), 2),
            "n": len(sub),
            "hit_rate": round(len(wins) / len(sub) * 100, 2),
            "avg_return_pct": round(statistics.mean(signed), 3),
            "avg_option_proxy_return_pct": round(
                statistics.mean([r.option_proxy_return_pct for r in sub]), 3
            ),
        })
    return out


def _tau_search(universe: list[str], timeframe: Timeframe, horizon: int,
                min_bars: int, stride: int) -> tuple[float, float]:
    """Grid-search BULLISH and BEARISH τ to maximise aggregate profit-factor.

    Returns (best_bull_tau, best_bear_tau) rounded to 2 decimal places.
    """
    import itertools
    bull_vals = [round(v, 2) for v in [x * 0.05 for x in range(5, 13)]]   # 0.25..0.60
    bear_vals = [round(-v, 2) for v in [x * 0.05 for x in range(5, 13)]]  # -0.25..-0.60

    # Collect raw bar results once per symbol (at τ=0 to get all scores)
    all_rows: list[BarResult] = []
    for sym in universe:
        _, rows = backtest_symbol(
            sym, timeframe, horizon, min_bars, stride=stride,
            bull_tau=0.01, bear_tau=-0.01,  # include everything
        )
        all_rows.extend(rows)

    if not all_rows:
        return 0.30, -0.40

    best_pf = -1.0
    best_pair = (0.30, -0.40)
    for bt, br in itertools.product(bull_vals, bear_vals):
        bull_b, bear_b, neu_b = Bucket(), Bucket(), Bucket()
        for r in all_rows:
            if r.score >= bt:
                bull_b.add(_signed_directional_return("BULLISH", r.fwd_return_pct))
            elif r.score <= br:
                bear_b.add(_signed_directional_return("BEARISH", r.fwd_return_pct))
            else:
                neu_b.add(_signed_directional_return("NEUTRAL", r.fwd_return_pct))
        bs, brs = bull_b.summary(), bear_b.summary()
        pf_bull = bs.get("profit_factor") or 0.0
        pf_bear = brs.get("profit_factor") or 0.0
        n_bull, n_bear = bs.get("n", 0), brs.get("n", 0)
        # Require at least 50 bars in each directional bucket
        if n_bull < 50 or n_bear < 50:
            continue
        combined_pf = (pf_bull * n_bull + pf_bear * n_bear) / (n_bull + n_bear)
        if combined_pf > best_pf:
            best_pf = combined_pf
            best_pair = (bt, br)
    return best_pair


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeframe", default="daily",
                    choices=["1h", "4h", "daily", "weekly"])
    ap.add_argument("--horizon", type=int, default=5,
                    help="forward return horizon in bars")
    ap.add_argument("--min-bars", type=int, default=260,
                    help="first bar index to start scoring from (warm-up)")
    ap.add_argument("--stride", type=int, default=1,
                    help="bar spacing between scored points")
    ap.add_argument("--min-conviction", type=float, default=0.0,
                    help="ignore signals below this conviction floor (0..1)")
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="override the curated ticker list")
    ap.add_argument("--out", default="backtest_verdict.json")
    ap.add_argument("--search-tau", action="store_true",
                    help="grid-search BULLISH/BEARISH thresholds for best PF and "
                         "emit signal_thresholds.json alongside the backtest artifact")
    ap.add_argument("--bull-tau", type=float, default=None,
                    help="fixed BULLISH min_score (e.g. 0.35); overrides --search-tau")
    ap.add_argument("--bear-tau", type=float, default=None,
                    help="fixed BEARISH max_score (e.g. -0.60); overrides --search-tau")
    ap.add_argument("--write-thresholds", action="store_true",
                    help="write signal_thresholds.json from this run's aggregate stats")
    ap.add_argument("--thresholds-out", default="signal_thresholds.json",
                    help="output path for signal_thresholds.json")
    args = ap.parse_args()

    universe = args.symbols or [m["symbol"] for m in TICKERS]

    # Optional: tune thresholds first, then run backtest with the best τ.
    bull_tau, bear_tau = 0.30, -0.40  # defaults
    if args.bull_tau is not None:
        bull_tau = float(args.bull_tau)
    if args.bear_tau is not None:
        bear_tau = float(args.bear_tau)
    if args.search_tau and args.bull_tau is None and args.bear_tau is None:
        print("Searching for best BULLISH / BEARISH τ …")
        bull_tau, bear_tau = _tau_search(
            universe, args.timeframe, args.horizon, args.min_bars, args.stride
        )
        print(f"  Best τ: BULL ≥ {bull_tau}  BEAR ≤ {bear_tau}")

    per_symbol: list[dict] = []
    global_buckets: dict[str, Bucket] = {
        "BULLISH": Bucket(),
        "BEARISH": Bucket(),
        "NEUTRAL": Bucket(),
    }
    all_rows: list[BarResult] = []
    total_rows = 0
    for sym in universe:
        summary, rows = backtest_symbol(
            sym, args.timeframe, args.horizon, args.min_bars,
            stride=args.stride, hc_filter=args.min_conviction,
            bull_tau=bull_tau, bear_tau=bear_tau,
        )
        if not summary.get("skipped"):
            summary["option_proxy"] = _option_proxy_summary(rows)
        per_symbol.append(summary)
        all_rows.extend(rows)
        total_rows += len(rows)
        for r in rows:
            global_buckets[r.verdict].add(_signed_directional_return(r.verdict, r.fwd_return_pct))
        if summary.get("skipped"):
            print(f"[{sym}] skipped — {summary['reason']}")
            continue
        b = summary["buckets"]
        print(
            f"[{sym:<6}] bars={summary['bars_tested']:>4}  "
            f"BULL n={b['BULLISH'].get('n',0):>3} hit={b['BULLISH'].get('hit_rate','—'):>5}  "
            f"avg={b['BULLISH'].get('avg_return_pct','—'):>6}%  "
            f"| BEAR n={b['BEARISH'].get('n',0):>3} hit={b['BEARISH'].get('hit_rate','—'):>5}  "
            f"avg={b['BEARISH'].get('avg_return_pct','—'):>6}%  "
            f"| NEU n={b['NEUTRAL'].get('n',0):>3} "
            f"avg={b['NEUTRAL'].get('avg_return_pct','—'):>6}%"
        )

    agg = {k: v.summary() for k, v in global_buckets.items()}
    calibration = {
        "BULLISH": _calibration_bins(all_rows, "BULLISH"),
        "BEARISH": _calibration_bins(all_rows, "BEARISH"),
        "NEUTRAL": _calibration_bins(all_rows, "NEUTRAL"),
    }
    option_proxy_agg = _option_proxy_summary(all_rows)
    print()
    print("=" * 78)
    print(
        f"AGGREGATE (tf={args.timeframe} horizon={args.horizon}b "
        f"τ_bull={bull_tau} τ_bear={bear_tau} total_rows={total_rows})"
    )
    for name in ("BULLISH", "BEARISH", "NEUTRAL"):
        s = agg[name]
        if s.get("n", 0) == 0:
            print(f"  {name:<7}  no bars")
            continue
        print(
            f"  {name:<7}  n={s['n']:>5}  hit={s['hit_rate']:>5}%  "
            f"avg={s['avg_return_pct']:>6}%  med={s['median_return_pct']:>6}%  "
            f"pf={s['profit_factor']}  "
            f"best={s['best_pct']:>6}%  worst={s['worst_pct']:>6}%"
        )
    print("=" * 78)

    out = {
        "timeframe": args.timeframe,
        "horizon": args.horizon,
        "min_bars": args.min_bars,
        "stride": args.stride,
        "min_conviction": args.min_conviction,
        "bull_tau": bull_tau,
        "bear_tau": bear_tau,
        "per_symbol": per_symbol,
        "aggregate": agg,
        "option_proxy_aggregate": option_proxy_agg,
        "calibration": calibration,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\nwrote {args.out}")

    if args.search_tau or args.write_thresholds:
        thr_path = Path(args.thresholds_out)
        existing: dict = {}
        if thr_path.exists():
            try:
                existing = json.loads(thr_path.read_text())
            except Exception:
                existing = {}
        existing.setdefault("thresholds", {})
        existing["thresholds"]["BULLISH"] = {
            "min_score": bull_tau,
            "probability_pct": agg.get("BULLISH", {}).get("hit_rate", 55.5),
        }
        existing["thresholds"]["BEARISH"] = {
            "max_score": bear_tau,
            "probability_pct": agg.get("BEARISH", {}).get("hit_rate", 52.5),
        }
        existing["thresholds"]["NEUTRAL"] = {
            "probability_pct": agg.get("NEUTRAL", {}).get("hit_rate", 55.5),
        }
        existing["calibration"] = calibration
        existing["generated_by"] = (
            "backtest_verdict --search-tau"
            if args.search_tau
            else f"backtest_verdict --bull-tau {bull_tau} --bear-tau {bear_tau}"
        )
        existing["version"] = existing.get("version", 1)
        thr_path.write_text(json.dumps(existing, indent=2))
        print(f"wrote tuned thresholds → {thr_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
