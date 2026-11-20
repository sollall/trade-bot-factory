"""
eth_btc_relval の損切り (STOP_Z / MAX_HOLD_HOURS) が歯止めとして働くことを見張る。

損切りの値は「結果を見てから選べない」ことが要件なので、固定値であること自体を検証する
(tests/test_carry_breaker.py と同じ考え方)。
"""

import importlib

from tbf.backtest.engine import backtest
from tbf.core.types import HOUR, Candle
from tbf.registry import load_bot

BOT_ID = "eth_btc_relval"
SPEC = {"STOP_Z": 4.0, "MAX_HOLD_HOURS": 120}
T0 = 1_767_225_600_000  # 2026-01-01
WINDOW = 336
SHIFT = 400  # ETH が 100 -> 101 に飛ぶ足 (それまでは平ら = z が計算できず何もしない)
# 水準が飛んでから k 本目の z は sqrt((WINDOW - k) / k)。STOP_Z=4 を下回るのは k=20 (z=3.97) で、
# その足の確定で建てる判断 -> 次の足の始値で約定するので、建つのは SHIFT + 20 本目。
ENTER = SHIFT + 20


def bars(prices):
    return [Candle(T0 + i * HOUR, p, p, p, p) for i, p in enumerate(prices)]


def run(eth_prices):
    bot = load_bot(BOT_ID).with_params(z_window=WINDOW)
    r = backtest(bot, {"eth": bars(eth_prices), "btc": bars([100.0] * len(eth_prices))}, {})
    moves = [((e.time - T0) // HOUR, e.kind) for e in r.events if e.kind in ("enter", "exit")]
    return r, moves


def test_stops_are_fixed_values_in_the_bot_source():
    mod = importlib.import_module(f"bots.{BOT_ID}.bot")
    assert {k: getattr(mod, k) for k in SPEC} == SPEC
    # params に損切りの鍵が無いこと = --sweep で探索できない
    assert set(load_bot(BOT_ID).params) == {"z_window", "entry_z", "exit_z", "leg_weight"}


def test_bot_is_paper_only_and_market_neutral():
    bot = load_bot(BOT_ID)
    assert bot.deploy == ("paper",)
    assert all(i.is_perp and i.venue == "hyperliquid" for i in bot.instruments.values())


def test_enters_against_the_rich_leg_and_exits_on_reversion():
    # ETH だけ上がる (ETH が割高) -> ETH ショート / BTC ロング。元の水準に戻ったら手仕舞い
    r, moves = run([100.0] * SHIFT + [101.0] * 40 + [100.0] * 60)
    assert moves == [(ENTER, "enter"), (SHIFT + 41, "exit")]
    opened = {f.instrument: f for f in r.fills if not f.reduce}
    assert opened["eth"].side == "sell" and opened["btc"].side == "buy"
    # 同額で反対 (差はスリッページぶんだけ)
    assert abs(opened["eth"].qty * opened["eth"].price - opened["btc"].qty * opened["btc"].price) < 5.0
    assert not r.final.holdings


def test_does_not_enter_while_z_is_beyond_the_stop():
    # 飛んだ直後は |z| >= STOP_Z。そこでは建てない (建てた瞬間に損切りになる)
    _, moves = run([100.0] * SHIFT + [101.0] * 19)
    assert moves == []


def test_time_stop_closes_after_max_hold_hours():
    # 水準が飛んだまま戻らない。z は 0.5 より上に 268 本残るので、先に時間切れになる
    r, moves = run([100.0] * SHIFT + [101.0] * 300)
    assert moves == [(ENTER, "enter"), (ENTER + SPEC["MAX_HOLD_HOURS"], "exit")]
    assert not r.final.holdings


def test_z_stop_closes_when_the_gap_widens_and_does_not_reenter():
    # 建てたあとさらに開く -> |z| >= STOP_Z で損切り。開いたままの間は建て直さない
    r, moves = run([100.0] * SHIFT + [101.0] * 30 + [110.0] * 300)
    assert moves == [(ENTER, "enter"), (SHIFT + 31, "exit")]
    assert sum(f.realized_pnl for f in r.fills) < 0  # 損切りなので負け
    assert not r.final.holdings
