"""取引所ごとのマーケットデータ。どれも prices / recent / history / funding を持つ"""

from __future__ import annotations

from typing import Optional, Protocol

from tbf.core.types import Candle, FundingPoint
from tbf.http import Http


class VenueMarket(Protocol):
    async def prices(self, symbols: list[str]) -> dict[str, float]: ...
    async def recent(self, symbol: str, timeframe: str, count: int, now: int) -> list[Candle]: ...
    async def history(self, symbol: str, timeframe: str, start: int, end: int) -> list[Candle]: ...
    async def funding(self, symbol: str, start: int, end: int) -> list[FundingPoint]: ...


#: 価格・足を取れる取引所。bot の instruments の price_venue はここにある名前でなければならない
#: (発注先の venue は別。国内業者で建てて価格は海外で見る、という組み合わせがある)。
#: scripts/build_worker.py がデプロイ前に突き合わせる。
MARKET_VENUES = ("coinbase", "hyperliquid", "binance")

#: Cloudflare Workers から安定して取れない取得元。手元 (バックテスト) からは 200 で取れるので、
#: バックテストは通るのに本番だけ毎 tick 落ちる、という形でしか表に出ない (TRA-81)。
#: ここにある取得元を price_venue にした bot は scripts/build_worker.py がデプロイ前に落とす。
#: 実測と理由は docs/market-data.md。
UNREACHABLE_FROM_WORKERS = ("binance",)


def venue_timeframes(venue: str) -> tuple[str, ...]:
    """その取得元で取れる足。空なら TIMEFRAME_MS の全部が取れる"""
    if venue == "coinbase":
        from tbf.market.coinbase import TIMEFRAMES

        return TIMEFRAMES
    return ()


def default_markets(
    http: Http,
    coinbase_url: Optional[str] = None,
    hyperliquid_url: Optional[str] = None,
    binance_url: Optional[str] = None,
) -> dict[str, VenueMarket]:
    from tbf.market.binance import DEFAULT_MARKET_DATA_URL, BinanceMarket
    from tbf.market.coinbase import EXCHANGE_URL, CoinbaseMarket
    from tbf.market.hyperliquid import INFO_URL, HyperliquidMarket

    return {
        "coinbase": CoinbaseMarket(http, coinbase_url or EXCHANGE_URL),
        "hyperliquid": HyperliquidMarket(http, hyperliquid_url or INFO_URL),
        # Workers からは使えない (UNREACHABLE_FROM_WORKERS)。手元での比較・調査用に残してある
        "binance": BinanceMarket(http, binance_url or DEFAULT_MARKET_DATA_URL),
    }
