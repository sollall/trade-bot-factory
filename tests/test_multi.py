"""複数銘柄 (現物 + 先物, 取引所間) のバックテスト"""

import json
import math
import random
from datetime import datetime, timezone

import pytest

from tbf import Bot, Fees, Risk, perp, spot
from tbf.backtest.__main__ import main as backtest_main
from tbf.backtest.engine import backtest
from tbf.core.types import HOUR, Candle, FundingPoint
from tbf.market.hyperliquid import fetch_candles, fetch_funding
from tbf.registry import load_bot

T0 = 1_767_225_600_000  # 2026-01-01
FREE = Fees(0, 0)


def flat_bars(prices, highs=None, lows=None):
    return [
        Candle(T0 + i * HOUR, prices[i - 1] if i else p, (highs or prices)[i], (lows or prices)[i], p)
        for i, p in enumerate(prices)
    ]


def funding_series(rates, premium=0.0):
    return [FundingPoint(T0 + (i + 1) * HOUR, r, premium) for i, r in enumerate(rates)]


def hedged(**kw):
    def strat(ctx):
        if ctx.holding("perp").qty == 0:
            w = ctx.neutral_weight()
            return {"spot": w, "perp": -w}
        return None

    base = dict(
        id="h", instruments={"spot": spot("X", fees=FREE), "perp": perp("X", fees=FREE)},
        timeframe="1h", lookback=1, capital=10_000, leverage=3, transfer_fee_usd=0, cash_buffer=0,
        risk=Risk(max_drawdown=None), strategy=strat,
    )
    return Bot(**{**base, **kw})


def run(bot, prices, rates, **kw):
    bars = flat_bars(prices, kw.get("highs"), kw.get("lows"))
    return backtest(bot, {"spot": bars, "perp": bars}, {"perp": funding_series(rates)})


def test_flat_market_keeps_capital():
    r = run(hedged(), [100.0] * 50, [0.0] * 50)
    assert r.metrics.end_equity == pytest.approx(10_000)
    assert r.count("enter") == 1
    # 初期資金は現物の取引所にあり、先物の証拠金ぶんが移されている
    assert r.final.cash["hyperliquid"] == pytest.approx(10_000 * 0.75 / 3)


def test_funding_income_matches_notional_times_rate():
    rate = 0.0001
    r = run(hedged(), [100.0] * 101, [rate] * 101)
    # 最初の足の確定で判断し、次の足の始値で建てる -> 受け取るのは 100 時間分
    assert r.funding_income == pytest.approx(7_500 * rate * 100, rel=1e-6)
    assert r.metrics.end_equity == pytest.approx(10_000 + r.funding_income)
    assert r.price_pnl == pytest.approx(0, abs=1e-6)


def test_random_walk_is_hedged_and_rebalanced():
    random.seed(7)
    p, prices, highs, lows = 3000.0, [], [], []
    for _ in range(3000):
        o = p
        p *= math.exp(random.gauss(0, 0.008))
        prices.append(p)
        highs.append(max(o, p) * 1.002)
        lows.append(min(o, p) * 0.998)
    r = run(hedged(), prices, [0.00002] * 3000, highs=highs, lows=lows)
    assert r.count("rebalance") > 5 and r.count("liquidation") == 0
    # 値動きは完全に打ち消され、資産 = 元本 + ファンディング
    assert r.price_pnl == pytest.approx(0, abs=1e-6)
    assert r.final.holdings["spot"].qty == pytest.approx(-r.final.holdings["perp"].qty)


def test_spike_liquidates_and_unwinds():
    prices = [100.0] * 10
    highs = list(prices)
    highs[5] = 125.0  # 5 倍レバなら +20% 弱で清算
    r = run(hedged(leverage=5), prices, [0.0] * 10, highs=highs)
    assert r.count("liquidation") == 1
    # 証拠金 (資金の 1/6) を失い、外れた現物は手仕舞い、次の足から建て直している
    assert r.count("enter") == 2
    assert r.metrics.end_equity == pytest.approx(10_000 * 5 / 6, rel=1e-6)


def test_fees_and_transfer_costs():
    fees = Fees(0.001, 0)
    bot = hedged(instruments={"spot": spot("X", fees=fees), "perp": perp("X", fees=fees)}, transfer_fee_usd=1, cash_buffer=0.005)
    r = run(bot, [100.0] * 10, [0.0] * 10)
    notional = 7_500 * 0.995
    assert r.fees == pytest.approx(notional * 0.001 * 2, rel=1e-3)
    assert r.transfer_costs == 1
    assert r.metrics.end_equity == pytest.approx(10_000 - r.fees - 1)
    assert r.final.holdings["spot"].qty == pytest.approx(-r.final.holdings["perp"].qty)


def test_pair_on_one_venue_needs_no_transfer():
    def strat(ctx):
        return {"a": 0.5, "b": -0.5}

    bot = Bot(
        id="pair", instruments={"a": perp("A", fees=FREE), "b": perp("B", fees=FREE)}, timeframe="1h", lookback=1,
        capital=1_000, transfer_fee_usd=5, risk=Risk(max_drawdown=None), strategy=strat,
    )
    a = flat_bars([100, 100, 110, 110])
    b = flat_bars([50, 50, 50, 50])
    # 初期資金を先物の取引所に置く (主銘柄が hyperliquid の先物なので自動でそうなる)
    r = backtest(bot, {"a": a, "b": b}, {"a": [], "b": []})
    assert r.transfer_costs == 0
    assert r.metrics.end_equity == pytest.approx(1_000 + 5 * 10)  # A を 5 枚ロング -> +10 で +50


def test_carry_bot_enters_and_exits_on_funding():
    bot = load_bot("eth_funding_carry").with_params(lookback_hours=8, entry_apr=10, exit_apr=0, min_hold_hours=4)
    rates = [0.00002] * 30 + [-0.00002] * 60  # 年率 +17.5% -> -17.5%
    bars = flat_bars([3000.0] * 90)
    r = backtest(bot, {"spot": bars, "perp": bars}, {"perp": funding_series(rates)})
    assert [e.kind for e in r.events if e.kind in ("enter", "exit")] == ["enter", "exit"]
    assert 0 < r.bars_in_market < 90


class FakeHL:
    """Hyperliquid info API の代役 (1 回あたりの件数を絞ってページングを検証)"""

    async def request(self, method, url, headers=None, body=None):
        req = json.loads(body)
        if req["type"] == "fundingHistory":
            rows = [
                {"coin": "ETH", "fundingRate": "0.0000125", "premium": "0.0001", "time": T0 + h * HOUR + 7}
                for h in range(1, 24)
                if req["startTime"] <= T0 + h * HOUR + 7 <= req["endTime"]
            ][:5]
            return 200, json.dumps(rows)
        if req["type"] == "candleSnapshot":
            q = req["req"]
            rows = [
                {"t": T0 + h * HOUR, "o": "100", "h": "101", "l": "99", "c": "100", "v": "1"}
                for h in range(23)
                if q["startTime"] <= T0 + h * HOUR <= q["endTime"]
            ][:7]
            return 200, json.dumps(rows)
        return 400, "bad"


async def test_hyperliquid_fetch_paginates():
    end = T0 + 24 * HOUR
    f = await fetch_funding(FakeHL(), "ETH", T0, end)
    c = await fetch_candles(FakeHL(), "ETH", "1h", T0, end)
    assert len(f) == 23 and f[0].time == T0 + HOUR  # +7ms は時間単位に丸める
    assert len(c) == 23 and c[-1].time == T0 + 22 * HOUR


async def test_cli_compares_bots_from_cache(tmp_path, capsys):
    hours = 300
    end = T0 + hours * HOUR
    candles = [[T0 + i * HOUR, 3000, 3001, 2999, 3000, 1] for i in range(hours)]
    funding = [[T0 + (i + 1) * HOUR, 0.00002, 0.0] for i in range(hours)]
    (tmp_path / f"binance_spot_ETHUSDT_1h_{T0}_{end}.json").write_text(json.dumps({"candles": candles, "funding": []}))
    (tmp_path / f"hyperliquid_perp_ETH_1h_{T0}_{end}.json").write_text(json.dumps({"candles": candles, "funding": funding}))
    to = datetime.fromtimestamp(end / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat()
    code = await backtest_main(
        ["--bot", "eth_funding_carry,eth_funding_hedged", "--from", "2026-01-01", "--to", to,
         "--cache", str(tmp_path), "--out", str(tmp_path / "out"), "--sweep", "leverage=2,3"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "=== 比較" in out and "leverage=2" in out and "leverage=3" in out
    report = json.loads((tmp_path / "out" / "eth_funding_carry.json").read_text())
    assert len(report["runs"]) == 2 and report["runs"][0]["funding_income"] > 0
    assert (tmp_path / "out" / "compare.html").read_text().startswith("<!doctype html>")
