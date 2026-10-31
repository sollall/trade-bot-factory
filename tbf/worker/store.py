from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any, Optional

from tbf.core.bot import Bot
from tbf.core.portfolio import Portfolio, holding_value
from tbf.core.types import EquityPoint, Fill, Position
from tbf.worker.db import DB


@dataclass
class BotRow:
    bot_id: str
    strategy_state: str
    # 最後に戦略を評価した確定足の開始時刻 (ms)
    last_candle: int
    status: str  # running | halted
    halt_reason: Optional[str]
    peak_equity: float
    day: str
    day_start_equity: float
    consecutive_losses: int
    updated_at: int
    portfolio: Optional[str] = None
    targets: Optional[str] = None
    last_prices: Optional[str] = None
    last_funding: int = 0
    account_version: int = 0
    # 1 銘柄時代の列 (portfolio が空のときの移行用)
    cash: float = 0.0
    qty: float = 0.0
    avg_price: float = 0.0

    def load_portfolio(self, bot: Bot) -> Portfolio:
        if self.portfolio:
            return Portfolio.from_json(json.loads(self.portfolio))
        pf = Portfolio.initial(self.cash, bot.instruments[bot.primary].venue)
        if self.qty:
            pf.holdings[bot.primary] = Position(self.qty, self.avg_price)
        return pf

    def set_portfolio(self, pf: Portfolio) -> None:
        self.portfolio = json.dumps(pf.to_json())

    def prices(self) -> dict[str, float]:
        return json.loads(self.last_prices) if self.last_prices else {}

    def equity(self, bot: Bot, prices: Optional[dict[str, float]] = None) -> float:
        """現在の資産評価額。価格が分からない銘柄は建値で評価する"""
        pf = self.load_portfolio(bot)
        px = {**self.prices(), **(prices or {})}
        total = pf.total_cash
        for alias, pos in pf.holdings.items():
            inst = bot.instruments.get(alias)
            if inst is not None:
                total += holding_value(inst, pos, px.get(alias, pos.avg_price))
        return total

    def to_dict(self) -> dict[str, Any]:
        d = {f.name: getattr(self, f.name) for f in fields(self)}
        for k in ("portfolio", "targets", "last_prices", "strategy_state"):
            if d.get(k):
                d[k] = json.loads(d[k])
        for k in ("cash", "qty", "avg_price"):
            d.pop(k)
        return d


_COLS = {f.name for f in fields(BotRow)}


def _row(d: dict) -> BotRow:
    return BotRow(**{k: v for k, v in d.items() if k in _COLS})


class Store:
    def __init__(self, db: DB) -> None:
        self.db = db

    async def get_or_init(self, bot: Bot, now: int) -> BotRow:
        pf = Portfolio.initial(bot.capital, bot.instruments[bot.primary].venue)
        await self.db.execute(
            "INSERT OR IGNORE INTO bot_state (bot_id, cash, peak_equity, day_start_equity, updated_at, portfolio) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot.id, bot.capital, bot.capital, bot.capital, now, json.dumps(pf.to_json())),
        )
        row = await self.get(bot.id)
        assert row is not None
        return row

    async def get(self, bot_id: str) -> Optional[BotRow]:
        rows = await self.db.query("SELECT * FROM bot_state WHERE bot_id = ?", (bot_id,))
        return _row(rows[0]) if rows else None

    async def claim_candle(self, bot_id: str, candle: int) -> bool:
        """足 candle の処理権を取る。重複起動した cron が同じ足で二重発注しないためのガード"""
        n = await self.db.execute(
            "UPDATE bot_state SET last_candle = ? WHERE bot_id = ? AND last_candle < ?", (candle, bot_id, candle)
        )
        return n > 0

    async def save_monitor(self, r: BotRow) -> None:
        """監視用フィールドだけ更新 (口座は触らないので並行実行でも安全)"""
        await self.db.execute(
            "UPDATE bot_state SET peak_equity=?, day=?, day_start_equity=?, last_prices=?, updated_at=? WHERE bot_id=?",
            (r.peak_equity, r.day, r.day_start_equity, r.last_prices, r.updated_at, r.bot_id),
        )

    async def save_account(self, r: BotRow) -> bool:
        """口座と戦略状態を更新。読み込んだ後に他の実行が口座を変えていたら False (上書きしない)"""
        n = await self.db.execute(
            "UPDATE bot_state SET portfolio=?, targets=?, strategy_state=?, consecutive_losses=?, last_funding=?, "
            "updated_at=?, account_version=account_version+1 WHERE bot_id=? AND account_version=?",
            (r.portfolio, r.targets, r.strategy_state, r.consecutive_losses, r.last_funding, r.updated_at, r.bot_id, r.account_version),
        )
        if n > 0:
            r.account_version += 1
        return n > 0

    async def set_status(self, bot_id: str, status: str, reason: Optional[str]) -> None:
        await self.db.execute("UPDATE bot_state SET status=?, halt_reason=? WHERE bot_id=?", (status, reason, bot_id))

    async def resume(self, bot_id: str, equity: float, now: int) -> None:
        """再開。ピーク・日初・連敗数を現在値から数え直さないと即座にまた停止する"""
        await self.db.execute(
            "UPDATE bot_state SET status='running', halt_reason=NULL, peak_equity=?, day_start_equity=?, "
            "consecutive_losses=0, updated_at=? WHERE bot_id=?",
            (equity, equity, now, bot_id),
        )

    async def add_fill(self, bot_id: str, f: Fill, order_id: Optional[str], reason: Optional[str]) -> None:
        await self.db.execute(
            "INSERT INTO fills (bot_id, time, side, qty, price, fee, realized_pnl, order_id, reason, instrument, reduce) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (bot_id, f.time, f.side, f.qty, f.price, f.fee, f.realized_pnl, order_id, reason, f.instrument, int(f.reduce)),
        )

    async def add_equity(self, bot_id: str, p: EquityPoint) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO equity (bot_id, time, equity) VALUES (?,?,?)", (bot_id, p.time, p.equity)
        )

    async def add_event(self, bot_id: Optional[str], level: str, message: str, now: int) -> None:
        await self.db.execute(
            "INSERT INTO events (bot_id, time, level, message) VALUES (?,?,?,?)", (bot_id, now, level, message)
        )

    async def fills(self, bot_id: str, limit: int = 1000) -> list[Fill]:
        rows = await self.fill_rows(bot_id, limit)
        return [
            Fill(r["time"], r["side"], r["qty"], r["price"], r["fee"], r["realized_pnl"], r.get("instrument") or "", bool(r.get("reduce")))
            for r in rows
        ]

    async def fill_rows(self, bot_id: str, limit: int = 1000) -> list[dict]:
        """古い順 (直近 limit 件)"""
        return await self.db.query(
            "SELECT * FROM (SELECT * FROM fills WHERE bot_id=? ORDER BY time DESC, id DESC LIMIT ?) ORDER BY time, id",
            (bot_id, limit),
        )

    async def equity(self, bot_id: str, since: int = 0) -> list[EquityPoint]:
        rows = await self.db.query(
            "SELECT time, equity FROM equity WHERE bot_id=? AND time >= ? ORDER BY time", (bot_id, since)
        )
        return [EquityPoint(r["time"], r["equity"]) for r in rows]

    async def events(self, bot_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        if bot_id:
            return await self.db.query("SELECT * FROM events WHERE bot_id=? ORDER BY id DESC LIMIT ?", (bot_id, limit))
        return await self.db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
