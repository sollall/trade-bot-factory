from __future__ import annotations

from dataclasses import dataclass

from tbf.core.types import Fill, Position, Side


@dataclass(frozen=True)
class Portfolio:
    cash: float
    position: Position = Position()

    def equity(self, price: float) -> float:
        return self.cash + self.position.qty * price


def order_qty_for_target(
    p: Portfolio, price: float, target: float, threshold: float, cost_rate: float
) -> float:
    """目標比率に合わせるための base 数量差分 (+ 買い / - 売り)。閾値未満なら 0"""
    equity = p.equity(price)
    if equity <= 0 or price <= 0:
        return 0.0
    if target == 0:
        # 全決済は端数を残さない
        return -p.position.qty
    delta = equity * target / price - p.position.qty
    if abs(delta * price) < equity * threshold:
        return 0.0
    if delta > 0:
        # 手数料・スリッページ込みで現金を超えないようにする
        delta = min(delta, p.cash / (price * (1 + cost_rate) ** 2))
        if delta <= 0:
            return 0.0
    return delta


def apply_fill(p: Portfolio, fill: Fill) -> tuple[Portfolio, Fill]:
    """約定を反映した新しい口座と、realized_pnl を埋めた Fill を返す"""
    pos = p.position
    if fill.side == "buy":
        qty = pos.qty + fill.qty
        avg = (pos.qty * pos.avg_price + fill.qty * fill.price) / qty if qty > 0 else 0.0
        return Portfolio(p.cash - fill.qty * fill.price - fill.fee, Position(qty, avg)), fill
    sell_qty = min(fill.qty, pos.qty)
    pnl = (fill.price - pos.avg_price) * sell_qty - fill.fee
    remaining = pos.qty - sell_qty
    new_pos = Position(remaining, pos.avg_price) if remaining > 1e-12 else Position()
    out = Fill(fill.time, "sell", sell_qty, fill.price, fill.fee, pnl)
    return Portfolio(p.cash + sell_qty * fill.price - fill.fee, new_pos), out


def simulate_fill(
    side: Side, qty: float, ref_price: float, time: int, taker_rate: float, slippage_bps: float
) -> Fill:
    """スリッページ・手数料込みのシミュレーション約定"""
    slip = slippage_bps / 10_000 * ref_price
    price = ref_price + slip if side == "buy" else ref_price - slip
    return Fill(time, side, qty, price, qty * price * taker_rate)
