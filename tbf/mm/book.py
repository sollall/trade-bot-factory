"""記録と再生で共通に使う型と、値段の刻み"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Book:
    """ある時刻の気配。bids は高い順、asks は安い順の (値段, 数量)"""

    time: int  # 取引所の時刻 (ms)
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.best_ask - self.best_bid) / self.mid * 10_000


@dataclass(frozen=True)
class Trade:
    """市場で成立した約定。side は仕掛けた側: "B" = 買い手主導 (売り板を食った)、"A" = 売り手主導、
    "U" = 不明 (GMO コインだけ。API が仕掛けた側を返さない。tbf/mm/venues.py)"""

    time: int
    side: str
    px: float
    sz: float
    tid: int = 0


def trade_order(t: Trade) -> tuple:
    """再生で約定を流す順番。同じ時刻のものは、1 つの成行が板を食い進む向き
    (売り手主導は高い値段から、買い手主導は安い値段から) に並べる。API の返す順は一定でない"""
    return (t.time, t.side, -t.px if t.side == "A" else t.px, t.tid)


def price_decimals(px: float, sz_decimals: int) -> int:
    """その値段で使える小数の桁数。Hyperliquid の無期限は「有効数字 5 桁まで、かつ小数は
    (6 - szDecimals) 桁まで」。

    この規則は公式ドキュメントで確かめられていない (この環境から届かない)。記録した気配と約定の
    値段がすべてこの規則に収まることだけ確かめてある (docs/mm.md)。
    """
    if px <= 0:
        raise ValueError(f"値段が正でない: {px}")
    return max(0, min(4 - math.floor(math.log10(px)), 6 - sz_decimals))


def floor_to(x: float, step: float, decimals: int) -> float:
    """step の倍数に切り下げる (decimals は step の小数の桁数)。値段の刻みが決まっている会場 (GMO、bitbank) 用"""
    return round(math.floor(x / step + 1e-9) * step, decimals)


def ceil_to(x: float, step: float, decimals: int) -> float:
    return round(math.ceil(x / step - 1e-9) * step, decimals)


def round_down(px: float, decimals: int) -> float:
    return round(math.floor(px * 10**decimals + 1e-9) / 10**decimals, decimals)


def round_up(px: float, decimals: int) -> float:
    return round(math.ceil(px * 10**decimals - 1e-9) / 10**decimals, decimals)
