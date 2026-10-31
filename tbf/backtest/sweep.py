"""パラメータの上書き・総当たり・評価指標"""

from __future__ import annotations

import itertools
import math
from typing import Any

from tbf.backtest.engine import BacktestResult
from tbf.core.bot import Bot

# --param / --sweep で戦略の params ではなく bot の設定として扱うキー
BOT_SETTINGS = {"leverage", "capital", "rebalance_band", "transfer_fee_usd", "rebalance_threshold", "cash_buffer"}

OBJECTIVES = ("sharpe", "return", "calmar")


def parse_value(raw: str) -> Any:
    for conv in (int, float):
        try:
            return conv(raw)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(raw.lower(), raw)


def parse_grid(specs: list[str]) -> list[dict[str, Any]]:
    """["a=1,2", "b=x,y"] -> 総当たりの組み合わせ"""
    keys = [kv.partition("=")[0] for kv in specs]
    vals = [[parse_value(v) for v in kv.partition("=")[2].split(",")] for kv in specs]
    return [dict(zip(keys, vs)) for vs in itertools.product(*vals)] or [{}]


def configure(bot: Bot, overrides: dict[str, Any]) -> Bot:
    settings = {k: float(v) for k, v in overrides.items() if k in BOT_SETTINGS}
    params = {k: v for k, v in overrides.items() if k not in BOT_SETTINGS}
    out = bot.with_params(**params) if params else bot
    return out.with_settings(**settings) if settings else out


def label(combo: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in combo.items())


def score(r: BacktestResult, objective: str) -> float:
    """大きいほど良い。取引がなく評価できないものは -inf"""
    m = r.metrics
    if len(r.equity) < 2:
        return -math.inf
    if objective == "return":
        return m.total_return
    if objective == "sharpe":
        return m.sharpe if m.sharpe != 0 or m.total_return != 0 else -math.inf
    if objective == "calmar":
        return r.apr / m.max_drawdown if m.max_drawdown > 0 else (math.inf if r.apr > 0 else -math.inf)
    raise ValueError(f"objective must be one of {OBJECTIVES}")
