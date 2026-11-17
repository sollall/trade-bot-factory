"""
価格データの取得元。TRA-81 で paper の 6 bot が毎 tick 全滅した件の歯止め。

起きたこと: Binance は Cloudflare の US colo からの通信を必ず弾く (403 / 451 地域制限) ので、
Worker では約半分の tick で 1 件も価格が取れない。一方バックテストは手元から同じ URL を
200 で取れるため、テストは 107 passed のまま通ってしまっていた。
対応は現物の取得元を Coinbase に移すこと (実測の比較は docs/market-data.md)。

見張るのは次の4つ。
  1. bot の価格の取得元が「実装がある・Workers から届く・その足がある」こと。
     どれか欠けたらデプロイ前に落ちる
  2. バックテストと Worker が同じ取得元・同じホストを使うこと
  3. Coinbase の足の取り込み (新しい順・1 回 300 本・進行中の足を混ぜない)
  4. 共有IPの流量制限 (429) で tick を落とさないこと
"""

import pytest

from scripts import build_worker as build
from tbf import spot
from tbf.core.types import TIMEFRAME_MS
from tbf.http import RETRY_STATUSES, request_retrying
from tbf.market import MARKET_VENUES, UNREACHABLE_FROM_WORKERS, default_markets, venue_timeframes
from tbf.market.binance import MARKET_DATA_HOSTS
from tbf.market.coinbase import EXCHANGE_URL, MAX_CANDLES, TIMEFRAMES, CoinbaseMarket, fetch_candles
from tbf.market.hyperliquid import HyperliquidMarket
from tbf.registry import bot_ids, load_bot
from tbf.worker.app import Settings

H = TIMEFRAME_MS["1h"]


class FakeHttp:
    """URL ごとに (status, body) を返す。status を 2 回目以降で変えるために呼び出しも数える"""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []

    async def request(self, method, url, headers=None, body=None):
        self.calls.append(url)
        return self.handler(url, len(self.calls))


def coinbase_candles(url: str, _n: int = 1) -> tuple[int, str]:
    """Coinbase と同じ形で返す: 新しい順・[時刻(秒), 安値, 高値, 始値, 終値, 出来高]"""
    import json
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    g, start, end = (int(q[k][0]) for k in ("granularity", "start", "end"))
    if (end - start) // g + 1 > MAX_CANDLES:  # 本物は 400 を返す
        return 400, '{"message":"granularity too small for the requested time range."}'
    rows = [[t, "1", "3", "2", str(t), "1"] for t in range(end // g * g, start - 1, -g)]
    return 200, json.dumps(rows)


# ---- 1. bot の価格が本番で取れることをデプロイ前に確かめる ----

@pytest.mark.parametrize("bot_id", bot_ids())
def test_every_bot_can_get_a_price_on_the_worker(bot_id):
    bot = load_bot(bot_id)
    for alias, inst in bot.instruments.items():
        assert inst.price_venue in MARKET_VENUES, (bot_id, alias, inst.price_venue)
        assert inst.price_venue not in UNREACHABLE_FROM_WORKERS, (bot_id, alias, inst.price_venue)
    assert not build.price_errors(bot)


def test_build_refuses_a_bot_whose_price_venue_has_no_implementation():
    """国内業者で建てる bot が price_source を付け忘れたら、デプロイ前に落ちること"""
    bot = load_bot("btc_carry_breaker")
    broken = bot.with_settings(instruments={**bot.instruments, "spot": spot("BTCUSDT", venue="gmo")})
    errors = build.price_errors(broken)
    assert len(errors) == 1 and "gmo" in errors[0] and "spot" in errors[0]


def test_build_refuses_a_price_venue_that_workers_cannot_reach():
    """TRA-81 の再発防止: Binance から価格を取る bot はデプロイできない"""
    bot = load_bot("sma_cross")
    broken = bot.with_settings(
        instruments={"BTCUSDT": spot("BTCUSDT", price_source=("binance", "BTCUSDT"))}
    )
    errors = build.price_errors(broken)
    assert len(errors) == 1 and "binance" in errors[0] and "Workers" in errors[0]


def test_build_refuses_a_timeframe_the_price_venue_does_not_have():
    """Coinbase に 30m・4h 足は無い。使おうとしたらデプロイ前に落ちること"""
    for missing in ("30m", "4h"):
        errors = build.price_errors(load_bot("sma_cross").with_settings(timeframe=missing))
        assert len(errors) == 1 and missing in errors[0] and "coinbase" in errors[0]


def test_market_venues_matches_what_default_markets_builds():
    assert set(MARKET_VENUES) == set(default_markets(FakeHttp(lambda u, n: (200, "{}"))))


def test_coinbase_timeframes_are_the_ones_the_api_accepts():
    assert set(TIMEFRAMES) <= set(TIMEFRAME_MS) and venue_timeframes("coinbase") == TIMEFRAMES
    assert "30m" not in TIMEFRAMES and "4h" not in TIMEFRAMES
    assert venue_timeframes("hyperliquid") == ()  # 制限なし


# ---- 2. バックテストと Worker が同じ出所を使う ----

def test_backtest_and_worker_use_the_same_source():
    """完了条件: バックテストで使う価格と本番で使う価格が同じ出所であること"""
    worker_url = Settings.from_env({"MODE": "paper"}.get).coinbase_url
    backtest = default_markets(FakeHttp(lambda u, n: (200, "{}")))["coinbase"]
    assert isinstance(backtest, CoinbaseMarket)
    assert worker_url == backtest.base_url == EXCHANGE_URL


def test_coinbase_url_env_overrides_the_default():
    env = {"MODE": "paper", "COINBASE_URL": "https://example.test"}
    assert Settings.from_env(env.get).coinbase_url == "https://example.test"


def test_binance_is_kept_only_for_local_comparison():
    """手元からは取れるので残してあるが、本番の取得元にはしない"""
    assert "binance" in MARKET_VENUES and "binance" in UNREACHABLE_FROM_WORKERS
    assert MARKET_DATA_HOSTS[0] == "https://api-gcp.binance.com"


# ---- 3. Coinbase の足の取り込み ----

async def test_candles_come_back_oldest_first_and_without_the_open_candle():
    http = FakeHttp(coinbase_candles)
    now = 1_800_000_000_000 // H * H
    candles = await fetch_candles(http, "BTC-USD", "1h", now - 5 * H, now)
    assert [c.time for c in candles] == [now - 5 * H + i * H for i in range(5)]  # now の足は未確定
    assert candles[0].open == 2.0 and candles[0].high == 3.0 and candles[0].low == 1.0


async def test_long_ranges_are_split_into_requests_of_300_candles():
    """バックテストの既定 180 日 (4320 本) が 400 にならずに取れること"""
    http = FakeHttp(coinbase_candles)
    now = 1_800_000_000_000 // H * H
    candles = await fetch_candles(http, "BTC-USD", "1h", now - 4320 * H, now)
    assert len(candles) == 4320
    assert len(http.calls) == 15  # 4320 / 300
    assert all(c.time < candles[i + 1].time for i, c in enumerate(candles[:-1]))


async def test_recent_returns_exactly_the_requested_number_of_closed_candles():
    market = CoinbaseMarket(FakeHttp(coinbase_candles))
    now = 1_800_000_000_000 // H * H + 30_000  # 足の途中
    candles = await market.recent("BTC-USD", "1h", 60, now)
    assert len(candles) == 60 and candles[-1].time + H <= now


async def test_a_timeframe_coinbase_does_not_have_fails_loudly():
    market = CoinbaseMarket(FakeHttp(coinbase_candles))
    with pytest.raises(RuntimeError, match="30m"):
        await market.recent("BTC-USD", "30m", 10, 1_800_000_000_000)


async def test_prices_are_read_per_product():
    http = FakeHttp(lambda u, n: (200, '{"price":"3000.5","bid":"3000","ask":"3001"}'))
    assert await CoinbaseMarket(http).prices(["ETH-USD", "BTC-USD"]) == {"BTC-USD": 3000.5, "ETH-USD": 3000.5}
    assert http.calls == [f"{EXCHANGE_URL}/products/{p}/ticker" for p in ("BTC-USD", "ETH-USD")]


async def test_spot_has_no_funding():
    assert await CoinbaseMarket(FakeHttp(coinbase_candles)).funding("BTC-USD", 0, H) == []


# ---- 4. 流量制限 (429) で tick を落とさない ----

def test_the_statuses_worth_retrying_cover_rate_limits_and_server_errors():
    assert 429 in RETRY_STATUSES and 503 in RETRY_STATUSES
    assert 403 not in RETRY_STATUSES and 451 not in RETRY_STATUSES  # 地域制限は待っても直らない


async def test_a_rate_limited_request_is_retried_in_the_same_tick():
    http = FakeHttp(lambda u, n: (429, "too many requests") if n == 1 else (200, '{"price":"100"}'))
    assert await CoinbaseMarket(http).prices(["BTC-USD"]) == {"BTC-USD": 100.0}
    assert len(http.calls) == 2


async def test_hyperliquid_is_retried_too():
    """perp レッグの取得元。実測で 13 回中 3 回 429 が出た"""
    http = FakeHttp(lambda u, n: (429, "rate limited") if n < 3 else (200, '{"BTC":"60000"}'))
    assert await HyperliquidMarket(http).prices(["BTC"]) == {"BTC": 60000.0}
    assert len(http.calls) == 3


async def test_giving_up_names_the_status():
    http = FakeHttp(lambda u, n: (429, "too many requests"))
    with pytest.raises(RuntimeError, match="429"):
        await CoinbaseMarket(http).prices(["BTC-USD"])
    assert len(http.calls) == 3  # RETRY_DELAYS + 1 回で諦める


async def test_a_request_that_succeeds_is_not_retried():
    http = FakeHttp(lambda u, n: (200, '{"price":"100"}'))
    await CoinbaseMarket(http).prices(["BTC-USD"])
    assert len(http.calls) == 1


async def test_status_that_will_not_recover_is_not_retried():
    """403 / 451 (地域制限) は待っても直らないので、取り直さずすぐ諦める"""
    http = FakeHttp(lambda u, n: (451, "Unavailable For Legal Reasons"))
    status, _ = await request_retrying(http, "GET", "https://example.test")
    assert status == 451 and len(http.calls) == 1
