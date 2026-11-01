"""Binance の公開マーケットデータ"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from tbf.core.types import TIMEFRAME_MS, Candle, FundingPoint
from tbf.http import Http

# 地域制限を受けにくい公開データ用エンドポイント
DEFAULT_MARKET_DATA_URL = "https://data-api.binance.vision"


async def _get_json(http: Http, url: str):
    status, text = await http.request("GET", url)
    if status != 200:
        raise RuntimeError(f"GET {url}: HTTP {status} {text[:200]}")
    return json.loads(text)


async def fetch_klines(
    http: Http,
    symbol: str,
    timeframe: str,
    *,
    start_time: int | None = None,
    end_time: int | None = None,
    limit: int = 1000,
    base_url: str = DEFAULT_MARKET_DATA_URL,
) -> list[Candle]:
    q = {"symbol": symbol, "interval": timeframe, "limit": min(limit, 1000)}
    if start_time is not None:
        q["startTime"] = start_time
    if end_time is not None:
        q["endTime"] = end_time
    raw = await _get_json(http, f"{base_url}/api/v3/klines?{urlencode(q)}")
    return [Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in raw]


async def fetch_recent_closed(
    http: Http, symbol: str, timeframe: str, count: int, now: int, base_url: str = DEFAULT_MARKET_DATA_URL
) -> list[Candle]:
    """直近の確定足 count 本"""
    tf = TIMEFRAME_MS[timeframe]
    candles = await fetch_klines(http, symbol, timeframe, limit=count + 1, base_url=base_url)
    return [c for c in candles if c.time + tf <= now][-count:]


async def fetch_range(
    http: Http, symbol: str, timeframe: str, start: int, end: int, base_url: str = DEFAULT_MARKET_DATA_URL
) -> list[Candle]:
    """期間指定でページングしながら取得 (バックテスト用)"""
    tf = TIMEFRAME_MS[timeframe]
    out: list[Candle] = []
    cursor = start
    while cursor < end:
        batch = await fetch_klines(
            http, symbol, timeframe, start_time=cursor, end_time=end - 1, limit=1000, base_url=base_url
        )
        if not batch:
            break
        out.extend(batch)
        cursor = batch[-1].time + tf
    return [c for c in out if c.time + tf <= end]


async def fetch_prices(http: Http, symbols: list[str], base_url: str = DEFAULT_MARKET_DATA_URL) -> dict[str, float]:
    """複数シンボルの現在価格をまとめて取得"""
    uniq = sorted(set(symbols))
    if not uniq:
        return {}
    q = urlencode({"symbols": json.dumps(uniq, separators=(",", ":"))})
    body = await _get_json(http, f"{base_url}/api/v3/ticker/price?{q}")
    return {t["symbol"]: float(t["price"]) for t in body}


@dataclass
class BinanceMarket:
    """Worker から使うマーケットデータ"""

    http: Http
    base_url: str = DEFAULT_MARKET_DATA_URL

    async def prices(self, symbols: list[str]) -> dict[str, float]:
        return await fetch_prices(self.http, symbols, self.base_url)

    async def recent(self, symbol: str, timeframe: str, count: int, now: int) -> list[Candle]:
        return await fetch_recent_closed(self.http, symbol, timeframe, count, now, self.base_url)

    async def history(self, symbol: str, timeframe: str, start: int, end: int) -> list[Candle]:
        return await fetch_range(self.http, symbol, timeframe, start, end, self.base_url)

    async def funding(self, symbol: str, start: int, end: int) -> list[FundingPoint]:
        return []  # 現物にファンディングはない
