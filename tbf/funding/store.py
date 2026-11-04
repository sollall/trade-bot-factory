"""シャドーモードの記録 (sqlite)

生の時系列を全部持つと 1 週間で数百万行になるので、バケット集計を主に残す。
net_apr(hold) は gross と往復コストから後で導けるので、hold_days ごとの列は持たない。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tbf.funding.spread import DepthMiss, Episode, Spread

SCHEMA = """
-- 稼働性: ポーリングごとの成否・遅延・レート制限到達
CREATE TABLE IF NOT EXISTS polls (
  time INTEGER NOT NULL, venue TEXT NOT NULL, ok INTEGER NOT NULL,
  calls INTEGER NOT NULL, latency_ms REAL, rate_limited INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS polls_time ON polls (time);

-- 会場・銘柄ごとのファンディング (年率) のバケット集計。neg_n は年率が負だった回数 = 符号反転の材料
CREATE TABLE IF NOT EXISTS funding_buckets (
  bucket INTEGER NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL,
  n INTEGER NOT NULL, sum_apr REAL NOT NULL, min_apr REAL NOT NULL, max_apr REAL NOT NULL,
  neg_n INTEGER NOT NULL, last_apr REAL NOT NULL, last_time INTEGER NOT NULL,
  PRIMARY KEY (bucket, venue, symbol)
);

-- 会場ペア × 銘柄 × サイズ の実効スプレッドのバケット集計
CREATE TABLE IF NOT EXISTS spread_buckets (
  bucket INTEGER NOT NULL, short_venue TEXT NOT NULL, long_venue TEXT NOT NULL,
  symbol TEXT NOT NULL, size_usd REAL NOT NULL, n INTEGER NOT NULL,
  sum_gross REAL NOT NULL, min_gross REAL NOT NULL, max_gross REAL NOT NULL,
  sum_cost_bps REAL NOT NULL, min_cost_bps REAL NOT NULL, max_cost_bps REAL NOT NULL,
  PRIMARY KEY (bucket, short_venue, long_venue, symbol, size_usd)
);

-- net_apr が閾値以上だった連続区間
CREATE TABLE IF NOT EXISTS episodes (
  short_venue TEXT NOT NULL, long_venue TEXT NOT NULL, symbol TEXT NOT NULL, size_usd REAL NOT NULL,
  start INTEGER NOT NULL, end INTEGER NOT NULL, polls INTEGER NOT NULL,
  first_net_apr REAL NOT NULL, max_net_apr REAL NOT NULL, max_gross_apr REAL NOT NULL,
  min_breakeven_days REAL
);
CREATE INDEX IF NOT EXISTS episodes_start ON episodes (start);

-- そのサイズを板で約定できなかった回数 (サイズを上げると取れなくなる、の証拠)
CREATE TABLE IF NOT EXISTS depth_misses (
  bucket INTEGER NOT NULL, venue TEXT NOT NULL, symbol TEXT NOT NULL, size_usd REAL NOT NULL,
  n INTEGER NOT NULL, PRIMARY KEY (bucket, venue, symbol, size_usd)
);

-- 確定済みファンディングの履歴 (backfill)。rate は「ロングが払うとき +」に正規化済み。
-- hours はその区間の長さ (Paradex は連続課金なのでサンプル間隔が入る)。
CREATE TABLE IF NOT EXISTS funding_history (
  venue TEXT NOT NULL, symbol TEXT NOT NULL, time INTEGER NOT NULL,
  hours REAL NOT NULL, rate REAL NOT NULL, mark REAL,
  PRIMARY KEY (venue, symbol, time)
);
CREATE INDEX IF NOT EXISTS funding_history_time ON funding_history (time);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# 計測中の DB に後から足す列。計測を midway でやり直したくないので追加のみ
# (arb / worker 側の migration と同じ方針)。既に回っている sqlite にも安全に当たる。
ADDED_COLUMNS = {
    "spread_buckets": [
        ("sum_basis_bps", "REAL NOT NULL DEFAULT 0"),
        ("min_basis_bps", "REAL NOT NULL DEFAULT 0"),
        ("max_basis_bps", "REAL NOT NULL DEFAULT 0"),
        # 2 会場のスナップショットの取得時刻の差 (ms)。これが大きいと basis は
        # 「時刻のずれ」を測っていることになるので、必ず併記する
        ("sum_lag_ms", "REAL NOT NULL DEFAULT 0"),
        ("max_lag_ms", "REAL NOT NULL DEFAULT 0"),
    ],
    "episodes": [
        # 片方の会場のデータが古くて計算から外れていた回数 (閉じずに保留した区間)
        ("pending_polls", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


class FundingStore:
    def __init__(self, path: str | Path, bucket_sec: float = 300.0) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.bucket_ms = int(bucket_sec * 1000)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        for table, columns in ADDED_COLUMNS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def bucket(self, time: int) -> int:
        return time // self.bucket_ms * self.bucket_ms

    # ---- 書き込み

    def add_poll(
        self,
        time: int,
        venue: str,
        ok: bool,
        calls: int,
        latency_ms: float | None,
        rate_limited: bool,
        error: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO polls VALUES (?,?,?,?,?,?,?)",
            (time, venue, int(ok), calls, latency_ms, int(rate_limited), error),
        )

    def add_funding(self, time: int, venue: str, symbol: str, apr: float) -> None:
        self.conn.execute(
            """INSERT INTO funding_buckets VALUES (?,?,?,1,?,?,?,?,?,?)
               ON CONFLICT (bucket, venue, symbol) DO UPDATE SET
                 n = n + 1, sum_apr = sum_apr + excluded.sum_apr,
                 min_apr = MIN(min_apr, excluded.min_apr), max_apr = MAX(max_apr, excluded.max_apr),
                 neg_n = neg_n + excluded.neg_n,
                 last_apr = CASE WHEN excluded.last_time >= last_time THEN excluded.last_apr ELSE last_apr END,
                 last_time = MAX(last_time, excluded.last_time)""",
            (self.bucket(time), venue, symbol, apr, apr, apr, int(apr < 0), apr, time),
        )

    def add_spread(self, time: int, s: Spread) -> None:
        cost, basis, lag = s.cost_bps, s.basis_bps, float(s.lag_ms)
        # 列を後から足せるように、位置ではなく列名で入れる
        self.conn.execute(
            """INSERT INTO spread_buckets
                 (bucket, short_venue, long_venue, symbol, size_usd, n,
                  sum_gross, min_gross, max_gross, sum_cost_bps, min_cost_bps, max_cost_bps,
                  sum_basis_bps, min_basis_bps, max_basis_bps, sum_lag_ms, max_lag_ms)
               VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (bucket, short_venue, long_venue, symbol, size_usd) DO UPDATE SET
                 n = n + 1,
                 sum_gross = sum_gross + excluded.sum_gross,
                 min_gross = MIN(min_gross, excluded.min_gross), max_gross = MAX(max_gross, excluded.max_gross),
                 sum_cost_bps = sum_cost_bps + excluded.sum_cost_bps,
                 min_cost_bps = MIN(min_cost_bps, excluded.min_cost_bps),
                 max_cost_bps = MAX(max_cost_bps, excluded.max_cost_bps),
                 sum_basis_bps = sum_basis_bps + excluded.sum_basis_bps,
                 min_basis_bps = MIN(min_basis_bps, excluded.min_basis_bps),
                 max_basis_bps = MAX(max_basis_bps, excluded.max_basis_bps),
                 sum_lag_ms = sum_lag_ms + excluded.sum_lag_ms,
                 max_lag_ms = MAX(max_lag_ms, excluded.max_lag_ms)""",
            (
                self.bucket(time), s.short_venue, s.long_venue, s.symbol, s.size_usd,
                s.gross_apr, s.gross_apr, s.gross_apr, cost, cost, cost, basis, basis, basis, lag, lag,
            ),
        )

    def add_depth_miss(self, time: int, m: DepthMiss) -> None:
        self.conn.execute(
            """INSERT INTO depth_misses VALUES (?,?,?,?,1)
               ON CONFLICT (bucket, venue, symbol, size_usd) DO UPDATE SET n = n + 1""",
            (self.bucket(time), m.venue, m.symbol, m.size_usd),
        )

    def add_episode(self, e: Episode) -> None:
        self.conn.execute(
            """INSERT INTO episodes
                 (short_venue, long_venue, symbol, size_usd, start, end, polls,
                  first_net_apr, max_net_apr, max_gross_apr, min_breakeven_days, pending_polls)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                e.short_venue, e.long_venue, e.symbol, e.size_usd, e.start, e.end, e.polls,
                e.first_net_apr, e.max_net_apr, e.max_gross_apr, e.min_breakeven_days, e.pending_polls,
            ),
        )

    def add_history(self, venue: str, symbol: str, points) -> int:
        """確定ファンディングの履歴を入れる (同じ時刻は上書き)"""
        rows = [(venue, symbol, p.time, p.hours, p.rate, p.mark) for p in points]
        self.conn.executemany("INSERT OR REPLACE INTO funding_history VALUES (?,?,?,?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def history(self, since: int, until: int) -> dict[tuple[str, str], list]:
        """(会場, 銘柄) -> FundingPoint のリスト (時刻の昇順)"""
        from tbf.funding.history import FundingPoint

        out: dict[tuple[str, str], list] = {}
        for r in self.conn.execute(
            "SELECT venue, symbol, time, hours, rate, mark FROM funding_history"
            " WHERE time >= ? AND time <= ? ORDER BY time",
            (since, until),
        ):
            out.setdefault((r["venue"], r["symbol"]), []).append(
                FundingPoint(r["time"], r["hours"], r["rate"], r["mark"])
            )
        return out

    def history_coverage(self) -> list:
        return self.conn.execute(
            "SELECT venue, symbol, COUNT(*) n, MIN(time) lo, MAX(time) hi,"
            " SUM(mark IS NOT NULL) marks FROM funding_history GROUP BY venue, symbol ORDER BY venue, symbol"
        ).fetchall()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value)
        )

    def note_start(self, time: int) -> None:
        """最初に run した時刻だけは上書きしない (計測期間の分母になる)"""
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('first_run_ms', ?)", (str(time),))
        self.set_meta("last_run_ms", str(time))
        self.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def commit(self) -> None:
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def prune(self, before: int) -> None:
        """生の polls だけ消す (バケット集計とエピソードは残す)"""
        self.conn.execute("DELETE FROM polls WHERE time < ?", (before,))
        self.conn.commit()
