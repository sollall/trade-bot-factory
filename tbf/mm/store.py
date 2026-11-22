"""気配と約定の記録 (sqlite)。記録したものが再生の入力になるので、古いデータは消さない"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tbf.mm.book import Book, Trade, trade_order

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (coin TEXT PRIMARY KEY, sz_decimals INTEGER NOT NULL, updated INTEGER NOT NULL);
-- time は取引所の時刻、recv は手元で受け取った時刻 (どちらも ms)。bids / asks は [[値段, 数量], ...] の JSON
CREATE TABLE IF NOT EXISTS books (
  coin TEXT NOT NULL, time INTEGER NOT NULL, recv INTEGER NOT NULL,
  best_bid REAL NOT NULL, best_ask REAL NOT NULL, bid_sz REAL NOT NULL, ask_sz REAL NOT NULL,
  bids TEXT NOT NULL, asks TEXT NOT NULL,
  PRIMARY KEY (coin, time)
);
-- side: 'B' = 買い手主導、'A' = 売り手主導
CREATE TABLE IF NOT EXISTS trades (
  coin TEXT NOT NULL, time INTEGER NOT NULL, tid INTEGER NOT NULL, side TEXT NOT NULL,
  px REAL NOT NULL, sz REAL NOT NULL, recv INTEGER NOT NULL,
  PRIMARY KEY (coin, time, tid)
);
-- 約定の取りこぼしの可能性。前回までに見た約定と今回の応答に重なりが無かった。
-- 抜けた件数は分からない。抜けたとすれば after より後、before より前 (どちらも取引所の時刻)
CREATE TABLE IF NOT EXISTS gaps (coin TEXT NOT NULL, time INTEGER NOT NULL, after INTEGER NOT NULL, before INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS gaps_coin ON gaps (coin, before);
CREATE TABLE IF NOT EXISTS polls (
  time INTEGER NOT NULL, coin TEXT NOT NULL, kind TEXT NOT NULL, latency_ms REAL, error TEXT, new_rows INTEGER
);
CREATE INDEX IF NOT EXISTS polls_time ON polls (time);
"""


class MmStore:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ---- 書き込み (record)

    def set_coin(self, coin: str, sz_decimals: int, now: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO coins VALUES (?,?,?)", (coin, sz_decimals, now))

    def add_book(self, coin: str, book: Book, recv: int) -> int:
        """同じ時刻の気配が既にあれば足さない。足した行数を返す"""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO books VALUES (?,?,?,?,?,?,?,?,?)",
            (coin, book.time, recv, book.best_bid, book.best_ask, book.bids[0][1], book.asks[0][1],
             json.dumps(book.bids), json.dumps(book.asks)),
        )
        return cur.rowcount

    def add_trades(self, coin: str, trades: list[Trade], recv: int) -> int:
        """既に記録した約定 (時刻と tid が同じ) は足さない。新しく足した件数を返す"""
        new = 0
        for t in trades:
            new += self.conn.execute(
                "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?)", (coin, t.time, t.tid, t.side, t.px, t.sz, recv)
            ).rowcount
        return new

    def add_gap(self, coin: str, time: int, after: int, before: int) -> None:
        self.conn.execute("INSERT INTO gaps VALUES (?,?,?,?)", (coin, time, after, before))

    def add_poll(self, time: int, coin: str, kind: str, latency_ms: float | None, error: str | None, new_rows: int | None) -> None:
        self.conn.execute("INSERT INTO polls VALUES (?,?,?,?,?,?)", (time, coin, kind, latency_ms, error, new_rows))

    def commit(self) -> None:
        self.conn.commit()

    # ---- 読み出し (report / replay)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def coins(self) -> list[str]:
        return [r[0] for r in self.query("SELECT DISTINCT coin FROM books ORDER BY coin")]

    def sz_decimals(self, coin: str) -> int | None:
        rows = self.query("SELECT sz_decimals FROM coins WHERE coin = ?", (coin,))
        return rows[0][0] if rows else None

    def last_time(self, coin: str | None = None) -> int:
        """記録の末尾 (取引所の時刻)。--hours はここから遡る"""
        if coin is None:
            return self.query("SELECT COALESCE(MAX(time), 0) FROM books")[0][0]
        return self.query("SELECT COALESCE(MAX(time), 0) FROM books WHERE coin = ?", (coin,))[0][0]

    def books(self, coin: str, since: int = 0) -> list[Book]:
        return [
            Book(r["time"], tuple((p, s) for p, s in json.loads(r["bids"])), tuple((p, s) for p, s in json.loads(r["asks"])))
            for r in self.query("SELECT time, bids, asks FROM books WHERE coin = ? AND time >= ? ORDER BY time", (coin, since))
        ]

    def trades(self, coin: str, since: int = 0) -> list[Trade]:
        rows = self.query("SELECT time, side, px, sz, tid FROM trades WHERE coin = ? AND time >= ?", (coin, since))
        return sorted((Trade(r["time"], r["side"], r["px"], r["sz"], r["tid"]) for r in rows), key=trade_order)

    def recent_trade_keys(self, coin: str, limit: int = 200) -> list[tuple[int, int]]:
        """直近の約定の (時刻, tid)。起動し直したときに、止まっていた間の取りこぼしを見つけるのに使う"""
        return [(r[0], r[1]) for r in self.query(
            "SELECT time, tid FROM trades WHERE coin = ? ORDER BY time DESC LIMIT ?", (coin, limit))]

    def gap_count(self, coin: str, since: int, until: int) -> int:
        return self.query(
            "SELECT COUNT(*) FROM gaps WHERE coin = ? AND before >= ? AND after <= ?", (coin, since, until))[0][0]
