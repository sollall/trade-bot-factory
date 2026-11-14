"""在庫ヘッジの実コスト (依頼票 4)。Hyperliquid perp の板を成行で歩かせる。

買い指値が約定したら現物の在庫を抱えるので perp を **売る** = HL の bid を食う。
売り指値が約定したら在庫が減るので perp を **買う** = HL の ask を食う。

bitbank と HL は同じ銘柄なので、ヘッジの数量は現物の数量そのまま (JPY 建ての金額を
bitbank の約定値段で割った枚数)。スリッページは比なので為替は要らない。
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from tbf.jpmaker import declared
from tbf.jpmaker.sim import Fill


@dataclass(frozen=True)
class HlSample:
    time: int  # 取得時刻 (ms)
    bids: list[tuple[float, float]]  # (値段, 枚数) 上から
    asks: list[tuple[float, float]]

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2.0


def walk_levels(levels: list[tuple[float, float]], amount: float) -> tuple[float, float]:
    """成行で `amount` 枚を歩かせたときの (平均約定値段, 約定できた枚数)

    板の記録した深さで足りなければ、足りた分だけを返す (残りは「板が足りない」として扱う)。
    """
    filled = 0.0
    cost = 0.0
    for px, sz in levels:
        if filled >= amount:
            break
        take = min(sz, amount - filled)
        filled += take
        cost += take * px
    return (cost / filled if filled > 0 else 0.0), filled


def slippage_ratio(sample: HlSample, hedge_side: str, amount: float) -> float | None:
    """中値に対する平均約定値段のずれ (比, **正なら損**)。板が足りなければ None

    hedge_side: "sell" = bid を食う / "buy" = ask を食う
    """
    levels = sample.bids if hedge_side == "sell" else sample.asks
    mid = sample.mid
    avg, filled = walk_levels(levels, amount)
    if mid <= 0 or filled < amount - 1e-12:
        return None
    return (mid - avg) / mid if hedge_side == "sell" else (avg - mid) / mid


def _sample_at(samples: list[HlSample], times: list[int], target_ms: int, tolerance_sec: float) -> HlSample | None:
    if not samples:
        return None
    i = bisect_left(times, target_ms)
    best: tuple[float, HlSample] | None = None
    for j in (i - 1, i):
        if 0 <= j < len(samples):
            d = abs(samples[j].time - target_ms) / 1000.0
            if best is None or d < best[0]:
                best = (d, samples[j])
    if best is None or best[0] > tolerance_sec:
        return None
    return best[1]


def hedge_cost(
    fill: Fill,
    samples: list[HlSample],
    times: list[int],
    delay_ms: float,
    tolerance_sec: float | None = None,
) -> dict | None:
    """1 件の約定をヘッジしたときのコスト。測れなければ None (**推定で埋めない**)

    返り値 (すべて約定金額に対する比):
      slippage      板を歩いた分
      taker         HL のテイカー手数料 x 脚数 (宣言値)
      cost_ratio    純益の式に入れる合計 = slippage + taker
      drift         ヘッジが入るまでの時間差の間に HL の中値が動いた分 (**式には入れない**。
                    依頼票 4 の「時間差の間の値動き」として別に報告する)
      used_delay_ms 実際に使えた標本までの時間差
    """
    tol = tolerance_sec if tolerance_sec is not None else max(3.0, declared.HL_POLL_SEC * 2)
    at_fill = _sample_at(samples, times, fill.time, tol)
    at_hedge = _sample_at(samples, times, int(fill.time + delay_ms), tol)
    if at_fill is None or at_hedge is None:
        return None
    hedge_side = "sell" if fill.side == "buy" else "buy"
    amount = fill.notional_jpy / fill.price if fill.price > 0 else 0.0
    slip = slippage_ratio(at_hedge, hedge_side, amount)
    if slip is None:
        return None
    taker = declared.HL_TAKER_FEE * declared.HL_TAKER_LEGS
    sign = 1.0 if hedge_side == "sell" else -1.0
    drift = -sign * (at_hedge.mid - at_fill.mid) / at_fill.mid
    return {
        "slippage": slip,
        "taker": taker,
        "cost_ratio": slip + taker,
        "drift": drift,
        "used_delay_ms": float(at_hedge.time - fill.time),
    }
