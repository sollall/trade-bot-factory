"""
複数取引所・複数銘柄の口座計算。

- 現金は取引所ごとに持つ (cash[venue])
- 現物: 買うと現金が減り数量が増える。評価額 = 数量 × 価格
- 先物: 約定しても現金は動かず (手数料のみ)、決済時に実現損益が現金に入る。評価額 = 含み損益
- 先物口座の価値 = その取引所の現金 + 先物の含み損益。これが維持証拠金を割ると清算
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Mapping, Optional

from tbf.core.instrument import Fees, Instrument
from tbf.core.types import Fill, Position, Side


@dataclass
class Portfolio:
    cash: dict[str, float] = field(default_factory=dict)
    holdings: dict[str, Position] = field(default_factory=dict)

    @classmethod
    def initial(cls, capital: float, venue: str) -> "Portfolio":
        return cls(cash={venue: capital})

    def copy(self) -> "Portfolio":
        return copy.deepcopy(self)

    def qty(self, alias: str) -> float:
        return self.holdings.get(alias, Position()).qty

    @property
    def total_cash(self) -> float:
        return sum(self.cash.values())

    # ---- 永続化 ----
    def to_json(self) -> dict:
        return {
            "cash": self.cash,
            "positions": {a: [p.qty, p.avg_price] for a, p in self.holdings.items() if p.qty != 0},
        }

    @classmethod
    def from_json(cls, d: Mapping) -> "Portfolio":
        return cls(
            cash={k: float(v) for k, v in d.get("cash", {}).items()},
            holdings={a: Position(float(q), float(p)) for a, (q, p) in d.get("positions", {}).items()},
        )


def holding_value(inst: Instrument, pos: Position, price: float) -> float:
    """現物は時価、先物は含み損益"""
    if inst.is_perp:
        return pos.qty * (price - pos.avg_price)
    return pos.qty * price


def equity(pf: Portfolio, instruments: Mapping[str, Instrument], prices: Mapping[str, float]) -> float:
    total = pf.total_cash
    for alias, pos in pf.holdings.items():
        if pos.qty:
            total += holding_value(instruments[alias], pos, prices[alias])
    return total


def weights(pf: Portfolio, instruments: Mapping[str, Instrument], prices: Mapping[str, float]) -> dict[str, float]:
    eq = equity(pf, instruments, prices)
    return {a: (pf.qty(a) * prices[a] / eq if eq > 0 else 0.0) for a in instruments}


def perp_venue_value(pf: Portfolio, instruments: Mapping[str, Instrument], prices: Mapping[str, float], venue: str) -> float:
    v = pf.cash.get(venue, 0.0)
    for alias, pos in pf.holdings.items():
        inst = instruments[alias]
        if inst.venue == venue and inst.is_perp and pos.qty:
            v += holding_value(inst, pos, prices[alias])
    return v


def perp_venue_notional(pf: Portfolio, instruments: Mapping[str, Instrument], prices: Mapping[str, float], venue: str) -> float:
    return sum(
        abs(pos.qty) * prices[a]
        for a, pos in pf.holdings.items()
        if instruments[a].venue == venue and instruments[a].is_perp
    )


def effective_leverage(pf: Portfolio, instruments, prices, venue: str) -> float:
    notional = perp_venue_notional(pf, instruments, prices, venue)
    if notional == 0:
        return 0.0
    value = perp_venue_value(pf, instruments, prices, venue)
    return notional / value if value > 0 else math.inf


def simulate_fill(alias: str, side: Side, qty: float, ref_price: float, time: int, fees: Fees) -> Fill:
    """スリッページ・手数料込みのシミュレーション約定"""
    slip = fees.slippage_bps / 10_000 * ref_price
    price = ref_price + slip if side == "buy" else ref_price - slip
    return Fill(time, side, qty, price, qty * price * fees.taker_rate, instrument=alias)


def apply_fill(pf: Portfolio, inst: Instrument, fill: Fill) -> Fill:
    """約定を口座に反映し (pf を書き換える)、realized_pnl / reduce を埋めた Fill を返す"""
    alias = fill.instrument
    pos = pf.holdings.get(alias, Position())
    qty = fill.qty
    if inst.kind == "spot" and fill.side == "sell":
        qty = min(qty, max(pos.qty, 0.0))
    signed = qty if fill.side == "buy" else -qty
    venue = inst.venue
    pf.cash.setdefault(venue, 0.0)

    reducing = pos.qty != 0 and (pos.qty > 0) != (signed > 0)
    closed = min(abs(signed), abs(pos.qty)) if reducing else 0.0
    gross_pnl = (fill.price - pos.avg_price) * closed * (1 if pos.qty > 0 else -1) if reducing else 0.0

    if inst.kind == "spot":
        pf.cash[venue] -= signed * fill.price + fill.fee
    else:
        pf.cash[venue] += gross_pnl - fill.fee

    new_qty = pos.qty + signed
    if abs(new_qty) < 1e-12:
        new_pos = Position()
    elif not reducing:
        new_pos = Position(new_qty, (pos.qty * pos.avg_price + signed * fill.price) / new_qty)
    elif (new_qty > 0) == (pos.qty > 0):
        new_pos = Position(new_qty, pos.avg_price)
    else:  # ドテン: 残りは新しい建値
        new_pos = Position(new_qty, fill.price)
    if new_pos.qty:
        pf.holdings[alias] = new_pos
    else:
        pf.holdings.pop(alias, None)

    return Fill(
        fill.time,
        fill.side,
        qty,
        fill.price,
        fill.fee,
        gross_pnl - fill.fee if reducing else 0.0,
        alias,
        reducing,
    )


def settle_funding(pf: Portfolio, inst: Instrument, alias: str, rate: float, oracle_price: float) -> float:
    """ファンディングの受け払い。+ ならショートが受け取る。受け取った額を返す"""
    qty = pf.qty(alias)
    if not inst.is_perp or not qty:
        return 0.0
    amount = -qty * oracle_price * rate
    pf.cash[inst.venue] = pf.cash.get(inst.venue, 0.0) + amount
    return amount


def liquidated_venues(
    pf: Portfolio, instruments: Mapping[str, Instrument], adverse_prices: Mapping[str, float]
) -> list[str]:
    """不利な価格 (ロングは安値, ショートは高値) で先物口座が維持証拠金を割る取引所"""
    out = []
    for venue in {i.venue for i in instruments.values() if i.is_perp}:
        perps = [(a, p) for a, p in pf.holdings.items() if instruments[a].venue == venue and instruments[a].is_perp and p.qty]
        if not perps:
            continue
        value = perp_venue_value(pf, instruments, adverse_prices, venue)
        maintenance = sum(abs(p.qty) * adverse_prices[a] * instruments[a].maintenance_margin for a, p in perps)
        if value <= maintenance:
            out.append(venue)
    return out


def liquidate_venue(pf: Portfolio, instruments: Mapping[str, Instrument], venue: str) -> None:
    """清算: その取引所の先物を全て失い、証拠金 (現金) も全損とみなす (保守的)"""
    for alias in [a for a, p in pf.holdings.items() if instruments[a].venue == venue and instruments[a].is_perp]:
        pf.holdings.pop(alias)
    pf.cash[venue] = 0.0


def position_of(pf: Portfolio, alias: str) -> Optional[Position]:
    return pf.holdings.get(alias)
