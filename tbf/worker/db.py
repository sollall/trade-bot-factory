"""DB の抽象。Cloudflare では D1、テストでは sqlite3 を使う"""

from __future__ import annotations

from typing import Any, Protocol, Sequence


class DB(Protocol):
    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """変更行数を返す"""
        ...

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]: ...


class SqliteDB:
    """テスト / ローカル用 (Pyodide では sqlite3 が標準で読み込めないので遅延 import)"""

    def __init__(self, path: str = ":memory:") -> None:
        import sqlite3

        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def migrate(self, sql: str) -> None:
        self.conn.executescript(sql)

    async def execute(self, sql, params=()):
        cur = self.conn.execute(sql, tuple(params))
        self.conn.commit()
        return cur.rowcount

    async def query(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]
