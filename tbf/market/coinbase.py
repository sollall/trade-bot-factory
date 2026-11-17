"""Coinbase Exchange の公開マーケットデータ (現物)

現物の価格・足はここから取る。Binance / Bybit は Cloudflare Workers の US colo から
必ず弾かれ、OKX は共有出口IPの 429 で落ちるため、Workers から安定して取れて
180 日の履歴もある取得元が Coinbase だけだった (実測は docs/market-data.md / TRA-81)。

バックテストも Worker もこのモジュールを使うので、両者の価格の出所は同じになる。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from tbf.core.types import TIMEFRAME_MS, Candle, FundingPoint
from tbf.http import Http, request_retrying

EXCHANGE_URL = "https://api.exchange.coinbase.com"

#: Coinbase が受け付ける足の長さ (秒)。30m と 4h は無い (TIMEFRAME_MS には あるので要注意)
GRANULARITY_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86_400}
TIMEFRAMES = tuple(GRANULARITY_SEC)

#: 1 リクエストで返る足の上限。超えて頼むと 400
#: ("granularity too small for the requested time range. Count of aggregations requested exceeds 300")
MAX_CANDLES = 300


async def _get_json(http: Http, base_url: str, path: str):
    status, text = await request_retrying(http, "GET", base_url + path)
    if status != 200:
        raise RuntimeError(f"GET {base_url}{path}: HTTP {status} {text[:200]}")
    return json.loads(text)


def _granularity(timeframe: str) -> int:
    if timeframe not in GRANULARITY_SEC:
        raise RuntimeError(f"coinbase に {timeframe} 足はありません (使えるのは {', '.join(TIMEFRAMES)})")
    return GRANULARITY_SEC[timeframe]


async def fetch_candles(
    http: Http, product: str, timeframe: str, start: int, end: int, base_url: str = EXCHANGE_URL
) -> list[Candle]:
    """[start, end] の確定足を古い順に返す。1 リクエスト MAX_CANDLES 本までなので区切って取る"""
    tf = TIMEFRAME_MS[timeframe]
    granularity = _granularity(timeframe)
    out: dict[int, Candle] = {}
    cursor = start
    while cursor < end:
        stop = min(cursor + (MAX_CANDLES - 1) * tf, end)
        q = urlencode({"granularity": granularity, "start": cursor // 1000, "end": stop // 1000})
        for row in await _get_json(http, base_url, f"/products/{product}/candles?{q}"):
            # [時刻(秒), 安値, 高値, 始値, 終値, 出来高] の新しい順。Candle の順に並べ替える
            t = int(row[0]) * 1000
            out[t] = Candle(t, float(row[3]), float(row[2]), float(row[1]), float(row[4]), float(row[5]))
        cursor = stop + tf  # 足が無い時間帯でも前に進む (止まると無限ループになる)
    return [out[t] for t in sorted(out) if start <= t and t + tf <= end]


async def fetch_prices(http: Http, products: list[str], base_url: str = EXCHANGE_URL) -> dict[str, float]:
    """現在価格。銘柄ごとに 1 リクエスト (いまの bot は 2 銘柄なので、全銘柄をまとめて返す
    /products/stats の 500 銘柄ぶんを毎分読むより軽い)"""
    out: dict[str, float] = {}
    for product in sorted(set(products)):
        body = await _get_json(http, base_url, f"/products/{product}/ticker")
        out[product] = float(body["price"])
    return out


@dataclass
class CoinbaseMarket:
    http: Http
    base_url: str = EXCHANGE_URL

    async def prices(self, symbols: list[str]) -> dict[str, float]:
        return await fetch_prices(self.http, symbols, self.base_url)

    async def recent(self, symbol: str, timeframe: str, count: int, now: int) -> list[Candle]:
        tf = TIMEFRAME_MS[timeframe]
        candles = await fetch_candles(self.http, symbol, timeframe, now - (count + 2) * tf, now, self.base_url)
        return candles[-count:]

    async def history(self, symbol: str, timeframe: str, start: int, end: int) -> list[Candle]:
        return await fetch_candles(self.http, symbol, timeframe, start, end, self.base_url)

    async def funding(self, symbol: str, start: int, end: int) -> list[FundingPoint]:
        return []  # 現物にファンディングはない
