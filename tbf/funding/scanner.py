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

from tbf.funding.config import FundingConfig, VenueConfig
from tbf.funding.spread import EpisodeTracker, evaluate
from tbf.funding.store import FundingStore
from tbf.funding.venues import VenueClient, VenueSnapshot

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
    ) -> None:
        self.cfg = cfg
        self.client = client
        self.store = store
        self.clock = clock
        self.log = log
        self.tracker = EpisodeTracker(cfg.min_net_apr, cfg.rank_hold_days)
        self.latest: dict[str, VenueSnapshot] = {}
        self.next_due: dict[str, int] = {v.name: 0 for v in cfg.venues}
        self.closed_episodes = 0

    def due(self, now: int) -> list[VenueConfig]:
        return [
            v
            for v in self.cfg.venues
            if now + due_slack_ms(v.poll_interval_sec) >= self.next_due.get(v.name, 0)
        ]

    async def poll_once(self) -> None:
        now = self.clock()
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
        spreads, misses = evaluate(fresh, self.cfg)
        for s in spreads:
            self.store.add_spread(now, s)
        for m in misses:
            self.store.add_depth_miss(now, m)
        for ep in self.tracker.update(now, spreads):
            self.store.add_episode(ep)
            self.closed_episodes += 1
            self.log(
                f"episode short {ep.short_venue} / long {ep.long_venue} {ep.symbol} ${ep.size_usd:,.0f}: "
                f"{ep.duration_sec / 60:.1f}分 ({ep.polls} polls) "
                f"net {ep.first_net_apr * 100:+.2f}% -> max {ep.max_net_apr * 100:+.2f}% "
                f"(粗 {ep.max_gross_apr * 100:+.2f}%, breakeven "
                + (f"{ep.min_breakeven_days:.1f}日)" if ep.min_breakeven_days is not None else "なし)")
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
        self.store.set_meta("last_run_ms", str(self.clock()))
        self.store.commit()
