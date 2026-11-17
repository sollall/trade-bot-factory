"""Binance の公開マーケットデータ

【本番では使わない】Binance は Cloudflare Workers の US colo からの通信を必ず弾くので
(403 / 451 地域制限)、Worker では約半分の tick で価格が取れない。本番・バックテストの
現物の取得元は Coinbase (tbf/market/coinbase.py)。このモジュールは手元での比較・調査用に
残してあり、tbf.market.UNREACHABLE_FROM_WORKERS に入っているので bot の price_source に
すると scripts/build_worker.py がデプロイ前に落とす。実測は docs/market-data.md (TRA-81)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from tbf.core.types import TIMEFRAME_MS, Candle, FundingPoint
from tbf.http import Http

# 公開データ用のホスト。先頭を既定にし、失敗したら順に次を試す。
#
# data-api.binance.vision と api.binance.com / api1 / api2 は Cloudflare Workers の出口IP を
# 403 で弾く (TRA-81 で計測)。api-gcp.binance.com だけは Workers からも 200 で返る。
# 中身は同じで、1h 足 1000 本と ticker/price がどちらのホストでも完全一致することを確認済み。
# ただし api-gcp も US colo では 451 を返すので、Workers から安定して使えるホストは無い。
#
# ホストごとの遮断はまた起きうるので、1 つが落ちても残りで続けられるように並べてある。
# 先頭を変えたいときは BinanceMarket(base_url=...) / fetch_*(base_url=...) で渡す。
MARKET_DATA_HOSTS = (
    "https://api-gcp.binance.com",
    "https://data-api.binance.vision",
    "https://api.binance.com",
)
DEFAULT_MARKET_DATA_URL = MARKET_DATA_HOSTS[0]


async def _get_json(http: Http, base_url: str, path: str):
    """base_url で取れなければ MARKET_DATA_HOSTS の残りを試す"""
    hosts = [base_url] + [h for h in MARKET_DATA_HOSTS if h != base_url]
    error = ""
    for host in hosts:
        status, text = await http.request("GET", host + path)
        if status == 200:
            return json.loads(text)
        if not error:  # 最初のホストの失敗を伝える (そこが本来の取得先なので)
            error = f"GET {host}{path}: HTTP {status} {text[:200]}"
    raise RuntimeError(error)


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
    raw = await _get_json(http, base_url, f"/api/v3/klines?{urlencode(q)}")
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
    body = await _get_json(http, base_url, f"/api/v3/ticker/price?{q}")
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
