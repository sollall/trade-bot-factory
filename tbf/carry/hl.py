"""Hyperliquid の公開 info API の読み取りだけ (A① 用)

注文系のエンドポイントは一切呼ばない。認証もしない。秘密鍵も API キーも使わない。

このモジュールの存在理由は、社内で実際に踏んだ罠をここで閉じ込めることにある:

  - `spotMetaAndAssetCtxs` は `universe` (349 件) と `assetCtxs` (1188 件) の長さが一致しない。
    位置で zip すると静かにズレる (実測: `universe[105].name == "@107"` に対し
    `assetCtxs[105].coin == "@105"`)。リサーチャーはこれで HYPE のベーシスを
    +10,524,622bps と誤算出した。-> **必ず `coin` フィールドで結合する。**
  - `l2Book` は上位 20 段しか返さない。-> `Depth` が「取得できた段数と合計金額」を併せ持ち、
    歩き切れなければ None を返す (足りない板で小さいスリッページを捏造しない)。
  - `fundingHistory` は 1 レスポンス 500 件上限。-> カーソルでページングする。
  - `candleSnapshot` は時間足ごとに最新 5000 本まで。-> このモジュールでは足を使わない。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from tbf.http import Http

INFO_URL = "https://api.hyperliquid.xyz/info"
HOUR_MS = 3_600_000
# fundingHistory の 1 レスポンスあたりの件数上限 (実測で 500 丁度が返る)
FUNDING_PAGE_LIMIT = 500


class HlError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class Depth:
    """板の片側。`l2Book` が返した段数ぶんしか持っていないことを呼び出し側に隠さない"""

    # 買い板は価格の降順、売り板は昇順
    levels: tuple[tuple[float, float], ...]
    truncated_at: int

    @property
    def notional_usd(self) -> float:
        return sum(px * sz for px, sz in self.levels)

    @property
    def best(self) -> Optional[float]:
        return self.levels[0][0] if self.levels else None


@dataclass(frozen=True)
class Book:
    coin: str
    time: int
    bids: Depth
    asks: Depth

    @property
    def mid(self) -> Optional[float]:
        if not self.bids.levels or not self.asks.levels:
            return None
        return (self.bids.levels[0][0] + self.asks.levels[0][0]) / 2


@dataclass(frozen=True)
class FundingPoint:
    time: int
    rate: float
    premium: float


@dataclass
class InfoClient:
    """1 回のポーリングぶんの呼び出しをまとめ、遅延・回数・レート制限到達を数える"""

    http: Http
    url: str = INFO_URL
    latency_ms: float = 0.0
    calls: int = 0
    rate_limited: bool = False
    clock: Callable[[], float] = time.monotonic
    # 取得元を報告に書けるように、投げたリクエストボディを残す
    requests: list[str] = field(default_factory=list)

    async def post(self, body: dict) -> Any:
        payload = json.dumps(body)
        started = self.clock()
        status, text = await self.http.request("POST", self.url, {"content-type": "application/json"}, payload)
        self.latency_ms += (self.clock() - started) * 1000
        self.calls += 1
        self.requests.append(payload)
        if status in (418, 429):
            self.rate_limited = True
            raise HlError(f"{body.get('type')}: HTTP {status} (rate limited)", status, True)
        if status != 200:
            raise HlError(f"{body.get('type')}: HTTP {status} {text[:160]}", status)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise HlError(f"{body.get('type')}: not JSON: {e} ({text[:80]})") from e

    # ---- 現物

    async def spot_meta(self) -> dict:
        return await self.post({"type": "spotMeta"})

    async def spot_ctx(self, pair: str) -> dict:
        """現物ペアの context。**coin フィールドで結合する** (位置で zip しない)"""
        meta, ctxs = await self.post({"type": "spotMetaAndAssetCtxs"})
        by_coin = {c["coin"]: c for c in ctxs if "coin" in c}
        if pair not in by_coin:
            raise HlError(f"spotMetaAndAssetCtxs に {pair} の assetCtx がありません")
        if not any(u.get("name") == pair for u in meta["universe"]):
            raise HlError(f"spotMetaAndAssetCtxs の universe に {pair} がありません")
        return by_coin[pair]

    # ---- perp

    async def perp_ctx(self, coin: str) -> tuple[dict, dict]:
        """(universe の定義, assetCtx) を返す。perp は長さが一致するが、それも検証する"""
        meta, ctxs = await self.post({"type": "metaAndAssetCtxs"})
        universe = meta["universe"]
        if len(universe) != len(ctxs):
            raise HlError(
                f"metaAndAssetCtxs の universe ({len(universe)}) と assetCtxs ({len(ctxs)}) の長さが一致しません"
                " — 位置で結合できないので中断します"
            )
        for u, c in zip(universe, ctxs):
            if u.get("name") == coin:
                return u, c
        raise HlError(f"metaAndAssetCtxs の universe に {coin} がありません")

    async def margin_table(self, table_id: int) -> dict:
        return await self.post({"type": "marginTable", "id": table_id})

    # ---- 板

    async def book(self, coin: str, depth_limit: int = 20) -> Book:
        raw = await self.post({"type": "l2Book", "coin": coin})
        levels = raw.get("levels") or [[], []]
        bids, asks = levels[0], levels[1]
        return Book(
            coin=coin,
            time=int(raw.get("time") or 0),
            bids=Depth(_levels(bids, descending=True), len(bids)),
            asks=Depth(_levels(asks, descending=False), len(asks)),
        )

    # ---- ファンディング

    async def funding_history(self, coin: str, start: int, end: int) -> list[FundingPoint]:
        """1 時間値。500 件上限なのでカーソルでページングする"""
        out: dict[int, FundingPoint] = {}
        cursor = start
        while cursor < end:
            batch = await self.post(
                {"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end}
            )
            if not batch:
                break
            for f in batch:
                t = round_hour(int(f["time"]))
                out[t] = FundingPoint(t, float(f["fundingRate"]), float(f.get("premium") or 0))
            last = int(batch[-1]["time"])
            if last + 1 <= cursor:
                break
            cursor = last + 1
            if len(batch) < FUNDING_PAGE_LIMIT:
                break
        return [out[t] for t in sorted(out)]

    # ---- 手数料 (読み取り。アドレスは照会だけで、署名も送金もしない)

    async def user_fees(self, address: str) -> dict:
        return await self.post({"type": "userFees", "user": address})


def round_hour(ms: int) -> int:
    """fundingHistory の time は毎時 +数十ms でずれるので、毎時丁度に丸める"""
    return (ms + HOUR_MS // 2) // HOUR_MS * HOUR_MS


def _levels(raw: list, descending: bool) -> tuple[tuple[float, float], ...]:
    out = []
    for r in raw:
        px, sz = (float(r["px"]), float(r["sz"])) if isinstance(r, dict) else (float(r[0]), float(r[1]))
        if px > 0 and sz > 0:
            out.append((px, sz))
    # 会場が並び順を変えても壊れないように、こちらで並べ直す
    out.sort(key=lambda l: l[0], reverse=descending)
    return tuple(out)
