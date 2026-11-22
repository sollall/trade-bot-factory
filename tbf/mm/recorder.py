"""記録: 気配と約定をポーリングして残す。売買はしない。鍵も要らない。\n\n会場ごとの API の読み方は tbf/mm/venues.py。polls の kind は、どの会場でも気配が "l2Book"、約定が "recentTrades"\n(集計がこの名前で引くので、Hyperliquid の呼び名をそのまま使っている)。\n"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from tbf.http import Http
from tbf.market.hyperliquid import INFO_URL
from tbf.mm.book import Book, Trade
from tbf.mm.store import MmStore
from tbf.mm.venues import Hyperliquid, RateLimited, Venue
from tbf.mm.venues import parse_hyperliquid_book as parse_book  # noqa: F401  前からの名前
from tbf.mm.venues import parse_hyperliquid_trades as parse_trades  # noqa: F401

# ---- 流量制限
#
# 上限と 1 回あたりの重みは会場ごと (tbf/mm/venues.py)。どの会場も公式の裏取りが無いか不十分なので、
# 外れていても上限を踏まないように、既定では上限の半分までしか使わない (DEFAULT_BUDGET)。
WEIGHT_LIMIT_PER_MIN = int(Hyperliquid.limit_per_min)  # 前からの名前 (Hyperliquid の値)
WEIGHTS = {"l2Book": int(Hyperliquid.book_cost), "recentTrades": int(Hyperliquid.trades_cost), "meta": 20}
#: 上限のうち使ってよい割合。残りは、429 の取り直し (tbf/http.py。1 回の失敗で最大 4 回投げる) と、
#: 同じ IP から出る他の用途 (バックテストの足の取得など) のために空けておく
DEFAULT_BUDGET = 0.5
#: 429 を受けたら、この秒数は何も投げない (取り直しを重ねて制限を長引かせないため)。会場ごとの値は Venue.cooldown_sec
COOLDOWN_SEC = Hyperliquid.cooldown_sec


def weight_per_min(n_coins: int, interval: float, trades_interval: float, venue: Optional[Venue] = None) -> float:
    """この設定が毎分使う量 (取り直しは含まない)。Hyperliquid は重み、ほかの会場は回数"""
    return (venue or Hyperliquid()).per_min(n_coins, interval, trades_interval)


def check_budget(
    n_coins: int, interval: float, trades_interval: float, budget: float = DEFAULT_BUDGET, venue: Optional[Venue] = None
) -> float:
    """毎分の量が使ってよい量を超える設定なら ValueError。超えなければその量を返す"""
    venue = venue or Hyperliquid()
    if interval <= 0 or trades_interval <= 0:
        raise ValueError("間隔は正の秒数にしてください")
    if not 0 < budget <= 1:
        raise ValueError("--budget は 0 より大きく 1 以下 (1 = 上限いっぱい)")
    used, allowed = venue.per_min(n_coins, interval, trades_interval), venue.limit_per_min * budget
    if used > allowed:
        raise ValueError(
            f"流量制限を超える設定です: 毎分の{venue.unit} {used:.0f} > 使ってよい量 {allowed:.0f} "
            f"(上限 {venue.limit_per_min:.0f} x {budget:g})。銘柄を減らすか、間隔を延ばしてください "
            f"(気配 1 回 = {venue.book_cost:g}、約定 1 回 = {venue.trades_cost:g})"
        )
    return used


class Recorder:
    def __init__(
        self,
        http: Http,
        store: MmStore,
        coins: list[str],
        interval: float = 2.0,
        trades_interval: float = 10.0,
        depth: int = 10,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        log: Callable[[str], None] = print,
        url: str = INFO_URL,
        on_book: Optional[Callable[[str, Book, int], None]] = None,
        on_trades: Optional[Callable[[str, list[Trade], int], None]] = None,
        venue: Optional[Venue] = None,
        min_gap: Optional[float] = None,
    ) -> None:
        self.venue = venue or Hyperliquid(url)
        # リクエストを投げ始める間隔の下限。会場によっては、合計が少なくても同時に投げると弾かれる (GMO)
        self.min_gap = self.venue.min_gap_sec if min_gap is None else min_gap
        self._gate = asyncio.Lock()
        self._last_request = 0.0
        # on_book(銘柄, 新しく残した気配, 受け取った時刻) / on_trades(銘柄, 新しく残した約定, 取りにいった時刻)。
        # 記録に残したものだけを、残した後に渡す (paper が記録と同じものを見るように)。時刻は clock の ms
        self.on_book, self.on_trades = on_book, on_trades
        self.http, self.store, self.coins, self.url = http, store, coins, url
        self.interval, self.trades_interval, self.depth = interval, trades_interval, depth
        self.clock, self.log = clock, log
        # 銘柄ごとの「これまでに見た約定」。前回止まった間の取りこぼしも分かるように、記録から引き継ぐ
        self.seen: dict[str, set[tuple[int, int]]] = {c: set(store.recent_trade_keys(c)) for c in coins}
        self.gaps = 0
        self.rate_limited = False

    async def setup(self) -> None:
        """違う会場の db でないこと、銘柄が実在することを確かめ、銘柄の決まりごと (刻み・最小数量・手数料) を残す"""
        self.store.claim_venue(self.venue.name)
        for inst in (await self.venue.instruments(self.http, self.coins)).values():
            self.store.set_instrument(inst, self.clock())
        self.store.commit()

    async def _wait_turn(self) -> None:
        if self.min_gap <= 0:
            return
        async with self._gate:
            wait = self._last_request + self.min_gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _fetch(self, kind: str, coin: str, fetch: Callable[[], Awaitable], handle: Callable[[object, int, int], int]) -> None:
        await self._wait_turn()
        now, started = self.clock(), time.monotonic()
        try:
            got = await fetch()
            latency = (time.monotonic() - started) * 1000
            self.store.add_poll(now, coin, kind, latency, None, handle(got, now, self.clock()))
        except Exception as e:  # noqa: BLE001  1 銘柄の失敗で他を止めない
            msg = f"{type(e).__name__}: {e}"
            self.rate_limited = self.rate_limited or isinstance(e, RateLimited) or "HTTP 429" in msg
            self.store.add_poll(now, coin, kind, None, msg[:300], None)
            self.log(f"[{coin}] {kind} error: {msg[:200]}")

    def _on_book(self, coin: str, book: Book, recv: int) -> int:
        new = self.store.add_book(coin, book, recv)
        if new and self.on_book:
            self.on_book(coin, book, recv)
        return new

    def _on_trades(self, coin: str, trades: list[Trade], started: int, recv: int) -> int:
        keys = {(t.time, t.tid) for t in trades}
        seen = self.seen[coin]
        # 約定は直近の何件かしか返らない (Hyperliquid 10、bitbank 60、GMO 100)。前回までと 1 件も重ならなければ、
        # その間の約定を取りこぼしたかもしれない (ちょうど全部が新しいだけ、のこともある。件数は分からない)
        if keys and seen and not keys & seen:
            self.store.add_gap(coin, recv, max(t for t, _ in seen), min(t.time for t in trades))
            self.gaps += 1
        new = self.store.add_trades(coin, trades, recv)
        if keys:
            self.seen[coin] = keys  # 次に比べる相手は直近の応答だけで足りる
        if self.on_trades:
            self.on_trades(coin, new, started)
        return len(new)

    async def poll_books(self) -> None:
        await asyncio.gather(*(
            self._fetch("l2Book", c, lambda c=c: self.venue.book(self.http, c, self.depth),
                        lambda book, started, recv, c=c: self._on_book(c, book, recv))
            for c in self.coins
        ))
        self.store.commit()

    async def poll_trades(self) -> None:
        await asyncio.gather(*(
            self._fetch("recentTrades", c, lambda c=c: self.venue.trades(self.http, c),
                        lambda trades, started, recv, c=c: self._on_trades(c, trades, started, recv))
            for c in self.coins
        ))
        self.store.commit()

    async def run(self, stop: Optional[asyncio.Event] = None, max_seconds: Optional[float] = None) -> None:
        stop = stop or asyncio.Event()
        started = time.monotonic()
        next_book = next_trades = started
        while not stop.is_set() and (max_seconds is None or time.monotonic() - started < max_seconds):
            now = time.monotonic()
            try:
                if now >= next_trades:
                    await self.poll_trades()
                    next_trades = max(next_trades + self.trades_interval, now)
                if now >= next_book:
                    await self.poll_books()
                    next_book = max(next_book + self.interval, now)
            except Exception as e:  # noqa: BLE001  記録は止めない
                self.log(f"poll failed: {type(e).__name__}: {e}")
                next_book, next_trades = max(next_book, now + self.interval), max(next_trades, now + self.interval)
            if self.rate_limited:
                self.rate_limited = False
                self.log(f"429 (流量制限) を受けたので {self.venue.cooldown_sec:.0f} 秒休みます")
                next_book = next_trades = time.monotonic() + self.venue.cooldown_sec
            delay = max(0.0, min(next_book, next_trades) - time.monotonic())
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
