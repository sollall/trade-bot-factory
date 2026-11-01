from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from tbf.core.bot import Bot, Context, Targets, normalize_targets
from tbf.core.portfolio import Portfolio, equity, weights
from tbf.core.types import Candle, FundingPoint, Mode


def run_strategy(
    bot: Bot,
    time: int,
    bars: Mapping[str, list[Candle]],
    funding: Mapping[str, list[FundingPoint]],
    pf: Portfolio,
    prices: Mapping[str, float],
    state: dict[str, Any],
    mode: Mode,
) -> tuple[Optional[Targets], Optional[str]]:
    """バックテスト・ペーパー・本番で共通の意思決定。state は戦略が書き換えうる"""
    ctx = Context(
        time=time,
        bars={a: list(bars.get(a, []))[-bot.lookback :] for a in bot.instruments},
        funding_history={a: list(funding.get(a, [])) for a in bot.instruments if bot.instruments[a].is_perp},
        holdings={a: p for a, p in pf.holdings.items()},
        weights=weights(pf, bot.instruments, prices),
        cash=pf.total_cash,
        equity=equity(pf, bot.instruments, prices),
        params=bot.params,
        state=state,
        mode=mode,
        instruments=dict(bot.instruments),
        primary=bot.primary,
        leverage=bot.leverage,
    )
    return normalize_targets(bot, bot.strategy(ctx))


def utc_day(time_ms: int) -> str:
    return datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
