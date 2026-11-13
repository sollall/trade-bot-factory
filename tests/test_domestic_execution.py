"""
国内業者のコストモデルと指値 (メイカー) 執行。

TRA-57。見張るのは次の3つ。
  1. メイカーがマイナス料率 (リベート) でも cost_rate / 口座計算が壊れないこと
  2. 指値が「戦略が見た値段に置いて 1 本ぶん待つ」で約定し、来なければ方針どおり落ちること
  3. 国内会場は本番 (live) に出せないままであること (LIVE_SUPPORTED は Binance 現物だけ)
"""

import pytest

from scripts import build_worker as build
from tbf import Bot, Fees, Risk, perp, spot
from tbf.backtest.data import load_bot_data
from tbf.backtest.engine import backtest
from tbf.core.instrument import DEFAULT_FEES, SYMBOL_FEES, Instrument, default_fees
from tbf.core.portfolio import Portfolio, apply_fill, limit_filled, simulate_fill
from tbf.core.types import Candle, FundingPoint
from tbf.registry import load_bot
from tbf.worker.brokers import LIVE_SUPPORTED, BinanceBroker, OrderRequest, PaperBroker

H = 3_600_000
CARRY_BOTS = ["btc_carry_breaker", "eth_carry_breaker"]

# TRA-50 §1-2 / §1-4 の実測表。(会場, 銘柄) -> (テイカー, メイカー, 片道スリッページ bps)
MEASURED = {
    ("gmo", "BTCUSDT"): (0.0005, -0.0001, 4.2),
    ("gmo", "ETHUSDT"): (0.0005, -0.0001, 2.6),
    ("bitbank", "BTCUSDT"): (0.0010, 0.0000, 0.4),
    ("bitbank", "ETHUSDT"): (0.0012, -0.0002, 2.4),
}


# ---- 1. 手数料表 ----

def test_default_fees_has_both_domestic_venues():
    assert ("gmo", "spot") in DEFAULT_FEES and ("bitbank", "spot") in DEFAULT_FEES


@pytest.mark.parametrize("key,expected", sorted(MEASURED.items()))
def test_domestic_fees_match_the_measured_table(key, expected):
    venue, symbol = key
    taker, maker, slip = expected
    f = default_fees(venue, "spot", symbol)
    assert (f.taker_rate, f.maker_rate, f.slippage_bps) == (taker, maker, slip)
    assert f.rate("market") == taker and f.rate("limit") == maker


def test_venue_default_is_the_more_expensive_of_its_symbols():
    """銘柄を指定し忘れたときに安い方へ倒れないこと (DEFAULT_FEES は保守側)"""
    for venue in ("gmo", "bitbank"):
        base = DEFAULT_FEES[(venue, "spot")]
        for (v, _, sym), f in SYMBOL_FEES.items():
            if v == venue:
                assert base.cost_rate >= f.cost_rate, (venue, sym)


def test_maker_rate_defaults_to_taker_when_not_measured():
    f = Fees(0.001, 5.0)
    assert f.maker_rate is None and f.rate("limit") == 0.001


# ---- 2. マイナス料率 ----

def test_negative_maker_rate_makes_cost_rate_negative():
    # リベートがスリッページより大きいと、約定代金に対するコストは負になる
    f = Fees(0.0005, 0.0, maker_rate=-0.0002)
    assert f.cost_rate_of("limit") == pytest.approx(-0.0002)
    assert f.cost_rate_of("limit") < 0 < f.cost_rate  # 成行側は正のまま
    # 実測値 (スリッページ込み) ではまだ正。順序だけ確かめる
    gmo = default_fees("gmo", "spot", "ETHUSDT")
    assert gmo.cost_rate_of("limit") < gmo.cost_rate_of("market")


def test_negative_fee_credits_cash_instead_of_charging_it():
    f = Fees(0.0, 0.0, maker_rate=-0.0002)
    fill = simulate_fill("spot", "buy", 1.0, 100.0, 0, f, "limit")
    assert fill.price == 100.0 and fill.fee == pytest.approx(-0.02)
    pf = Portfolio(cash={"gmo": 1000.0})
    inst = Instrument("gmo", "spot", "ETHUSDT", fees=f)
    apply_fill(pf, inst, fill)
    # 100 払って 0.02 受け取る => 現金は 900.02
    assert pf.cash["gmo"] == pytest.approx(900.02)
    assert pf.qty("spot") == 1.0


def test_negative_fee_survives_a_full_backtest():
    bars = [Candle(i * H, 100.0, 100.5, 99.5, 100.0, 1) for i in range(50)]
    rebate = Fees(0.0, 0.0, maker_rate=-0.0010)
    b = Bot(
        id="rebate", timeframe="1h", lookback=1, capital=1000, risk=Risk(max_drawdown=None),
        instruments={"spot": Instrument("gmo", "spot", "X", fees=rebate, order_type="limit")},
        strategy=lambda ctx: 1.0,
    )
    r = backtest(b, {"spot": bars})
    assert r.limit_orders > 0 and r.limit_fallbacks == 0
    assert r.fees < 0  # 手数料の合計がマイナス = リベートを受け取っている
    assert r.metrics.end_equity > 1000


# ---- 3. 指値の約定 / 未約定 ----

def test_limit_filled_uses_the_bar_range():
    assert limit_filled("buy", 100.0, low=99.0, high=101.0)
    assert not limit_filled("buy", 100.0, low=100.5, high=101.0)
    assert limit_filled("sell", 100.0, low=99.0, high=100.0)
    assert not limit_filled("sell", 100.0, low=98.0, high=99.5)


def test_limit_fill_has_no_slippage_and_uses_the_maker_rate():
    f = Fees(0.001, 50.0, maker_rate=-0.0001)
    market = simulate_fill("s", "buy", 1.0, 100.0, 0, f)
    limit = simulate_fill("s", "buy", 1.0, 100.0, 0, f, "limit")
    assert market.price == pytest.approx(100.5) and market.fee > 0
    assert limit.price == 100.0 and limit.fee < 0


def _gap_bot(order_type, unfilled, gap):
    """1 本目の終値 100 のあと、ずっと 100*gap で張り付く足 (買い指値 100 は約定しない)"""
    bars = [Candle(0, 100.0, 100.0, 100.0, 100.0, 1)]
    px = 100.0 * gap
    bars += [Candle(i * H, px, px, px, px, 1) for i in range(1, 6)]
    b = Bot(
        id="gapbot", timeframe="1h", lookback=1, capital=1000, risk=Risk(max_drawdown=None),
        instruments={"spot": Instrument("gmo", "spot", "X", fees=Fees(0.001, 10.0, maker_rate=-0.0001),
                                        order_type=order_type, unfilled=unfilled)},
        strategy=lambda ctx: 1.0,
    )
    return backtest(b, {"spot": bars})


def test_unfilled_limit_falls_back_to_market_at_the_bar_close():
    r = _gap_bot("limit", "market", 1.05)  # 値段が 5% 逃げたまま戻らない
    assert r.limit_orders >= 1 and r.limit_fallbacks >= 1 and r.limit_maker_fills == 0
    assert r.fallback_rate == 1.0
    # 落ちた先は足の終値 + 成行のスリッページ・テイカー料率
    first = r.fills[0]
    assert first.price == pytest.approx(105.0 * 1.001) and first.fee > 0


def test_unfilled_limit_can_skip_and_is_reissued_next_bar():
    r = _gap_bot("limit", "skip", 1.05)
    # 1 本目は見送り、2 本目は逃げた先の終値に置き直してメイカーで約定する
    assert (r.limit_orders, r.limit_skips, r.limit_fallbacks, r.limit_maker_fills) == (2, 1, 0, 1)
    assert r.fills[0].price == pytest.approx(105.0) and r.fills[0].fee < 0


def test_limit_fills_as_maker_when_the_price_comes_back():
    r = _gap_bot("limit", "market", 0.99)  # 下に走ったので買い指値 100 には届く
    assert r.limit_maker_fills >= 1 and r.limit_fallbacks == 0
    assert r.fills[0].price == pytest.approx(100.0) and r.fills[0].fee < 0


def test_market_orders_do_not_touch_the_limit_counters():
    r = _gap_bot("market", "market", 1.05)
    assert r.limit_orders == 0 and r.fallback_rate is None


# ---- 4. ペーパー / 本番のブローカー ----

def _req(inst, side, ref, limit=None, order_type=None):
    return OrderRequest("b", "spot", inst, side, 1.0, ref, "cid", 0, order_type, limit)


async def test_paper_broker_makes_a_maker_fill_when_the_price_is_there():
    inst = Instrument("gmo", "spot", "X", fees=Fees(0.001, 10.0, maker_rate=-0.0001), order_type="limit")
    res = await PaperBroker().execute(_req(inst, "buy", ref=99.0, limit=100.0))
    assert res.maker and res.fill.price == pytest.approx(100.0) and res.fill.fee < 0


async def test_paper_broker_falls_back_to_market_when_the_price_ran_away():
    inst = Instrument("gmo", "spot", "X", fees=Fees(0.001, 10.0, maker_rate=-0.0001), order_type="limit")
    res = await PaperBroker().execute(_req(inst, "buy", ref=101.0, limit=100.0))
    assert not res.maker and res.fill.price == pytest.approx(101.0 * 1.001) and res.fill.fee > 0


async def test_paper_broker_can_skip_instead():
    inst = Instrument("gmo", "spot", "X", fees=Fees(0.001, 10.0, maker_rate=-0.0001),
                      order_type="limit", unfilled="skip")
    assert await PaperBroker().execute(_req(inst, "buy", ref=101.0, limit=100.0)) is None


async def test_live_broker_refuses_limit_orders():
    inst = Instrument("binance", "spot", "BTCUSDT", order_type="limit")
    with pytest.raises(RuntimeError, match="指値"):
        await BinanceBroker(None, "k", "s").execute(_req(inst, "buy", ref=100.0))


# ---- 5. bot 定義 ----

def test_live_support_is_still_binance_spot_only():
    assert LIVE_SUPPORTED == {("binance", "spot")}


@pytest.mark.parametrize("bot_id", CARRY_BOTS)
def test_carry_bots_trade_spot_domestically_on_paper_only(bot_id):
    bot = load_bot(bot_id)
    spot_leg = bot.instruments["spot"]
    assert spot_leg.venue in ("gmo", "bitbank")
    assert spot_leg.order_type == "limit" and spot_leg.unfilled == "market"
    assert bot.deploy == ("paper",)
    # 価格データは Binance のまま (国内業者の JPY 足は取り込んでいない)
    assert spot_leg.price_source == ("binance", spot_leg.symbol)
    assert spot_leg.fees == default_fees(spot_leg.venue, "spot", spot_leg.symbol)


@pytest.mark.parametrize("bot_id", CARRY_BOTS)
def test_domestic_spot_cannot_be_deployed_live(bot_id):
    live = load_bot(bot_id).with_settings(deploy=("paper", "live"))
    assert any("spot" in e for e in build.live_errors(live))


@pytest.mark.parametrize("bot_id", CARRY_BOTS)
async def test_price_data_is_fetched_from_binance_not_the_domestic_venue(bot_id):
    bot = load_bot(bot_id)
    asked: list[tuple[str, str]] = []

    class FakeMarket:
        def __init__(self, name):
            self.name = name

        async def history(self, symbol, timeframe, start, end):
            asked.append((self.name, symbol))
            return [Candle(start + i * H, 1.0, 1.0, 1.0, 1.0, 1) for i in range(3)]

        async def funding(self, symbol, start, end):
            return [FundingPoint(start, 0.0, 0.0)]

    markets = {"binance": FakeMarket("binance"), "hyperliquid": FakeMarket("hyperliquid")}
    bars, _ = await load_bot_data(bot, 0, 3 * H, None, markets)
    assert set(bars) == {"spot", "perp"}
    assert ("binance", bot.instruments["spot"].symbol) in asked
    assert not any(v in ("gmo", "bitbank") for v, _ in asked)
