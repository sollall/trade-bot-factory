from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Timeframe = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
Mode = Literal["backtest", "paper", "live"]
Side = Literal["buy", "sell"]

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


@dataclass(frozen=True)
class Candle:
    """確定足。time は足の開始時刻 (UTC ms)"""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Position:
    """qty は保有数量 (base 通貨, 現物なので >= 0)"""

    qty: float = 0.0
    avg_price: float = 0.0


@dataclass(frozen=True)
class Fill:
    time: int
    side: Side
    qty: float
    price: float
    fee: float
    # この約定で確定した損益 (sell のみ非 0)
    realized_pnl: float = 0.0


@dataclass(frozen=True)
class EquityPoint:
    time: int
    equity: float
