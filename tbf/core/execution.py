"""
目標比率 -> 注文の計画と執行。バックテスト・ペーパー・本番で共通。

順序: 1. 持ち高を減らす注文  2. 不足する取引所へ資金を移す  3. 持ち高を増やす注文
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Optional

from tbf.core.bot import Bot, Targets
from tbf.core.portfolio import Portfolio, apply_fill, equity, perp_venue_notional, perp_venue_value
from tbf.core.types import Fill, Side


@dataclass(frozen=True)
class Order:
    alias: str
    side: Side
    qty: float
    ref_price: float
    reduce: bool


@dataclass(frozen=True)
class Transfer:
    src: str
    dst: str
    amount: float


@dataclass
class ExecutionReport:
    fills: list[Fill] = field(default_factory=list)
    transfers: list[Transfer] = field(default_factory=list)
    transfer_costs: float = 0.0
    skipped: list[str] = field(default_factory=list)


def scale_targets(bot: Bot, targets: Targets) -> Targets:
    """現物の買い代金 + 先物の証拠金 (想定元本 / leverage) が資産を超えないよう全体を縮める"""
    usage = sum(
        (max(w, 0.0) if bot.instruments[a].kind == "spot" else abs(w) / bot.leverage) for a, w in targets.items()
    )
    # 現物だけなら買いは手元の現金の範囲に収めるので余裕は不要。先物を含むときは手数料ぶん残す
    uses_perp = any(w and bot.instruments[a].is_perp for a, w in targets.items())
    cap = 1 - bot.cash_buffer if uses_perp else 1.0
    k = cap / usage if usage > cap else 1.0
    return {a: w * k for a, w in targets.items()}


def plan_orders(bot: Bot, pf: Portfolio, prices: Mapping[str, float], targets: Targets) -> list[Order]:
    eq = equity(pf, bot.instruments, prices)
    if eq <= 0:
        return []
    scaled = scale_targets(bot, targets)
    orders: list[Order] = []
    for alias in bot.instruments:
        # 目標に書かれていない銘柄は 0 (手仕舞い)
        w = scaled.get(alias, 0.0)
        if w == 0 and pf.qty(alias) == 0:
            continue
        px = prices[alias]
        cur = pf.qty(alias)
        tgt = w * eq / px
        delta = tgt - cur
        if abs(delta) * px < 1e-9:
            continue
        closing = tgt == 0 and cur != 0
        if not closing and abs(delta) * px < bot.rebalance_threshold * eq:
            continue
        if cur != 0 and tgt != 0 and (cur > 0) != (tgt > 0):
            # ドテンは「決済」と「新規」に分ける
            orders.append(Order(alias, "sell" if cur > 0 else "buy", abs(cur), px, True))
            orders.append(Order(alias, "buy" if tgt > 0 else "sell", abs(tgt), px, False))
            continue
        reduce = abs(tgt) < abs(cur)
        orders.append(Order(alias, "buy" if delta > 0 else "sell", abs(delta), px, reduce))
    return sorted(orders, key=lambda o: not o.reduce)


def plan_transfers(bot: Bot, pf: Portfolio, prices: Mapping[str, float], increases: list[Order]) -> list[Transfer]:
    """増やす注文に必要な現金 (現物の買い代金 + 先物の証拠金) が足りない取引所へ、余っている取引所から移す"""
    insts = bot.instruments
    venues = set(pf.cash) | {i.venue for i in insts.values()}
    desired: dict[str, float] = {v: 0.0 for v in venues}
    added_notional: dict[str, float] = {v: 0.0 for v in venues}
    for o in increases:
        inst = insts[o.alias]
        if inst.kind == "spot":
            desired[inst.venue] += o.qty * o.ref_price * (1 + inst.fees.cost_rate)  # type: ignore[union-attr]
        else:
            added_notional[inst.venue] += o.qty * o.ref_price
    for v in venues:
        notional = perp_venue_notional(pf, insts, prices, v) + added_notional[v]
        if notional > 0:
            # 先物口座から引き出せるのは「口座価値 (現金 + 含み損益) - 必要証拠金」まで。
            # 含み益があれば現金がマイナスになるまで引き出せる (クロスマージンの引き出し可能額と同じ考え方)
            upnl = perp_venue_value(pf, insts, prices, v) - pf.cash.get(v, 0.0)
            desired[v] += notional / bot.leverage - upnl
    surplus = {v: pf.cash.get(v, 0.0) - desired[v] for v in venues}
    transfers = []
    for dst in sorted(venues, key=lambda v: surplus[v]):
        need = -surplus[dst]
        while need >= 1.0:
            src = max(venues, key=lambda v: surplus[v])
            avail = surplus[src] - bot.transfer_fee_usd
            if src == dst or avail < 1.0:
                break
            amount = min(need, avail)
            transfers.append(Transfer(src, dst, amount))
            surplus[src] -= amount + bot.transfer_fee_usd
            surplus[dst] += amount
            need -= amount
    return transfers


FillFn = Callable[[Order], Awaitable[Optional[Fill]]]
TransferFn = Callable[[Transfer], Awaitable[bool]]
OnFill = Callable[[Fill], Awaitable[None]]


async def execute_targets(
    bot: Bot,
    pf: Portfolio,
    prices: Mapping[str, float],
    targets: Targets,
    fill_fn: FillFn,
    transfer_fn: TransferFn,
    on_fill: Optional[OnFill] = None,
) -> ExecutionReport:
    """
    pf を書き換えながら目標まで売買する。
    fill_fn は約定 (None なら未約定)、transfer_fn は資金移動の実行、on_fill は口座に反映した直後の約定 (記録用)。
    途中で例外が出た場合も、それまでの約定は pf に反映済み。
    """
    rep = ExecutionReport()
    orders = plan_orders(bot, pf, prices, targets)

    async def run(o: Order) -> None:
        fill = await fill_fn(o)
        if fill is None:
            rep.skipped.append(f"{o.side} {o.qty:.8g} {o.alias}")
            return
        applied = apply_fill(pf, bot.instruments[o.alias], fill)
        rep.fills.append(applied)
        if on_fill:
            await on_fill(applied)

    for o in [o for o in orders if o.reduce]:
        await run(o)

    increases = [o for o in orders if not o.reduce]
    for t in plan_transfers(bot, pf, prices, increases):
        if await transfer_fn(t):
            pf.cash[t.src] = pf.cash.get(t.src, 0.0) - t.amount - bot.transfer_fee_usd
            pf.cash[t.dst] = pf.cash.get(t.dst, 0.0) + t.amount
            rep.transfers.append(t)
            rep.transfer_costs += bot.transfer_fee_usd

    for o in increases:
        inst = bot.instruments[o.alias]
        if inst.kind == "spot" and o.side == "buy":
            # 現物の買いは、その取引所の現金の範囲まで
            affordable = max(0.0, pf.cash.get(inst.venue, 0.0)) / (o.ref_price * (1 + inst.fees.cost_rate))  # type: ignore[union-attr]
            if affordable < o.qty:
                o = Order(o.alias, o.side, affordable, o.ref_price, o.reduce)
            if o.qty * o.ref_price < 1e-6:
                rep.skipped.append(f"buy {o.alias} (no cash at {inst.venue})")
                continue
        await run(o)
    return rep
