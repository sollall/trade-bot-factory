"""
worker/entry.py (Cloudflare 固有の層) を、js / pyodide / workers モジュールのスタブで動かす。
本物の Pyodide ランタイムではないが、D1 / fetch / Response の受け渡しと
scripts/build_worker.py の成果物がそのまま import できることを確認する。
"""

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tbf.worker.db import SqliteDB

ROOT = Path(__file__).resolve().parent.parent
HOUR = 3_600_000
NOW = 1_800_000_000_000 // HOUR * HOUR + 30_000


class FakeStmt:
    def __init__(self, db, sql, args=()):
        self.db, self.sql, self.args = db, sql, args

    def bind(self, *args):
        assert all(a is not None for a in args), "None must not reach D1.bind"
        return FakeStmt(self.db, self.sql, args)

    async def run(self):
        n = await self.db.execute(self.sql, self.args)
        return types.SimpleNamespace(meta=types.SimpleNamespace(changes=n))

    async def all(self):
        return types.SimpleNamespace(results=await self.db.query(self.sql, self.args))


class FakeD1:
    def __init__(self, db):
        self.db = db

    def prepare(self, sql):
        return FakeStmt(self.db, sql)


class FakeFetchResponse:
    def __init__(self, status, text):
        self.status, self._text = status, text

    async def text(self):
        return self._text


async def fake_fetch(url, opts):
    if "/ticker/price" in url:
        symbols = json.loads(parse_qs(urlparse(url).query)["symbols"][0])
        return FakeFetchResponse(200, json.dumps([{"symbol": s, "price": "150"} for s in symbols]))
    if "/klines" in url:
        cur = NOW // HOUR * HOUR
        rows = [[cur - i * HOUR, "1", "1", "1", str(100 + 100 - i), "1"] for i in range(100, -1, -1)]
        return FakeFetchResponse(200, json.dumps(rows))
    return FakeFetchResponse(404, "")


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers


@pytest.fixture
def entry(monkeypatch):
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_worker.py")], check=True, capture_output=True)
    js = types.ModuleType("js")
    js.JSON = types.SimpleNamespace(stringify=json.dumps)
    js.Object = types.SimpleNamespace(fromEntries=dict)
    js.fetch = fake_fetch
    ffi = types.ModuleType("pyodide.ffi")
    ffi.to_js = lambda v, dict_converter=None: v
    pyodide = types.ModuleType("pyodide")
    pyodide.ffi = ffi
    workers = types.ModuleType("workers")
    workers.Response = FakeResponse
    workers.WorkerEntrypoint = type("WorkerEntrypoint", (), {})
    for name, mod in {"js": js, "pyodide": pyodide, "pyodide.ffi": ffi, "workers": workers}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    # .build/ を Cloudflare と同じくモジュールルートにする
    monkeypatch.syspath_prepend(str(ROOT / ".build"))
    for m in [m for m in sys.modules if m in ("entry", "bot_registry") or m == "bots" or m.startswith("bots.")]:
        monkeypatch.delitem(sys.modules, m)
    return importlib.import_module("entry")


def make_worker(entry, db, **env):
    w = entry.Default()
    w.env = types.SimpleNamespace(DB=FakeD1(db), MODE="paper", **env)
    return w


def migrated_db():
    db = SqliteDB()
    db.migrate((ROOT / "migrations" / "0001_init.sql").read_text())
    return db


async def test_scheduled_and_fetch_through_entry(entry):
    db = migrated_db()
    w = make_worker(entry, db, ADMIN_TOKEN="t")
    await w.scheduled(types.SimpleNamespace(cron="* * * * *", scheduledTime=float(NOW)))

    rows = await db.query("SELECT bot_id, status, qty, halt_reason FROM bot_state ORDER BY bot_id")
    assert [r["bot_id"] for r in rows] == ["rsi_reversion", "sma_cross"]
    assert not await db.query("SELECT * FROM events WHERE level='error'")
    # 上昇トレンドのデータなので sma_cross は買っている
    assert next(r for r in rows if r["bot_id"] == "sma_cross")["qty"] > 0
    # halt_reason は NULL のまま (None を D1 に渡さず NULL リテラルに置換している)
    assert all(r["halt_reason"] is None for r in rows)

    req = types.SimpleNamespace(method="GET", url="https://w/api/bots", headers={"authorization": "Bearer t"})
    res = await w.fetch(req)
    assert res.status == 200 and res.headers["content-type"] == "application/json"
    assert {b["id"] for b in json.loads(res.body)} == {"rsi_reversion", "sma_cross"}

    req = types.SimpleNamespace(method="GET", url="https://w/", headers={})
    assert (await w.fetch(req)).status == 401


def test_d1_null_substitution(entry):
    d1 = entry.D1(FakeD1(None))
    stmt = d1._stmt("UPDATE t SET a=?, b=? WHERE c=?", [1, None, 3])
    assert stmt.sql == "UPDATE t SET a=?, b=NULL WHERE c=?" and stmt.args == (1, 3)
