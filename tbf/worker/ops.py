"""運用のための読み取り専用の集計: ヘルスチェック・Prometheus メトリクス・管理外のポジション検出"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Mapping, Optional, Sequence

from tbf.core.bot import Bot
from tbf.core.types import TIMEFRAME_MS
from tbf.worker.store import BotRow

# cron は毎分。これより長く完了記録が更新されなければ止まっているとみなす
STALE_MS = 3 * 60_000


def open_positions(row: Mapping[str, Any]) -> dict[str, float]:
    """bot_state の 1 行から、0 でない持ち高 {別名: 数量} を取り出す (1 銘柄時代の列にも対応)"""
    raw = row.get("portfolio")
    if raw:
        pf = json.loads(raw) if isinstance(raw, str) else raw
        return {a: float(q) for a, (q, _p) in (pf.get("positions") or {}).items() if float(q) != 0}
    qty = float(row.get("qty") or 0)
    return {"(primary)": qty} if qty else {}


def unmanaged_positions(rows: Sequence[Mapping[str, Any]], managed_ids: Sequence[str]) -> dict[str, dict[str, float]]:
    """今のデプロイに含まれないのに持ち高が残っている bot。
    bot を消したり deploy から外したりすると、その bot の持ち高は誰も見ていない状態で取引所に残る"""
    managed = set(managed_ids)
    out = {}
    for r in rows:
        if r["bot_id"] in managed:
            continue
        pos = open_positions(r)
        if pos:
            out[r["bot_id"]] = pos
    return out


def unknown_instruments(rows: Sequence[Mapping[str, Any]], bots: Sequence[Bot]) -> dict[str, dict[str, float]]:
    """デプロイする bot の持ち高のうち、新しい定義の instruments にない別名 (別名の変更・銘柄の削除で取り残される)"""
    by_id = {b.id: b for b in bots}
    out = {}
    for r in rows:
        bot = by_id.get(r["bot_id"])
        if bot is None or not r.get("portfolio"):
            continue
        lost = {a: q for a, q in open_positions(r).items() if a not in bot.instruments}
        if lost:
            out[bot.id] = lost
    return out


def health(heartbeat: Optional[dict], now: int, bots: int, build: str) -> dict[str, Any]:
    if not heartbeat:
        return {"ok": False, "reason": "no tick recorded yet", "build": build, "bots": bots}
    age = now - int(heartbeat["time"])
    ok = age <= STALE_MS
    out = {
        "ok": ok,
        "build": build,
        "last_tick_build": heartbeat["build"],
        "last_tick": int(heartbeat["time"]),
        "age_sec": round(age / 1000, 1),
        "bots": bots,
        "last_tick_errors": int(heartbeat["errors"]),
    }
    if not ok:
        out["reason"] = f"last tick {age // 1000}s ago"
    return out


# ---- Prometheus text format ----


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _Metrics:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.families: dict[str, tuple[str, list[str]]] = {}

    def add(self, name: str, help_: str, value: float, **labels: str) -> None:
        fam = self.families.setdefault(name, (help_, []))
        lab = {"mode": self.mode, **labels}
        ls = ",".join(f'{k}="{_esc(str(v))}"' for k, v in lab.items())
        fam[1].append(f"{name}{{{ls}}} {float(value):.10g}")

    def render(self) -> str:
        lines = []
        for name, (help_, samples) in self.families.items():
            lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge", *samples]
        return "\n".join(lines) + "\n"


def render_metrics(
    mode: str,
    build: str,
    now: int,
    bots: Sequence[Bot],
    rows: Sequence[BotRow],
    heartbeat: Optional[dict],
    errors_1h: Mapping[str, int],
) -> str:
    m = _Metrics(mode)
    m.add("tbf_build_info", "Deployed build (commit)", 1, build=build)
    m.add("tbf_bots", "Bots deployed to this worker", len(bots))
    if heartbeat:
        m.add("tbf_heartbeat_timestamp_seconds", "Last successful cron tick", int(heartbeat["time"]) / 1000)
        m.add("tbf_heartbeat_age_seconds", "Seconds since last successful cron tick", (now - int(heartbeat["time"])) / 1000)
        m.add("tbf_last_tick_errors", "Bots that errored in the last tick", int(heartbeat["errors"]))
        m.add("tbf_last_tick_duration_seconds", "Duration of the last tick", int(heartbeat["duration_ms"]) / 1000)

    by_id = {r.bot_id: r for r in rows}
    for bot in bots:
        r = by_id.get(bot.id)
        m.add("tbf_bot_errors_1h", "Error events in the last hour", errors_1h.get(bot.id, 0), bot=bot.id)
        if r is None:
            continue
        eq = r.equity(bot)
        m.add("tbf_bot_running", "1 if running, 0 if halted", 1 if r.status == "running" else 0, bot=bot.id)
        m.add("tbf_bot_capital_usd", "Allocated capital", bot.capital, bot=bot.id)
        m.add("tbf_bot_equity_usd", "Current equity at last known prices", eq, bot=bot.id)
        m.add("tbf_bot_peak_equity_usd", "Peak equity", r.peak_equity, bot=bot.id)
        m.add("tbf_bot_drawdown_ratio", "Drawdown from peak (0-1)", 1 - eq / r.peak_equity if r.peak_equity > 0 else 0, bot=bot.id)
        m.add("tbf_bot_return_ratio", "Return on capital", eq / bot.capital - 1 if bot.capital else 0, bot=bot.id)
        m.add(
            "tbf_bot_daily_return_ratio", "Return since UTC day start",
            eq / r.day_start_equity - 1 if r.day_start_equity > 0 else 0, bot=bot.id,
        )
        m.add("tbf_bot_consecutive_losses", "Consecutive losing closes", r.consecutive_losses, bot=bot.id)
        m.add("tbf_bot_updated_timestamp_seconds", "Last monitor update", r.updated_at / 1000, bot=bot.id)
        m.add("tbf_bot_timeframe_seconds", "Candle length", TIMEFRAME_MS[bot.timeframe] / 1000, bot=bot.id)
        if r.last_candle:
            m.add("tbf_bot_last_candle_timestamp_seconds", "Last candle the strategy evaluated", r.last_candle / 1000, bot=bot.id)
        for alias, pos in r.load_portfolio(bot).holdings.items():
            if pos.qty:
                m.add("tbf_bot_position_qty", "Position size", pos.qty, bot=bot.id, instrument=str(bot.instruments.get(alias, alias)))

    for bot_id, pos in unmanaged_positions([asdict(r) for r in rows], [b.id for b in bots]).items():
        for alias, qty in pos.items():
            m.add("tbf_unmanaged_position_qty", "Position left by a bot that is no longer deployed", qty, bot=bot_id, instrument=alias)
    return m.render()
