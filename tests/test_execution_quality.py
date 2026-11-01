import json

import pytest

from tbf import Bot
from tbf.core.types import HOUR, Fill
from tbf.worker.app import App, Settings
from tbf.worker.ops import execution_summary
from tbf.worker.runner import execution_info

from test_worker import NOW, FakeHttp, FakeMarket, carry_bot, make_db, single_bot

CLOSE = NOW // HOUR * HOUR  # 直前の 1h 足が確定した時刻 (NOW はその 30 秒後)


def make_app(bots, db, markets, clock, http=None):
    s = Settings(mode="paper", webhook_url="https://hook")
    return App(bots, s, db, http or FakeHttp(), markets, build="b", clock=clock)


def test_execution_info_sign():
    bot = single_bot()
    alias = bot.primary
    fees = bot.instruments[alias].fees
    slip = 1 + fees.slippage_bps / 10_000
    # 買い: 想定より高く買えば不利 (+)
    ex = execution_info(bot, Fill(0, "buy", 1, 101 * slip, 0, instrument=alias), 100, 2500)
    assert ex.gap_bps == pytest.approx(100.0) and ex.delay_ms == 2500 and ex.bench_price == 100
    # 売り: 想定より安く売れば不利 (+)
    sell_px = 99 * (1 - fees.slippage_bps / 10_000)
    ex = execution_info(bot, Fill(0, "sell", 1, sell_px, 0, instrument=alias), 100, 0)
    assert ex.gap_bps == pytest.approx(100.0)
    # 想定どおりなら 0
    ex = execution_info(bot, Fill(0, "buy", 1, 100 * slip, 0, instrument=alias), 100, 0)
    assert ex.gap_bps == pytest.approx(0.0)


def test_execution_summary():
    rows = [
        {"bot_id": "a", "qty": 1, "bench_price": 100, "delay_ms": 1000, "gap_bps": 10},
        {"bot_id": "a", "qty": -3, "bench_price": 100, "delay_ms": 3000, "gap_bps": -2},
        {"bot_id": "b", "qty": 2, "bench_price": 50, "delay_ms": 500, "gap_bps": 4},
    ]
    s = execution_summary(rows)
    assert s["a"]["fills"] == 2 and s["a"]["delay_avg_sec"] == 2.0 and s["a"]["delay_max_sec"] == 3.0
    # 代金加重: (10*100 - 2*300) / 400 = 1bps, 金額 0.1 - 0.06 = 0.04 USD
    assert s["a"]["gap_bps"] == pytest.approx(1.0) and s["a"]["gap_usd"] == pytest.approx(0.04)
    assert s["b"]["gap_bps"] == pytest.approx(4.0)
    assert execution_summary([]) == {}


async def test_paper_fill_records_delay_gap_and_cron_lag():
    db, http = make_db(), FakeHttp()
    started = NOW + 4_000  # cron の予定時刻より 4 秒遅れて動き出した
    ticks = iter(range(started, started + 10_000, 700))
    app = make_app([single_bot()], db, {"binance": FakeMarket(price=150)}, lambda: next(ticks), http)
    await app.scheduled("* * * * *", NOW)

    fill = (await db.query("SELECT * FROM fills"))[0]
    # FakeMarket の直前の確定足の終値は 159、いまの価格は 150 → 買いが想定より安い (有利 = マイナス)
    assert fill["bench_price"] == 159
    assert fill["gap_bps"] == pytest.approx((150 / 159 - 1) * 10_000)
    # 足の確定 (CLOSE) から発注まで: 予定時刻の 30 秒 + 動き出しの遅れ 4 秒 + 処理中の経過
    assert 34_000 <= fill["delay_ms"] < 45_000
    hb = (await db.query("SELECT * FROM heartbeat"))[0]
    assert hb["lag_ms"] == 4_000 and hb["duration_ms"] > 0

    body = json.loads((await app.handle("GET", "https://w/api/bots", {}, now=NOW)).body)
    ex = body[0]["execution"]
    assert ex["fills"] == 1 and ex["delay_avg_sec"] == pytest.approx(fill["delay_ms"] / 1000, abs=0.01)
    detail = json.loads((await app.handle("GET", "https://w/api/bots/up", {}, now=NOW)).body)
    assert detail["execution"]["fills"] == 1

    text = (await app.handle("GET", "https://w/metrics", {}, now=NOW)).body
    assert 'tbf_last_tick_lag_seconds{mode="paper"} 4' in text.splitlines()
    assert 'tbf_bot_exec_delay_seconds{mode="paper",bot="up",stat="p95"}' in text
    assert 'tbf_bot_exec_gap_bps{mode="paper",bot="up"} -' in text
    h = json.loads((await app.handle("GET", "https://w/healthz", {}, now=NOW)).body)
    assert h["last_tick_lag_sec"] == 4.0

    await app.scheduled("5 0 * * *", NOW + 1)
    assert "対バックテスト" in http.posts[-1]


async def test_halt_flatten_is_not_measured():
    """停止時の手仕舞いは戦略の判断ではないので計測しない"""
    db = make_db()
    clock = lambda: NOW  # noqa: E731
    app = make_app([carry_bot()], db, {"binance": FakeMarket(3000), "hyperliquid": FakeMarket(3000)}, clock)
    await app.scheduled("* * * * *", NOW)
    n = len(await db.query("SELECT * FROM fills WHERE gap_bps IS NOT NULL"))
    assert n == 2
    await app.handle("POST", "https://w/api/bots/carry/halt?flatten=true", {}, now=NOW + 1)
    rows = await db.query("SELECT * FROM fills ORDER BY id")
    assert len(rows) == 4 and rows[2]["gap_bps"] is None and rows[3]["delay_ms"] is None


def test_preview_report_shows_execution():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("preview", Path(__file__).resolve().parent.parent / "scripts" / "preview.py")
    preview = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preview)
    bots = [{"id": "x", "status": "running", "equity": 1000, "metrics": {"trades": 0},
             "execution": {"delay_avg_sec": 2.04, "delay_p95_sec": 5.5, "gap_bps": 1.234, "gap_usd": 0.5}}]
    body = preview.render_report({}, (200, {}), (200, bots), 0)
    assert "2.0s / 5.5s | +1.2bps (+0.50 USD)" in body and "対バックテスト" in body
