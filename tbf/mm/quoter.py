"""クォーター: 気配と在庫を見て、どこに指値を出すかを決める関数。

差し替えるときは同じ形の関数を書いて QUOTERS に足す:

    def my_quoter(book: Book, inventory: float, params: dict[str, float]) -> Quote

- book は判断の時点の気配。それより後のデータは渡されない (先読みできない)
- inventory は今の在庫 (枚数。売り越しは負)
- 返すのは「出したい」値段と数量。刻みへの丸め (買いは切り下げ、売りは切り上げ)、数量の丸め、
  相手の気配に当たる指値の取り下げ (ポストオンリー) は再生側 (replay.py) がやる
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from tbf.mm.book import Book


@dataclass(frozen=True)
class Quote:
    """bid / ask が None なら、その側は出さない。数量は枚数"""

    bid: Optional[float] = None
    bid_size: float = 0.0
    ask: Optional[float] = None
    ask_size: float = 0.0


Quoter = Callable[[Book, float, dict[str, float]], Quote]

#: 基準クォーターの既定値。--param k=v で上書きする。採用の判断に使った値ではなく、出発点
BASELINE_PARAMS: dict[str, float] = {
    "half_spread_bps": 3.0,  # 基準の値段から片側に離す幅
    "size_usd": 100.0,  # 片側 1 本の大きさ
    "max_inventory_usd": 500.0,  # 在庫がこれ以上になったら、増やす側は出さない
    "skew_bps": 3.0,  # 在庫が上限のとき、両方の値段を在庫を減らす向きにずらす幅 (在庫に比例)
}


def baseline(book: Book, inventory: float, params: dict[str, float]) -> Quote:
    """基準: ミッド ± half_spread_bps。買い越しなら両方の値段を下げ (買いにくく売りやすく)、
    売り越しなら上げる。在庫が上限に達した側は出さない"""
    mid = book.mid
    inv_usd = inventory * mid
    cap = params["max_inventory_usd"]
    center = mid * (1 - params["skew_bps"] / 10_000 * (inv_usd / cap if cap > 0 else 0.0))
    half = params["half_spread_bps"] / 10_000
    size = params["size_usd"] / mid
    return Quote(
        bid=center * (1 - half) if inv_usd < cap else None,
        bid_size=size,
        ask=center * (1 + half) if inv_usd > -cap else None,
        ask_size=size,
    )


QUOTERS: dict[str, tuple[Quoter, dict[str, float]]] = {"baseline": (baseline, BASELINE_PARAMS)}


def parse_params(defaults: dict[str, float], overrides: list[str]) -> dict[str, float]:
    """["half_spread_bps=4", ...] を既定値に重ねる。知らない名前は打ち間違いなので止める"""
    out = dict(defaults)
    for item in overrides:
        key, sep, value = item.partition("=")
        if not sep or key not in defaults:
            raise ValueError(f"--param {item!r}: k=v の形で、k は {', '.join(defaults)} のどれか")
        out[key] = float(value)
    return out
