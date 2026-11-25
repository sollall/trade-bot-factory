"""再生の約定を、後から割って読むための集計 (日ごと・銘柄ごとの表、取り分の標準誤差)。

ここは数えるだけで、約定の判定はしない (判定は tbf/mm/replay.py の Sim だけ)。

標準誤差の出し方
  取り分 (bps) は代金で加重した平均 = Σ(取り分の金額) / Σ(約定代金)。約定を時間の塊 (既定 5 分) に分け、
  塊 j の金額 N_j と代金 D_j から、比 R = ΣN/ΣD の標準誤差を sqrt(G/(G-1) · Σ(N_j − R·D_j)²) / ΣD で出す
  (G は約定のあった塊の数)。同じ値動きの中で続けて付いた約定は互いに独立でないので、1 件ずつではなく塊で数える。
  塊が 2 個に満たなければ出さない (? と表示)。塊が少ないうちは、この ± 自体が当てにならない。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from tbf.mm.replay import BUY, Replayed, Result

DEFAULT_BLOCK_MIN = 5.0


@dataclass(frozen=True)
class Sample:
    """約定 1 件ぶん。金額は建値の通貨"""

    time: int
    side: int
    size: float
    notional: float
    edge: float  # 約定した瞬間の取り分
    marks: tuple[Optional[float], ...]  # horizons の順。N 秒後のミッドで測った取り分。測れなければ None


def samples(rep: Replayed, horizons: tuple[int, ...]) -> list[Sample]:
    return [
        Sample(
            f.time, f.side, f.size, f.px * f.size, f.side * (f.mid - f.px) * f.size,
            tuple(None if rep.marks[h][i] is None else f.side * (rep.marks[h][i] - f.px) * f.size for h in horizons),
        )
        for i, f in enumerate(rep.res.fills)
    ]


def ratio(items: Iterable[tuple[int, float, float]], block_ms: int) -> tuple[float, int, Optional[float], int]:
    """(時刻, 取り分の金額, 約定代金) の並びから、(代金加重の取り分 bps, 件数, その標準誤差 bps, 塊の数)。
    標準誤差は、塊が 2 個に満たなければ None。出し方はモジュールの docstring"""
    num = den = 0.0
    n = 0
    blocks: dict[int, list[float]] = {}
    for t, a, b in items:
        num += a
        den += b
        n += 1
        blk = blocks.setdefault(t // block_ms, [0.0, 0.0])
        blk[0] += a
        blk[1] += b
    if not den:
        return math.nan, 0, None, 0
    r, g = num / den, len(blocks)
    se = math.sqrt(g / (g - 1) * sum((a - r * b) ** 2 for a, b in blocks.values())) / den * 10_000 if g >= 2 else None
    return r * 10_000, n, se, g


def edge(ss: list[Sample], block_ms: int):
    return ratio(((s.time, s.edge, s.notional) for s in ss), block_ms)


def markout(ss: list[Sample], k: int, block_ms: int):
    """horizons の k 番目の秒数で測った取り分"""
    return ratio(((s.time, s.marks[k], s.notional) for s in ss if s.marks[k] is not None), block_ms)


def errors(ss: list[Sample], horizons: tuple[int, ...], block_ms: int) -> tuple[dict[int, Optional[float]], str]:
    """format_result に渡す形: (N 秒 -> 標準誤差, 添える説明)"""
    out = {h: markout(ss, k, block_ms)[2] for k, h in enumerate(horizons)}
    g = len({s.time // block_ms for s in ss})
    return out, f"± は標準誤差 ({block_ms / 60_000:g} 分ごとの塊のばらつきから。約定のあった塊 {g} 個。出し方は docs/mm.md)"


def _cell(stat: tuple[float, int, Optional[float], int]) -> str:
    bps, n, se, _ = stat
    if not n:
        return f"{'-':>11}"
    return f"{bps:+6.2f}±" + (f"{se:<4.2f}" if se is not None else "?   ")


def _heads(horizons: tuple[int, ...]) -> str:
    return "".join(f"  {str(h) + '秒後 ±':>9}" for h in horizons)


def tz_name(tz_hours: float) -> str:
    return {0: "UTC", 9: "JST"}.get(tz_hours, f"UTC{tz_hours:+g}")


def format_buckets(
    ss: list[Sample], horizons: tuple[int, ...], *, first: int, last: int, width_ms: int, tz_hours: float = 0,
    block_ms: int, sym: str, inventory: bool = True,
) -> str:
    """約定を width_ms ごと (既定は UTC の区切り。tz_hours=9 で JST) に割った表。first〜last は再生した区間で、
    約定の無かった区切りも行に出す。在庫は通し (その区切りの終わりに持っていた枚数)。
    inventory=False は銘柄をまたいだ合計の表 (枚数は足せないので在庫を出さない)"""
    shift = int(tz_hours * 3_600_000)
    day = width_ms == 86_400_000
    name = ("日ごと" if day else f"{width_ms / 3_600_000:g} 時間ごと") + f" ({tz_name(tz_hours)})"
    lines = [
        f"{name:<20} 約定    買/売         代金    瞬間{_heads(horizons)}" + ("   在庫 (終わり)" if inventory else "")
    ]
    by: dict[int, list[Sample]] = {}
    for s in ss:
        by.setdefault((s.time + shift) // width_ms, []).append(s)

    def row(label: str, part: list[Sample], pos: Optional[float]) -> str:
        buys = sum(1 for s in part if s.side == BUY)
        usd = sum(s.notional for s in part)
        e = edge(part, block_ms)
        text = (
            f"  {label:<18} {len(part):>5} {buys:>4}/{len(part) - buys:<4} {sym}{usd:>11,.0f}  "
            + (f"{e[0]:+6.2f}" if part else f"{'-':>6}")
            + "".join("  " + _cell(markout(part, k, block_ms)) for k in range(len(horizons)))
        )
        return text + (f"   {pos:+g}" if pos is not None else "")

    pos = 0.0
    for b in range((first + shift) // width_ms, (last + shift) // width_ms + 1):
        part = by.get(b, [])
        pos = round(pos + sum(s.side * s.size for s in part), 9)
        label = time.strftime("%Y-%m-%d" if day else "%Y-%m-%d %H:%M", time.gmtime(b * width_ms / 1000))
        lines.append(row(label, part, pos if inventory else None))
    lines.append(row("通し", ss, pos if inventory else None))
    lines.append(
        f"  ※ 取り分は bps (代金で加重、手数料を引く前)。± は標準誤差 ({block_ms / 60_000:g} 分ごとの塊のばらつきから。"
        "? は塊が 2 個に満たない)。"
        + ("在庫は通しで、その区切りの終わりの枚数" if inventory else "銘柄をまたいだ合計なので在庫は出さない")
    )
    return "\n".join(lines)


def format_summary(
    rows: list[tuple[str, Result, list[Sample]]], horizons: tuple[int, ...], *, block_ms: int, sym: str, quote: str,
) -> str:
    """銘柄ごとの 1 行と合計。合計の取り分は全銘柄の約定を合わせて代金で加重したもの (± の塊も銘柄をまたいで時刻で分ける)"""
    lines = [
        f"{'銘柄':<10} 約定 (a)/(b)/(c)          代金     取り分     手数料       在庫       合計    瞬間{_heads(horizons)}   最後の在庫"
    ]
    total = [0.0] * 6
    everything: list[Sample] = []
    for coin, res, ss in rows:
        k = [sum(1 for f in res.fills if f.kind == kind) for kind in ("through", "queue", "cross")]
        last_value = res.position * res.last_mid
        lines.append(
            f"{coin:<10} {len(res.fills):>5} {f'{k[0]}/{k[1]}/{k[2]}':<12} {sym}{res.volume_usd:>11,.0f} {res.spread_pnl:+10.3f} "
            f"{res.fee_pnl + 0.0:+10.3f} {res.inventory_pnl:+10.3f} {res.total_pnl:+10.3f}  "
            + (f"{res.edge_bps:+6.2f}" if res.fills else f"{'-':>6}")
            + "".join("  " + _cell(markout(ss, i, block_ms)) for i in range(len(horizons)))
            + f"   {sym}{last_value:+,.0f}"
        )
        for i, v in enumerate((len(res.fills), res.volume_usd, res.spread_pnl, res.fee_pnl, res.inventory_pnl, res.total_pnl)):
            total[i] += v
        everything += ss
    e = edge(everything, block_ms)
    lines.append(
        f"{'合計':<10} {total[0]:>5.0f} {'':<12} {sym}{total[1]:>11,.0f} {total[2]:+10.3f} {total[3] + 0.0:+10.3f} "
        f"{total[4]:+10.3f} {total[5]:+10.3f}  "
        + (f"{e[0]:+6.2f}" if everything else f"{'-':>6}")
        + "".join("  " + _cell(markout(everything, i, block_ms)) for i in range(len(horizons)))
    )
    lines.append(
        f"  ※ 金額は {quote}。ファンディングと、残った在庫を閉じる費用は未計上。取り分は bps (代金で加重、手数料を引く前)。"
        f"± は標準誤差 ({block_ms / 60_000:g} 分ごとの塊のばらつきから。? は塊が 2 個に満たない)"
    )
    return "\n".join(lines)
