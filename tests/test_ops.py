import json
import subprocess
import sys
from pathlib import Path

from tbf import Bot
from tbf.worker.app import App, Settings
from tbf.worker.ops import STALE_MS, open_positions, unknown_instruments, unmanaged_positions

from test_worker import NOW, FakeHttp, FakeMarket, carry_bot, make_db, single_bot

ROOT = Path(__file__).resolve().parent.parent


def make_app(bots, db, http=None, **settings):
    markets = {"binance": FakeMarket(3000), "hyperliquid": FakeMarket(3000)}
    return App(bots, Settings(mode="paper", admin_token="t", webhook_url="https://hook", **settings), db, http or FakeHttp(), markets, build="abc123")


async def test_heartbeat_and_healthz():
    db = make_db()
    app = make_app([carry_bot()], db)
    res = await app.handle("GET", "https://w/healthz", {}, now=NOW)
    assert res.status == 503 and json.loads(res.body)["ok"] is False

    await app.scheduled("* * * * *", NOW)
    res = await app.handle("GET", "https://w/healthz", {}, now=NOW + 1000)  # 認証なしで見られる
    body = json.loads(res.body)
    assert res.status == 200 and body["ok"] and body["build"] == body["last_tick_build"] == "abc123"
    assert body["mode"] == "paper" and body["bots"] == 1 and body["last_tick_errors"] == 0
    assert "equity" not in res.body  # 成績は出さない

    res = await app.handle("GET", "https://w/healthz", {}, now=NOW + STALE_MS + 1)
    assert res.status == 503 and "last tick" in json.loads(res.body)["reason"]


async def test_heartbeat_with_no_bots_and_ping():
    class PingHttp(FakeHttp):
        async def request(self, method, url, headers=None, body=None):
            self.posts.append((method, url))
            return 200, ""

    db, http = make_db(), PingHttp()
    app = make_app([], db, http, heartbeat_url="https://hc.example/ping/x")
    await app.scheduled("* * * * *", NOW)
    assert (await app.handle("GET", "https://w/healthz", {}, now=NOW)).status == 200
    assert http.posts == [("GET", "https://hc.example/ping/x")]


async def test_heartbeat_counts_bot_errors():
    db = make_db()
    bad = Bot(id="bad", symbol="BTCUSDT", timeframe="1h", lookback=10, capital=1000, strategy=lambda ctx: 1 / 0)
    app = make_app([bad, carry_bot()], db)
    await app.scheduled("* * * * *", NOW)
    hb = json.loads((await app.handle("GET", "https://w/healthz", {}, now=NOW)).body)
    assert hb["ok"] and hb["last_tick_errors"] == 1


async def test_metrics():
    db = make_db()
    app = make_app([carry_bot()], db)
    assert (await app.handle("GET", "https://w/metrics", {}, now=NOW)).status == 401
    await app.scheduled("* * * * *", NOW)
    res = await app.handle("GET", "https://w/metrics", {"Authorization": "Bearer t"}, now=NOW + 5000)
    assert res.status == 200 and res.content_type.startswith("text/plain")
    lines = res.body.splitlines()
    assert 'tbf_build_info{mode="paper",build="abc123"} 1' in lines
    assert 'tbf_bot_running{mode="paper",bot="carry"} 1' in lines
    assert 'tbf_heartbeat_age_seconds{mode="paper"} 5' in lines
    assert any(l.startswith('tbf_bot_equity_usd{mode="paper",bot="carry"} ') for l in lines)
    assert any(l.startswith('tbf_bot_position_qty{mode="paper",bot="carry",instrument="ETH:perp@hyperliquid"} -') for l in lines)
    assert "# TYPE tbf_bot_drawdown_ratio gauge" in lines
    assert "tbf_unmanaged_position_qty" not in res.body


async def test_unmanaged_positions_reported():
    db, http = make_db(), FakeHttp()
    await make_app([carry_bot()], db).scheduled("* * * * *", NOW)  # carry が建てる
    # carry を消した版をデプロイした
    app = make_app([single_bot()], db, http)
    res = await app.handle("GET", "https://w/metrics", {"Authorization": "Bearer t"}, now=NOW)
    assert 'tbf_unmanaged_position_qty{mode="paper",bot="carry",instrument="perp"}' in res.body
    await app.scheduled("5 0 * * *", NOW)
    assert any("carry" in p and "持ち高が残っています" in p for p in http.posts)


def test_open_positions_formats():
    assert open_positions({"portfolio": json.dumps({"cash": {}, "positions": {"a": [1.5, 10], "b": [0, 0]}})}) == {"a": 1.5}
    assert open_positions({"portfolio": None, "qty": 2.0}) == {"(primary)": 2.0}
    assert open_positions({"portfolio": None, "qty": 0}) == {}
    rows = [
        {"bot_id": "kept", "portfolio": json.dumps({"positions": {"a": [1, 1]}})},
        {"bot_id": "gone_flat", "portfolio": json.dumps({"positions": {}})},
        {"bot_id": "gone_open", "portfolio": json.dumps({"positions": {"a": [-1, 1]}})},
    ]
    assert unmanaged_positions(rows, ["kept"]) == {"gone_open": {"a": -1.0}}


def run_guard(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "deploy_guard.py"), *args], capture_output=True, text=True)


def test_preflight(tmp_path):
    rows = tmp_path / "rows.json"
    # wrangler d1 execute --json の形
    rows.write_text(json.dumps([{"results": [
        {"bot_id": "sma_cross", "status": "running", "portfolio": json.dumps({"positions": {"BTCUSDT": [1, 1]}}), "qty": 0},
        {"bot_id": "removed_bot", "status": "running", "portfolio": json.dumps({"positions": {"x": [3, 1]}}), "qty": 0},
    ], "success": True}]))
    # live に出ている bot はないので、どちらも live からは外れる扱い
    r = run_guard("preflight", "--mode", "live", "--rows", str(rows))
    assert r.returncode == 1 and "removed_bot" in r.stdout and "sma_cross" in r.stdout
    assert run_guard("preflight", "--mode", "live", "--rows", str(rows), "--allow", "removed_bot,sma_cross").returncode == 0
    # paper は sma_cross を管理しているので removed_bot だけ。paper は警告のみ
    r = run_guard("preflight", "--mode", "paper", "--rows", str(rows))
    assert r.returncode == 0 and "removed_bot" in r.stdout and "sma_cross は" not in r.stdout
    rows.write_text("[]")
    assert run_guard("preflight", "--mode", "live", "--rows", str(rows)).returncode == 0


def test_unknown_instruments():
    rows = [{"bot_id": "carry", "portfolio": json.dumps({"positions": {"spot": [1, 1], "old_perp": [-1, 1]}})}]
    assert unknown_instruments(rows, [carry_bot()]) == {"carry": {"old_perp": -1.0}}
    assert unknown_instruments(rows, [single_bot()]) == {}  # 管理外は unmanaged_positions 側で扱う
