"""btc_vol_breakout: 高値抜けで入り、安値割れで出る。大きさは 目標ボラ ÷ 実現ボラ (0〜1)"""

import importlib
import math

from tbf.backtest.engine import backtest
from tbf.core.types import Candle
from tbf.market.coinbase import TIMEFRAMES
from tbf.registry import load_bot

BOT_ID = "btc_vol_breakout"
DAY = 86_400_000
T0 = 1_767_225_600_000  # 2026-01-01
mod = importlib.import_module(f"bots.{BOT_ID}.bot")


def bars(closes):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append(Candle(T0 + i * DAY, prev, max(prev, c), min(prev, c), c))
        prev = c
    return out


def test_bot_is_paper_only_and_reads_prices_from_coinbase():
    bot = load_bot(BOT_ID)
    assert bot.deploy == ("paper",)
    inst = bot.instruments[bot.primary]
    assert inst.kind == "spot" and inst.price_source == ("coinbase", "BTC-USD")
    assert bot.timeframe in TIMEFRAMES  # Coinbase に 4h 足は無い
    p = bot.params
    assert p["exit_n"] < p["entry_n"] and bot.lookback > max(p["entry_n"], p["vol_window"])


def test_realized_vol_is_annualised_stdev_of_daily_log_returns():
    closes = [100.0 * math.exp(0.01 * (i % 2)) for i in range(31)]  # 日次 ±1%
    vol = mod.realized_vol(closes, 30)
    assert abs(vol - 0.01 * math.sqrt(30 / 29) * math.sqrt(365)) < 1e-9
    assert mod.realized_vol(closes[:30], 30) is None


def test_target_size_is_clamped_to_0_1():
    quiet = [100.0] * 31
    wild = [100.0 * math.exp(0.05 * (i % 2)) for i in range(31)]  # 年率 約 97%
    assert mod.target_size(quiet, 0.4, 30) == 1.0
    size = mod.target_size(wild, 0.4, 30)
    assert abs(size - 0.4 / mod.realized_vol(wild, 30)) < 1e-12 and 0 < size < 0.5


def test_enters_on_breakout_and_exits_on_shorter_low_break():
    flat = [100.0 + 0.5 * (i % 2) for i in range(70)]
    up = [103.0 + i for i in range(15)]  # 20 本高値 (100.5) を上抜け
    down = [90.0] * 5  # 10 本安値を下抜け
    r = backtest(load_bot(BOT_ID), bars(flat + up + down))
    moves = [((e.time - T0) // DAY, e.kind) for e in r.events if e.kind in ("enter", "exit")]
    # 70 本目の確定で上抜け -> 71 本目の始値で買う。85 本目の確定で下抜け -> 86 本目の始値で売る
    assert moves == [(71, "enter"), (86, "exit")]
    assert all(f.side == "buy" for f in r.fills if not f.reduce)  # ロングだけ
    assert not r.final.holdings


def test_stays_flat_without_a_breakout():
    r = backtest(load_bot(BOT_ID), bars([100.0 + 0.5 * (i % 2) for i in range(120)]))
    assert r.fills == []


def test_position_is_smaller_when_volatility_is_high():
    wild = [100.0 * math.exp(0.05 * (i % 2)) for i in range(70)]
    r = backtest(load_bot(BOT_ID), bars(wild + [120.0, 121.0]))
    first = r.fills[0]
    weight = first.qty * first.price / 1000
    assert 0.3 < weight < 0.5  # 目標 40% ÷ 実現 約 97%
