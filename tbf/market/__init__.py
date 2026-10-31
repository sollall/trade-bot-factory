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


def default_markets(http: Http, binance_url: Optional[str] = None, hyperliquid_url: Optional[str] = None) -> dict[str, VenueMarket]:
    from tbf.market.binance import DEFAULT_MARKET_DATA_URL, BinanceMarket
    from tbf.market.hyperliquid import INFO_URL, HyperliquidMarket

    return {
        "binance": BinanceMarket(http, binance_url or DEFAULT_MARKET_DATA_URL),
        "hyperliquid": HyperliquidMarket(http, hyperliquid_url or INFO_URL),
    }
