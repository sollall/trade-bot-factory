"""
ファンディング回路遮断器 (採用基準 §1-4-1) の歯止めとしての性質を見張る。

遮断器は「結果を見てから閾値を選べない」ことが要件なので、閾値が固定値であること自体が
検証対象になる。build_worker.py は bots/<id>/ の中だけを Worker に運ぶため固定値は bot ごとに
書き写すしかなく、ここで写し間違いとずれを捕まえる。
"""

import importlib

import pytest

from tbf.core.types import HOUR, Candle, FundingPoint
from tbf.backtest.engine import backtest
from tbf.registry import load_bot

BREAKER_BOTS = ["btc_carry_breaker", "eth_carry_breaker"]
SPEC = {"WINDOW_HOURS": 72, "BREAK_APR": -25.0, "RESTORE_APR": -10.0}
T0 = 1_767_225_600_000  # 2026-01-01


def apr_to_rate(apr: float) -> float:
    """年率 (%) -> 1 時間あたりのファンディング率"""
    return apr / (24 * 365 * 100)


@pytest.mark.parametrize("bot_id", BREAKER_BOTS)
def test_thresholds_are_fixed_values_in_the_bot_source(bot_id):
    mod = importlib.import_module(f"bots.{bot_id}.bot")
    assert {k: getattr(mod, k) for k in SPEC} == SPEC
    # params が空であること = --sweep に渡せる鍵がない = 閾値を探索できない
    assert load_bot(bot_id).params == {}


def test_both_breakers_use_the_same_thresholds():
    mods = [importlib.import_module(f"bots.{i}.bot") for i in BREAKER_BOTS]
    assert len({tuple(getattr(m, k) for k in SPEC) for m in mods}) == 1


@pytest.mark.parametrize("bot_id", BREAKER_BOTS)
def test_breaker_opens_below_minus_25_and_restores_at_minus_10(bot_id):
    # 平均が窓いっぱいに入れ替わるよう、各相場を 72 時間より長く続ける
    aprs = [5.0] * 100 + [-40.0] * 100 + [0.0] * 100
    rates = [apr_to_rate(a) for a in aprs]
    bars = [Candle(T0 + i * HOUR, 100.0, 100.0, 100.0, 100.0) for i in range(len(rates))]
    funding = [FundingPoint(T0 + (i + 1) * HOUR, r, 0.0) for i, r in enumerate(rates)]

    r = backtest(load_bot(bot_id), {"spot": bars, "perp": bars}, {"perp": funding})
    moves = [e for e in r.events if e.kind in ("enter", "exit")]
    # 建てる -> 遮断で畳む -> 回復して建て直す
    assert [e.kind for e in moves] == ["enter", "exit", "enter"]
    # 畳む / 建て直す時刻が閾値どおりであること。-40% が 72 時間平均を -25% 未満に押し下げるのは
    # 49 時間目、0% が -10% 以上に戻すのは 54 時間目。どちらも閾値から一意に決まる。
    assert [(e.time - T0) // HOUR for e in moves[1:]] == [100 + 49, 200 + 54]
    assert r.final.holdings["perp"].qty != 0  # 最後は建っている (= 復帰した)
    assert r.count("liquidation") == 0


@pytest.mark.parametrize("bot_id", BREAKER_BOTS)
def test_breaker_stays_closed_while_funding_history_is_short(bot_id):
    # 窓を満たすデータが無い間は「事故の証拠がない」とみなして建てる (None で止めない)
    rates = [apr_to_rate(-40.0)] * 10
    bars = [Candle(T0 + i * HOUR, 100.0, 100.0, 100.0, 100.0) for i in range(len(rates))]
    funding = [FundingPoint(T0 + (i + 1) * HOUR, r, 0.0) for i, r in enumerate(rates)]

    r = backtest(load_bot(bot_id), {"spot": bars, "perp": bars}, {"perp": funding})
    assert r.count("enter") == 1 and r.count("exit") == 0
    assert r.final.holdings["perp"].qty != 0
