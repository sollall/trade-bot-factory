import math

import pytest

from tbf import Bot, Decision, Fees, Risk, perp, spot
from tbf.backtest.engine import backtest
from tbf.core import indicators as ind
from tbf.core.bot import normalize_targets
from tbf.core.metrics import compute_metrics
from tbf.core.portfolio import Portfolio, apply_fill, equity, simulate_fill
from tbf.core.risk import check_risk
from tbf.core.types import Candle, EquityPoint, Fill, Position

H = 3_600_000
FREE = Fees(0, 0)


def candles(closes):
    return [Candle(i * H, closes[i - 1] if i else c, c, c, c, 1) for i, c in enumerate(closes)]


def make_bot(**kw):
    base = dict(
        id="bh", symbol="X", timeframe="1h", lookback=1, capital=1000,
        risk=Risk(max_drawdown=None), fees=FREE, strategy=lambda ctx: 1,
    )
    return Bot(**{**base, **kw})


# ---- bot 定義 ----

def test_symbol_shorthand_and_validation():
    b = make_bot()
    assert b.primary == "X" and b.instruments["X"].kind == "spot" and b.instruments["X"].fees == FREE
    with pytest.raises(ValueError):
        make_bot(id="Bad-Id")
    with pytest.raises(ValueError):
        make_bot(timeframe="2h")
    with pytest.raises(ValueError):
        make_bot(deploy=["prod"])
    with pytest.raises(ValueError):
        Bot(id="x", timeframe="1h", lookback=1, capital=1, strategy=lambda c: None)


def test_normalize_targets():
    b = Bot(id="m", instruments={"s": spot("A"), "p": perp("A")}, timeframe="1h", lookback=1, capital=1,
            leverage=3, strategy=lambda c: None)
    assert normalize_targets(b, None) == (None, None)
    assert normalize_targets(b, 0.5)[0] == {"s": 0.5, "p": 0.0}
    assert normalize_targets(b, {"p": -10})[0] == {"s": 0.0, "p": -3.0}  # 先物は ±leverage まで
    assert normalize_targets(b, {"s": -1, "p": math.nan})[0] == {"s": 0.0, "p": 0.0}  # 現物はロングのみ
    assert normalize_targets(b, Decision(weights={"s": 1}, reason="r")) == ({"s": 1.0, "p": 0.0}, "r")
    with pytest.raises(ValueError):
        normalize_targets(b, {"nope": 1})


# ---- 口座計算 ----

def test_spot_fill_accounting():
    s = spot("A", fees=FREE)
    pf = Portfolio.initial(1000, "binance")
    apply_fill(pf, s, Fill(0, "buy", 2, 100, 1, instrument="a"))
    assert pf.cash["binance"] == pytest.approx(799) and pf.holdings["a"] == Position(2, 100)
    f = apply_fill(pf, s, Fill(0, "sell", 5, 110, 1, instrument="a"))  # 持っている分しか売れない
    assert f.qty == 2 and f.reduce and f.realized_pnl == pytest.approx(19)
    assert pf.cash["binance"] == pytest.approx(1018) and "a" not in pf.holdings


def test_perp_fill_accounting_and_flip():
    p = perp("A", fees=FREE)
    pf = Portfolio(cash={"hyperliquid": 100})
    apply_fill(pf, p, Fill(0, "sell", 1, 100, 0.1, instrument="p"))  # ショート
    assert pf.cash["hyperliquid"] == pytest.approx(99.9)  # 先物は手数料だけ現金が動く
    assert equity(pf, {"p": p}, {"p": 90}) == pytest.approx(109.9)
    f = apply_fill(pf, p, Fill(0, "buy", 3, 90, 0, instrument="p"))  # 買い戻して 2 枚ロングへドテン
    assert f.reduce and f.realized_pnl == pytest.approx(10)
    assert pf.holdings["p"] == Position(2, 90)


def test_simulated_fill_slippage():
    f = simulate_fill("a", "buy", 1, 100, 0, Fees(0.001, 10))
    assert f.price == pytest.approx(100.1) and f.fee == pytest.approx(0.1001)


# ---- リスク・指標 ----

def test_risk_checks():
    base = dict(equity=100, peak_equity=100, day_start_equity=100, consecutive_losses=0)
    assert check_risk(Risk(max_drawdown=0.2), **base) is None
    assert "drawdown" in check_risk(Risk(max_drawdown=0.2), **{**base, "equity": 79})
    assert "daily" in check_risk(Risk(max_drawdown=None, max_daily_loss=0.05), **{**base, "equity": 94, "peak_equity": 200})
    assert "consecutive" in check_risk(Risk(max_drawdown=None, max_consecutive_losses=3), **{**base, "consecutive_losses": 3})


def test_metrics():
    m = compute_metrics(
        [EquityPoint(0, 100), EquityPoint(H, 120), EquityPoint(2 * H, 90), EquityPoint(3 * H, 110)],
        [Fill(0, "sell", 1, 1, 0, 5, reduce=True), Fill(1, "buy", 1, 1, 0, -2, reduce=True), Fill(2, "buy", 1, 1, 0)],
    )
    assert m.max_drawdown == pytest.approx(0.25)
    assert m.total_return == pytest.approx(0.1)
    assert m.trades == 2 and m.win_rate == 0.5
    assert m.profit_factor == pytest.approx(2.5)
    assert m.consecutive_losses == 1


def test_indicators():
    assert ind.sma([1, 2, 3, 4], 2) == 3.5
    assert ind.sma([1], 2) is None
    assert ind.rsi([1, 2, 3, 4, 5, 6], 3) == 100
    assert ind.ema([1, 1, 1, 1], 2) == 1


# ---- 1 銘柄のバックテスト (従来の bot がそのまま動くこと) ----

def test_backtest_executes_on_next_open():
    r = backtest(make_bot(), candles([100, 200, 200]))
    assert len(r.fills) == 1
    assert r.fills[0].price == 100  # 足 0 確定で判断 -> 足 1 の始値 (=100)
    assert r.metrics.end_equity == pytest.approx(2000)


def test_backtest_halts_and_flattens():
    r = backtest(make_bot(risk=Risk(max_drawdown=0.3)), candles([100, 100, 60, 60, 120]))
    assert "drawdown" in r.halted.reason
    assert not r.final.holdings
    assert r.metrics.end_equity == pytest.approx(600)


def test_buy_never_exceeds_cash_with_fees():
    r = backtest(make_bot(fees=Fees(0.001, 5)), candles([100, 100, 100]))
    assert r.final.cash["binance"] >= -1e-9


def test_strategy_state_persists_and_nan_is_flat():
    seen = []

    def strat(ctx):
        ctx.state["n"] = ctx.state.get("n", 0) + 1
        seen.append(ctx.state["n"])
        return Decision(math.nan) if ctx.state["n"] == 1 else None

    r = backtest(make_bot(strategy=strat), candles([1, 2, 3]))
    assert seen == [1, 2, 3]
    assert r.fills == []


def test_sample_bots_load():
    from tbf.registry import load_all

    ids = {b.id for b in load_all()}
    assert {"sma_cross", "rsi_reversion", "eth_funding_carry", "eth_funding_hedged"} <= ids
