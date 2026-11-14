"""シャドーモードの記録 (sqlite)。`tbf/arb/store.py` と同じ考え方。

**書くのは読み取った市場の様子だけ。**注文は出さないので、注文・残高の表は無い。
`polls` は arb と同じ (稼働性の測定用)。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
-- bitbank の最良気配の 1 秒標本
CREATE TABLE IF NOT EXISTS book (
  time INTEGER NOT NULL,          -- 取得時刻 (ms, こちらの時計)
  venue_time INTEGER,             -- 板の timestamp (ms, 会場の時計)
  pair TEXT NOT NULL,
  bid REAL NOT NULL, bid_size REAL NOT NULL,
  ask REAL NOT NULL, ask_size REAL NOT NULL,
  sequence_id TEXT,
  PRIMARY KEY (pair, time)
);
CREATE INDEX IF NOT EXISTS book_time ON book (time);

-- 約定テープ。transaction_id で重複排除するので、取りこぼし以外は欠けない
CREATE TABLE IF NOT EXISTS trades (
  transaction_id INTEGER NOT NULL, pair TEXT NOT NULL,
  time INTEGER NOT NULL,          -- executed_at (ms)
  side TEXT NOT NULL,             -- bitbank の side (テイカーの向きとして扱う: declared.TAPE_SIDE_IS_TAKER)
  price REAL NOT NULL, amount REAL NOT NULL,
  PRIMARY KEY (pair, transaction_id)
);
CREATE INDEX IF NOT EXISTS trades_time ON trades (pair, time);

-- Hyperliquid perp の板 (ヘッジ先)。levels は上から declared.HL_DEPTH_LEVELS 段ぶんの JSON
CREATE TABLE IF NOT EXISTS hl_book (
  time INTEGER NOT NULL,          -- 取得時刻 (ms, こちらの時計)
  venue_time INTEGER,             -- 会場の time (ms)
  coin TEXT NOT NULL,
  bid REAL NOT NULL, ask REAL NOT NULL,
  bids_json TEXT NOT NULL, asks_json TEXT NOT NULL,
  PRIMARY KEY (coin, time)
);
CREATE INDEX IF NOT EXISTS hl_book_time ON hl_book (time);

-- 稼働性: API ごとのレイテンシ・エラー・レート制限
CREATE TABLE IF NOT EXISTS polls (
  time INTEGER NOT NULL, venue TEXT NOT NULL, endpoint TEXT NOT NULL,
  latency_ms REAL, status INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS polls_time ON polls (time);

-- テープの取りこぼしの疑い。`transactions` が最新 60 件しか返さないので、
-- 返ってきた全件が未知だった場合は「その手前が落ちた可能性がある」ことだけ記録する。
-- **ゼロ埋めはしない。**レポートでこの区間を「未測定」として列挙するために使う。
CREATE TABLE IF NOT EXISTS tape_overflow (
  time INTEGER NOT NULL, pair TEXT NOT NULL, n INTEGER NOT NULL,
  oldest_time INTEGER NOT NULL, prev_latest_time INTEGER
);

-- 収集プロセスの起動・停止 (記録の穴をあとから特定するため)
CREATE TABLE IF NOT EXISTS runs (
  time INTEGER NOT NULL, event TEXT NOT NULL, detail TEXT
);
"""


class JpMakerStore:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        # 1 秒標本を 7 日ぶん書くので、同期を緩めて書き込みを軽くする (落ちても最後の数秒だけ)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")

    # --- 書き込み ---
    def add_poll(
        self, time: int, venue: str, endpoint: str, latency_ms: float | None, status: int | None, error: str | None
    ) -> None:
        self.conn.execute("INSERT INTO polls VALUES (?,?,?,?,?,?)", (time, venue, endpoint, latency_ms, status, error))

    def add_book(
        self,
        time: int,
        venue_time: int | None,
        pair: str,
        bid: float,
        bid_size: float,
        ask: float,
        ask_size: float,
        sequence_id: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO book VALUES (?,?,?,?,?,?,?,?)",
            (time, venue_time, pair, bid, bid_size, ask, ask_size, sequence_id),
        )

    def add_trades(self, pair: str, trades: list[dict]) -> int:
        """返り値は **新規に入った件数** (重複排除後)"""
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?)",
            [
                (
                    int(t["transaction_id"]),
                    pair,
                    int(t["executed_at"]),
                    str(t["side"]),
                    float(t["price"]),
                    float(t["amount"]),
                )
                for t in trades
            ],
        )
        return self.conn.total_changes - before

    def add_hl_book(
        self, time: int, venue_time: int | None, coin: str, bids: list[list], asks: list[list]
    ) -> None:
        if not bids or not asks:
            return
        self.conn.execute(
            "INSERT OR IGNORE INTO hl_book VALUES (?,?,?,?,?,?,?)",
            (time, venue_time, coin, float(bids[0][0]), float(asks[0][0]), json.dumps(bids), json.dumps(asks)),
        )

    def add_tape_overflow(self, time: int, pair: str, n: int, oldest_time: int, prev_latest_time: int | None) -> None:
        self.conn.execute("INSERT INTO tape_overflow VALUES (?,?,?,?,?)", (time, pair, n, oldest_time, prev_latest_time))

    def add_run_event(self, time: int, event: str, detail: str | None = None) -> None:
        self.conn.execute("INSERT INTO runs VALUES (?,?,?)", (time, event, detail))
        self.conn.commit()

    def latest_trade_time(self, pair: str) -> int | None:
        row = self.conn.execute("SELECT MAX(time) t FROM trades WHERE pair=?", (pair,)).fetchone()
        return row["t"] if row and row["t"] is not None else None

    def commit(self) -> None:
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()
