import json
from datetime import datetime, timezone

import pytest

from tbf.neutral.__main__ import main as neutral_main
from tbf.neutral.data import HOUR, FundingPoint, HourBar, fetch_funding, fetch_hour_bars
from tbf.neutral.engine import NeutralParams, backtest_neutral
from tbf.neutral.strategies import NeutralContext, always_in, funding_threshold

T0 = 1_767_225_600_000  # 2026-01-01
FREE = dict(spot_fee=0, perp_fee=0, slippage_bps=0, transfer_fee_usd=0, cash_buffer=0)


def series(prices, rates, premium=0.0, highs=None):
    bars = [HourBar(T0 + i * HOUR, p, (highs or prices)[i], p, p) for i, p in enumerate(prices)]
    funding = [FundingPoint(T0 + (i + 1) * HOUR, r, premium) for i, r in enumerate(rates)]
    return bars, funding


def test_flat_market_no_costs_keeps_capital():
    bars, funding = series([100.0] * 50, [0.0] * 50)
    r = backtest_neutral(bars, funding, always_in, NeutralParams(**FREE))
    assert r.end_equity == pytest.approx(10_000)
    assert r.count("enter") == 1


def test_funding_income_matches_notional_times_rate():
    rate = 0.0001
    bars, funding = series([100.0] * 101, [rate] * 101)
    r = backtest_neutral(bars, funding, always_in, NeutralParams(leverage=3, **FREE))
    notional = 10_000 * 3 / 4
    # 最初の足の終わりに建てるので、受け取るのはその後の 100 時間分
    assert r.funding_income == pytest.approx(notional * rate * 100, rel=1e-6)
    assert r.end_equity == pytest.approx(10_000 + r.funding_income)
    assert r.price_pnl == pytest.approx(0, abs=1e-6)


def test_price_trend_is_hedged_and_rebalanced():
    prices = [100 * 1.003**i for i in range(200)]  # 約 +80%
    bars, funding = series(prices, [0.0] * 200)
    r = backtest_neutral(bars, funding, always_in, NeutralParams(leverage=3, **FREE))
    assert r.end_equity == pytest.approx(10_000, rel=1e-6)
    assert r.count("rebalance") > 0 and r.count("liquidation") == 0


def test_spike_liquidates_short_and_unwinds_spot():
    prices = [100.0] * 10
    highs = list(prices)
    highs[5] = 125.0  # 5 倍レバなら +20% 弱で清算
    bars, funding = series(prices, [0.0] * 10, highs=highs)
    r = backtest_neutral(bars, funding, always_in, NeutralParams(leverage=5, **FREE))
    assert r.count("liquidation") == 1
    # 証拠金 (資金の 1/6) を失い、現物は手仕舞って再び建て直している
    assert r.end_equity == pytest.approx(10_000 * 5 / 6, rel=1e-6)
    assert r.count("enter") == 2


def test_fees_and_transfer_costs_are_charged():
    bars, funding = series([100.0] * 10, [0.0] * 10)
    p = NeutralParams(leverage=3, spot_fee=0.001, perp_fee=0.001, slippage_bps=0, transfer_fee_usd=1, cash_buffer=0)
    r = backtest_neutral(bars, funding, always_in, p)
    assert r.fees == pytest.approx(7_500 * 0.001 * 2)
    assert r.transfer_costs == 1
    assert r.end_equity == pytest.approx(10_000 - 15 - 1)


def test_threshold_strategy_enters_and_exits():
    rates = [0.00002] * 30 + [-0.00002] * 60  # 年率 +17.5% -> -17.5%
    bars, funding = series([100.0] * 90, rates)
    r = backtest_neutral(
        bars, funding, funding_threshold, NeutralParams(**FREE),
        {"lookback_hours": 8, "entry_apr": 10, "exit_apr": 0, "min_hold_hours": 4},
    )
    kinds = [e.kind for e in r.events]
    assert kinds == ["enter", "exit"]
    assert r.hours_in_market < 90


def test_context_funding_apr():
    ctx = NeutralContext(0, [FundingPoint(0, 0.0001, 0)] * 3, [], 0, 0, {})
    assert ctx.funding_apr(3) == pytest.approx(0.0001 * 24 * 365 * 100)
    assert ctx.funding_apr(4) is None


def test_params_split():
    engine, strat = NeutralParams.split({"leverage": 2, "entry_apr": 5})
    assert engine.leverage == 2 and strat == {"entry_apr": 5}


class FakeHL:
    """Hyperliquid info API の代役 (1 回あたりの件数を絞ってページングを検証)"""

    def __init__(self, hours):
        self.hours = hours
        self.calls = 0

    async def request(self, method, url, headers=None, body=None):
        self.calls += 1
        req = json.loads(body)
        if req["type"] == "fundingHistory":
            rows = [
                {"coin": "ETH", "fundingRate": "0.0000125", "premium": "0.0001", "time": T0 + h * HOUR + 7}
                for h in range(1, self.hours + 1)
                if req["startTime"] <= T0 + h * HOUR + 7 <= req["endTime"]
            ][:5]
            return 200, json.dumps(rows)
        if req["type"] == "candleSnapshot":
            q = req["req"]
            rows = [
                {"t": T0 + h * HOUR, "o": "100", "h": "101", "l": "99", "c": "100"}
                for h in range(self.hours)
                if q["startTime"] <= T0 + h * HOUR <= q["endTime"]
            ][:7]
            return 200, json.dumps(rows)
        return 400, "bad"


async def test_fetch_paginates_and_rounds_hours():
    http = FakeHL(23)
    end = T0 + 24 * HOUR
    f = await fetch_funding(http, "ETH", T0, end)
    b = await fetch_hour_bars(http, "ETH", T0, end)
    assert len(f) == 23 and f[0].time == T0 + HOUR  # +7ms は丸められる
    assert len(b) == 23 and b[-1].time == T0 + 22 * HOUR


async def test_cli_runs_from_cache(tmp_path, capsys):
    hours = 300
    bars = [[T0 + i * HOUR, 100, 101, 99, 100] for i in range(hours)]
    funding = [[T0 + (i + 1) * HOUR, 0.00002, 0.0001] for i in range(hours)]
    end = T0 + hours * HOUR
    (tmp_path / f"hl_ETH_{T0}_{end}.json").write_text(json.dumps({"bars": bars, "funding": funding}))
    to = datetime.fromtimestamp(end / 1000, tz=timezone.utc).replace(tzinfo=None).isoformat()
    code = await neutral_main(
        ["--coin", "ETH", "--from", "2026-01-01", "--to", to, "--cache", str(tmp_path), "--out", str(tmp_path / "out"),
         "--sweep", "entry_apr=5,50"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "持ちっぱなし" in out and "entry_apr=5" in out and "entry_apr=50" in out
    report = json.loads((tmp_path / "out" / "neutral_ETH.json").read_text())
    assert len(report["runs"]) == 2
    assert (tmp_path / "out" / "neutral_ETH.html").read_text().startswith("<!doctype html>")
