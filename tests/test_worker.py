import json
from pathlib import Path

import pytest

from tbf import Bot, Risk
from tbf.core.types import TIMEFRAME_MS, Candle
from tbf.worker.app import App, Settings
from tbf.worker.brokers import OrderRequest, SymbolInfo, floor_to_step, format_qty, parse_order
from tbf.worker.db import SqliteDB

ROOT = Path(__file__).resolve().parent.parent
HOUR = TIMEFRAME_MS["1h"]
NOW = 1_800_000_000_000 // HOUR * HOUR + 30_000  # 足が確定した 30 秒後


class FakeHttp:
    """Binance の公開 API と Webhook の代役"""

    def __init__(self):
        self.price = 150.0
        self.posts = []

    async def request(self, method, url, headers=None, body=None):
        if method == "POST":
            self.posts.append(json.loads(body)["text"])
            return 204, ""
        if "/ticker/price" in url:
            return 200, json.dumps([{"symbol": "BTCUSDT", "price": str(self.price)}])
        if "/klines" in url:
            cur = NOW // HOUR * HOUR
            rows = [[cur - i * HOUR, "100", "101", "99", str(100 + 60 - i), "1"] for i in range(60, -1, -1)]
            return 200, json.dumps(rows)
        return 404, ""


def make_db():
    db = SqliteDB()
    db.migrate((ROOT / "migrations" / "0001_init.sql").read_text())
    return db


def make_app(http, db, token=None, **bot_kw):
    bot = Bot(
        id="up", symbol="BTCUSDT", timeframe="1h", lookback=10, capital=1000,
        risk=Risk(max_drawdown=0.2), strategy=lambda ctx: 1 if ctx.closes[-1] > ctx.closes[0] else 0,
        **bot_kw,
    )
    other = Bot(id="live_only", symbol="BTCUSDT", timeframe="1h", lookback=1, capital=1, deploy=["live"], strategy=lambda c: 1)
    return App([bot, other], Settings(mode="paper", webhook_url="https://hook", admin_token=token), db, http)


async def test_tick_trades_once_per_candle_and_records():
    http, db = FakeHttp(), make_db()
    app = make_app(http, db)
    await app.scheduled("* * * * *", NOW)
    await app.scheduled("* * * * *", NOW + 1000)  # 同じ足では再発注しない

    fills = await db.query("SELECT * FROM fills")
    assert len(fills) == 1 and fills[0]["side"] == "buy"
    state = (await db.query("SELECT * FROM bot_state WHERE bot_id='up'"))[0]
    assert state["cash"] >= 0 and state["qty"] > 0
    assert state["last_candle"] == NOW // HOUR * HOUR - HOUR
    assert len(await db.query("SELECT * FROM equity")) == 1
    assert any("up buy" in p for p in http.posts)
    # live 専用 bot は paper では動かない
    assert not await db.query("SELECT * FROM bot_state WHERE bot_id='live_only'")


async def test_drawdown_halts_flattens_and_resume():
    http, db = FakeHttp(), make_db()
    app = make_app(http, db)
    await app.scheduled("* * * * *", NOW)
    http.price = 50.0
    await app.scheduled("* * * * *", NOW + 60_000)

    state = (await db.query("SELECT * FROM bot_state WHERE bot_id='up'"))[0]
    assert state["status"] == "halted" and "drawdown" in state["halt_reason"]
    assert state["qty"] == 0
    assert any("halted" in p and "position closed" in p for p in http.posts)

    res = await app.handle("POST", "https://w/api/bots/up/resume", {})
    assert res.status == 200
    state = (await db.query("SELECT * FROM bot_state WHERE bot_id='up'"))[0]
    assert state["status"] == "running" and state["peak_equity"] == pytest.approx(state["cash"])


async def test_api_auth_and_dashboard():
    http, db = FakeHttp(), make_db()
    app = make_app(http, db, token="secret")
    assert (await app.handle("GET", "https://w/api/bots", {})).status == 401
    await app.handle("POST", "https://w/api/tick", {"Authorization": "Bearer secret"}, now=NOW)
    res = await app.handle("GET", "https://w/api/bots/up", {"Authorization": "Bearer secret"}, now=NOW)
    body = json.loads(res.body)
    assert res.status == 200 and len(body["recent_fills"]) == 1
    dash = await app.handle("GET", "https://w/?token=secret", {}, now=NOW)
    assert dash.status == 200 and "up" in dash.body and "running" in dash.body
    assert (await app.handle("GET", "https://w/api/bots/nope", {"authorization": "Bearer secret"})).status == 404


async def test_manual_halt_and_daily_report():
    http, db = FakeHttp(), make_db()
    app = make_app(http, db)
    await app.scheduled("* * * * *", NOW)
    res = await app.handle("POST", "https://w/api/bots/up/halt?flatten=true", {}, now=NOW + 1)
    assert json.loads(res.body)["flatten"] is True
    state = (await db.query("SELECT * FROM bot_state WHERE bot_id='up'"))[0]
    assert state["status"] == "halted" and state["qty"] == 0
    await app.scheduled("5 0 * * *", NOW + 2)
    assert "daily report" in http.posts[-1]


async def test_live_requires_keys():
    app = App([], Settings(mode="live"), make_db(), FakeHttp())
    with pytest.raises(RuntimeError):
        app.runtime(NOW)


def test_binance_order_parsing_and_rounding():
    assert floor_to_step(0.123456, 0.001) == pytest.approx(0.123)
    assert format_qty(0.30000000000000004, 0.0001) == "0.3000"
    info = SymbolInfo("BTC", "USDT", 0.00001, 0.00001, 5)
    req = OrderRequest("b", "BTCUSDT", "buy", 1, 100, "id", 0.001, 0, 0)
    res = {
        "orderId": 1, "executedQty": "1.0", "cummulativeQuoteQty": "100", "transactTime": 5,
        "fills": [{"price": "100", "qty": "1", "commission": "0.001", "commissionAsset": "BTC"}],
    }
    bf = parse_order(res, req, info)
    # base 建て手数料は受取数量から差し引き、quote 換算で fee に計上
    assert bf.fill.qty == pytest.approx(0.999)
    assert bf.fill.fee == pytest.approx(0.1)
    assert bf.fill.qty * bf.fill.price + bf.fill.fee == pytest.approx(100)
