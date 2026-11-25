"""気配と約定の記録 (sqlite)。記録したものが再生の入力になるので、古いデータは消さない"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Iterator

from tbf.mm.book import Book, Trade, trade_order
from tbf.mm.venues import Instrument, hyperliquid_instrument

FOREVER = 1 << 62
#: 逐次読み (iter_*) が 1 回の問い合わせで取る行数。メモリに載るのはこの件数ぶんだけ
CHUNK = 2000

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

-- ---- 会場。1 つの db には 1 つの会場だけを入れる (meta の venue)。meta が無い古い db は hyperliquid。
-- instruments は銘柄の決まりごと (刻み・最小数量・手数料など。Instrument を JSON で)。無い銘柄は coins から hyperliquid として読む
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instruments (coin TEXT PRIMARY KEY, spec TEXT NOT NULL, updated INTEGER NOT NULL);

-- ---- 実況の paper (tbf/mm/paper.py)。1 回の起動が 1 つの run。設定の違う実行を混ぜないため
CREATE TABLE IF NOT EXISTS paper_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started INTEGER NOT NULL, stopped INTEGER,
  coins TEXT NOT NULL, quoter TEXT NOT NULL, params TEXT NOT NULL, maker_rate REAL NOT NULL,
  latency_ms INTEGER NOT NULL, cross_fills INTEGER NOT NULL,
  interval REAL NOT NULL, trades_interval REAL NOT NULL, depth INTEGER NOT NULL
);
-- 疑似約定。side: 1 = 買い、-1 = 売り。kind: through = (a)、queue = (b)、cross = (c)。mid は約定の直前の気配のミッド
CREATE TABLE IF NOT EXISTS paper_fills (
  run INTEGER NOT NULL, coin TEXT NOT NULL, time INTEGER NOT NULL, side INTEGER NOT NULL,
  px REAL NOT NULL, size REAL NOT NULL, kind TEXT NOT NULL, fee REAL NOT NULL, mid REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS paper_fills_run ON paper_fills (run, coin, time);
-- 口座のスナップショット。1 分に 1 行 (その分の最後の状態で上書き)。time は最後に処理した気配の時刻、
-- first_book はこの run が最初に処理した気配の時刻。equity = cash + position * mid - fees (0 から始まる損益)。
-- late_* は replay と食い違いうる出来事の回数。stats はシミュレータの成績を丸ごと (JSON)
CREATE TABLE IF NOT EXISTS paper_account (
  run INTEGER NOT NULL, coin TEXT NOT NULL, minute INTEGER NOT NULL, time INTEGER NOT NULL, first_book INTEGER NOT NULL,
  position REAL NOT NULL, cash REAL NOT NULL, fees REAL NOT NULL, mid REAL NOT NULL, equity REAL NOT NULL,
  late_trades INTEGER NOT NULL, late_books INTEGER NOT NULL, stats TEXT NOT NULL,
  PRIMARY KEY (run, coin, minute)
);
-- run で使った銘柄ごとのメイカー手数料率 (会場の値をそのまま使うと銘柄で違う)。行が無い古い run は paper_runs.maker_rate
CREATE TABLE IF NOT EXISTS paper_run_fees (run INTEGER NOT NULL, coin TEXT NOT NULL, maker_rate REAL NOT NULL, PRIMARY KEY (run, coin));
"""


class MmStore:
    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        """readonly=True は読むだけ (report / replay / paper-report 用)。ファイルもテーブルも作らず、何も書かない。
        記録中の db に向けてよい: 読み取りのロックは 1 回の問い合わせの間だけで、大きい読み出しは iter_* が
        CHUNK 件ずつに切るので、記録側の commit を長く待たせない。ファイルが無ければ sqlite3.OperationalError"""
        self.readonly = readonly
        if readonly:
            self.conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
            self.conn.row_factory = sqlite3.Row
            # 古い db には後から足したテーブル (meta、instruments、paper_*) が無い。読むだけなので作らず、無いものとして読む
            self._tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def has_table(self, name: str) -> bool:
        """読み取り専用で開いた古い db には無いテーブルがある。書き込みで開いたときは必ず全部ある"""
        return not self.readonly or name in self._tables

    # ---- 書き込み (record)

    def set_coin(self, coin: str, sz_decimals: int, now: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO coins VALUES (?,?,?)", (coin, sz_decimals, now))

    def venue(self) -> str:
        """この db の会場。会場を残す前に作った db は hyperliquid"""
        rows = self.query("SELECT value FROM meta WHERE key = 'venue'") if self.has_table("meta") else []
        return rows[0][0] if rows else "hyperliquid"

    def source(self) -> str:
        """記録の取り方: "rest" (ポーリング) か "ws" (配信)。残す前に作った db は rest。
        再生の仮定の一部は取り方で変わる (tbf/mm/replay.py)"""
        rows = self.query("SELECT value FROM meta WHERE key = 'source'") if self.has_table("meta") else []
        return rows[0][0] if rows else "rest"

    def claim_venue(self, venue: str, source: str = "rest") -> None:
        """この db に venue の記録を足してよいか確かめ、会場と取り方を残す。違う会場・違う取り方の db なら ValueError"""
        rows = self.query("SELECT value FROM meta WHERE key = 'venue'")
        has_books = bool(self.query("SELECT 1 FROM books LIMIT 1"))
        current = rows[0][0] if rows else ("hyperliquid" if has_books else None)
        if current is not None and current != venue:
            raise ValueError(
                f"この db は {current} の記録です。{venue} の記録は別のファイルに取ってください (--db data/mm_{venue}.sqlite など)")
        if has_books and self.source() != source:
            names = {"rest": "ポーリング (rest)", "ws": "配信 (ws)"}
            raise ValueError(
                f"この db は {names[self.source()]} の記録です。{names[source]} の記録は別のファイルに取ってください "
                "(取り方で再生の仮定が変わるので混ぜない)")
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('venue', ?)", (venue,))
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('source', ?)", (source,))

    def set_instrument(self, inst: Instrument, now: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO instruments VALUES (?,?,?)", (inst.coin, json.dumps(dataclasses.asdict(inst)), now))
        if inst.sz_decimals is not None:
            self.set_coin(inst.coin, inst.sz_decimals, now)  # 前の版のコードでも読めるように

    def instrument(self, coin: str) -> Instrument | None:
        rows = self.query("SELECT spec FROM instruments WHERE coin = ?", (coin,)) if self.has_table("instruments") else []
        if rows:
            return Instrument(**json.loads(rows[0][0]))
        d = self.sz_decimals(coin)
        return None if d is None else hyperliquid_instrument(coin, d)

    def add_book(self, coin: str, book: Book, recv: int) -> int:
        """同じ時刻の気配が既にあれば足さない。足した行数を返す"""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO books VALUES (?,?,?,?,?,?,?,?,?)",
            (coin, book.time, recv, book.best_bid, book.best_ask, book.bids[0][1], book.asks[0][1],
             json.dumps(book.bids), json.dumps(book.asks)),
        )
        return cur.rowcount

    def add_trades(self, coin: str, trades: list[Trade], recv: int) -> list[Trade]:
        """既に記録した約定 (時刻と tid が同じ) は足さない。新しく足したものを返す"""
        return [
            t for t in trades
            if self.conn.execute(
                "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?)", (coin, t.time, t.tid, t.side, t.px, t.sz, recv)
            ).rowcount
        ]

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

    def first_time(self, coin: str | None = None) -> int:
        """記録の先頭 (取引所の時刻)。--since / --until の `+24h` はここから数える。記録が無ければ 0"""
        if coin is None:
            return min((self.first_time(c) for c in self.coins()), default=0)
        return self.query("SELECT COALESCE(MIN(time), 0) FROM books WHERE coin = ?", (coin,))[0][0]

    def iter_books(self, coin: str, since: int = 0, until: int = FOREVER, chunk: int = CHUNK) -> Iterator[Book]:
        """books() と同じものを時刻順に 1 件ずつ。chunk 件ごとに問い合わせを切るので、気配が何百万件あっても
        メモリに載るのは chunk 件だけで、読み取りのロックも持ち続けない (記録中の db を読める)"""
        after = since - 1
        while True:
            rows = self.query(
                "SELECT time, bids, asks FROM books WHERE coin = ? AND time > ? AND time <= ? ORDER BY time LIMIT ?",
                (coin, after, until, chunk))
            for r in rows:
                yield Book(r[0], tuple(map(tuple, json.loads(r[1]))), tuple(map(tuple, json.loads(r[2]))))
            if len(rows) < chunk:
                return
            after = rows[-1][0]

    def iter_tops(self, coin: str, since: int = 0, until: int = FOREVER, chunk: int = CHUNK) -> Iterator[tuple]:
        """最良気配だけを時刻順に: (time, recv, best_bid, best_ask, bid_sz, ask_sz)。板の JSON を読まないので速い (report 用)"""
        after = since - 1
        while True:
            rows = self.query(
                "SELECT time, recv, best_bid, best_ask, bid_sz, ask_sz FROM books WHERE coin = ? AND time > ? AND time <= ? "
                "ORDER BY time LIMIT ?", (coin, after, until, chunk))
            yield from map(tuple, rows)
            if len(rows) < chunk:
                return
            after = rows[-1][0]

    def iter_trades(self, coin: str, since: int = 0, until: int = FOREVER, chunk: int = CHUNK) -> Iterator[Trade]:
        """約定を時刻順に 1 件ずつ。同じ時刻の中は tid の順で、trades() の並び (trade_order) とは違う。
        再生は Sim.step が気配の間ごとに並べ直すので、時刻の順だけ合っていれば足りる"""
        after = (since - 1, (1 << 63) - 1)  # (時刻, tid) の続きから。最初は since 以降の全部
        while True:
            rows = self.query(
                "SELECT time, side, px, sz, tid FROM trades WHERE coin = ? AND (time, tid) > (?, ?) AND time <= ? "
                "ORDER BY time, tid LIMIT ?", (coin, *after, until, chunk))
            for r in rows:
                yield Trade(r[0], r[1], r[2], r[3], r[4])
            if len(rows) < chunk:
                return
            after = (rows[-1][0], rows[-1][4])

    def books(self, coin: str, since: int = 0, until: int = FOREVER) -> list[Book]:
        return [
            Book(r["time"], tuple((p, s) for p, s in json.loads(r["bids"])), tuple((p, s) for p, s in json.loads(r["asks"])))
            for r in self.query(
                "SELECT time, bids, asks FROM books WHERE coin = ? AND time >= ? AND time <= ? ORDER BY time", (coin, since, until))
        ]

    def trades(self, coin: str, since: int = 0, until: int = FOREVER) -> list[Trade]:
        rows = self.query(
            "SELECT time, side, px, sz, tid FROM trades WHERE coin = ? AND time >= ? AND time <= ?", (coin, since, until))
        return sorted((Trade(r["time"], r["side"], r["px"], r["sz"], r["tid"]) for r in rows), key=trade_order)

    def recent_trade_keys(self, coin: str, limit: int = 200) -> list[tuple[int, int]]:
        """直近の約定の (時刻, tid)。起動し直したときに、止まっていた間の取りこぼしを見つけるのに使う"""
        return [(r[0], r[1]) for r in self.query(
            "SELECT time, tid FROM trades WHERE coin = ? ORDER BY time DESC LIMIT ?", (coin, limit))]

    def gap_count(self, coin: str, since: int, until: int) -> int:
        return self.query(
            "SELECT COUNT(*) FROM gaps WHERE coin = ? AND before >= ? AND after <= ?", (coin, since, until))[0][0]
