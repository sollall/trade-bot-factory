"""見積もりから機会を計算し、何ポーリング続いたかを追跡する"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from tbf.arb.config import ArbConfig
from tbf.arb.quotes import ChainQuote


@dataclass(frozen=True)
class Opportunity:
    sell_chain: str
    buy_chain: str
    size: float
    sell_out: float
    buy_in: float
    gross_usd: float
    cost_usd: float
    net_usd: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.sell_chain, self.buy_chain)


def mid_price(quotes: list[ChainQuote]) -> Optional[float]:
    """ガス代換算用の base 価格 (最小サイズの売り値の平均)"""
    px = [q.sizes[0].sell_out / q.sizes[0].size for q in quotes if q.sizes and q.sizes[0].sell_out]
    return sum(px) / len(px) if px else None


def evaluate(quotes: list[ChainQuote], cfg: ArbConfig) -> list[Opportunity]:
    """全ての (売るチェーン, 買うチェーン) × サイズの損益を計算する"""
    px = mid_price(quotes)
    if px is None:
        return []
    out = []
    for a in quotes:
        for b in quotes:
            if a.chain == b.chain:
                continue
            for sa, sb in zip(a.sizes, b.sizes):
                if sa.sell_out is None or sb.buy_in is None:
                    continue
                gross = sa.sell_out - sb.buy_in
                cost = a.gas_usd(sa.sell_gas, px) + b.gas_usd(sb.buy_gas, px) + sa.sell_out * cfg.rebalance_bps / 10_000
                out.append(Opportunity(a.chain, b.chain, sa.size, sa.sell_out, sb.buy_in, gross, cost, gross - cost))
    return out


def best_per_pair(opps: list[Opportunity]) -> dict[tuple[str, str], Opportunity]:
    best: dict[tuple[str, str], Opportunity] = {}
    for o in opps:
        if o.key not in best or o.net_usd > best[o.key].net_usd:
            best[o.key] = o
    return best


@dataclass
class Episode:
    """同じ向きの機会が連続していた期間"""

    sell_chain: str
    buy_chain: str
    start: int
    end: int
    polls: int
    first_net_usd: float
    max_net_usd: float
    best_size: float


@dataclass
class EpisodeTracker:
    min_profit_usd: float
    open: dict[tuple[str, str], Episode] = field(default_factory=dict)

    def update(self, now: int, best: dict[tuple[str, str], Opportunity]) -> list[Episode]:
        """今回のポーリング結果を反映し、終わったエピソードを返す"""
        closed = []
        for key in list(self.open):
            o = best.get(key)
            if o is None or o.net_usd < self.min_profit_usd:
                closed.append(self.open.pop(key))
        for key, o in best.items():
            if o.net_usd < self.min_profit_usd:
                continue
            ep = self.open.get(key)
            if ep is None:
                self.open[key] = Episode(o.sell_chain, o.buy_chain, now, now, 1, o.net_usd, o.net_usd, o.size)
            else:
                ep.end, ep.polls = now, ep.polls + 1
                if o.net_usd > ep.max_net_usd:
                    ep.max_net_usd, ep.best_size = o.net_usd, o.size
        return closed
