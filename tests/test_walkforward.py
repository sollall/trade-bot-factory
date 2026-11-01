import json
import math
from datetime import datetime, timezone

import pytest

from tbf import Bot, Fees, Risk
from tbf.backtest.__main__ import main as backtest_main
from tbf.backtest.engine import backtest_async
from tbf.backtest.sweep import configure, parse_grid, score
from tbf.backtest.walkforward import DAY, chain, make_folds, walk_forward
from tbf.core.types import HOUR, Candle

T0 = 1_767_225_600_000  # 2026-01-01
FREE = Fees(0, 0)


def daily(closes):
    return [Candle(T0 + i * DAY, closes[i - 1] if i else c, c, c, c) for i, c in enumerate(closes)]


def long_or_flat(**kw):
    """params.side == 1 ならロング、0 ならノーポジ"""
    base = dict(
        id="lf", symbol="X", timeframe="1d", lookback=1, capital=1000, fees=FREE,
        risk=Risk(max_drawdown=None), params={"side": 0}, strategy=lambda ctx: ctx.params["side"],
    )
    return Bot(**{**base, **kw})


def test_make_folds():
    folds = make_folds(0, 100, 50, 20)
    assert folds == [(0, 50, 50, 70), (20, 70, 70, 90)]
    assert make_folds(0, 60, 50, 20) == []


def test_parse_grid_and_configure():
    combos = parse_grid(["a=1,2", "leverage=2,3"])
    assert combos == [{"a": 1, "leverage": 2}, {"a": 1, "leverage": 3}, {"a": 2, "leverage": 2}, {"a": 2, "leverage": 3}]
    b = configure(long_or_flat(), {"side": 1, "leverage": 2})
    assert b.params["side"] == 1 and b.leverage == 2.0
    assert parse_grid([]) == [{}]


async def test_trade_window_uses_history_but_trades_only_inside():
    seen = []

    def strat(ctx):
        seen.append(len(ctx.closes))
        return 1

    bot = long_or_flat(lookback=5, strategy=strat)
    bars = daily([100 + i for i in range(30)])
    r = await backtest_async(bot, {"X": bars}, trade_from=T0 + 10 * DAY, trade_until=T0 + 20 * DAY)
    assert r.equity[0].time == T0 + 10 * DAY and r.equity[-1].time == T0 + 20 * DAY
    assert all(f.time >= T0 + 11 * DAY for f in r.fills)  # 最初の判断は窓の 1 本目の確定後
    assert seen[0] == 5  # 窓より前の足が lookback ぶん渡されている
    assert r.bars_total == 10


async def test_chain_compounds_folds():
    bot = long_or_flat(params={"side": 1})
    a = await backtest_async(bot, {"X": daily([100, 100, 110, 110])})  # +10%
    b = await backtest_async(bot, {"X": daily([200, 200, 220, 220])})  # +10%
    c = chain(bot, [a, b])
    assert c.metrics.total_return == pytest.approx(0.21)
    assert c.equity[0].equity == 1000 and len(c.fills) == 2


async def test_walk_forward_picks_what_worked_in_training():
    # 前半 120 日は上昇、後半 120 日は下落
    closes = [100 * 1.015**i for i in range(120)] + [100 * 1.015**119 * 0.99**i for i in range(1, 121)]
    bars = {"X": daily(closes)}
    combos = parse_grid(["side=0,1"])
    res = await walk_forward(long_or_flat(), bars, {}, combos, train_days=60, test_days=30, objective="return")
    chosen = [f.chosen["side"] for f in res.folds]
    # 学習期間が上昇ならロング、下落ならノーポジを選ぶ
    assert chosen[0] == 1 and chosen[-1] == 0
    # 相場が変わった直後の窓では、学習期間に合わせた設定が外れて損をする
    assert any(f.test.metrics.total_return < 0 for f in res.folds)
    assert sum(res.choices.values()) == len(res.folds)
    # 全期間で見た最良はロング (上げ幅の方が大きいので)
    assert res.in_sample_combo == {"side": 1}


async def test_walk_forward_errors():
    bars = {"X": daily([100] * 50)}
    with pytest.raises(ValueError, match="2 つ以上"):
        await walk_forward(long_or_flat(), bars, {}, [{"side": 1}], 30, 10)
    with pytest.raises(ValueError, match="短すぎ"):
        await walk_forward(long_or_flat(), bars, {}, parse_grid(["side=0,1"]), 40, 20)


def test_score_objectives():
    from tbf.backtest.engine import BacktestResult
    from tbf.core.metrics import compute_metrics
    from tbf.core.portfolio import Portfolio
    from tbf.core.types import EquityPoint

    eq = [EquityPoint(0, 100), EquityPoint(DAY, 90), EquityPoint(2 * DAY, 120)]
    r = BacktestResult("x", compute_metrics(eq, []), eq, [], None, Portfolio())
    assert score(r, "return") == pytest.approx(0.2)
    assert score(r, "calmar") == pytest.approx(r.apr / 0.1)
    assert math.isfinite(score(r, "sharpe"))
    empty = BacktestResult("x", compute_metrics([], []), [], [], None, Portfolio())
    assert score(empty, "sharpe") == -math.inf


async def test_cli_walk_forward_from_cache(tmp_path, capsys):
    days = 200
    end = T0 + days * 24 * HOUR
    # 4 時間足ではなく bot の足 (1h) でキャッシュを置く
    hours = days * 24
    candles = [[T0 + i * HOUR, 3000, 3001, 2999, 3000, 1] for i in range(hours)]
    rates = [0.00003 if (i // (24 * 40)) % 2 == 0 else -0.00001 for i in range(hours)]
    funding = [[T0 + (i + 1) * HOUR, r, 0.0] for i, r in enumerate(rates)]
    (tmp_path / f"binance_spot_ETHUSDT_1h_{T0}_{end}.json").write_text(json.dumps({"candles": candles, "funding": []}))
    (tmp_path / f"hyperliquid_perp_ETH_1h_{T0}_{end}.json").write_text(json.dumps({"candles": candles, "funding": funding}))
    to = datetime.fromtimestamp(end / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat()
    code = await backtest_main(
        ["--bot", "eth_funding_carry", "--from", "2026-01-01", "--to", to, "--cache", str(tmp_path),
         "--out", str(tmp_path / "out"), "--sweep", "entry_apr=5,20", "--walk-forward", "60:30"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "ウォークフォワード: 学習 60 日 / 検証 30 日" in out and "検証期間をつないだ成績 (OOS)" in out
    report = json.loads((tmp_path / "out" / "eth_funding_carry.walkforward.json").read_text())
    assert len(report["folds"]) == 4 and report["oos"]["funding_income"] != 0
    assert "ウォークフォワード" in (tmp_path / "out" / "eth_funding_carry.walkforward.html").read_text()


async def test_cli_rejects_walk_forward_without_sweep():
    with pytest.raises(SystemExit):
        await backtest_main(["--bot", "eth_funding_carry", "--walk-forward", "60:30"])
