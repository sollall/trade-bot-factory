"""確定ファンディングでの主指標

ライブのスナップショットから出す net_apr は「今の予測レートが hold 日続く」という
外挿なので、14 日保有の損益を 7 日のライブ計測で検証することはできない。
ここでは history.py で埋めた**確定済み**のファンディングを使い、

    ある時点 t で建てて t + hold 日に解消したとき、
    実際に確定したファンディングの差 − 往復コスト

を計算する。これが主指標。ライブで測り続ける必要があるのは板 (スリッページ) と
会場間の価格差 (basis) だけになる。

往復コストはライブ計測の実測値を使う。履歴の板は取れないので、
「過去の時点のスリッページ」は分からない。これは近似であり、レポートに明記する。

分母の約束: net は**片脚の建玉**に対する分数。2 会場に証拠金を置くので、
資本に対する利回りは leverage 次第で変わる (capital_apr を併記)。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Iterable, Optional

from tbf.funding.history import HOUR_MS, FundingPoint, hour_floor

BPS = 10_000.0
DAYS_PER_YEAR = 365.0

PairKey = tuple[str, str, str]  # (ショート会場, ロング会場, 銘柄)


# ------------------------------------------------------------------ 1 時間刻みの系列


class HourlySeries:
    """(会場, 銘柄) ごとの「その 1 時間に確定した分数」と mark 価格

    会場によって区間の長さが違う (Paradex は 8 時間サンプル) ので、
    rate を区間の時間数で割って各時間に配る。合計は保たれる。
    """

    def __init__(self, start_hour: int, hours: int) -> None:
        self.start_hour = start_hour
        self.rates: list[Optional[float]] = [None] * hours
        self.marks: list[Optional[float]] = [None] * hours

    @property
    def hours(self) -> int:
        return len(self.rates)

    def index(self, ms: int) -> int:
        return (hour_floor(ms) - self.start_hour) // HOUR_MS

    def add(self, p: FundingPoint) -> None:
        span = max(1, int(round(p.hours)))
        per_hour = p.rate / span
        # 区間の終わりが確定時刻なので、[time - span時間, time) の各時間に配る
        last = self.index(p.time) - 1 if p.hours >= 1 else self.index(p.time)
        for k in range(span):
            i = last - k
            if 0 <= i < self.hours:
                self.rates[i] = (self.rates[i] or 0.0) + per_hour
        # mark も「その時間の終値」として同じ枠に置く (レートと添字を揃える)
        if p.mark and 0 <= last < self.hours:
            self.marks[last] = p.mark

    def finalize(self) -> None:
        """累積和を作る。sum(i0, i1) を O(1) にする"""
        self._cum = [0.0] * (self.hours + 1)
        self._cnt = [0] * (self.hours + 1)
        for i, r in enumerate(self.rates):
            self._cum[i + 1] = self._cum[i] + (r or 0.0)
            self._cnt[i + 1] = self._cnt[i] + (1 if r is not None else 0)
        # mark は穴があくので直近既知で埋める (価格は 1 時間で消えたりしない)
        self._mark_ffill: list[Optional[float]] = [None] * self.hours
        last: Optional[float] = None
        for i, m in enumerate(self.marks):
            last = m if m is not None else last
            self._mark_ffill[i] = last

    def sum(self, i0: int, i1: int) -> tuple[float, int]:
        """[i0, i1) に確定した合計と、データのあった時間数"""
        i0, i1 = max(0, i0), min(self.hours, i1)
        if i1 <= i0:
            return 0.0, 0
        return self._cum[i1] - self._cum[i0], self._cnt[i1] - self._cnt[i0]

    def mark_at(self, i: int) -> Optional[float]:
        if not 0 <= i < self.hours:
            return None
        return self._mark_ffill[i]


def build_series(points: dict[tuple[str, str], list[FundingPoint]]) -> dict[tuple[str, str], HourlySeries]:
    """全系列を同じ 1 時間グリッドに載せる"""
    times = [p.time for ps in points.values() for p in ps]
    if not times:
        return {}
    start_hour = hour_floor(min(times)) - 24 * HOUR_MS  # 8h サンプルが先頭にかかるぶんの余白
    hours = (hour_floor(max(times)) - start_hour) // HOUR_MS + 1
    out: dict[tuple[str, str], HourlySeries] = {}
    for key, ps in points.items():
        s = HourlySeries(start_hour, hours)
        for p in ps:
            s.add(p)
        s.finalize()
        out[key] = s
    return out


def series_span(series: dict[tuple[str, str], HourlySeries]) -> tuple[int, int]:
    """データのある時間の範囲 (ms, 半開区間)"""
    lo, hi = None, None
    for s in series.values():
        idx = [i for i, r in enumerate(s.rates) if r is not None]
        if not idx:
            continue
        a = s.start_hour + idx[0] * HOUR_MS
        b = s.start_hour + (idx[-1] + 1) * HOUR_MS
        lo = a if lo is None else min(lo, a)
        hi = b if hi is None else max(hi, b)
    if lo is None or hi is None:
        return 0, 0
    return lo, hi


# ------------------------------------------------------------------ 1 本の建玉


@dataclass(frozen=True)
class RealizedTrade:
    short_venue: str
    long_venue: str
    symbol: str
    entry: int
    hold_days: float
    # 期間中に確定したファンディングの差 (片脚の建玉に対する分数)
    gross: float
    # 往復コスト (bps)。乗り換えがない窓では 0 を入れる
    cost_bps: float
    # 2 会場の価格の相対的なずれによる損益 (分数)。net には入れず別に出す
    basis_pnl: Optional[float]
    # 両会場にデータがあった時間数と、あるべき時間数
    hours: int
    expected_hours: int

    @property
    def key(self) -> PairKey:
        return (self.short_venue, self.long_venue, self.symbol)

    @property
    def coverage(self) -> float:
        return self.hours / self.expected_hours if self.expected_hours else 0.0

    @property
    def net(self) -> float:
        return self.gross - self.cost_bps / BPS

    @property
    def net_apr(self) -> float:
        return self.net * DAYS_PER_YEAR / self.hold_days

    @property
    def gross_apr(self) -> float:
        return self.gross * DAYS_PER_YEAR / self.hold_days

    @property
    def basis_apr(self) -> Optional[float]:
        if self.basis_pnl is None:
            return None
        return self.basis_pnl * DAYS_PER_YEAR / self.hold_days

    @property
    def net_apr_with_basis(self) -> Optional[float]:
        if self.basis_pnl is None:
            return None
        return (self.net + self.basis_pnl) * DAYS_PER_YEAR / self.hold_days


def pair_keys(series: dict[tuple[str, str], HourlySeries], tradable: Iterable[str], symbols: Iterable[str]) -> list[PairKey]:
    tradable = list(tradable)
    out = []
    for sym in symbols:
        have = [v for v in tradable if (v, sym) in series]
        for a in have:
            for b in have:
                if a != b:
                    out.append((a, b, sym))
    return out


def realize(
    series: dict[tuple[str, str], HourlySeries],
    key: PairKey,
    entry: int,
    hold_days: float,
    cost_bps: float,
    min_coverage: float = 0.9,
) -> Optional[RealizedTrade]:
    short_v, long_v, sym = key
    sa, sb = series.get((short_v, sym)), series.get((long_v, sym))
    if sa is None or sb is None:
        return None
    i0, i1 = sa.index(entry), sa.index(entry + int(hold_days * 24) * HOUR_MS)
    expected = i1 - i0
    if expected <= 0:
        return None
    ga, na = sa.sum(i0, i1)
    gb, nb = sb.sum(sb.index(entry), sb.index(entry + int(hold_days * 24) * HOUR_MS))
    hours = min(na, nb)
    if hours < expected * min_coverage:
        return None
    # 建てた時刻の価格は「その 1 時間前の終値」(= 建てた瞬間の値) を使う。
    # 取れなければ建てた時間の終値で代用する。
    pa0 = sa.mark_at(i0 - 1) or sa.mark_at(i0)
    pb0 = sb.mark_at(i0 - 1) or sb.mark_at(i0)
    pa1, pb1 = sa.mark_at(i1 - 1), sb.mark_at(i1 - 1)
    basis = None
    if pa0 and pa1 and pb0 and pb1:
        # ショート脚は下がって得、ロング脚は上がって得。相対的なずれだけが残る
        basis = (pb1 / pb0) - (pa1 / pa0)
    # ショート側は受け取り、ロング側は支払い
    return RealizedTrade(short_v, long_v, sym, entry, hold_days, ga - gb, cost_bps, basis, hours, expected)


def entry_grid(start: int, end: int, hold_days: float, step_hours: float) -> list[int]:
    """建てる時刻の格子。hold 日ぶんの出口が収まるところまで"""
    step = max(HOUR_MS, int(step_hours * HOUR_MS))
    last = end - int(hold_days * 24) * HOUR_MS
    return list(range(hour_floor(start), hour_floor(last) + 1, step)) if last >= start else []


# ------------------------------------------------------------------ 分布


@dataclass(frozen=True)
class RealizedStat:
    key: PairKey
    n: int
    median_net_apr: float
    p25_net_apr: float
    p75_net_apr: float
    worst_net_apr: float
    best_net_apr: float
    win_ratio: float
    median_gross_apr: float
    median_basis_apr: Optional[float]
    median_with_basis_apr: Optional[float]
    cost_bps: float

    @property
    def label(self) -> str:
        return f"S:{self.key[0]} / L:{self.key[1]}"


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return 0.0
    return xs[min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))]


def realized_stats(trades: list[RealizedTrade]) -> list[RealizedStat]:
    grouped: dict[PairKey, list[RealizedTrade]] = {}
    for t in trades:
        grouped.setdefault(t.key, []).append(t)
    out = []
    for key, ts in grouped.items():
        nets = [t.net_apr for t in ts]
        basis = [t.basis_apr for t in ts if t.basis_apr is not None]
        withb = [t.net_apr_with_basis for t in ts if t.net_apr_with_basis is not None]
        out.append(
            RealizedStat(
                key=key,
                n=len(ts),
                median_net_apr=statistics.median(nets),
                p25_net_apr=_q(nets, 0.25),
                p75_net_apr=_q(nets, 0.75),
                worst_net_apr=min(nets),
                best_net_apr=max(nets),
                win_ratio=sum(1 for x in nets if x > 0) / len(nets),
                median_gross_apr=statistics.median([t.gross_apr for t in ts]),
                median_basis_apr=statistics.median(basis) if basis else None,
                median_with_basis_apr=statistics.median(withb) if withb else None,
                cost_bps=ts[0].cost_bps,
            )
        )
    return sorted(out, key=lambda s: s.median_net_apr, reverse=True)


# ------------------------------------------------------------------ ウォークフォワード


@dataclass(frozen=True)
class RealizedWindow:
    index: int
    start: int
    end: int
    hold_days: float
    # 前の窓 (IS) で最良だったペア = この窓で持つペア
    picked: Optional[PairKey]
    picked_is_net_apr: Optional[float]
    # そのペアをこの窓で持った結果 (OOS)。乗り換えがあった窓だけ往復コストを課す
    oos_net_apr: Optional[float]
    oos_gross_apr: Optional[float]
    oos_basis_apr: Optional[float]
    switched: bool
    # 後から見た最良ペア (こちらは新規に建てるので往復コストを課す)
    best: Optional[PairKey]
    best_net_apr: Optional[float]
    coverage: Optional[float]

    @property
    def efficiency(self) -> Optional[float]:
        if self.oos_net_apr is None or self.best_net_apr is None or self.best_net_apr <= 0:
            return None
        return self.oos_net_apr / self.best_net_apr


def walkforward_realized(
    series: dict[tuple[str, str], HourlySeries],
    keys: list[PairKey],
    start: int,
    end: int,
    hold_days: float,
    cost_bps: dict[PairKey, float],
    default_cost_bps: float,
    min_coverage: float = 0.9,
) -> list[RealizedWindow]:
    """1 窓 = 保有期間。評価する期間と保有期間を一致させる

    以前の実装には 2 つのずれがあった:
      - 窓が 1 日なのに保有を 14 日としてコストを按分していた (期間が一致していない)
      - 前の窓と違うペアに乗り換えたときの往復コストを課していなかった
        -> ペアが頻繁に入れ替わると OOS が実際より良く見える
    ここでは窓の長さ = hold_days、乗り換えた窓だけ往復コストを課す。
    """
    span = int(hold_days * 24) * HOUR_MS
    n = (end - start) // span
    if n < 2:
        return []
    # 最後の窓がデータの端で切れないよう、終わり側に揃えて窓を切る
    first = end - n * span
    out: list[RealizedWindow] = []
    prev_nets: dict[PairKey, float] = {}
    held: Optional[PairKey] = None
    for i in range(n):
        ws = first + i * span
        full = {
            k: t
            for k in keys
            if (t := realize(series, k, ws, hold_days, cost_bps.get(k, default_cost_bps), min_coverage)) is not None
        }
        nets = {k: t.net_apr for k, t in full.items()}
        picked = max(prev_nets, key=lambda k: prev_nets[k]) if prev_nets else None
        # 後から見た最良は OOS と同じコストの土台で選ぶ。
        # 「今持っているペアは乗り換えコストを払わない」を後見側にも適用しないと、
        # 持ち続けた窓で OOS > 後見最良 になって効率が 100% を超えてしまう。
        hind = {
            k: t.net_apr
            for k in full
            if (
                t := realize(
                    series, k, ws, hold_days, 0.0 if k == held else cost_bps.get(k, default_cost_bps), min_coverage
                )
            )
            is not None
        }
        best = max(hind, key=lambda k: hind[k]) if hind else None
        oos = oos_gross = oos_basis = None
        switched = False
        cov = None
        if picked is not None and picked in full:
            switched = picked != held
            charge = cost_bps.get(picked, default_cost_bps) if switched else 0.0
            t = realize(series, picked, ws, hold_days, charge, min_coverage)
            if t is not None:
                oos, oos_gross, oos_basis, cov = t.net_apr, t.gross_apr, t.basis_apr, t.coverage
        out.append(
            RealizedWindow(
                index=i + 1,
                start=ws,
                end=ws + span,
                hold_days=hold_days,
                picked=picked,
                picked_is_net_apr=prev_nets.get(picked) if picked else None,
                oos_net_apr=oos,
                oos_gross_apr=oos_gross,
                oos_basis_apr=oos_basis,
                switched=switched,
                best=best,
                best_net_apr=hind.get(best) if best else None,
                coverage=cov,
            )
        )
        if picked is not None and picked in full:
            held = picked
        prev_nets = nets
    return out


def capital_apr(leg_apr: float, leverage: float) -> float:
    """片脚の建玉に対する年率 -> 置いた資本に対する年率

    片脚 N ドルを leverage 倍で建てるので証拠金は N / leverage、2 会場で 2N / leverage。
    leverage = 1 (フル担保) なら資本利回りは片脚建玉基準の半分になる。
    """
    if leverage <= 0:
        raise ValueError("leverage must be > 0")
    return leg_apr * leverage / 2.0
