"""記録 -> 1 件ずつの損益 -> 窓ごとの年率。

**全期間 1 回通した値は主指標にしない** (リーダー指示)。主指標はウォークフォワードの
窓ごとの行で、各窓の OOS の値と「後から見た最良設定」に対する比 (効率) を併記する。

純益の式は依頼票 §合否の線 1 のまま:
  日次純益 = Σ(約定金額 x (取れた半スプレッド + 0.02%)) - 逆選択損 - ヘッジコスト(スリッページ + 0.045%x2)
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field

from tbf.jpmaker import declared, hedge as hedge_mod
from tbf.jpmaker.sim import BookSample, Fill, Trade, adverse_loss_jpy, simulate
from tbf.jpmaker.store import JpMakerStore

DAY_MS = 86_400_000
YEAR_SEC = 365 * 24 * 3600


@dataclass
class PairData:
    pair: str
    samples: list[BookSample] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    hl: list[hedge_mod.HlSample] = field(default_factory=list)

    @property
    def times(self) -> list[int]:
        return [s.time for s in self.samples]

    @property
    def hl_times(self) -> list[int]:
        return [s.time for s in self.hl]


def load(store: JpMakerStore, pairs: dict[str, str], since: int, until: int) -> dict[str, PairData]:
    out: dict[str, PairData] = {}
    for pair, coin in pairs.items():
        d = PairData(pair=pair)
        for r in store.query(
            "SELECT time, bid, bid_size, ask, ask_size FROM book WHERE pair=? AND time>=? AND time<? ORDER BY time",
            (pair, since, until),
        ):
            d.samples.append(BookSample(r["time"], r["bid"], r["bid_size"], r["ask"], r["ask_size"]))
        for r in store.query(
            "SELECT time, side, price, amount FROM trades WHERE pair=? AND time>=? AND time<? ORDER BY time",
            (pair, since, until),
        ):
            d.trades.append(Trade(r["time"], r["side"], r["price"], r["amount"]))
        for r in store.query(
            "SELECT time, bids_json, asks_json FROM hl_book WHERE coin=? AND time>=? AND time<? ORDER BY time",
            (coin, since, until),
        ):
            bids = [(float(p), float(s)) for p, s in json.loads(r["bids_json"])]
            asks = [(float(p), float(s)) for p, s in json.loads(r["asks_json"])]
            if bids and asks:
                d.hl.append(hedge_mod.HlSample(r["time"], bids, asks))
        out[pair] = d
    return out


@dataclass
class FillEcon:
    """約定 1 件の内訳。測れなかった項目は None のまま残す (ゼロ埋めしない)"""

    fill: Fill
    half_spread: float
    adverse: dict[int, float | None]
    hedge: dict | None

    @property
    def gross_jpy(self) -> float:
        return self.fill.notional_jpy * (self.half_spread + declared.BITBANK_MAKER_REBATE)

    @property
    def adverse_jpy(self) -> float | None:
        return self.adverse.get(declared.ADVERSE_HORIZON_FOR_NET_SEC)

    @property
    def hedge_jpy(self) -> float | None:
        return self.fill.notional_jpy * self.hedge["cost_ratio"] if self.hedge else None

    @property
    def measured(self) -> bool:
        """純益を出せる約定か。1 項目でも未測定なら式に入れない (件数は報告する)"""
        return self.adverse_jpy is not None and self.hedge_jpy is not None

    @property
    def net_jpy(self) -> float | None:
        if not self.measured:
            return None
        return self.gross_jpy - self.adverse_jpy - self.hedge_jpy


def economics(data: PairData, fills: list[Fill], delay_ms: float) -> list[FillEcon]:
    times, hl_times = data.times, data.hl_times
    out = []
    for f in fills:
        adv = {h: adverse_loss_jpy(f, data.samples, times, h) for h in declared.ADVERSE_HORIZONS_SEC}
        out.append(
            FillEcon(
                fill=f,
                half_spread=f.half_spread_ratio,
                adverse=adv,
                hedge=hedge_mod.hedge_cost(f, data.hl, hl_times, delay_ms),
            )
        )
    return out


def window_bounds(since: int, until: int, days: float) -> list[tuple[int, int]]:
    """UTC 0 時の格子で窓を切る。端の窓は短くなる (実経過時間で年率化する)"""
    step = int(days * DAY_MS)
    first = (since // step) * step
    out = []
    t = first
    while t < until:
        ws, we = max(t, since), min(t + step, until)
        if we > ws:
            out.append((ws, we))
        t += step
    return out


def coverage(times: list[int], ws: int, we: int, poll_sec: float) -> tuple[float, int]:
    """窓の実測カバレッジ (0..1) と、その窓の標本数"""
    n = sum(1 for t in times if ws <= t < we)
    expected = (we - ws) / 1000.0 / poll_sec
    return (min(1.0, n / expected) if expected > 0 else 0.0), n


@dataclass
class Cell:
    """ある窓・ある設定 (銘柄 x サイズ x 推定) の集計"""

    scope: str  # 銘柄名、または "ALL" (宣言グリッドの 5 銘柄合計)
    size_jpy: float
    mode: str
    n_fills: int = 0
    n_measured: int = 0
    filled_jpy: float = 0.0
    taker_jpy: float = 0.0
    gross_jpy: float = 0.0
    adverse_sum_jpy: float = 0.0
    # 約定 1 件ごとの逆選択損の「約定金額に対する比」。中央値の基準を出すために持つ。
    adverse_ratios: list[float] = field(default_factory=list)
    # "sum" = 依頼票の式どおり 1 件ずつの合計 (判定に使う)。"median" = 1 件あたりの
    # 中央値 x 約定金額で置き換えた感度。declared.ADVERSE_BASIS_FOR_NET が既定。
    adverse_basis: str = declared.ADVERSE_BASIS_FOR_NET
    hedge_jpy: float = 0.0
    measured_sec: float = 0.0
    n_pairs: int = 1

    @property
    def adverse_median_ratio(self) -> float | None:
        if not self.adverse_ratios:
            return None
        return statistics.median(self.adverse_ratios)

    @property
    def adverse_jpy(self) -> float:
        if self.adverse_basis == "median":
            r = self.adverse_median_ratio
            return 0.0 if r is None else r * self.filled_jpy
        if self.adverse_basis != "sum":
            raise ValueError(f"unknown adverse basis: {self.adverse_basis}")
        return self.adverse_sum_jpy

    @property
    def net_jpy(self) -> float:
        return self.gross_jpy - self.adverse_jpy - self.hedge_jpy

    @property
    def capital_jpy(self) -> float:
        return declared.capital_jpy(self.size_jpy, self.n_pairs)

    @property
    def apy(self) -> float | None:
        if self.measured_sec <= 0 or self.capital_jpy <= 0:
            return None
        return self.net_jpy / self.capital_jpy * (YEAR_SEC / self.measured_sec)

    @property
    def fill_ratio(self) -> float | None:
        """自分の値段に届いたテイカー注文のうち、取れた割合"""
        return self.filled_jpy / self.taker_jpy if self.taker_jpy > 0 else None


def _collect(cell: Cell, econs: list[FillEcon], ws: int, we: int) -> None:
    for e in econs:
        if not (ws <= e.fill.time < we):
            continue
        cell.n_fills += 1
        if not e.measured:
            continue
        cell.n_measured += 1
        cell.filled_jpy += e.fill.notional_jpy
        cell.gross_jpy += e.gross_jpy
        cell.adverse_sum_jpy += e.adverse_jpy or 0.0
        if e.fill.notional_jpy > 0 and e.adverse_jpy is not None:
            cell.adverse_ratios.append(e.adverse_jpy / e.fill.notional_jpy)
        cell.hedge_jpy += e.hedge_jpy or 0.0


@dataclass
class Measurement:
    """1 回の計測の全部。レポートはここから作る"""

    since: int
    until: int
    poll_sec: float
    delay_ms: float
    # (pair, side, size, mode) -> SimResult
    sims: dict[tuple, object] = field(default_factory=dict)
    # (pair, side, size, mode) -> list[FillEcon]
    econs: dict[tuple, list[FillEcon]] = field(default_factory=dict)
    data: dict[str, PairData] = field(default_factory=dict)


def run_all(data: dict[str, PairData], since: int, until: int, poll_sec: float, delay_ms: float) -> Measurement:
    m = Measurement(since=since, until=until, poll_sec=poll_sec, delay_ms=delay_ms, data=data)
    for pair, d in data.items():
        for side in ("buy", "sell"):
            for size in declared.SIZES_JPY:
                for mode in declared.FILL_MODES:
                    key = (pair, side, size, mode)
                    res = simulate(pair, d.samples, d.trades, side, size, mode)
                    m.sims[key] = res
                    m.econs[key] = economics(d, res.fills, delay_ms)
    return m


def cells_for_window(
    m: Measurement,
    ws: int,
    we: int,
    size: float,
    mode: str,
    adverse_basis: str = declared.ADVERSE_BASIS_FOR_NET,
) -> tuple[dict[str, Cell], Cell]:
    """その窓の銘柄ごとの集計と、宣言グリッド (5 銘柄) 合計

    `adverse_basis` は逆選択損を 1 件ずつの合計で見るか (="sum"、判定に使う基準)、
    1 件あたりの中央値で置き換えるか (="median"、感度) の切り替え。中央値はその窓の
    その集計単位に入った約定だけから取る (ALL 行は 5 銘柄の約定を混ぜた中央値)。
    """
    per_pair: dict[str, Cell] = {}
    for pair, d in m.data.items():
        cov, _ = coverage(d.times, ws, we, m.poll_sec)
        c = Cell(scope=pair, size_jpy=size, mode=mode, n_pairs=1, adverse_basis=adverse_basis)
        c.measured_sec = (we - ws) / 1000.0 * cov
        for side in ("buy", "sell"):
            key = (pair, side, size, mode)
            _collect(c, m.econs[key], ws, we)
            res = m.sims[key]
            c.taker_jpy += getattr(res, "taker_notional_jpy", 0.0)
        per_pair[pair] = c
    total = Cell(scope="ALL", size_jpy=size, mode=mode, n_pairs=len(m.data), adverse_basis=adverse_basis)
    covs = []
    for pair, c in per_pair.items():
        total.n_fills += c.n_fills
        total.n_measured += c.n_measured
        total.filled_jpy += c.filled_jpy
        total.gross_jpy += c.gross_jpy
        total.adverse_sum_jpy += c.adverse_sum_jpy
        total.adverse_ratios.extend(c.adverse_ratios)
        total.hedge_jpy += c.hedge_jpy
        total.taker_jpy += c.taker_jpy
        covs.append(c.measured_sec / ((we - ws) / 1000.0) if we > ws else 0.0)
    total.measured_sec = (we - ws) / 1000.0 * (sum(covs) / len(covs) if covs else 0.0)
    return per_pair, total


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    i = min(len(v) - 1, max(0, int(math.ceil(q * len(v))) - 1))
    return v[i]
