"""A① シャドーモード計測の記録 (sqlite)

TRA-10 の `data/funding.sqlite` とは**別ファイル**に書く (依頼票の指定)。

生の時系列を全部残す。5 分間隔 × 30 日で samples は 8,640 行、板の歩き (fills) は
サイズ 4 × 脚 2 × 方向 2 = 16 行 × 8,640 = 約 14 万行で、sqlite には何の負担でもない。
日次 net の CSV と時間帯別の深さ分布を後から出し直せることのほうが大事なので、
バケット集計はせず生で持つ。

列は後から足せるように、INSERT は常に列名を書く (tbf/funding/store.py と同じ方針)。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from tbf.carry.position import Entry, Fill, Mark

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- 建玉 (計測開始時に 1 回だけ入り、以後変えない)
CREATE TABLE IF NOT EXISTS position (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  time INTEGER NOT NULL, size REAL NOT NULL,
  spot_px REAL NOT NULL, perp_px REAL NOT NULL,
  spot_notional_usd REAL NOT NULL, perp_notional_usd REAL NOT NULL,
  margin_usd REAL NOT NULL, capital_usd REAL NOT NULL,
  spot_fee_bps REAL NOT NULL, perp_fee_bps REAL NOT NULL,
  spot_slip_bps REAL NOT NULL, perp_slip_bps REAL NOT NULL,
  spot_mid REAL NOT NULL, perp_mid REAL NOT NULL
);

-- 5 分間隔の評価 (iii)
CREATE TABLE IF NOT EXISTS samples (
  time INTEGER PRIMARY KEY,
  spot_mark REAL NOT NULL, spot_mid REAL, perp_mark REAL NOT NULL, perp_mid REAL, perp_oracle REAL,
  basis_bps REAL NOT NULL, funding_rate_1h REAL, premium REAL,
  funding_cum_usd REAL NOT NULL, mtm_usd REAL NOT NULL, net_pnl_usd REAL NOT NULL,
  perp_equity_usd REAL NOT NULL, perp_notional_usd REAL NOT NULL, margin_ratio REAL NOT NULL,
  margin_warn INTEGER NOT NULL, liquidated INTEGER NOT NULL,
  spot_day_ntl_vlm REAL, perp_day_ntl_vlm REAL, open_interest REAL
);

-- 板を歩いた結果 (i)(iv)。足りなかった場合は avg_px / slip_bps が NULL = 未測定
CREATE TABLE IF NOT EXISTS fills (
  time INTEGER NOT NULL, leg TEXT NOT NULL, side TEXT NOT NULL, size_usd REAL NOT NULL,
  ref_px REAL NOT NULL, avg_px REAL, slip_bps REAL, fee_bps REAL NOT NULL,
  depth_usd REAL NOT NULL, levels_seen INTEGER NOT NULL,
  PRIMARY KEY (time, leg, side, size_usd)
);

-- 実現ファンディング (ii)。fundingHistory の 1 時間値をそのまま積む
CREATE TABLE IF NOT EXISTS funding (
  time INTEGER PRIMARY KEY, rate REAL NOT NULL, premium REAL,
  mark_px REAL, amount_usd REAL, nominal_amount_usd REAL
);

-- 稼働性 (カバー率の分母・分子を後から出せるように)
CREATE TABLE IF NOT EXISTS polls (
  time INTEGER NOT NULL, ok INTEGER NOT NULL, calls INTEGER NOT NULL,
  latency_ms REAL, rate_limited INTEGER NOT NULL DEFAULT 0, error TEXT
);
CREATE INDEX IF NOT EXISTS polls_time ON polls (time);
"""

ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    # 計測中の DB に後から列を足す場合はここに書く (ALTER TABLE ADD COLUMN のみ)。
    # 30 日の計測を midway でやり直したくないので、既存列の変更はしない。
}


class CarryStore:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
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

    # ---- 建玉

    def save_entry(self, e: Entry) -> None:
        """既に入っていれば何もしない (再起動しても建玉を作り直さない)"""
        self.conn.execute(
            """INSERT OR IGNORE INTO position
                 (id, time, size, spot_px, perp_px, spot_notional_usd, perp_notional_usd,
                  margin_usd, capital_usd, spot_fee_bps, perp_fee_bps, spot_slip_bps, perp_slip_bps,
                  spot_mid, perp_mid)
               VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                e.time, e.size, e.spot_px, e.perp_px, e.spot_notional_usd, e.perp_notional_usd,
                e.margin_usd, e.capital_usd, e.spot_fee_bps, e.perp_fee_bps,
                e.spot_slip_bps, e.perp_slip_bps, e.spot_mid, e.perp_mid,
            ),
        )
        self.conn.commit()

    def load_entry(self) -> Optional[Entry]:
        row = self.conn.execute("SELECT * FROM position WHERE id = 1").fetchone()
        if row is None:
            return None
        return Entry(
            time=row["time"], size=row["size"], spot_px=row["spot_px"], perp_px=row["perp_px"],
            spot_notional_usd=row["spot_notional_usd"], perp_notional_usd=row["perp_notional_usd"],
            margin_usd=row["margin_usd"], capital_usd=row["capital_usd"],
            spot_fee_bps=row["spot_fee_bps"], perp_fee_bps=row["perp_fee_bps"],
            spot_slip_bps=row["spot_slip_bps"], perp_slip_bps=row["perp_slip_bps"],
            spot_mid=row["spot_mid"], perp_mid=row["perp_mid"],
        )

    # ---- 書き込み

    def add_sample(self, m: Mark, extra: dict) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO samples
                 (time, spot_mark, spot_mid, perp_mark, perp_mid, perp_oracle, basis_bps,
                  funding_rate_1h, premium, funding_cum_usd, mtm_usd, net_pnl_usd,
                  perp_equity_usd, perp_notional_usd, margin_ratio, margin_warn, liquidated,
                  spot_day_ntl_vlm, perp_day_ntl_vlm, open_interest)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.time, m.spot_px, extra.get("spot_mid"), m.perp_px, extra.get("perp_mid"),
                extra.get("perp_oracle"), m.basis_bps, extra.get("funding_rate_1h"),
                extra.get("premium"), m.funding_cum_usd, m.mtm_usd, m.net_pnl_usd,
                m.perp_equity_usd, m.perp_notional_usd, m.margin_ratio,
                int(m.margin_warn), int(m.liquidated),
                extra.get("spot_day_ntl_vlm"), extra.get("perp_day_ntl_vlm"), extra.get("open_interest"),
            ),
        )

    def add_fill(self, time: int, leg: str, f: Fill) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO fills
                 (time, leg, side, size_usd, ref_px, avg_px, slip_bps, fee_bps, depth_usd, levels_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (time, leg, f.side, f.size_usd, f.ref_px, f.avg_px, f.slip_bps, f.fee_bps, f.depth_usd, f.levels_seen),
        )

    def add_funding(self, time: int, rate: float, premium: float, mark_px: float,
                    amount_usd: float, nominal_amount_usd: float) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO funding (time, rate, premium, mark_px, amount_usd, nominal_amount_usd)
               VALUES (?,?,?,?,?,?)""",
            (time, rate, premium, mark_px, amount_usd, nominal_amount_usd),
        )

    def add_poll(self, time: int, ok: bool, calls: int, latency_ms: Optional[float],
                 rate_limited: bool, error: Optional[str]) -> None:
        self.conn.execute(
            "INSERT INTO polls (time, ok, calls, latency_ms, rate_limited, error) VALUES (?,?,?,?,?,?)",
            (time, int(ok), calls, latency_ms, int(rate_limited), error),
        )

    # ---- meta

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value)
        )

    def set_meta_json(self, key: str, value) -> None:
        self.set_meta(key, json.dumps(value, ensure_ascii=False, sort_keys=True))

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def note_start(self, time: int) -> None:
        """最初に run した時刻は上書きしない (計測期間の分母になる)"""
        self.conn.execute("INSERT OR IGNORE INTO meta VALUES ('first_run_ms', ?)", (str(time),))
        self.set_meta("last_run_ms", str(time))
        self.commit()

    # ---- 読み出し

    def funding_cum_usd(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(amount_usd), 0) AS s FROM funding").fetchone()
        return float(row["s"])

    def last_funding_time(self) -> Optional[int]:
        row = self.conn.execute("SELECT MAX(time) AS t FROM funding").fetchone()
        return int(row["t"]) if row and row["t"] is not None else None

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def commit(self) -> None:
        self.conn.commit()
