"""シャドーモードの記録 (sqlite)"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tbf.arb.opportunity import Episode, Opportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
  time INTEGER NOT NULL, sell_chain TEXT NOT NULL, buy_chain TEXT NOT NULL, size REAL NOT NULL,
  sell_out REAL NOT NULL, buy_in REAL NOT NULL, gross_usd REAL NOT NULL, cost_usd REAL NOT NULL, net_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS opportunities_time ON opportunities (time);
CREATE TABLE IF NOT EXISTS episodes (
  sell_chain TEXT NOT NULL, buy_chain TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
  polls INTEGER NOT NULL, first_net_usd REAL NOT NULL, max_net_usd REAL NOT NULL, best_size REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS episodes_start ON episodes (start);
-- 1 分ごとの価格差 (bps) の集計。機会がなくても市場の様子が分かるように
CREATE TABLE IF NOT EXISTS spreads (
  minute INTEGER NOT NULL, sell_chain TEXT NOT NULL, buy_chain TEXT NOT NULL,
  n INTEGER NOT NULL, min_bps REAL NOT NULL, max_bps REAL NOT NULL, sum_bps REAL NOT NULL,
  PRIMARY KEY (minute, sell_chain, buy_chain)
);
CREATE TABLE IF NOT EXISTS polls (
  time INTEGER NOT NULL, chain TEXT NOT NULL, block INTEGER, latency_ms REAL, error TEXT
);
CREATE INDEX IF NOT EXISTS polls_time ON polls (time);
"""


class ArbStore:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add_poll(self, time: int, chain: str, block: int | None, latency_ms: float | None, error: str | None) -> None:
        self.conn.execute("INSERT INTO polls VALUES (?,?,?,?,?)", (time, chain, block, latency_ms, error))

    def add_opportunity(self, time: int, o: Opportunity) -> None:
        self.conn.execute(
            "INSERT INTO opportunities VALUES (?,?,?,?,?,?,?,?,?)",
            (time, o.sell_chain, o.buy_chain, o.size, o.sell_out, o.buy_in, o.gross_usd, o.cost_usd, o.net_usd),
        )

    def add_episode(self, e: Episode) -> None:
        self.conn.execute(
            "INSERT INTO episodes VALUES (?,?,?,?,?,?,?,?)",
            (e.sell_chain, e.buy_chain, e.start, e.end, e.polls, e.first_net_usd, e.max_net_usd, e.best_size),
        )

    def add_spread(self, time: int, o: Opportunity) -> None:
        """最小サイズでの粗利の価格差 (bps)"""
        bps = o.gross_usd / o.sell_out * 10_000 if o.sell_out else 0.0
        self.conn.execute(
            """INSERT INTO spreads VALUES (?,?,?,1,?,?,?)
               ON CONFLICT (minute, sell_chain, buy_chain) DO UPDATE SET
               n = n + 1, min_bps = MIN(min_bps, excluded.min_bps), max_bps = MAX(max_bps, excluded.max_bps),
               sum_bps = sum_bps + excluded.sum_bps""",
            (time // 60_000 * 60_000, o.sell_chain, o.buy_chain, bps, bps, bps),
        )

    def commit(self) -> None:
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def prune(self, before: int) -> None:
        """古い生データを消す (エピソードと 1 分集計は残す)"""
        self.conn.execute("DELETE FROM polls WHERE time < ?", (before,))
        self.conn.execute("DELETE FROM opportunities WHERE time < ?", (before,))
        self.conn.commit()
