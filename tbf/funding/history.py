"""確定済みファンディング履歴の取得 (読み取り専用)

ライブ計測 7 日では「14 日保有して黒字か」は検証できない。ライブで取れるのは
各会場が今返している**予測レート**で、net_apr(14d) はそれが 14 日続くという外挿に
なってしまう。予測レートは 1 時間の中でも動くので、エピソードの長さや符号反転の
回数にもノイズが乗る。

各会場には**確定したファンディングの履歴 API** があるので、そこから数か月ぶんを
埋める。これで「ある時点で建てて hold 日後に解消したとき、実際に確定した
ファンディングの合計はいくらだったか」を主指標にできる (realized.py)。
ライブで測り続ける必要があるのは板 (スリッページ) と価格差 (basis) だけになる。

正規化の約束 (会場ごとに建ても符号の表し方も違うので、ここで 1 つに揃える):

    FundingPoint.rate  = その区間に建玉に対して実際に動いた分数。**ロングが払うとき +**
    FundingPoint.hours = その区間の長さ (時間)
    年率 = rate / hours * 24 * 365   (venues.annualize と同じ式)

注文系の endpoint は呼ばない。認証もしない。
"""

from __future__ import annotations

import asyncio
import bisect
import json
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from tbf.funding.config import VenueConfig
from tbf.funding.venues import Session, VenueError, _check_lighter_code

HOUR_MS = 3_600_000

# 1 リクエストで返る上限 (会場のドキュメント / 実測)。これに達したらページングする
HL_PAGE = 500
DYDX_PAGE = 1000
LIGHTER_PAGE = 750
BINANCE_PAGE = 1000
HL_CANDLE_PAGE = 5000
# 累積インデックスのサンプル点の上限 (90 日を 8 時間刻みで 270 点)
MAX_SAMPLES = 2000

# ページをまたぐときの待ち時間。BAN されると計測が止まるので控えめに刻む
PAGE_PAUSE_SEC = 0.15


@dataclass(frozen=True)
class FundingPoint:
    """1 区間ぶんの確定したファンディング"""

    # 区間の終わり (確定時刻, ms)
    time: int
    hours: float
    # 建玉に対する分数。ロングが払うとき +
    rate: float
    # その時点の mark 価格 (取れた会場だけ)。会場間の価格差の推移に使う
    mark: Optional[float] = None

    @property
    def apr(self) -> float:
        return self.rate / self.hours * 24 * 365


def hour_floor(ms: int) -> int:
    return ms // HOUR_MS * HOUR_MS


def _attach_marks(points: list[FundingPoint], marks: dict[int, float]) -> list[FundingPoint]:
    if not marks:
        return points
    out = []
    for p in points:
        m = p.mark if p.mark else marks.get(hour_floor(p.time))
        out.append(FundingPoint(p.time, p.hours, p.rate, m))
    return out


# ------------------------------------------------------------------ Hyperliquid


async def _hyperliquid_marks(s: Session, v: VenueConfig, native: str, start: int, end: int) -> dict[int, float]:
    """1h 足の終値を mark 価格の代わりに使う (fundingHistory は価格を返さない)"""
    out: dict[int, float] = {}
    cursor = start
    while cursor < end:
        rows = await s.json(
            "POST",
            v.base_url,
            json.dumps({"type": "candleSnapshot", "req": {"coin": native, "interval": "1h", "startTime": cursor, "endTime": end}}),
        )
        if not rows:
            break
        for r in rows:
            out[hour_floor(int(r["t"]))] = float(r["c"])
        nxt = int(rows[-1]["t"]) + HOUR_MS
        if nxt <= cursor or len(rows) < HL_CANDLE_PAGE:
            break
        cursor = nxt
        await asyncio.sleep(PAGE_PAUSE_SEC)
    return out


async def history_hyperliquid(s: Session, v: VenueConfig, native: str, start: int, end: int) -> list[FundingPoint]:
    out: list[FundingPoint] = []
    cursor = start
    while cursor < end:
        rows = await s.json(
            "POST", v.base_url, json.dumps({"type": "fundingHistory", "coin": native, "startTime": cursor, "endTime": end})
        )
        if not rows:
            break
        for r in rows:
            t = int(r["time"])
            if start <= t <= end:
                # fundingRate は 1 時間ぶん。+ でロングが払う
                out.append(FundingPoint(t, v.funding_interval_hours, float(r["fundingRate"])))
        nxt = int(rows[-1]["time"]) + 1
        if nxt <= cursor or len(rows) < HL_PAGE:
            break
        cursor = nxt
        await asyncio.sleep(PAGE_PAUSE_SEC)
    return _attach_marks(out, await _hyperliquid_marks(s, v, native, start, end))


# ------------------------------------------------------------------ dYdX


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


async def history_dydx(s: Session, v: VenueConfig, native: str, start: int, end: int) -> list[FundingPoint]:
    """historicalFunding は price (oracle) も返すので、mark 価格はここで揃う"""
    out: list[FundingPoint] = []
    before = end
    while True:
        q = urllib.parse.quote(_iso(before), safe="")
        rows = (
            await s.json("GET", f"{v.base_url}/v4/historicalFunding/{native}?limit={DYDX_PAGE}&effectiveBeforeOrAt={q}")
        ).get("historicalFunding") or []
        if not rows:
            break
        times = []
        for r in rows:
            t = _parse_iso(r["effectiveAt"])
            times.append(t)
            if start <= t <= end:
                out.append(FundingPoint(t, v.funding_interval_hours, float(r["rate"]), float(r["price"])))
        oldest = min(times)
        if oldest <= start or len(rows) < DYDX_PAGE or oldest >= before:
            break
        before = oldest - 1
        await asyncio.sleep(PAGE_PAUSE_SEC)
    return out


# ------------------------------------------------------------------ Lighter


async def _lighter_market_id(s: Session, v: VenueConfig, native: str) -> int:
    details = await s.json("GET", f"{v.base_url}/api/v1/orderBookDetails")
    _check_lighter_code(details)
    for d in details["order_book_details"]:
        if d["symbol"] == native:
            return int(d["market_id"])
    raise VenueError(f"{native} is not listed")


async def history_lighter(s: Session, v: VenueConfig, native: str, start: int, end: int) -> list[FundingPoint]:
    """/api/v1/fundings (resolution=1h)

    注意が 2 つある:
      - rate は「1 時間あたりの％」で、しかも**符号を持たない**。どちらが払ったかは
        direction で表される (direction=long ならロングが払う = 正)。ここを取り違えると
        符号が逆になるので、direction で符号を付ける。
      - value は「建玉 1 単位あたりに動いた額」なので、value / |rate 分数| が mark 価格になる。
    """
    mid = await _lighter_market_id(s, v, native)
    out: list[FundingPoint] = []
    seen: set[int] = set()
    end_ts, start_ts = end // 1000, start // 1000
    while end_ts > start_ts:
        page = await s.json(
            "GET",
            f"{v.base_url}/api/v1/fundings?market_id={mid}&resolution=1h"
            f"&start_timestamp={start_ts}&end_timestamp={end_ts}&count_back={LIGHTER_PAGE}",
        )
        _check_lighter_code(page)
        rows = page.get("fundings") or []
        if not rows:
            break
        for r in rows:
            t = int(r["timestamp"]) * 1000
            if not (start <= t <= end) or t in seen:
                continue
            seen.add(t)
            pct = float(r["rate"])
            frac = pct / 100.0
            sign = -1.0 if str(r.get("direction", "long")).lower() == "short" else 1.0
            mark = float(r["value"]) / frac if frac else None
            out.append(FundingPoint(t, 1.0, sign * frac, mark))
        oldest = min(int(r["timestamp"]) for r in rows)
        if oldest * 1000 <= start or len(rows) < LIGHTER_PAGE or oldest - 1 >= end_ts:
            break
        end_ts = oldest - 1
        await asyncio.sleep(PAGE_PAUSE_SEC)
    return out


# ------------------------------------------------------------------ Paradex


async def _paradex_marks(s: Session, v: VenueConfig, native: str, start: int, end: int) -> dict[int, float]:
    out: dict[int, float] = {}
    chunk = 10 * 24 * HOUR_MS
    cursor = start
    while cursor < end:
        hi = min(end, cursor + chunk)
        rows = (
            await s.json(
                "GET",
                f"{v.base_url}/v1/markets/klines?symbol={native}&resolution=60"
                f"&start_at={cursor}&end_at={hi}&price_kind=mark",
            )
        ).get("results") or []
        for r in rows:
            out[hour_floor(int(r[0]))] = float(r[4])
        cursor = hi + 1
        await asyncio.sleep(PAGE_PAUSE_SEC)
    return out


async def history_paradex(
    s: Session, v: VenueConfig, native: str, start: int, end: int, sample_hours: float = 8.0
) -> list[FundingPoint]:
    """Paradex のファンディングは連続課金で、/v1/funding/data が累積の funding_index を返す

    レートを足し合わせるより、累積インデックスの差を取るほうが正確 (取りこぼしがない)。
    建玉 1 単位あたりに動いた額が index の差なので、mark 価格で割れば分数になる。
    5 秒刻みの生データを全部引くと量が多すぎるので、sample_hours ごとに
    `end_at` を指定して 1 行だけ取る。
    """
    step = max(HOUR_MS, int(sample_hours * HOUR_MS))
    if (end - start) // step > MAX_SAMPLES:
        raise VenueError(
            f"paradex: サンプル点が多すぎます ({(end - start) // step} 点 > {MAX_SAMPLES})。"
            "期間を短くするか history_sample_hours を大きくしてください"
        )
    marks = await _paradex_marks(s, v, native, start, end)
    samples: list[tuple[int, float]] = []
    t = start
    while t <= end:
        res = (
            await s.json("GET", f"{v.base_url}/v1/funding/data?market={native}&end_at={t}&page_size=1")
        ).get("results") or []
        if res:
            samples.append((int(res[0]["created_at"]), float(res[0]["funding_index"])))
        t += step
        await asyncio.sleep(PAGE_PAUSE_SEC)
    hours_sorted = sorted(marks)
    out: list[FundingPoint] = []
    for (ta, ia), (tb, ib) in zip(samples, samples[1:]):
        hours = (tb - ta) / HOUR_MS
        if hours <= 0:
            continue
        mark = _nearest_mark(marks, hours_sorted, ta)
        if not mark:
            continue
        out.append(FundingPoint(tb, hours, (ib - ia) / mark, _nearest_mark(marks, hours_sorted, tb)))
    return out


def _nearest_mark(marks: dict[int, float], hours_sorted: list[int], t: int) -> Optional[float]:
    """その時刻に最も近い時間足の mark。足に穴があっても拾えるようにする"""
    if not hours_sorted:
        return None
    h = hour_floor(t)
    if h in marks:
        return marks[h]
    i = bisect.bisect_left(hours_sorted, h)
    cands = [hours_sorted[j] for j in (i - 1, i) if 0 <= j < len(hours_sorted)]
    if not cands:
        return None
    return marks[min(cands, key=lambda c: abs(c - h))]


# ------------------------------------------------------------------ Binance (参照のみ)


async def history_binance(s: Session, v: VenueConfig, native: str, start: int, end: int) -> list[FundingPoint]:
    out: list[FundingPoint] = []
    cursor = start
    while cursor < end:
        rows = await s.json(
            "GET", f"{v.base_url}/fapi/v1/fundingRate?symbol={native}&startTime={cursor}&endTime={end}&limit={BINANCE_PAGE}"
        )
        if not rows:
            break
        for r in rows:
            t = int(r["fundingTime"])
            if start <= t <= end:
                out.append(
                    FundingPoint(t, v.funding_interval_hours, float(r["fundingRate"]), float(r.get("markPrice") or 0) or None)
                )
        nxt = int(rows[-1]["fundingTime"]) + 1
        if nxt <= cursor or len(rows) < BINANCE_PAGE:
            break
        cursor = nxt
        await asyncio.sleep(PAGE_PAUSE_SEC)
    return out


HistoryAdapter = Callable[[Session, VenueConfig, str, int, int], Awaitable[list[FundingPoint]]]

HISTORY_ADAPTERS: dict[str, HistoryAdapter] = {
    "hyperliquid": history_hyperliquid,
    "dydx": history_dydx,
    "lighter": history_lighter,
    "paradex": history_paradex,
    "binance": history_binance,
}


def has_history(v: VenueConfig) -> bool:
    return v.adapter in HISTORY_ADAPTERS


# 累積インデックスをサンプルして履歴を作る会場 (サンプル間隔を受け取る)
SAMPLED_ADAPTERS = {"paradex"}


async def fetch_history(s: Session, v: VenueConfig, symbol: str, start: int, end: int) -> list[FundingPoint]:
    adapter = HISTORY_ADAPTERS.get(v.adapter)
    if adapter is None:
        raise VenueError(f"{v.name}: 履歴アダプタがありません (adapter={v.adapter!r})")
    kwargs = {}
    if v.adapter in SAMPLED_ADAPTERS and v.history_sample_hours:
        kwargs["sample_hours"] = v.history_sample_hours
    points = await adapter(s, v, v.symbols[symbol], start, end, **kwargs)
    return sorted(points, key=lambda p: p.time)
