"""依存ライブラリなしのテクニカル指標 (Cloudflare 上でもそのまま動く)"""

from __future__ import annotations

from typing import Optional, Sequence


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values: Sequence[float], period: int = 14) -> Optional[float]:
    """Wilder の RSI"""
    if len(values) <= period:
        return None
    gain = loss = 0.0
    for a, b in zip(values[:period], values[1 : period + 1]):
        d = b - a
        gain += max(d, 0)
        loss += max(-d, 0)
    gain /= period
    loss /= period
    for a, b in zip(values[period:], values[period + 1 :]):
        d = b - a
        gain = (gain * (period - 1) + max(d, 0)) / period
        loss = (loss * (period - 1) + max(-d, 0)) / period
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def stdev(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    w = values[-period:]
    m = sum(w) / period
    return (sum((v - m) ** 2 for v in w) / period) ** 0.5
