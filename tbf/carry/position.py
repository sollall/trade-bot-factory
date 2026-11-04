"""A① の建玉モデル (純粋関数。HTTP も sqlite もここには入れない)

資本規約 — 依頼票の確定事項。変更しない:

    C = N + N/3  (現物ロング N + perp 証拠金 N/3)。N = $7,500, C = $10,000。
    **年率・シャープ・最大DD の分母は常に C。** 片脚名目 N で割った利回りは出さない。

「HL 現物残高が perp 証拠金に算入されるか」は未確定 (TRA-14 §8)。オーナー判断により
保守側 = 算入されない前提で進める。したがって perp の証拠金率は **perp 口座だけ**
(証拠金 N/3 + perp 含み損益 + 受取ファンディング) で評価し、現物側の含み益で救わない。
これが A① の実質的なリスクであり、(iii) で数えるのはその回数である。

損益の内訳 (依頼票のシャープ算出規約どおり):

    net = 受取ファンディング累計 − ベーシス変動の評価損益 − 往復コスト(建玉時に一括計上)

2 脚合計の評価損益は、現物と perp の価格差 (ベーシス) の変化だけで決まる:

    現物ロング   = S × (spot_t − spot_0)
    perp ショート = S × (perp_0 − perp_t)
    合計         = −S × [(perp_t − spot_t) − (perp_0 − spot_0)]
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from tbf.carry.hl import Depth

BPS = 10_000.0
DAYS_PER_YEAR = 365.0


# ---------------------------------------------------------------- 板を歩く


def avg_fill_px(levels: Sequence[tuple[float, float]], notional_usd: float) -> Optional[float]:
    """板を成行で食べて notional_usd ぶん約定したときの平均価格。板が足りなければ None

    「食べられるところまで食べた平均」を返すとスリッページを小さく見せてしまうので、
    足りない場合は評価しない (tbf/funding/spread.py と同じ方針)。
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


@dataclass(frozen=True)
class Fill:
    """板を歩いて出した片脚の約定見込み。**表示価格 (最良気配) は使わない**"""

    side: str  # "buy" / "sell"
    size_usd: float
    ref_px: float  # 基準価格 = 板の mid
    avg_px: Optional[float]
    slip_bps: Optional[float]
    fee_bps: float
    depth_usd: float  # 取得できた段数ぶんの合計金額
    levels_seen: int

    @property
    def ok(self) -> bool:
        return self.avg_px is not None and self.slip_bps is not None

    @property
    def cost_bps(self) -> Optional[float]:
        """手数料 + スリッページ。片脚片道の実効コスト"""
        return None if self.slip_bps is None else self.fee_bps + self.slip_bps


def walk(depth: Depth, size_usd: float, ref_px: float, side: str, fee_bps: float) -> Fill:
    avg = avg_fill_px(depth.levels, size_usd)
    slip = None
    if avg is not None and ref_px > 0:
        diff = (avg - ref_px) if side == "buy" else (ref_px - avg)
        slip = diff / ref_px * BPS
    return Fill(side, size_usd, ref_px, avg, slip, fee_bps, depth.notional_usd, len(depth.levels))


# ---------------------------------------------------------------- 建玉


@dataclass(frozen=True)
class Entry:
    """建玉の確定内容 (計測開始時に 1 回だけ決まり、以後変えない)"""

    time: int
    size: float  # 両脚共通の HYPE 枚数
    spot_px: float  # 現物の平均約定価格 (板を歩いた値)
    perp_px: float  # perp の平均約定価格 (板を歩いた値)
    spot_notional_usd: float
    perp_notional_usd: float
    margin_usd: float
    capital_usd: float
    spot_fee_bps: float
    perp_fee_bps: float
    spot_slip_bps: float
    perp_slip_bps: float
    spot_mid: float
    perp_mid: float

    @property
    def entry_cost_bps(self) -> float:
        """片脚名目に対する片道の実効コスト (両脚合計)"""
        return self.spot_fee_bps + self.spot_slip_bps + self.perp_fee_bps + self.perp_slip_bps

    @property
    def entry_cost_usd(self) -> float:
        return (self.spot_fee_bps + self.spot_slip_bps) / BPS * self.spot_notional_usd + (
            self.perp_fee_bps + self.perp_slip_bps
        ) / BPS * self.perp_notional_usd

    @property
    def basis_abs(self) -> float:
        """建玉時の価格差 (perp − 現物, USD)"""
        return self.perp_px - self.spot_px


@dataclass(frozen=True)
class Mark:
    """ある時刻の評価"""

    time: int
    spot_px: float
    perp_px: float
    basis_bps: float
    funding_cum_usd: float
    spot_pnl_usd: float
    perp_pnl_usd: float
    mtm_usd: float  # 2 脚合計の評価損益 (= −S × ベーシス変動)
    entry_cost_usd: float
    net_pnl_usd: float
    perp_equity_usd: float
    perp_notional_usd: float
    margin_ratio: float
    margin_warn: bool
    liquidated: bool


def basis_bps(perp_px: float, spot_px: float) -> float:
    """(perp − 現物) / 現物 の bps。現物を基準にする"""
    if spot_px <= 0:
        raise ValueError("spot_px must be > 0")
    return (perp_px - spot_px) / spot_px * BPS


def mark(
    entry: Entry,
    time: int,
    spot_px: float,
    perp_px: float,
    funding_cum_usd: float,
    maintenance_frac: float,
    warn_frac: float,
) -> Mark:
    s = entry.size
    spot_pnl = s * (spot_px - entry.spot_px)
    perp_pnl = s * (entry.perp_px - perp_px)
    mtm = spot_pnl + perp_pnl
    cost = entry.entry_cost_usd
    # perp 口座の自己資本: 現物側の含み益は入れない (保守側 = 1.33N 規約)。
    # 手数料は建玉時に差し引かれるので、perp 脚ぶんの手数料・スリッページを証拠金から引く。
    perp_entry_cost = (entry.perp_fee_bps + entry.perp_slip_bps) / BPS * entry.perp_notional_usd
    perp_equity = entry.margin_usd + perp_pnl + funding_cum_usd - perp_entry_cost
    perp_notional = s * perp_px
    ratio = perp_equity / perp_notional if perp_notional > 0 else 0.0
    return Mark(
        time=time,
        spot_px=spot_px,
        perp_px=perp_px,
        basis_bps=basis_bps(perp_px, spot_px),
        funding_cum_usd=funding_cum_usd,
        spot_pnl_usd=spot_pnl,
        perp_pnl_usd=perp_pnl,
        mtm_usd=mtm,
        entry_cost_usd=cost,
        net_pnl_usd=funding_cum_usd + mtm - cost,
        perp_equity_usd=perp_equity,
        perp_notional_usd=perp_notional,
        margin_ratio=ratio,
        margin_warn=ratio < warn_frac,
        liquidated=ratio <= maintenance_frac,
    )


def funding_amount_usd(rate: float, size: float, mark_px: float) -> float:
    """perp ショートが 1 時間で受け取る額。HL は建玉の時価名目に対して付く

    rate > 0 ならロング -> ショート方向に支払われるので、ショートの受取は正。
    """
    return rate * size * mark_px


# ---------------------------------------------------------------- 指標


@dataclass(frozen=True)
class Stats:
    n: int
    mean: float
    std: float
    sharpe: float
    # σ の推定誤差 (標本標準偏差の標準誤差 ≈ σ/√(2(n−1)))
    std_se: float
    # 年率シャープの標準誤差 (Lo 2002: SE(SR) ≈ √((1 + SR_d²/2)/n) を年率化)
    sharpe_se: float

    @property
    def sharpe_low(self) -> float:
        return self.sharpe - self.sharpe_se

    @property
    def sharpe_high(self) -> float:
        return self.sharpe + self.sharpe_se


def stats(returns: Sequence[float], periods_per_year: float = DAYS_PER_YEAR) -> Optional[Stats]:
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return Stats(n, mean, 0.0, float("inf") if mean > 0 else 0.0, 0.0, float("nan"))
    sr_period = mean / std
    sr_ann = sr_period * math.sqrt(periods_per_year)
    std_se = std / math.sqrt(2 * (n - 1))
    sr_se = math.sqrt((1 + sr_period**2 / 2) / n) * math.sqrt(periods_per_year)
    return Stats(n, mean, std, sr_ann, std_se, sr_se)


def max_drawdown(cum: Sequence[float]) -> float:
    """累積損益系列 (USD) の最大ドローダウン (USD, 正の値)"""
    peak = 0.0
    worst = 0.0
    for v in cum:
        peak = max(peak, v)
        worst = max(worst, peak - v)
    return worst


def annualized(
    net_pnl_usd: float, capital_usd: float, elapsed_days: float, min_days: float = 1.0
) -> Optional[float]:
    """実現 net 年率。分母は常に C

    min_days 未満の経過で年率化すると建玉コストだけが 365 倍されて無意味な数字になるので、
    その場合は None (= 未測定) を返す。
    """
    if elapsed_days < min_days or capital_usd <= 0:
        return None
    return net_pnl_usd / capital_usd * DAYS_PER_YEAR / elapsed_days
