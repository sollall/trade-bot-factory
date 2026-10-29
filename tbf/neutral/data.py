"""Hyperliquid の公開 API からファンディング履歴と 1 時間足を取得する (data/ にキャッシュ)"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tbf.http import Http

INFO_URL = "https://api.hyperliquid.xyz/info"
HOUR = 3_600_000


@dataclass(frozen=True)
class FundingPoint:
    # 支払いが発生した時刻 (UTC ms, 時間単位に丸めたもの)
    time: int
    # 1 時間あたりのファンディングレート (+ ならロングがショートに払う)
    rate: float
    # 先物価格とオラクル (現物指数) の乖離率
    premium: float


@dataclass(frozen=True)
class HourBar:
    # 足の開始時刻
    time: int
    open: float
    high: float
    low: float
    close: float


async def _post(http: Http, body: dict) -> list:
    status, text = await http.request("POST", INFO_URL, {"content-type": "application/json"}, json.dumps(body))
    if status != 200:
        raise RuntimeError(f"hyperliquid {body.get('type')}: HTTP {status} {text[:200]}")
    return json.loads(text)


def _round_hour(ms: int) -> int:
    return (ms + HOUR // 2) // HOUR * HOUR


async def fetch_funding(http: Http, coin: str, start: int, end: int) -> list[FundingPoint]:
    out: dict[int, FundingPoint] = {}
    cursor = start
    while cursor < end:
        batch = await _post(http, {"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end})
        if not batch:
            break
        for f in batch:
            t = _round_hour(int(f["time"]))
            out[t] = FundingPoint(t, float(f["fundingRate"]), float(f.get("premium") or 0))
        last = int(batch[-1]["time"])
        if last + 1 <= cursor:
            break
        cursor = last + 1
    return [out[t] for t in sorted(out) if start <= t < end]


async def fetch_hour_bars(http: Http, coin: str, start: int, end: int) -> list[HourBar]:
    out: dict[int, HourBar] = {}
    cursor = start
    while cursor < end:
        batch = await _post(
            http,
            {"type": "candleSnapshot", "req": {"coin": coin, "interval": "1h", "startTime": cursor, "endTime": end}},
        )
        if not batch:
            break
        for c in batch:
            t = int(c["t"])
            out[t] = HourBar(t, float(c["o"]), float(c["h"]), float(c["l"]), float(c["c"]))
        last = int(batch[-1]["t"])
        if last + HOUR <= cursor:
            break
        cursor = last + HOUR
    # 形成中の足は除く
    return [out[t] for t in sorted(out) if start <= t and t + HOUR <= end]


async def load_hyperliquid(
    http: Http, coin: str, start: int, end: int, cache_dir: Optional[Path] = Path("data")
) -> tuple[list[HourBar], list[FundingPoint]]:
    path = cache_dir / f"hl_{coin}_{start}_{end}.json" if cache_dir else None
    if path and path.exists():
        raw = json.loads(path.read_text())
        return [HourBar(*b) for b in raw["bars"]], [FundingPoint(*f) for f in raw["funding"]]
    bars = await fetch_hour_bars(http, coin, start, end)
    funding = await fetch_funding(http, coin, start, end)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "bars": [[b.time, b.open, b.high, b.low, b.close] for b in bars],
                    "funding": [[f.time, f.rate, f.premium] for f in funding],
                }
            )
        )
    return bars, funding
