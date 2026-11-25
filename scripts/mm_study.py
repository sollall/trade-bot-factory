"""
「ゆっくりした板型マーケットメイク」が手数料と逆行に勝てるかの下調べ。

Hyperliquid の無期限先物で、足の確定ごとに買いと売りの指値を 1 本ずつ出し直す前提で、
足データだけを使って粗く見積もる。発注はしない。公開マーケットデータの read-only 照会のみ。

  python scripts/mm_study.py [--coins BTC ETH] [--timeframes 1m 5m 15m]

模擬のルール (どれも「甘く見ない」側に倒してある):
  - 足 i の終値を仲値とみなし、幅 = k × 直近の 1 足ぶんの値動き (標準偏差) で両側に指値を置く
  - 持ち高が偏ったら、偏りを戻す向きに指値の中心をずらす (skew)。ただし仲値はまたがない
  - 足 i+1 の始値が既に指値を越えていたら、その指値は出せなかったものとする (post-only の拒否)
  - 足 i+1 の高値・安値が指値を「突き抜けた」ときだけ約定 (触れただけは約定にしない = 列の後ろ扱い)
  - 持ち高は ±max_inv 単位まで。上限の側の指値は出さない
  - 手数料はメイカー料率を毎約定に引く。ファンディングは入れていない

足だけでは分からないこと (板の列の順番、足の中の値動きの順序、発注の遅れ、429 での出し直し失敗) は
入っていない。ここで負ける設定は本番でも負けるが、ここで勝つ設定が本番で勝つとは限らない。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tbf.core.types import TIMEFRAME_MS, Candle
from tbf.http import UrllibHttp
from tbf.market.hyperliquid import fetch_candles

DAY = 86_400_000
# 幅の倍率 (直近の 1 足ぶんの標準偏差の何倍を片側の幅にするか)
KS = [0.25, 0.5, 1.0, 1.5, 2.0, 3.0]
# 持ち高 1 単位あたり、指値の中心を「片側の幅の何倍」ずらすか (上限 5 単位で 0.5 倍 / 1 倍ぶん)
SKEWS = [0.0, 0.1, 0.2]
# ずらした指値が仲値をまたがないよう、仲値から最低これだけ離す (bps)
MIN_OFFSET_BPS = 0.5
SIGMA_BARS = 60


def day(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


async def load(coin: str, tf: str, end: int, cache: Path) -> list[Candle]:
    """直近 5000 本 (API の上限)。data/ に 1 時間単位でキャッシュ"""
    path = cache / f"mm_hyperliquid_{coin}_{tf}_{end}.json"
    if path.exists():
        return [Candle(*c) for c in json.loads(path.read_text())]
    candles = await fetch_candles(UrllibHttp(), coin, tf, end - 5000 * TIMEFRAME_MS[tf], end)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([[c.time, c.open, c.high, c.low, c.close, c.volume] for c in candles]))
    return candles


def simulate(
    candles: list[Candle], k: float, skew: float, maker_rate: float, max_inv: int = 5, through_bps: float = 0.2
) -> dict[str, Any]:
    """1 回の注文 = 名目 1 ドル。損益は「持ち高の上限 (max_inv ドル)」に対する bps で返す"""
    rets = [math.log(candles[i].close / candles[i - 1].close) for i in range(1, len(candles))]
    through = through_bps / 10_000
    cash = pos = 0.0
    inv = 0  # 持ち高 (注文の単位数)
    fills = 0
    fee_total = 0.0
    half_sum = 0.0  # 約定した指値の、出した時点の仲値からの距離 (bps)
    markout_sum = 0.0  # 約定した足の終値で見た含み損益 (bps, マイナス = 逆行)
    rejected = quoted = 0
    equity: list[tuple[int, float]] = []

    for i in range(SIGMA_BARS, len(candles) - 1):
        window = rets[i - SIGMA_BARS : i]
        mean = sum(window) / len(window)
        sigma = math.sqrt(sum((r - mean) ** 2 for r in window) / (len(window) - 1))
        mid = candles[i].close
        nxt = candles[i + 1]
        h = k * sigma
        if h > 0:
            center = mid * (1 - skew * h * inv)
            floor = MIN_OFFSET_BPS / 10_000
            bid = min(center * (1 - h), mid * (1 - floor))
            ask = max(center * (1 + h), mid * (1 + floor))
            for side, px in ((1, bid), (-1, ask)):
                if (side == 1 and inv >= max_inv) or (side == -1 and inv <= -max_inv):
                    continue
                quoted += 1
                # 出す前に相場が指値を越えていたら post-only は弾かれる
                if (side == 1 and nxt.open <= px) or (side == -1 and nxt.open >= px):
                    rejected += 1
                    continue
                hit = nxt.low < px * (1 - through) if side == 1 else nxt.high > px * (1 + through)
                if not hit:
                    continue
                qty = 1.0 / px
                pos += side * qty
                cash -= side * 1.0
                cash -= maker_rate
                fee_total += maker_rate
                inv += side
                fills += 1
                half_sum += side * (mid - px) / mid * 10_000
                markout_sum += side * (nxt.close - px) / px * 10_000
        equity.append((nxt.time, cash + pos * nxt.close))

    days = (equity[-1][0] - equity[0][0]) / DAY
    # 日ごとの損益 (UTC の日付で区切る)
    by_day: dict[int, float] = {}
    prev = 0.0
    for t, e in equity:
        by_day[t // DAY] = by_day.get(t // DAY, 0.0) + (e - prev)
        prev = e
    daily = list(by_day.values())
    m = sum(daily) / len(daily)
    sd = math.sqrt(sum((d - m) ** 2 for d in daily) / (len(daily) - 1)) if len(daily) > 1 else 0.0
    peak = dd = 0.0
    for _, e in equity:
        peak = max(peak, e)
        dd = max(dd, peak - e)
    half_i = len(equity) // 2
    first = equity[half_i][1]
    final = equity[-1][1]
    to_bps = 10_000 / max_inv
    return {
        "k": k,
        "skew": skew,
        "days": round(days, 2),
        "fills": fills,
        "fills_per_day": round(fills / days, 1),
        "reject_rate": round(rejected / quoted, 3) if quoted else 0.0,
        "half_bps": round(half_sum / fills, 2) if fills else 0.0,
        "markout_bps": round(markout_sum / fills, 2) if fills else 0.0,
        "fee_bps": round(maker_rate * 10_000, 2),
        "net_per_fill_bps": round(final / fills * 10_000, 2) if fills else 0.0,
        "pnl_bps_per_day": round(final * to_bps / days, 1),
        "first_half_bps": round(first * to_bps, 1),
        "second_half_bps": round((final - first) * to_bps, 1),
        "max_dd_bps": round(dd * to_bps, 1),
        "t_stat": round(m / sd * math.sqrt(len(daily)), 2) if sd > 0 else 0.0,
    }


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", nargs="+", default=["BTC", "ETH"])
    ap.add_argument("--timeframes", nargs="+", default=["1m", "5m", "15m"], help="指値を出し直す間隔")
    ap.add_argument("--maker-bps", type=float, default=1.5, help="メイカー手数料 (bps)。Hyperliquid の基本ティアの想定値")
    ap.add_argument("--out", default="reports/mm_study")
    a = ap.parse_args(argv)

    end = int(time.time() * 1000) // TIMEFRAME_MS["1h"] * TIMEFRAME_MS["1h"]
    out: dict[str, Any] = {"end": day(end), "maker_bps": a.maker_bps, "runs": []}
    for coin in a.coins:
        for tf in a.timeframes:
            candles = await load(coin, tf, end, Path("data"))
            print(f"\n== {coin} {tf}  {day(candles[0].time)} 〜 {day(candles[-1].time)}  ({len(candles)} 本)")
            for fee_bps in (a.maker_bps, 0.0):
                rows = [simulate(candles, k, s, fee_bps / 10_000) for k in KS for s in SKEWS]
                wins = sum(1 for r in rows if r["pnl_bps_per_day"] > 0)
                both = sum(1 for r in rows if r["first_half_bps"] > 0 and r["second_half_bps"] > 0)
                print(f"-- 手数料 {fee_bps}bps: 黒字 {wins}/{len(rows)}、前半・後半とも黒字 {both}/{len(rows)}")
                print("   k  skew  約定/日  幅bps  逆行bps  1約定の純益bps  日次bps  前半  後半  最大DD  t値")
                for r in rows:
                    print(
                        f"{r['k']:5.2f} {r['skew']:4.1f} {r['fills_per_day']:8.1f} {r['half_bps']:6.2f} "
                        f"{r['markout_bps']:8.2f} {r['net_per_fill_bps']:12.2f} {r['pnl_bps_per_day']:10.1f} "
                        f"{r['first_half_bps']:7.1f} {r['second_half_bps']:7.1f} {r['max_dd_bps']:7.1f} {r['t_stat']:6.2f}"
                    )
                out["runs"].append({"coin": coin, "timeframe": tf, "fee_bps": fee_bps, "rows": rows})

    path = Path(a.out)
    path.mkdir(parents=True, exist_ok=True)
    (path / "result.json").write_text(json.dumps(out, indent=1))
    print(f"\n保存: {path / 'result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
