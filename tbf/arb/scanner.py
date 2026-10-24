"""シャドーモード: 機会を監視・記録するだけで、売買はしない"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from tbf.arb.config import ArbConfig
from tbf.arb.opportunity import EpisodeTracker, best_per_pair, evaluate
from tbf.arb.quotes import ChainQuote, fetch_chain_quote
from tbf.arb.store import ArbStore
from tbf.onchain.rpc import Rpc


class Scanner:
    def __init__(
        self,
        cfg: ArbConfig,
        rpcs: dict[str, Rpc],
        store: ArbStore,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        log: Callable[[str], None] = print,
    ) -> None:
        self.cfg = cfg
        self.rpcs = rpcs
        self.store = store
        self.clock = clock
        self.log = log
        self.tracker = EpisodeTracker(cfg.min_profit_usd)
        self.closed_episodes = 0

    async def poll_once(self) -> None:
        now = self.clock()
        results = await asyncio.gather(
            *(fetch_chain_quote(self.rpcs[c.name], c, self.cfg) for c in self.cfg.chains), return_exceptions=True
        )
        quotes: list[ChainQuote] = []
        for c, r in zip(self.cfg.chains, results):
            if isinstance(r, BaseException):
                self.store.add_poll(now, c.name, None, None, f"{type(r).__name__}: {r}")
                self.log(f"[{c.name}] error: {r}")
            else:
                self.store.add_poll(now, c.name, r.block, r.latency_ms, None)
                quotes.append(r)

        opps = evaluate(quotes, self.cfg)
        smallest = min(self.cfg.sizes)
        for o in opps:
            if o.size == smallest:
                self.store.add_spread(now, o)
            if o.net_usd >= self.cfg.min_profit_usd:
                self.store.add_opportunity(now, o)
        best = best_per_pair(opps)
        for ep in self.tracker.update(now, best):
            self.store.add_episode(ep)
            self.closed_episodes += 1
            self.log(
                f"episode {ep.sell_chain}->{ep.buy_chain} polls={ep.polls} "
                f"first ${ep.first_net_usd:.2f} max ${ep.max_net_usd:.2f} size {ep.best_size}"
            )
        self.store.commit()

    async def run(self, stop: Optional[asyncio.Event] = None, max_polls: Optional[int] = None) -> None:
        stop = stop or asyncio.Event()
        n = 0
        last_prune = 0
        while not stop.is_set() and (max_polls is None or n < max_polls):
            started = time.monotonic()
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001  監視は止めない
                self.log(f"poll failed: {type(e).__name__}: {e}")
            n += 1
            now = self.clock()
            if now - last_prune > 3_600_000:
                self.store.prune(now - 7 * 86_400_000)
                last_prune = now
            delay = max(0.0, self.cfg.poll_interval_sec - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
