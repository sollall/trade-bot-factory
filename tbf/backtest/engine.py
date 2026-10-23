from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from tbf.core.bot import Bot
from tbf.core.metrics import Metrics, compute_metrics
from tbf.core.portfolio import Portfolio, apply_fill, simulate_fill
from tbf.core.risk import check_risk, next_consecutive_losses
from tbf.core.step import run_strategy, utc_day
from tbf.core.types import Candle, EquityPoint, Fill


@dataclass
class Halt:
    time: int
    reason: str


@dataclass
class BacktestResult:
    bot_id: str
    metrics: Metrics
    equity: list[EquityPoint]
    fills: list[Fill]
    halted: Optional[Halt]
    final: Portfolio


def backtest(bot: Bot, candles: Sequence[Candle]) -> BacktestResult:
    """
    足 i の確定時に戦略を評価し、足 i+1 の始値で約定させる (先読みなし)。
    本番と同じリスク判定を適用し、違反したら以降の売買を止める。
    """
    p = Portfolio(bot.capital)
    state: dict = {}
    equity: list[EquityPoint] = []
    fills: list[Fill] = []
    pending = 0.0
    halted: Optional[Halt] = None
    day = ""
    day_start = peak = bot.capital
    losses = 0

    for i, c in enumerate(candles):
        if pending:
            side = "buy" if pending > 0 else "sell"
            sim = simulate_fill(side, abs(pending), c.open, c.time, bot.fees.taker_rate, bot.fees.slippage_bps)
            p, fill = apply_fill(p, sim)
            fills.append(fill)
            losses = next_consecutive_losses(losses, fill)
            pending = 0.0

        eq = p.equity(c.close)
        d = utc_day(c.time)
        if d != day:
            day = d
            day_start = equity[-1].equity if equity else eq
        equity.append(EquityPoint(c.time, eq))
        peak = max(peak, eq)

        if halted:
            continue
        breach = check_risk(
            bot.risk, equity=eq, peak_equity=peak, day_start_equity=day_start, consecutive_losses=losses
        )
        if breach:
            halted = Halt(c.time, breach)
            if bot.risk.flatten_on_halt and p.position.qty > 0:
                pending = -p.position.qty
            continue

        if i + 1 < bot.lookback:
            continue
        step = run_strategy(bot, candles[max(0, i + 1 - bot.lookback) : i + 1], p, c.close, state, "backtest")
        pending = step.qty

    return BacktestResult(bot.id, compute_metrics(equity, fills), equity, fills, halted, p)
