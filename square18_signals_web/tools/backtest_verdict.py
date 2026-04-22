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
from app.analyst.indicators import atr as _atr  # noqa: E402
from app.analyst.indicators import macd as _macd  # noqa: E402
from app.analyst.indicators import rsi as _rsi  # noqa: E402
from app.analyst.indicators import sma  # noqa: E402
from app.analyst.report import (  # noqa: E402
    _atr_block, _macd_block, _price_action, _rsi_block, _score_and_verdict,
    _sma_block, _volume_stats,
)


@dataclass
class BarResult:
    verdict: str
    conviction: float
    score: float
    fwd_return_pct: float


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


def _score_window(closes, highs, lows, volumes, sym, timeframe) -> tuple[str, float, float]:
    """Rebuild indicator blocks for the given window and return (verdict, conviction, score)."""
    sma50 = sma(closes, 50)
    sma200 = sma(closes, 200) if len(closes) >= 200 else [None] * len(closes)
    rsi_series = _rsi(closes, 14)
    macd_line, signal_line, hist_line = _macd(closes)
    atr_series = _atr(highs, lows, closes, 14)

    pa = _price_action(closes, highs, lows, sma50)
    vs = _volume_stats(volumes)
    sb = _sma_block(closes, sma50, sma200)
    rsi_b = _rsi_block(rsi_series)
    macd_b = _macd_block(macd_line, signal_line, hist_line)
    _atr_block(atr_series, closes)  # not used directly in _score_and_verdict

    score, verdict, conviction, _headline = _score_and_verdict(
        sym, timeframe, pa, vs, sb, rsi_b, macd_b
    )
    return verdict, conviction, score


def backtest_symbol(
    symbol: str,
    timeframe: Timeframe,
    horizon: int,
    min_bars: int,
    stride: int = 1,
    hc_filter: float = 0.0,
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
                closes, highs, lows, volumes, symbol, timeframe
            )
        except Exception:
            continue
        if conviction < hc_filter:
            continue
        spot = closes[-1]
        fwd = data.close[t + horizon]
        fwd_ret = (fwd / spot - 1) * 100
        rows.append(
            BarResult(
                verdict=verdict,
                conviction=round(conviction, 3),
                score=round(score, 3),
                fwd_return_pct=round(fwd_ret, 3),
            )
        )

    buckets: dict[str, Bucket] = {
        "BULLISH": Bucket(),
        "BEARISH": Bucket(),
        "NEUTRAL": Bucket(),
    }
    for r in rows:
        # For BEARISH we invert the sign — a correct BEARISH call means the
        # realised return should be *negative*, which is a "win" for the
        # downside trade.
        if r.verdict == "BEARISH":
            buckets["BEARISH"].add(-r.fwd_return_pct)
        else:
            buckets[r.verdict].add(r.fwd_return_pct)

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
    args = ap.parse_args()

    universe = args.symbols or [m["symbol"] for m in TICKERS]
    per_symbol: list[dict] = []
    global_buckets: dict[str, Bucket] = {
        "BULLISH": Bucket(),
        "BEARISH": Bucket(),
        "NEUTRAL": Bucket(),
    }
    total_rows = 0
    for sym in universe:
        summary, rows = backtest_symbol(
            sym, args.timeframe, args.horizon, args.min_bars,
            stride=args.stride, hc_filter=args.min_conviction,
        )
        per_symbol.append(summary)
        total_rows += len(rows)
        for r in rows:
            if r.verdict == "BEARISH":
                global_buckets["BEARISH"].add(-r.fwd_return_pct)
            else:
                global_buckets[r.verdict].add(r.fwd_return_pct)
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
    print()
    print("=" * 78)
    print(
        f"AGGREGATE (tf={args.timeframe} horizon={args.horizon}b "
        f"min_conv={args.min_conviction}, total_rows={total_rows})"
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
        "per_symbol": per_symbol,
        "aggregate": agg,
    }
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
