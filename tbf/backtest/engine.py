"""
汎用バックテスト (複数銘柄・現物 / 先物・ファンディング・清算)。

足 i の確定時に戦略を評価し、足 i+1 の始値で約定させる (先読みなし)。各足で:
  1. 前の足で決めた目標まで売買 (始値 + スリッページ)
  2. 足の高値 / 安値で先物の清算を判定
  3. 足の間に発生したファンディングを受け払い
  4. 終値で評価 → リスク判定 (違反したら停止) → 戦略 → 次の足の目標
"""

from __future__ import annotations

import asyncio
import bisect
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Union

from tbf.core.bot import Bot, Targets
from tbf.core.execution import Order, Transfer, execute_targets
from tbf.core.metrics import Metrics, compute_metrics
from tbf.core.portfolio import (
    Portfolio,
    effective_leverage,
    equity,
    liquidate_venue,
    liquidated_venues,
    settle_funding,
    simulate_fill,
)
from tbf.core.risk import check_risk, next_consecutive_losses
from tbf.core.step import run_strategy, utc_day
from tbf.core.types import HOUR, TIMEFRAME_MS, Candle, EquityPoint, Fill, FundingPoint

YEAR_MS = 365 * 24 * HOUR


@dataclass
class Halt:
    time: int
    reason: str


@dataclass
class Event:
    time: int
    kind: str  # enter | exit | rebalance | liquidation | halt
    detail: str = ""


@dataclass
class BacktestResult:
    bot_id: str
    metrics: Metrics
    equity: list[EquityPoint]
    fills: list[Fill]
    halted: Optional[Halt]
    final: Portfolio
    events: list[Event] = field(default_factory=list)
    funding_income: float = 0.0
    fees: float = 0.0
    transfer_costs: float = 0.0
    bars_in_market: int = 0
    bars_total: int = 0

    @property
    def apr(self) -> float:
        """年率換算リターン (複利)"""
        if len(self.equity) < 2 or self.equity[0].equity <= 0 or self.equity[-1].equity <= 0:
            return 0.0
        years = (self.equity[-1].time - self.equity[0].time) / YEAR_MS
        return (self.equity[-1].equity / self.equity[0].equity) ** (1 / years) - 1 if years > 0 else 0.0

    @property
    def price_pnl(self) -> float:
        """ファンディング・手数料・資金移動コスト以外の損益 (値動き・価格差の変化)"""
        m = self.metrics
        return m.end_equity - m.start_equity - self.funding_income + self.fees + self.transfer_costs

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e.kind == kind)

    def summary(self) -> dict[str, Any]:
        m = self.metrics
        return {
            "end_equity": m.end_equity,
            "total_return": m.total_return,
            "apr": self.apr,
            "max_drawdown": m.max_drawdown,
            "sharpe": m.sharpe,
            "trades": m.trades,
            "win_rate": m.win_rate,
            "funding_income": self.funding_income,
            "fees": self.fees,
            "transfer_costs": self.transfer_costs,
            "price_pnl": self.price_pnl,
            "time_in_market": self.bars_in_market / self.bars_total if self.bars_total else 0.0,
            "entries": self.count("enter"),
            "rebalances": self.count("rebalance"),
            "liquidations": self.count("liquidation"),
            "halted": self.halted.reason if self.halted else None,
        }


def aligned_times(bars: Mapping[str, Sequence[Candle]]) -> list[int]:
    """全銘柄に足がそろっている時刻だけ使う"""
    sets = [set(c.time for c in cs) for cs in bars.values()]
    return sorted(set.intersection(*sets)) if sets else []


async def backtest_async(
    bot: Bot,
    bars: Mapping[str, Sequence[Candle]],
    funding: Optional[Mapping[str, Sequence[FundingPoint]]] = None,
) -> BacktestResult:
    insts = bot.instruments
    missing = set(insts) - set(bars)
    if missing:
        raise ValueError(f"{bot.id}: no candles for {sorted(missing)}")
    tf = TIMEFRAME_MS[bot.timeframe]
    times = aligned_times({a: bars[a] for a in insts})
    by_time = {a: {c.time: c for c in bars[a]} for a in insts}
    fund = {a: sorted((funding or {}).get(a, []), key=lambda f: f.time) for a in insts if insts[a].is_perp}
    fund_times = {a: [f.time for f in fs] for a, fs in fund.items()}
    fund_window = max(bot.lookback * tf, 30 * 24 * HOUR)

    pf = Portfolio.initial(bot.capital, insts[bot.primary].venue)
    state: dict[str, Any] = {}
    hist: dict[str, list[Candle]] = {a: [] for a in insts}
    res = BacktestResult(bot.id, compute_metrics([], []), [], [], None, pf)
    pending: Optional[Targets] = None
    last_targets: Targets = {}
    day, day_start, peak, losses = "", bot.capital, bot.capital, 0
    premium = {a: 0.0 for a in fund}
    cur_time = 0

    async def fill_fn(o: Order) -> Fill:
        return simulate_fill(o.alias, o.side, o.qty, o.ref_price, cur_time, insts[o.alias].fees)  # type: ignore[arg-type]

    async def transfer_fn(t: Transfer) -> bool:
        return True

    if times:
        res.equity.append(EquityPoint(times[0], bot.capital))

    for i, t in enumerate(times):
        bar = {a: by_time[a][t] for a in insts}
        cur_time = t
        was_flat = not pf.holdings

        # 1. 執行
        if pending is not None:
            rep = await execute_targets(bot, pf, {a: b.open for a, b in bar.items()}, pending, fill_fn, transfer_fn)
            for f in rep.fills:
                res.fills.append(f)
                res.fees += f.fee
                losses = next_consecutive_losses(losses, f)
            res.transfer_costs += rep.transfer_costs
            if was_flat and pf.holdings:
                res.events.append(Event(t, "enter"))
            elif not was_flat and not pf.holdings:
                res.events.append(Event(t, "exit"))
            pending = None

        # 2. 清算
        liquidated = False
        if any(insts[a].is_perp for a in pf.holdings):
            adverse = {a: (b.high if pf.qty(a) < 0 else b.low) for a, b in bar.items()}
            for venue in liquidated_venues(pf, insts, adverse):
                liquidate_venue(pf, insts, venue)
                res.events.append(Event(t, "liquidation", venue))
                liquidated = True

        # 3. ファンディング (t, t + tf] に発生した分
        t_end = t + tf
        for a, fs in fund.items():
            lo = bisect.bisect_right(fund_times[a], t)
            hi = bisect.bisect_right(fund_times[a], t_end)
            for f in fs[lo:hi]:
                premium[a] = f.premium
                res.funding_income += settle_funding(pf, insts[a], a, f.rate, bar[a].close / (1 + f.premium))

        # 4. 評価・リスク・戦略
        for a in insts:
            hist[a].append(bar[a])
        closes = {a: b.close for a, b in bar.items()}
        eq = equity(pf, insts, closes)
        d = utc_day(t_end)
        if d != day:
            day, day_start = d, res.equity[-1].equity if res.equity else eq
        res.equity.append(EquityPoint(t_end, eq))
        peak = max(peak, eq)
        res.bars_total += 1
        if pf.holdings:
            res.bars_in_market += 1

        if liquidated:
            # ヘッジが外れた残りは手仕舞う。戦略は次の足から再開
            pending, last_targets = {}, {}
            continue
        if res.halted:
            continue
        breach = check_risk(bot.risk, equity=eq, peak_equity=peak, day_start_equity=day_start, consecutive_losses=losses)
        if breach:
            res.halted = Halt(t_end, breach)
            res.events.append(Event(t_end, "halt", breach))
            if bot.risk.flatten_on_halt and pf.holdings:
                pending = {}
            continue
        if i + 1 < bot.lookback:
            continue

        window = {a: hist[a][-bot.lookback :] for a in insts}
        fwin = {}
        for a, fs in fund.items():
            lo = bisect.bisect_right(fund_times[a], t_end - fund_window)
            hi = bisect.bisect_right(fund_times[a], t_end)
            fwin[a] = fs[lo:hi]
        targets, reason = run_strategy(bot, t_end, window, fwin, pf, closes, state, "backtest")
        if targets is not None:
            pending, last_targets = targets, targets
            continue
        # 戦略が「現状維持」でも、先物口座のレバレッジが目標から外れたら配分し直す
        for venue in {insts[a].venue for a in pf.holdings if insts[a].is_perp}:
            lev = effective_leverage(pf, insts, closes, venue)
            if lev > bot.leverage * bot.rebalance_band or lev < bot.leverage / bot.rebalance_band:
                pending = last_targets
                res.events.append(Event(t_end, "rebalance", f"{venue} leverage {lev:.2f} -> {bot.leverage:.2f}"))
                break

    res.metrics = compute_metrics(res.equity, res.fills)
    res.final = pf
    return res


def backtest(
    bot: Bot,
    bars: Union[Sequence[Candle], Mapping[str, Sequence[Candle]]],
    funding: Optional[Mapping[str, Sequence[FundingPoint]]] = None,
) -> BacktestResult:
    """同期版。1 銘柄なら足のリストをそのまま渡せる"""
    if not isinstance(bars, Mapping):
        bars = {bot.primary: bars}
    return asyncio.run(backtest_async(bot, bars, funding))
