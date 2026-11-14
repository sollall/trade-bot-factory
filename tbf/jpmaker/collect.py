"""シャドーモードの収集 (読み取り専用)。

叩く先はこの 3 つだけ。**どれも公開エンドポイントで、鍵も口座も要らない。**
  GET  {bitbank_public}/{pair}/depth          最良気配と数量 (1 秒間隔)
  GET  {bitbank_public}/{pair}/transactions   約定テープ 最新 60 件 (60 秒間隔)
  POST {hyperliquid_info} {"type":"l2Book"}   ヘッジ先の板 (5 秒間隔)

注文系 API には触らない。POST は Hyperliquid の `info` (読み取り) だけで、
これは署名の要らない公開エンドポイント。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable

from tbf.http import Http
from tbf.jpmaker.config import JpMakerConfig
from tbf.jpmaker.store import JpMakerStore

# 429 / 5xx を踏んだときの待ち。レート制限で BAN されると計測が止まるので強めに下がる。
BACKOFF_START_SEC = 5.0
BACKOFF_MAX_SEC = 300.0


def _now_ms() -> int:
    return int(time.time() * 1000)


class Collector:
    def __init__(
        self,
        cfg: JpMakerConfig,
        http: Http,
        store: JpMakerStore,
        log: Callable[[str], None] = print,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.store = store
        self.log = log
        self.clock = clock
        self.counts: dict[str, int] = {}

    def _bump(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    async def _get(self, venue: str, endpoint: str, url: str, body: str | None = None) -> dict | None:
        """1 回の取得。例外を飲んで `polls` に記録し、失敗なら None を返す"""
        t0 = time.monotonic()
        method = "POST" if body is not None else "GET"
        headers = {"content-type": "application/json"} if body is not None else None
        try:
            status, text = await self.http.request(method, url, headers, body)
        except Exception as e:  # noqa: BLE001
            self.store.add_poll(self.clock(), venue, endpoint, (time.monotonic() - t0) * 1000, None, f"{type(e).__name__}: {e}")
            self._bump(f"{endpoint}.exception")
            return None
        latency = (time.monotonic() - t0) * 1000
        if status != 200:
            self.store.add_poll(self.clock(), venue, endpoint, latency, status, text[:200])
            self._bump(f"{endpoint}.http_{status}")
            return None
        try:
            payload = json.loads(text)
        except ValueError as e:
            self.store.add_poll(self.clock(), venue, endpoint, latency, status, f"json: {e}")
            self._bump(f"{endpoint}.badjson")
            return None
        # bitbank は HTTP 200 でも success=0 でエラーを返す
        if isinstance(payload, dict) and payload.get("success") == 0:
            self.store.add_poll(self.clock(), venue, endpoint, latency, status, f"bitbank code {payload.get('data')}")
            self._bump(f"{endpoint}.api_error")
            return None
        self.store.add_poll(self.clock(), venue, endpoint, latency, status, None)
        self._bump(f"{endpoint}.ok")
        return payload

    # --- bitbank 板 ---
    async def poll_book(self, pair: str) -> bool:
        url = f"{self.cfg.bitbank_public}/{pair}/depth"
        payload = await self._get("bitbank", "depth", url)
        if payload is None:
            return False
        d = payload.get("data") or {}
        bids, asks = d.get("bids") or [], d.get("asks") or []
        if not bids or not asks:
            self.store.add_poll(self.clock(), "bitbank", "depth", None, 200, "empty book")
            return False
        self.store.add_book(
            self.clock(),
            int(d["timestamp"]) if d.get("timestamp") else None,
            pair,
            float(bids[0][0]),
            float(bids[0][1]),
            float(asks[0][0]),
            float(asks[0][1]),
            str(d.get("sequenceId")) if d.get("sequenceId") else None,
        )
        return True

    # --- bitbank 約定テープ ---
    async def poll_tape(self, pair: str) -> bool:
        prev_latest = self.store.latest_trade_time(pair)
        url = f"{self.cfg.bitbank_public}/{pair}/transactions"
        payload = await self._get("bitbank", "transactions", url)
        if payload is None:
            return False
        trades = (payload.get("data") or {}).get("transactions") or []
        if not trades:
            return True
        inserted = self.store.add_trades(pair, trades)
        self._bump("trades.new", inserted)
        # 全件が新規だった = この手前が窓から落ちた可能性がある。推定で埋めず、印だけ残す。
        if prev_latest is not None and inserted == len(trades):
            oldest = min(int(t["executed_at"]) for t in trades)
            if oldest > prev_latest:
                self.store.add_tape_overflow(self.clock(), pair, len(trades), oldest, prev_latest)
                self._bump("tape.overflow")
        return True

    # --- Hyperliquid 板 ---
    async def poll_hl(self, coin: str) -> bool:
        payload = await self._get("hyperliquid", "l2Book", self.cfg.hyperliquid_info, json.dumps({"type": "l2Book", "coin": coin}))
        if payload is None:
            return False
        levels = payload.get("levels") or []
        if len(levels) != 2 or not levels[0] or not levels[1]:
            return False
        n = self.cfg.hl_depth_levels
        bids = [[lv["px"], lv["sz"]] for lv in levels[0][:n]]
        asks = [[lv["px"], lv["sz"]] for lv in levels[1][:n]]
        self.store.add_hl_book(self.clock(), payload.get("time"), coin, bids, asks)
        return True

    # --- 周期タスク ---
    async def _loop(self, name: str, interval: float, offset: float, fn, stop: asyncio.Event) -> None:
        if offset:
            try:
                await asyncio.wait_for(stop.wait(), timeout=offset)
                return
            except asyncio.TimeoutError:
                pass
        backoff = 0.0
        next_at = time.monotonic()
        while not stop.is_set():
            ok = await fn()
            if ok:
                backoff = 0.0
            else:
                backoff = BACKOFF_START_SEC if backoff == 0 else min(backoff * 2, BACKOFF_MAX_SEC)
                self.log(f"{name}: backing off {backoff:.0f}s")
            next_at += interval + backoff
            sleep = max(0.0, next_at - time.monotonic())
            if sleep == 0.0:
                # 取得が間隔より長くかかった。間隔を詰めずに次の格子に合わせ直す
                next_at = time.monotonic() + interval
                sleep = interval
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep)
            except asyncio.TimeoutError:
                continue

    async def _commit_loop(self, stop: asyncio.Event) -> None:
        """1 秒標本を 1 件ずつ commit すると遅いので 5 秒ごとにまとめる"""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self.store.commit()
        self.store.commit()

    async def _status_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=600.0)
            except asyncio.TimeoutError:
                self.log("counts " + " ".join(f"{k}={v}" for k, v in sorted(self.counts.items())))

    async def run(self, stop: asyncio.Event) -> None:
        self.store.add_run_event(self.clock(), "start", json.dumps({"pairs": self.cfg.pair_names}))
        tasks = [self._commit_loop(stop), self._status_loop(stop)]
        for i, p in enumerate(self.cfg.pairs):
            # 同時に 5 本飛ばさないよう銘柄ごとにずらす
            tasks.append(
                self._loop(
                    f"depth {p.pair}",
                    self.cfg.book_poll_sec,
                    i * (self.cfg.book_poll_sec / max(1, len(self.cfg.pairs))),
                    lambda p=p: self.poll_book(p.pair),
                    stop,
                )
            )
            tasks.append(
                self._loop(
                    f"tape {p.pair}",
                    self.cfg.tape_poll_sec,
                    i * (self.cfg.tape_poll_sec / max(1, len(self.cfg.pairs))),
                    lambda p=p: self.poll_tape(p.pair),
                    stop,
                )
            )
            tasks.append(
                self._loop(
                    f"hl {p.hedge_coin}",
                    self.cfg.hl_poll_sec,
                    i * (self.cfg.hl_poll_sec / max(1, len(self.cfg.pairs))),
                    lambda p=p: self.poll_hl(p.hedge_coin),
                    stop,
                )
            )
        try:
            await asyncio.gather(*tasks)
        finally:
            self.store.commit()
            self.store.add_run_event(self.clock(), "stop", json.dumps(self.counts))
