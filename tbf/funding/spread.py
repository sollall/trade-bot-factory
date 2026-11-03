"""実効スプレッドの計算と、その水準が続いた時間 (エピソード) の追跡

粗スプレッド (gross_apr) だけでは意味がない。知りたいのは
  - 手数料とスリッページを引いて、何日持てば黒字になるか (breakeven_days)
  - その水準が何時間続いたか (Episode)
の 2 つ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from tbf.funding.config import FundingConfig, VenueConfig
from tbf.funding.venues import SymbolSnapshot, VenueSnapshot

BPS = 10_000.0
DAYS_PER_YEAR = 365.0


def avg_fill_price(levels: tuple[tuple[float, float], ...], notional_usd: float) -> Optional[float]:
    """板を上 (下) から成行で食べて notional_usd ぶん約定したときの平均価格

    板が足りなければ None。「食べられるところまで食べた平均」を返してしまうと
    スリッページを小さく見せてしまうので、足りない場合は評価しない。
    """
    if notional_usd <= 0:
        return None
    spent = 0.0
    qty = 0.0
    for px, sz in levels:
        if px <= 0 or sz <= 0:
            continue
        take = min(px * sz, notional_usd - spent)
        qty += take / px
        spent += take
        if spent >= notional_usd - 1e-9:
            return spent / qty
    return None


def slippage_bps(
    levels: tuple[tuple[float, float], ...], notional_usd: float, ref_price: float, buy: bool
) -> Optional[float]:
    """基準価格に対する平均約定価格の不利な乖離 (bps)。板が足りなければ None"""
    if ref_price <= 0:
        return None
    avg = avg_fill_price(levels, notional_usd)
    if avg is None:
        return None
    diff = (avg - ref_price) if buy else (ref_price - avg)
    return diff / ref_price * BPS


@dataclass(frozen=True)
class LegCost:
    """片脚ぶんのコスト。ショート脚は入るとき売り / 出るとき買い、ロング脚はその逆"""

    venue: str
    taker_bps: float
    sell_slip_bps: Optional[float]
    buy_slip_bps: Optional[float]

    @property
    def ok(self) -> bool:
        return self.sell_slip_bps is not None and self.buy_slip_bps is not None


def leg_cost(v: VenueConfig, snap: SymbolSnapshot, size_usd: float) -> LegCost:
    book = snap.book
    ref = snap.ref_price
    sell = slippage_bps(book.bids, size_usd, ref, buy=False) if book else None
    buy = slippage_bps(book.asks, size_usd, ref, buy=True) if book else None
    return LegCost(v.name, v.taker_bps, sell, buy)


@dataclass(frozen=True)
class Spread:
    short_venue: str
    long_venue: str
    symbol: str
    size_usd: float
    # ショート側の年率 - ロング側の年率 (ショートが受け取り、ロングが支払い)
    gross_apr: float
    entry_cost_bps: float
    exit_cost_bps: float
    short_apr: float
    long_apr: float

    @property
    def cost_bps(self) -> float:
        return self.entry_cost_bps + self.exit_cost_bps

    @property
    def breakeven_days(self) -> Optional[float]:
        """控除後に黒字になるまでの保有日数。粗スプレッドが 0 以下なら永遠に黒字にならない"""
        if self.gross_apr <= 0:
            return None
        return self.cost_bps / (self.gross_apr * BPS / DAYS_PER_YEAR)

    def net_apr(self, hold_days: float) -> float:
        """hold_days 持って解消した場合の、往復コスト控除後の年率"""
        return self.gross_apr - self.cost_bps / BPS * DAYS_PER_YEAR / hold_days

    @property
    def key(self) -> tuple[str, str, str, float]:
        return (self.short_venue, self.long_venue, self.symbol, self.size_usd)


@dataclass(frozen=True)
class DepthMiss:
    """そのサイズを板で約定できなかった記録 (サイズを上げると消える、を見せるため)"""

    venue: str
    symbol: str
    size_usd: float


def evaluate(
    snapshots: list[VenueSnapshot], cfg: FundingConfig
) -> tuple[list[Spread], list[DepthMiss]]:
    """全ての (ショート会場, ロング会場) × 銘柄 × サイズ の実効スプレッドを出す"""
    by_venue = {s.venue: s for s in snapshots}
    tradable = [v for v in cfg.tradable if v.name in by_venue]
    spreads: list[Spread] = []
    misses: list[DepthMiss] = []

    # 脚ごとのコストは (会場, 銘柄, サイズ) で決まるので先に 1 回だけ計算する
    costs: dict[tuple[str, str, float], LegCost] = {}
    for v in tradable:
        snap = by_venue[v.name]
        for sym, ss in snap.symbols.items():
            for size in cfg.sizes_usd:
                c = leg_cost(v, ss, size)
                costs[(v.name, sym, size)] = c
                if not c.ok:
                    misses.append(DepthMiss(v.name, sym, size))

    for a in tradable:
        for b in tradable:
            if a.name == b.name:
                continue
            sa, sb = by_venue[a.name], by_venue[b.name]
            for sym in cfg.symbols:
                if sym not in sa.symbols or sym not in sb.symbols:
                    continue
                short_apr = sa.symbols[sym].funding_apr
                long_apr = sb.symbols[sym].funding_apr
                for size in cfg.sizes_usd:
                    ca, cb = costs[(a.name, sym, size)], costs[(b.name, sym, size)]
                    if not ca.ok or not cb.ok:
                        continue
                    # 入る: A でショート (買い板を食う) + B でロング (売り板を食う)
                    entry = ca.taker_bps + cb.taker_bps + ca.sell_slip_bps + cb.buy_slip_bps
                    # 出る: A を買い戻す + B を売る
                    exit_ = ca.taker_bps + cb.taker_bps + ca.buy_slip_bps + cb.sell_slip_bps
                    spreads.append(
                        Spread(
                            short_venue=a.name,
                            long_venue=b.name,
                            symbol=sym,
                            size_usd=size,
                            gross_apr=short_apr - long_apr,
                            entry_cost_bps=entry,
                            exit_cost_bps=exit_,
                            short_apr=short_apr,
                            long_apr=long_apr,
                        )
                    )
    return spreads, misses


@dataclass
class Episode:
    """同じ (ショート会場, ロング会場, 銘柄, サイズ) で net_apr が閾値以上だった連続区間"""

    short_venue: str
    long_venue: str
    symbol: str
    size_usd: float
    start: int
    end: int
    polls: int
    first_net_apr: float
    max_net_apr: float
    max_gross_apr: float
    min_breakeven_days: Optional[float]

    @property
    def duration_sec(self) -> float:
        return (self.end - self.start) / 1000


@dataclass
class EpisodeTracker:
    """arb/opportunity.py の EpisodeTracker と同じ考え方

    一瞬だけ出る大きな差は実際には取れないので、「閾値を超えた状態が何秒続いたか」を記録する。
    """

    min_net_apr: float
    hold_days: float
    open: dict[tuple[str, str, str, float], Episode] = field(default_factory=dict)

    def update(self, now: int, spreads: list[Spread]) -> list[Episode]:
        """今回のポーリングを反映し、終わったエピソードを返す"""
        best: dict[tuple[str, str, str, float], Spread] = {}
        for s in spreads:
            cur = best.get(s.key)
            if cur is None or s.net_apr(self.hold_days) > cur.net_apr(self.hold_days):
                best[s.key] = s
        closed = []
        for key in list(self.open):
            s = best.get(key)
            if s is None or s.net_apr(self.hold_days) < self.min_net_apr:
                closed.append(self.open.pop(key))
        for key, s in best.items():
            net = s.net_apr(self.hold_days)
            if net < self.min_net_apr:
                continue
            ep = self.open.get(key)
            if ep is None:
                self.open[key] = Episode(
                    s.short_venue, s.long_venue, s.symbol, s.size_usd, now, now, 1, net, net,
                    s.gross_apr, s.breakeven_days,
                )
            else:
                ep.end, ep.polls = now, ep.polls + 1
                ep.max_net_apr = max(ep.max_net_apr, net)
                ep.max_gross_apr = max(ep.max_gross_apr, s.gross_apr)
                be = s.breakeven_days
                if be is not None and (ep.min_breakeven_days is None or be < ep.min_breakeven_days):
                    ep.min_breakeven_days = be
        return closed

    def flush(self, now: int) -> list[Episode]:
        """停止時に、開いているエピソードを打ち切って返す"""
        out = list(self.open.values())
        self.open.clear()
        return out
