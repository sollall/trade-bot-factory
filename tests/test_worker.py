import importlib.util
import json
from pathlib import Path

import pytest

from tbf import Bot, Risk, perp, spot
from tbf.core.types import HOUR, Candle, FundingPoint
from tbf.worker.app import App, Settings
from tbf.worker.brokers import OrderRequest, SymbolInfo, floor_to_step, format_qty, parse_order
from tbf.worker.db import SqliteDB

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000_000 // HOUR * HOUR + 30_000  # 足が確定した 30 秒後


class FakeMarket:
    """取引所 1 つ分のマーケットデータ"""

    def __init__(self, price=150.0, rate=0.0):
        self.price = price
        self.rate = rate
        self.funding_calls = 0

    async def prices(self, symbols):
        return {s: self.price for s in symbols}

    async def recent(self, symbol, timeframe, count, now):
        cur = now // HOUR * HOUR
        return [Candle(cur - i * HOUR, 100, 101, 99, 100 + 60 - i) for i in range(count, 0, -1)]

    async def history(self, symbol, timeframe, start, end):
        return []

    async def funding(self, symbol, start, end):
        self.funding_calls += 1
        t = start // HOUR * HOUR + HOUR
        out = []
        while t <= end:
            out.append(FundingPoint(t, self.rate, 0.0))
            t += HOUR
        return out


class FakeHttp:
    def __init__(self):
        self.posts = []

    async def request(self, method, url, headers=None, body=None):
        self.posts.append(json.loads(body)["text"])
        return 204, ""


def make_db():
    db = SqliteDB()
    for f in sorted((ROOT / "migrations").glob("*.sql")):
        db.migrate(f.read_text())
    return db


def single_bot(**kw):
    return Bot(
        id="up", symbol="BTCUSDT", timeframe="1h", lookback=10, capital=1000, risk=Risk(max_drawdown=0.2),
        strategy=lambda ctx: 1 if ctx.closes[-1] > ctx.closes[0] else 0, **kw,
    )


def carry_bot(**kw):
    def strat(ctx):
        if ctx.holding("perp").qty == 0:
            w = ctx.neutral_weight()
            return {"spot": w, "perp": -w}
        return None

    base = dict(
        id="carry", instruments={"spot": spot("ETHUSDT"), "perp": perp("ETH")}, timeframe="1h", lookback=10,
        capital=10_000, leverage=3, risk=Risk(max_drawdown=0.5), strategy=strat,
    )
    return Bot(**{**base, **kw})


def make_app(bots, db, markets, http=None, mode="paper", token=None):
    return App(bots, Settings(mode=mode, webhook_url="https://hook", admin_token=token), db, http or FakeHttp(), markets)


async def state(db, bot_id):
    row = (await db.query("SELECT * FROM bot_state WHERE bot_id=?", (bot_id,)))[0]
    row["portfolio"] = json.loads(row["portfolio"])
    return row


async def test_single_bot_trades_once_per_candle():
    db, http = make_db(), FakeHttp()
    live_only = Bot(id="live_only", symbol="BTCUSDT", timeframe="1h", lookback=1, capital=1, deploy=["live"], strategy=lambda c: 1)
    app = make_app([single_bot(), live_only], db, {"binance": FakeMarket()}, http)
    await app.scheduled("* * * * *", NOW)
    await app.scheduled("* * * * *", NOW + 1000)  # 同じ足では再発注しない

    fills = await db.query("SELECT * FROM fills")
    assert len(fills) == 1 and fills[0]["side"] == "buy" and fills[0]["instrument"] == "BTCUSDT"
    s = await state(db, "up")
    assert s["portfolio"]["cash"]["binance"] >= -1e-9 and s["portfolio"]["positions"]["BTCUSDT"][0] > 0
    assert s["last_candle"] == NOW // HOUR * HOUR - HOUR
    assert len(await db.query("SELECT * FROM equity")) == 1
    assert any("up buy" in p for p in http.posts)
    assert not await db.query("SELECT * FROM bot_state WHERE bot_id='live_only'")


async def test_drawdown_halts_flattens_and_resume():
    db, http = make_db(), FakeHttp()
    m = FakeMarket()
    app = make_app([single_bot()], db, {"binance": m}, http)
    await app.scheduled("* * * * *", NOW)
    m.price = 50.0
    await app.scheduled("* * * * *", NOW + 60_000)

    s = await state(db, "up")
    assert s["status"] == "halted" and "drawdown" in s["halt_reason"]
    assert not s["portfolio"]["positions"]
    assert any("halted" in p and "positions closed" in p for p in http.posts)

    res = await app.handle("POST", "https://w/api/bots/up/resume", {})
    assert res.status == 200
    s = await state(db, "up")
    assert s["status"] == "running" and s["peak_equity"] == pytest.approx(s["portfolio"]["cash"]["binance"])


async def test_carry_bot_paper_with_funding_and_transfer():
    db, http = make_db(), FakeHttp()
    bn, hl = FakeMarket(3000), FakeMarket(3000, rate=0.0001)
    app = make_app([carry_bot()], db, {"binance": bn, "hyperliquid": hl}, http)
    await app.scheduled("* * * * *", NOW)
    s = await state(db, "carry")
    pos = s["portfolio"]["positions"]
    assert pos["spot"][0] == pytest.approx(-pos["perp"][0])  # ヘッジされている
    assert s["portfolio"]["cash"]["hyperliquid"] > 0  # 証拠金が移されている
    events = [e["message"] for e in await db.query("SELECT * FROM events")]
    assert any(m.startswith("transfer") for m in events) and any(m.startswith("enter") for m in events)

    # 3 時間後: 3 回分のファンディングを受け取る
    await app.scheduled("* * * * *", NOW + 3 * HOUR)
    s2 = await state(db, "carry")
    qty = -s2["portfolio"]["positions"]["perp"][0]
    gained = s2["portfolio"]["cash"]["hyperliquid"] - s["portfolio"]["cash"]["hyperliquid"]
    assert gained == pytest.approx(qty * 3000 * 0.0001 * 3, rel=1e-6)
    assert len(await db.query("SELECT * FROM fills")) == 2  # 戦略は現状維持なので追加の売買なし


async def test_paper_liquidation_unwinds():
    db, http = make_db(), FakeHttp()
    bn, hl = FakeMarket(3000), FakeMarket(3000)
    app = make_app([carry_bot(leverage=5)], db, {"binance": bn, "hyperliquid": hl}, http)
    await app.scheduled("* * * * *", NOW)
    bn.price = hl.price = 3700  # 5 倍レバのショートは +20% 弱で清算
    await app.scheduled("* * * * *", NOW + 60_000)
    s = await state(db, "carry")
    assert not s["portfolio"]["positions"]
    assert s["portfolio"]["cash"]["hyperliquid"] == 0
    assert any("liquidated" in p for p in http.posts)


async def test_concurrent_update_is_not_overwritten():
    db = make_db()
    app = make_app([single_bot()], db, {"binance": FakeMarket()})
    await app.scheduled("* * * * *", NOW)
    from tbf.worker.store import Store

    store = Store(db)
    a = await store.get("up")
    b = await store.get("up")
    assert await store.save_account(a)
    assert not await store.save_account(b)  # 古い版からの保存は拒否


async def test_legacy_single_row_is_migrated():
    db = make_db()
    await db.execute(
        "INSERT INTO bot_state (bot_id, cash, qty, avg_price, peak_equity, day_start_equity, updated_at) VALUES ('up', 500, 5, 100, 1000, 1000, 0)"
    )
    app = make_app([single_bot()], db, {"binance": FakeMarket(price=100)})
    res = await app.handle("GET", "https://w/api/bots/up", {})
    body = json.loads(res.body)
    assert body["equity"] == pytest.approx(500 + 5 * 100)


async def test_api_auth_and_dashboard():
    db = make_db()
    app = make_app([carry_bot()], db, {"binance": FakeMarket(3000), "hyperliquid": FakeMarket(3000)}, token="secret")
    assert (await app.handle("GET", "https://w/api/bots", {})).status == 401
    await app.handle("POST", "https://w/api/tick", {"Authorization": "Bearer secret"}, now=NOW)
    res = await app.handle("GET", "https://w/api/bots/carry", {"Authorization": "Bearer secret"}, now=NOW)
    body = json.loads(res.body)
    assert res.status == 200 and len(body["recent_fills"]) == 2 and body["instruments"]["perp"] == "ETH:perp@hyperliquid"
    dash = await app.handle("GET", "https://w/?token=secret", {}, now=NOW)
    assert dash.status == 200 and "carry" in dash.body and "ETH:perp@hyperliquid" in dash.body
    assert (await app.handle("GET", "https://w/api/bots/nope", {"authorization": "Bearer secret"})).status == 404


async def test_dashboard_pages():
    db = make_db()
    app = make_app([carry_bot()], db, {"binance": FakeMarket(3000), "hyperliquid": FakeMarket(3000)}, token="s&c<ret")
    assert (await app.handle("GET", "https://w/bots/carry", {})).status == 401
    await app.scheduled("* * * * *", NOW)
    await db.execute("INSERT INTO events (bot_id, time, level, message) VALUES ('carry', ?, 'error', '<script>x</script>')", (NOW,))
    for i in range(3):
        await db.execute("INSERT OR REPLACE INTO equity (bot_id, time, equity) VALUES ('carry', ?, ?)", (NOW - (i + 1) * 3_600_000, 10_000 + i))

    top = await app.handle("GET", "https://w/?token=s%26c%3Cret", {}, now=NOW)
    # 一覧: 詳細へのリンクにトークンを引き継ぐ / イベントの中身はエスケープする
    assert top.status == 200 and 'href="bots/carry?token=s%26c%3Cret"' in top.body
    assert "cron 正常" in top.body and "&lt;script&gt;x" in top.body and "<script>x" not in top.body

    page = await app.handle("GET", "https://w/bots/carry?token=s%26c%3Cret&range=7d", {}, now=NOW)
    assert page.status == 200 and "持ち高" in page.body and "ETH:perp@hyperliquid" in page.body
    assert "data-pts=" in page.body and 'class="on" href="carry?token=s%26c%3Cret&range=7d"' in page.body
    assert "&lt;script&gt;x" in page.body and "<script>x" not in page.body
    # トークンなしで見られる環境ではリンクに何も足さない
    assert (await app.handle("GET", "https://w/bots/nope", {"Authorization": "Bearer s&c<ret"})).status == 404
    bearer = await app.handle("GET", "https://w/bots/carry?range=bogus", {"Authorization": "Bearer s&c<ret"}, now=NOW)
    assert bearer.status == 200 and 'href="carry?range=30d"' in bearer.body and "token=" not in bearer.body


def test_downsample_keeps_ends():
    from tbf.core.types import EquityPoint
    from tbf.worker.dashboard import downsample

    pts = [EquityPoint(i, float(i)) for i in range(10_000)]
    out = downsample(pts, 400)
    assert len(out) == 400 and out[0] is pts[0] and out[-1] is pts[-1]
    assert downsample(pts[:5], 400) == pts[:5]


async def test_manual_halt_and_daily_report():
    db, http = make_db(), FakeHttp()
    app = make_app([carry_bot()], db, {"binance": FakeMarket(3000), "hyperliquid": FakeMarket(3000)}, http)
    await app.scheduled("* * * * *", NOW)
    res = await app.handle("POST", "https://w/api/bots/carry/halt?flatten=true", {}, now=NOW + 1)
    assert json.loads(res.body)["flatten"] is True
    s = await state(db, "carry")
    assert s["status"] == "halted" and not s["portfolio"]["positions"]
    await app.scheduled("5 0 * * *", NOW + 2)
    assert "daily report" in http.posts[-1]


async def test_live_unsupported_venue_errors_without_trading():
    db, http = make_db(), FakeHttp()
    app = make_app([carry_bot(deploy=["live"])], db, {"binance": FakeMarket(3000), "hyperliquid": FakeMarket(3000)}, http, mode="live")
    await app.scheduled("* * * * *", NOW)
    assert not await db.query("SELECT * FROM fills")
    errors = await db.query("SELECT * FROM events WHERE level='error'")
    assert errors and ("未対応" in errors[0]["message"] or "未設定" in errors[0]["message"])


def test_build_rejects_live_bot_on_unsupported_venue():
    spec = importlib.util.spec_from_file_location("build_worker", ROOT / "scripts" / "build_worker.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    ok = Bot(id="ok", symbol="BTCUSDT", timeframe="1h", lookback=1, capital=1, deploy=["paper", "live"], strategy=lambda c: None)
    paper_only = carry_bot()
    bad = carry_bot(id="bad", deploy=["paper", "live"])
    assert build.live_errors(ok) == [] and build.live_errors(paper_only) == []
    errors = build.live_errors(bad)
    assert len(errors) == 1 and "perp" in errors[0] and "live にできません" in errors[0]


def test_binance_order_parsing_and_rounding():
    assert floor_to_step(0.123456, 0.001) == pytest.approx(0.123)
    assert format_qty(0.30000000000000004, 0.0001) == "0.3000"
    info = SymbolInfo("BTC", "USDT", 0.00001, 0.00001, 5)
    req = OrderRequest("b", "main", spot("BTCUSDT"), "buy", 1, 100, "id", 0)
    res = {
        "orderId": 1, "executedQty": "1.0", "cummulativeQuoteQty": "100", "transactTime": 5,
        "fills": [{"price": "100", "qty": "1", "commission": "0.001", "commissionAsset": "BTC"}],
    }
    bf = parse_order(res, req, info)
    # base 建て手数料は受取数量から差し引き、quote 換算で fee に計上
    assert bf.fill.qty == pytest.approx(0.999) and bf.fill.instrument == "main"
    assert bf.fill.fee == pytest.approx(0.1)
    assert bf.fill.qty * bf.fill.price + bf.fill.fee == pytest.approx(100)


async def test_bots_sharing_a_candle_fetch_it_once_per_tick():
    """同じ銘柄・同じ足を見ている bot が何本あっても、取得は 1 tick に 1 回。
    13:00 UTC の確定足で Coinbase の 429 (流量制限) を踏んだのは、これが 6 本重なったため (TRA-81)。
    短い lookback の bot も、まとめて取った足の新しい側を使う"""

    class Counting(FakeMarket):
        def __init__(self):
            super().__init__()
            self.recent_calls = []

        async def recent(self, symbol, timeframe, count, now):
            self.recent_calls.append((symbol, timeframe, count))
            return await super().recent(symbol, timeframe, count, now)

    seen: dict[str, int] = {}

    def strategy_for(bot_id):
        def strategy(ctx):
            seen[bot_id] = len(ctx.closes)
            return 0

        return strategy

    db, market = make_db(), Counting()
    bots = [
        Bot(
            id=f"look{n}", symbol="BTCUSDT", timeframe="1h", lookback=n, capital=1000,
            risk=Risk(max_drawdown=0.2), strategy=strategy_for(f"look{n}"),
        )
        for n in (5, 20, 50)
    ]
    await make_app(bots, db, {"binance": market}).scheduled("* * * * *", NOW)

    assert market.recent_calls == [("BTCUSDT", "1h", 50)]
    assert seen == {"look5": 5, "look20": 20, "look50": 50}
    assert not await db.query("SELECT * FROM events WHERE level='error'")


class FlakyCandles(FakeMarket):
    """最初の fails 回だけ足の取得が 429 で落ちる取得元"""

    def __init__(self, fails=1):
        super().__init__()
        self.fails = fails
        self.calls = 0

    async def recent(self, symbol, timeframe, count, now):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError('HTTP 429 {"message":"Public rate limit exceeded"}')
        return await super().recent(symbol, timeframe, count, now)


async def errors_in_last_tick(db):
    return (await db.query("SELECT errors FROM heartbeat WHERE name='tick'"))[0]["errors"]


async def events(db, bot_id):
    return [(r["level"], r["message"]) for r in await db.query("SELECT * FROM events WHERE bot_id=? ORDER BY id", (bot_id,))]


async def test_a_candle_that_could_not_be_fetched_is_retried_on_the_next_tick():
    """429 で足が取れなかった足の判断を落とさない。処理権を戻して次の tick でやり直す。
    戻さないと last_candle が進んだままになり、その足の売買が永久に飛ぶ (TRA-81)"""
    db, http, market = make_db(), FakeHttp(), FlakyCandles(fails=1)
    app = make_app([single_bot()], db, {"binance": market}, http)

    await app.scheduled("* * * * *", NOW)
    assert await errors_in_last_tick(db) == 0  # 取り逃しではないのでエラーに数えない
    assert [lvl for lvl, _ in await events(db, "up")] == ["warn"]
    assert not await db.query("SELECT * FROM fills")
    assert (await state(db, "up"))["last_candle"] < NOW // HOUR * HOUR - HOUR  # 処理権は戻っている

    await app.scheduled("* * * * *", NOW + 60_000)  # 同じ足でやり直す
    assert len(await db.query("SELECT * FROM fills")) == 1
    assert (await state(db, "up"))["last_candle"] == NOW // HOUR * HOUR - HOUR
    assert not [m for lvl, m in await events(db, "up") if lvl == "error"]


async def test_a_candle_missed_for_a_whole_period_is_reported_as_an_error():
    """1 本ぶん取り逃したら、黙って遅れ続けないようにエラーとして出す"""
    db, market = make_db(), FlakyCandles(fails=99)
    app = make_app([single_bot()], db, {"binance": market})

    await app.scheduled("* * * * *", NOW)  # 1 本目: 取り直す (warn)
    assert await errors_in_last_tick(db) == 0
    await app.scheduled("* * * * *", NOW + HOUR)  # 次の足も取れない: エラー
    assert await errors_in_last_tick(db) == 1
    assert [lvl for lvl, _ in await events(db, "up")] == ["warn", "error"]
    # 処理権は毎回戻しているので、取れるようになれば次の足から再開できる
    assert (await state(db, "up"))["last_candle"] < (NOW + HOUR) // HOUR * HOUR - HOUR
