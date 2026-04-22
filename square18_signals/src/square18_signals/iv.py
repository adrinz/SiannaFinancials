"""Implied-volatility rank and percentile.

Given a history of IV readings (one per trading day over some lookback,
typically 252 sessions for a trailing 52-week window), IV rank and IV
percentile summarize how "rich" current IV is vs its own history.

- **IV rank**   = (IV_now - IV_min) / (IV_max - IV_min) * 100
- **IV percentile** = (# days with IV < IV_now) / (# days in window) * 100

IV rank is sensitive to outliers (it uses min/max), IV percentile is robust.
Most traders look at both.
"""

from __future__ import annotations

from collections.abc import Sequence


def _validate_history(history: Sequence[float]) -> None:
    if len(history) == 0:
        raise ValueError("IV history must contain at least one observation")
    if any(x < 0 for x in history):
        raise ValueError("IV history contains a negative value")


def iv_rank(current_iv: float, history: Sequence[float]) -> float:
    """Return IV rank (0..100).

    Args:
        current_iv: today's IV (annualized decimal, e.g. 0.32).
        history: trailing window of IV readings. May or may not include today.

    Returns:
        0 when current_iv == min(history), 100 when == max(history).
        If the history has zero range (flat), returns 50 as a neutral
        default (no information).
    """
    if current_iv < 0:
        raise ValueError(f"current_iv must be >= 0 (got {current_iv})")
    _validate_history(history)
    lo = min(history)
    hi = max(history)
    rng = hi - lo
    if rng <= 0:
        return 50.0
    # Allow current_iv to be outside the historical range (clamp to [0,100]).
    rank = (current_iv - lo) / rng * 100.0
    return max(0.0, min(100.0, rank))


def iv_percentile(current_iv: float, history: Sequence[float]) -> float:
    """Return IV percentile (0..100) — fraction of history strictly below current.

    Args:
        current_iv: today's IV.
        history: trailing window of IV readings.
    """
    if current_iv < 0:
        raise ValueError(f"current_iv must be >= 0 (got {current_iv})")
    _validate_history(history)
    n = len(history)
    below = sum(1 for x in history if x < current_iv)
    return below / n * 100.0
