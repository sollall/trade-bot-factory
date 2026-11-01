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
HOUR = TIMEFRAME_MS["1h"]


@dataclass(frozen=True)
class Candle:
    """確定足。time は足の開始時刻 (UTC ms)"""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Position:
    """qty は保有数量 (+ ロング / - ショート)"""

    qty: float = 0.0
    avg_price: float = 0.0


@dataclass(frozen=True)
class FundingPoint:
    """無期限先物のファンディング"""

    # 支払いが発生した時刻 (UTC ms, 時間単位に丸めたもの)
    time: int
    # 1 回あたりのレート (+ ならロングがショートに払う)
    rate: float
    # 先物価格とオラクル (現物指数) の乖離率
    premium: float = 0.0


@dataclass(frozen=True)
class Fill:
    time: int
    side: Side
    qty: float
    price: float
    fee: float
    # 決済 (持ち高を減らす約定) で確定した損益 (手数料込み)
    realized_pnl: float = 0.0
    # bot 内の銘柄の別名
    instrument: str = ""
    # 持ち高を減らす約定か
    reduce: bool = False


@dataclass(frozen=True)
class EquityPoint:
    time: int
    equity: float
