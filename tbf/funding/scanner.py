"""シャドーモード: ファンディング差を監視・記録するだけ。売買はしない

会場ごとにポーリング間隔が違うので、各会場の「次に取る時刻」を持ち、
期限が来た会場だけ取り直す。他の会場は直近のスナップショットを使い回すが、
max_stale_sec より古くなったら計算から外す (落ちている会場の古い値で
見せかけの機会を作らないため)。
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from tbf.funding import clamp
from tbf.funding.config import FundingConfig, VenueConfig
from tbf.funding.spread import EpisodeTracker, evaluate, stale_price_venues
from tbf.funding.store import FundingStore
from tbf.funding.venues import CapQuote, VenueClient, VenueSnapshot

# 「次に取る時刻」を厳密な >= で判定すると、ループが 1ms 早く回った回にその会場が
# 丸ごと 1 周スキップされ、実効の取得間隔が倍になる。わずかな前倒しを許す。
DUE_SLACK_MS = 500


def due_slack_ms(interval_sec: float) -> int:
    return min(DUE_SLACK_MS, int(interval_sec * 1000 * 0.05))


class Scanner:
    def __init__(
        self,
        cfg: FundingConfig,
        client: VenueClient,
        store: FundingStore,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        log: Callable[[str], None] = print,
        api_caps: Optional[dict[tuple[str, str], CapQuote]] = None,
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.store = store
        self.clock = clock
        self.log = log
        self.tracker = EpisodeTracker(cfg.min_net_apr, cfg.rank_hold_days, cfg.max_stale_sec)
        self.clamp_tracker = clamp.ClampTracker(cfg.min_clamp_edge_apr)
        self.api_caps: dict[tuple[str, str], CapQuote] = dict(api_caps or {})
        self.caps = clamp.resolve_caps(cfg, self.api_caps)
        self.next_cap_check = 0
        self.latest: dict[str, VenueSnapshot] = {}
        self.next_due: dict[str, int] = {v.name: 0 for v in cfg.venues}
        self.closed_episodes = 0
        self.closed_clamp_episodes = 0

    def due(self, now: int) -> list[VenueConfig]:
        return [
            v
            for v in self.cfg.venues
            if now + due_slack_ms(v.poll_interval_sec) >= self.next_due.get(v.name, 0)
        ]

    async def refresh_caps(self, now: int) -> None:
        """ファンディング上限を取り直す

        会場はイベント中に上限や精算間隔を変える (INIT では Binance が精算間隔を
        4h -> 1h に変えた)。起動時に 1 回取るだけだと、上限が変わった瞬間から
        張り付きの判定が静かに間違い続ける。変化はログと caps テーブルに残す。
        """
        self.next_cap_check = now + int(self.cfg.cap_recheck_sec * 1000)
        results = await asyncio.gather(
            *(self.client.fetch_caps(v) for v in self.cfg.venues), return_exceptions=True
        )
        for v, quotes in zip(self.cfg.venues, results):
            if isinstance(quotes, BaseException):
                self.log(f"[{v.name}] 上限の取得に失敗: {type(quotes).__name__}: {quotes}")
                continue
            for q in quotes:
                old = self.api_caps.get((q.venue, q.symbol))
                self.api_caps[(q.venue, q.symbol)] = q
                if old is not None and (
                    abs(old.rate - q.rate) > 1e-12 or abs(old.interval_hours - q.interval_hours) > 1e-12
                ):
                    self.log(
                        f"[{q.venue}] {q.symbol} の上限が変わりました: "
                        f"{old.rate:g}/{old.interval_hours:g}h -> {q.rate:g}/{q.interval_hours:g}h"
                    )
        self.caps = clamp.resolve_caps(self.cfg, self.api_caps)
        for cap in self.caps.values():
            if self.store.note_cap(now, cap):
                self.log(f"[{cap.venue}] {cap.symbol} の上限を更新: {cap.rate:g}/{cap.interval_hours:g}h ({cap.source})")

    async def poll_once(self) -> None:
        now = self.clock()
        if now >= self.next_cap_check:
            try:
                await self.refresh_caps(now)
            except Exception as e:  # noqa: BLE001  上限が取れなくても計測は止めない
                self.log(f"上限の取り直しに失敗: {type(e).__name__}: {e}")
                self.next_cap_check = now + int(self.cfg.cap_recheck_sec * 1000)
        venues = self.due(now)
        results = await asyncio.gather(*(self.client.fetch(v) for v in venues), return_exceptions=True)
        for v, r in zip(venues, results):
            latency, calls, limited = self.client.last.get(v.name, (None, 0, False))
            self.next_due[v.name] = now + int(v.poll_interval_sec * 1000)
            if isinstance(r, BaseException):
                self.store.add_poll(now, v.name, False, calls, latency, limited, f"{type(r).__name__}: {r}")
                self.log(f"[{v.name}] error: {type(r).__name__}: {r}")
                if limited:
                    # レート制限に当たったら、その会場だけ間隔を一時的に 4 倍に伸ばす
                    self.next_due[v.name] = now + int(v.poll_interval_sec * 4000)
                    self.log(f"[{v.name}] rate limited -> back off to {v.poll_interval_sec * 4:g}s")
                continue
            self.store.add_poll(now, v.name, True, calls, latency, limited, None)
            self.latest[v.name] = r
            for sym, snap in r.symbols.items():
                self.store.add_funding(now, v.name, sym, snap.funding_apr)

        fresh = [s for s in self.latest.values() if now - s.time <= self.cfg.max_stale_sec * 1000]
        # 価格・板はもっと短い鮮度で切る (basis とスリッページは同時刻の板でないと意味がない)
        usable = {s.venue for s in fresh} - stale_price_venues(fresh, self.cfg, now)
        stale = {v.name for v in self.cfg.venues} - usable
        spreads, misses = evaluate(fresh, self.cfg, now)
        for s in spreads:
            self.store.add_spread(now, s)
        for m in misses:
            self.store.add_depth_miss(now, m)
        for ep in self.tracker.update(now, spreads, stale):
            self.store.add_episode(ep)
            self.closed_episodes += 1
            self.log(
                f"episode short {ep.short_venue} / long {ep.long_venue} {ep.symbol} ${ep.size_usd:,.0f}: "
                f"{ep.duration_sec / 60:.1f}分 ({ep.polls} polls) "
                f"net {ep.first_net_apr * 100:+.2f}% -> max {ep.max_net_apr * 100:+.2f}% "
                f"(粗 {ep.max_gross_apr * 100:+.2f}%, breakeven "
                + (f"{ep.min_breakeven_days:.1f}日)" if ep.min_breakeven_days is not None else "なし)")
            )

        # ファンディング上限の張り付きによるエッジ (clamp.py)。
        # 参照のみの会場も脚に入れて検出し、建てられるかは tradable で区別する
        edges = clamp.detect(fresh, self.cfg, self.caps)
        for e in edges:
            if self.clamp_tracker.open.get(e.key) is None:
                self.log(
                    f"clamp {e.symbol} short {e.short_venue} / long {e.long_venue}: "
                    f"差 年率 {e.edge_apr * 100:+,.0f}% ({e.pinned_venue} が上限 "
                    f"年率 {e.tight_cap_apr * 100:,.0f}% に張り付き"
                    + (f", 両者張付 -> 下限 年率 {e.floor_apr * 100:+,.0f}%" if e.floor_apr else "")
                    + (")" if e.tradable else ", 参照会場を含むので建てられません)")
                )
        for ep in self.clamp_tracker.update(now, edges):
            self.store.add_clamp_episode(ep)
            self.closed_clamp_episodes += 1
            self.log(
                f"clamp 終了 {ep.symbol} short {ep.short_venue} / long {ep.long_venue}: "
                f"{ep.duration_sec / 60:.1f}分 ({ep.polls} polls, 両者張付 {ep.both_pinned_polls}) "
                f"最大 年率 {ep.max_edge_apr * 100:+,.0f}%"
            )
        self.store.commit()

    async def run(self, stop: Optional[asyncio.Event] = None, max_polls: Optional[int] = None) -> None:
        stop = stop or asyncio.Event()
        # 全会場の間隔の最小値ごとに起きて、期限の来た会場だけ取る
        tick = max(1.0, min([v.poll_interval_sec for v in self.cfg.venues] + [self.cfg.poll_interval_sec]))
        n = 0
        last_prune = 0
        self.store.note_start(self.clock())
        while not stop.is_set() and (max_polls is None or n < max_polls):
            started = time.monotonic()
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001  監視は止めない
                self.log(f"poll failed: {type(e).__name__}: {e}")
            n += 1
            now = self.clock()
            if now - last_prune > 3_600_000:
                self.store.prune(now - int(self.cfg.retention_days * 86_400_000))
                self.store.set_meta("last_run_ms", str(now))
                last_prune = now
            delay = max(0.0, tick - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        # 開いているエピソードも、そこまでの継続時間として残す
        for ep in self.tracker.flush(self.clock()):
            self.store.add_episode(ep)
            self.closed_episodes += 1
        for ep in self.clamp_tracker.flush():
            self.store.add_clamp_episode(ep)
            self.closed_clamp_episodes += 1
        self.store.set_meta("last_run_ms", str(self.clock()))
        self.store.commit()
