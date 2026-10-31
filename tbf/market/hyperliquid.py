"""Hyperliquid の公開 API (無期限先物の足・ファンディング・現在価格)"""

from __future__ import annotations

import json
from dataclasses import dataclass

from tbf.core.types import HOUR, TIMEFRAME_MS, Candle, FundingPoint
from tbf.http import Http

INFO_URL = "https://api.hyperliquid.xyz/info"


async def _post(http: Http, body: dict, url: str = INFO_URL):
    status, text = await http.request("POST", url, {"content-type": "application/json"}, json.dumps(body))
    if status != 200:
        raise RuntimeError(f"hyperliquid {body.get('type')}: HTTP {status} {text[:200]}")
    return json.loads(text)


def _round_hour(ms: int) -> int:
    return (ms + HOUR // 2) // HOUR * HOUR


async def fetch_funding(http: Http, coin: str, start: int, end: int, url: str = INFO_URL) -> list[FundingPoint]:
    out: dict[int, FundingPoint] = {}
    cursor = start
    while cursor < end:
        batch = await _post(http, {"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end}, url)
        if not batch:
            break
        for f in batch:
            t = _round_hour(int(f["time"]))
            out[t] = FundingPoint(t, float(f["fundingRate"]), float(f.get("premium") or 0))
        last = int(batch[-1]["time"])
        if last + 1 <= cursor:
            break
        cursor = last + 1
    return [out[t] for t in sorted(out) if start <= t <= end]


async def fetch_candles(
    http: Http, coin: str, timeframe: str, start: int, end: int, url: str = INFO_URL
) -> list[Candle]:
    """確定足だけ返す (API は直近 5000 本まで)"""
    tf = TIMEFRAME_MS[timeframe]
    out: dict[int, Candle] = {}
    cursor = start
    while cursor < end:
        batch = await _post(
            http,
            {"type": "candleSnapshot", "req": {"coin": coin, "interval": timeframe, "startTime": cursor, "endTime": end}},
            url,
        )
        if not batch:
            break
        for c in batch:
            t = int(c["t"])
            out[t] = Candle(t, float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]), float(c.get("v") or 0))
        last = int(batch[-1]["t"])
        if last + tf <= cursor:
            break
        cursor = last + tf
    return [out[t] for t in sorted(out) if start <= t and t + tf <= end]


@dataclass
class HyperliquidMarket:
    http: Http
    url: str = INFO_URL

    async def prices(self, symbols: list[str]) -> dict[str, float]:
        mids = await _post(self.http, {"type": "allMids"}, self.url)
        return {s: float(mids[s]) for s in symbols if s in mids}

    async def recent(self, symbol: str, timeframe: str, count: int, now: int) -> list[Candle]:
        tf = TIMEFRAME_MS[timeframe]
        return (await fetch_candles(self.http, symbol, timeframe, now - (count + 2) * tf, now, self.url))[-count:]

    async def history(self, symbol: str, timeframe: str, start: int, end: int) -> list[Candle]:
        return await fetch_candles(self.http, symbol, timeframe, start, end, self.url)

    async def funding(self, symbol: str, start: int, end: int) -> list[FundingPoint]:
        return await fetch_funding(self.http, symbol, start, end, self.url)
