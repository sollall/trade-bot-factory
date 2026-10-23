from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Optional

from tbf.core.portfolio import Portfolio
from tbf.core.types import EquityPoint, Fill, Position
from tbf.worker.db import DB


@dataclass
class BotRow:
    bot_id: str
    cash: float
    qty: float
    avg_price: float
    strategy_state: str
    # 最後に戦略を評価した確定足の開始時刻 (ms)
    last_candle: int
    status: str  # running | halted
    halt_reason: Optional[str]
    peak_equity: float
    day: str
    day_start_equity: float
    consecutive_losses: int
    last_price: Optional[float]
    updated_at: int

    @property
    def portfolio(self) -> Portfolio:
        return Portfolio(self.cash, Position(self.qty, self.avg_price))

    def set_portfolio(self, p: Portfolio) -> None:
        self.cash, self.qty, self.avg_price = p.cash, p.position.qty, p.position.avg_price

    def equity(self, price: Optional[float] = None) -> float:
        return self.portfolio.equity(price if price is not None else (self.last_price or 0.0))

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


_COLS = {f.name for f in fields(BotRow)}


def _row(d: dict) -> BotRow:
    return BotRow(**{k: v for k, v in d.items() if k in _COLS})


class Store:
    def __init__(self, db: DB) -> None:
        self.db = db

    async def get_or_init(self, bot_id: str, capital: float, now: int) -> BotRow:
        await self.db.execute(
            "INSERT OR IGNORE INTO bot_state (bot_id, cash, peak_equity, day_start_equity, updated_at) VALUES (?, ?, ?, ?, ?)",
            (bot_id, capital, capital, capital, now),
        )
        row = await self.get(bot_id)
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
            "UPDATE bot_state SET peak_equity=?, day=?, day_start_equity=?, last_price=?, updated_at=? WHERE bot_id=?",
            (r.peak_equity, r.day, r.day_start_equity, r.last_price, r.updated_at, r.bot_id),
        )

    async def save_account(self, r: BotRow) -> None:
        """口座と戦略状態を更新。claim_candle で処理権を取った実行 (または停止処理) だけが呼ぶ"""
        await self.db.execute(
            "UPDATE bot_state SET cash=?, qty=?, avg_price=?, strategy_state=?, consecutive_losses=?, updated_at=? WHERE bot_id=?",
            (r.cash, r.qty, r.avg_price, r.strategy_state, r.consecutive_losses, r.updated_at, r.bot_id),
        )

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
            "INSERT INTO fills (bot_id, time, side, qty, price, fee, realized_pnl, order_id, reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (bot_id, f.time, f.side, f.qty, f.price, f.fee, f.realized_pnl, order_id, reason),
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
        return [Fill(r["time"], r["side"], r["qty"], r["price"], r["fee"], r["realized_pnl"]) for r in rows]

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
