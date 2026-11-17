"""1 つの tick のあいだ、同じ足を取り直さないための包み

確定足の tick では全 bot が同時に足を取りにいく。同じ銘柄・同じ足を見ている bot が
4 本あると同じ内容のリクエストが 4 回飛び、Coinbase の公開APIの流量制限
(429 Public rate limit exceeded) を踏む。Cloudflare Workers の出口IPは他の利用者と
共有なので、自分のリクエストが少なくても余裕は小さい (実測は docs/market-data.md / TRA-81)。

対策は取り直しではなく、そもそも投げないこと。tick の最初に「この tick で誰かが必要とする
最大本数」を bot の定義から数えておき、最初に欲しがった bot のところでその本数をまとめて
取る。あとの bot は手元の足を切り出して使う。

足は tick ごとに新しい包みを作る (`per_tick_markets`) ので、tick をまたいで古い足が
残ることはない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from tbf.core.bot import Bot
from tbf.core.types import Candle, FundingPoint
from tbf.market import VenueMarket


@dataclass
class CachingMarket:
    """VenueMarket を包んで、同じ問い合わせを 1 回に畳む"""

    inner: VenueMarket
    #: (銘柄, 足) -> この tick で誰かが必要とする最大本数
    needed: Mapping[tuple[str, str], int] = field(default_factory=dict)
    # (銘柄, 足, 時刻) -> (取った本数, 足)。取った本数を覚えておかないと、
    # 「取引所が足りない本数しか返さなかった」のと区別できず毎回取り直してしまう
    _candles: dict[tuple[str, str, int], tuple[int, list[Candle]]] = field(default_factory=dict)
    _prices: dict[str, float] = field(default_factory=dict)
    _funding: dict[tuple[str, int, int], list[FundingPoint]] = field(default_factory=dict)

    async def prices(self, symbols: list[str]) -> dict[str, float]:
        missing = sorted({s for s in symbols if s not in self._prices})
        if missing:
            self._prices.update(await self.inner.prices(missing))
        return {s: self._prices[s] for s in symbols if s in self._prices}

    async def recent(self, symbol: str, timeframe: str, count: int, now: int) -> list[Candle]:
        if count <= 0:
            return []
        key = (symbol, timeframe, now)
        got = self._candles.get(key)
        if got is None or got[0] < count:
            want = max(count, self.needed.get((symbol, timeframe), 0))
            got = (want, await self.inner.recent(symbol, timeframe, want, now))
            self._candles[key] = got
        return got[1][-count:]

    async def history(self, symbol: str, timeframe: str, start: int, end: int) -> list[Candle]:
        return await self.inner.history(symbol, timeframe, start, end)

    async def funding(self, symbol: str, start: int, end: int) -> list[FundingPoint]:
        key = (symbol, start, end)
        if key not in self._funding:
            self._funding[key] = await self.inner.funding(symbol, start, end)
        return self._funding[key]


def candles_needed(bots: Sequence[Bot]) -> dict[str, dict[tuple[str, str], int]]:
    """{取得元: {(銘柄, 足): 必要な最大本数}}。bot の定義だけから決まるので通信はしない"""
    out: dict[str, dict[tuple[str, str], int]] = {}
    for bot in bots:
        for inst in bot.instruments.values():
            per_venue = out.setdefault(inst.price_venue, {})
            key = (inst.price_symbol, bot.timeframe)
            per_venue[key] = max(per_venue.get(key, 0), bot.lookback)
    return out


def per_tick_markets(markets: Mapping[str, VenueMarket], bots: Sequence[Bot]) -> dict[str, VenueMarket]:
    """この tick だけ有効な包みを作る"""
    needed = candles_needed(bots)
    return {name: CachingMarket(m, needed.get(name, {})) for name, m in markets.items()}
