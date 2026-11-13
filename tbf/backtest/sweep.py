"""パラメータの上書き・総当たり・評価指標"""

from __future__ import annotations

import itertools
import math
from typing import Any

from dataclasses import replace

from tbf.backtest.engine import BacktestResult
from tbf.core.bot import Bot

# --param / --sweep で戦略の params ではなく bot の設定として扱うキー
BOT_SETTINGS = {"leverage", "capital", "rebalance_band", "transfer_fee_usd", "rebalance_threshold", "cash_buffer"}

# instruments.<別名>.<項目> で銘柄の「執行の前提」を差し替える。
# 戦略パラメータではなく、どの会場にどう出すかの入れ替え (宣言済みの会場・執行グリッドを並べるため)。
INSTRUMENT_FIELDS = {"venue", "order_type", "unfilled"}

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


def _instrument_override(key: str) -> tuple[str, str] | None:
    parts = key.split(".")
    if len(parts) == 3 and parts[0] in ("instrument", "instruments") and parts[2] in INSTRUMENT_FIELDS:
        return parts[1], parts[2]
    return None


def apply_instruments(bot: Bot, overrides: dict[str, Any]) -> Bot:
    """instruments.<別名>.<項目> の上書きを銘柄に反映する。会場を変えたら手数料は引き直す"""
    by_alias: dict[str, dict[str, Any]] = {}
    for key, value in overrides.items():
        target = _instrument_override(key)
        if target:
            by_alias.setdefault(target[0], {})[target[1]] = value
    if not by_alias:
        return bot
    insts = dict(bot.instruments)
    for alias, fields in by_alias.items():
        if alias not in insts:
            raise ValueError(f"{bot.id}: unknown instrument alias {alias}")
        inst = insts[alias]
        # 会場が変わると手数料の既定値も変わるので、bot 側で固定していないかぎり引き直させる
        if fields.get("venue", inst.venue) != inst.venue and bot.fees is None:
            fields = {**fields, "fees": None}
        insts[alias] = replace(inst, **fields)
    return bot.with_settings(instruments=insts)


def configure(bot: Bot, overrides: dict[str, Any]) -> Bot:
    rest = {k: v for k, v in overrides.items() if not _instrument_override(k)}
    settings = {k: float(v) for k, v in rest.items() if k in BOT_SETTINGS}
    params = {k: v for k, v in rest.items() if k not in BOT_SETTINGS}
    out = bot.with_params(**params) if params else bot
    out = out.with_settings(**settings) if settings else out
    return apply_instruments(out, overrides)


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
