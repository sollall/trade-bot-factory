from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from tbf.core.bot import Bot, Context, normalize
from tbf.core.portfolio import Portfolio, order_qty_for_target
from tbf.core.types import Candle, Mode


@dataclass(frozen=True)
class StepResult:
    # + 買い / - 売り / 0 なし (base 数量)
    qty: float
    target_exposure: Optional[float]
    reason: Optional[str] = None


def run_strategy(
    bot: Bot,
    candles: Sequence[Candle],
    portfolio: Portfolio,
    price: float,
    state: dict[str, Any],
    mode: Mode,
) -> StepResult:
    """バックテスト・ペーパー・本番で共通の意思決定。state は戦略が書き換えうる"""
    window = list(candles[-bot.lookback :])
    mark = window[-1].close if window else price
    ctx = Context(
        candles=window,
        position=portfolio.position,
        cash=portfolio.cash,
        equity=portfolio.equity(mark),
        params=bot.params,
        state=state,
        mode=mode,
    )
    decision = normalize(bot.strategy(ctx))
    if decision is None:
        return StepResult(0.0, None)
    qty = order_qty_for_target(
        portfolio, price, decision.target_exposure, bot.rebalance_threshold, bot.fees.cost_rate
    )
    return StepResult(qty, decision.target_exposure, decision.reason)


def utc_day(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
