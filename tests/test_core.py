import math

import pytest

from tbf import Bot, Decision, Fees, Risk
from tbf.backtest.engine import backtest
from tbf.core import indicators as ind
from tbf.core.metrics import compute_metrics
from tbf.core.portfolio import Portfolio, apply_fill, order_qty_for_target, simulate_fill
from tbf.core.risk import check_risk
from tbf.core.types import Candle, EquityPoint, Fill, Position

H = 3_600_000


def candles(closes):
    return [Candle(i * H, closes[i - 1] if i else c, c, c, c, 1) for i, c in enumerate(closes)]


def make_bot(**kw):
    base = dict(
        id="bh", symbol="X", timeframe="1h", lookback=1, capital=1000,
        risk=Risk(max_drawdown=None), fees=Fees(0, 0), strategy=lambda ctx: 1,
    )
    return Bot(**{**base, **kw})


def test_buy_to_target_never_exceeds_cash_even_with_slippage():
    p = Portfolio(1000)
    qty = order_qty_for_target(p, 100, 1, 0.01, 0.0015)
    after, _ = apply_fill(p, simulate_fill("buy", qty, 100, 0, 0.001, 5))
    assert after.cash >= 0
    assert after.position.qty == pytest.approx(qty)


def test_close_all_realizes_pnl_net_of_fee():
    p = Portfolio(0, Position(2, 100))
    assert order_qty_for_target(p, 110, 0, 0.5, 0) == -2
    after, fill = apply_fill(p, Fill(0, "sell", 2, 110, 1))
    assert fill.realized_pnl == pytest.approx(19)
    assert after.cash == pytest.approx(219)
    assert after.position.qty == 0


def test_small_rebalance_is_ignored():
    assert order_qty_for_target(Portfolio(5, Position(9.95, 100)), 100, 1, 0.01, 0) == 0


def test_risk_checks():
    base = dict(equity=100, peak_equity=100, day_start_equity=100, consecutive_losses=0)
    assert check_risk(Risk(max_drawdown=0.2), **base) is None
    assert "drawdown" in check_risk(Risk(max_drawdown=0.2), **{**base, "equity": 79})
    assert "daily" in check_risk(Risk(max_drawdown=None, max_daily_loss=0.05), **{**base, "equity": 94, "peak_equity": 200})
    assert "consecutive" in check_risk(Risk(max_drawdown=None, max_consecutive_losses=3), **{**base, "consecutive_losses": 3})


def test_metrics():
    m = compute_metrics(
        [EquityPoint(0, 100), EquityPoint(H, 120), EquityPoint(2 * H, 90), EquityPoint(3 * H, 110)],
        [Fill(0, "sell", 1, 1, 0, 5), Fill(1, "sell", 1, 1, 0, -2)],
    )
    assert m.max_drawdown == pytest.approx(0.25)
    assert m.total_return == pytest.approx(0.1)
    assert m.win_rate == 0.5
    assert m.profit_factor == pytest.approx(2.5)
    assert m.consecutive_losses == 1


def test_indicators():
    assert ind.sma([1, 2, 3, 4], 2) == 3.5
    assert ind.sma([1], 2) is None
    assert ind.rsi([1, 2, 3, 4, 5, 6], 3) == 100
    assert ind.ema([1, 1, 1, 1], 2) == 1


def test_backtest_executes_on_next_open():
    r = backtest(make_bot(), candles([100, 200, 200]))
    assert len(r.fills) == 1
    assert r.fills[0].price == 100  # 足 0 確定で判断 -> 足 1 の始値 (=100)
    assert r.metrics.end_equity == pytest.approx(2000)


def test_backtest_halts_and_flattens():
    r = backtest(make_bot(risk=Risk(max_drawdown=0.3)), candles([100, 100, 60, 60, 120]))
    assert "drawdown" in r.halted.reason
    assert r.final.position.qty == 0
    assert r.metrics.end_equity == pytest.approx(600)


def test_strategy_state_persists_and_return_types():
    seen = []

    def strat(ctx):
        ctx.state["n"] = ctx.state.get("n", 0) + 1
        seen.append(ctx.state["n"])
        return Decision(math.nan) if ctx.state["n"] == 1 else None

    r = backtest(make_bot(strategy=strat), candles([1, 2, 3]))
    assert seen == [1, 2, 3]
    assert r.fills == []  # NaN は 0 として扱われる


def test_bot_validation():
    with pytest.raises(ValueError):
        make_bot(id="Bad-Id")
    with pytest.raises(ValueError):
        make_bot(timeframe="2h")
    with pytest.raises(ValueError):
        make_bot(deploy=["prod"])


def test_sample_bots_load():
    from tbf.registry import load_all

    ids = [b.id for b in load_all()]
    assert "sma_cross" in ids
