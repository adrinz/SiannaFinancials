"""Load and cache signal quality configuration from signal_thresholds.json.

The file lives at the square18_signals_web root (same directory as
backtest_verdict.json).  When absent the module falls back to hardcoded
defaults derived from the stored backtest aggregate.

Usage
-----
    from .signal_config import load_signal_config, bullish_threshold, bearish_threshold

Re-reading is triggered automatically when the file's mtime changes (max
once per 60 s), so you can tune thresholds at runtime without a restart.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

_THRESHOLDS_PATH = Path(__file__).resolve().parent.parent.parent / "signal_thresholds.json"

# Defaults derived from backtest_verdict.json aggregate (daily, 5-bar horizon).
_DEFAULTS: dict = {
    "version": 1,
    "thresholds": {
        "BULLISH": {"min_score": 0.30, "probability_pct": 55.48},
        # Stricter bear threshold than raw 0.30 — filters the PF<1 weak-bear bucket.
        "BEARISH": {"max_score": -0.40, "probability_pct": 52.49},
        "NEUTRAL": {"probability_pct": 55.51},
    },
    "mtf": {
        "enabled": True,
        "confluence_bonus": 0.06,
        "conflict_penalty": 0.06,
        "strong_veto_threshold": 0.20,
    },
    "regime": {
        "enabled": True,
        "vix_high_suppress_bull": 28.0,
        "vix_extreme_suppress_all": 35.0,
        "breadth_low_suppress_bull": 35.0,
    },
}

_cache: dict = {}
_cache_ts: float = 0.0
_cache_mtime: float = 0.0
_RELOAD_INTERVAL = 60.0  # seconds between stat checks


def load_signal_config() -> dict:
    """Return the merged config dict, reloading from disk when stale."""
    global _cache, _cache_ts, _cache_mtime
    now = time.time()
    if _cache and (now - _cache_ts) < _RELOAD_INTERVAL:
        return _cache
    _cache_ts = now
    try:
        if _THRESHOLDS_PATH.exists():
            mtime = _THRESHOLDS_PATH.stat().st_mtime
            if mtime != _cache_mtime or not _cache:
                raw = json.loads(_THRESHOLDS_PATH.read_text())
                merged = _deep_merge(_DEFAULTS, raw)
                _cache = merged
                _cache_mtime = mtime
        else:
            if not _cache:
                _cache = _DEFAULTS.copy()
    except Exception:
        if not _cache:
            _cache = _DEFAULTS.copy()
    return _cache


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a copy of *base*."""
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def bullish_threshold() -> float:
    return float(load_signal_config()["thresholds"]["BULLISH"]["min_score"])


def bearish_threshold() -> float:
    """Returns a *negative* value (e.g. -0.40)."""
    return float(load_signal_config()["thresholds"]["BEARISH"]["max_score"])


def probability_for_verdict(verdict: str) -> Optional[float]:
    cfg = load_signal_config()
    return cfg["thresholds"].get(verdict, {}).get("probability_pct")
