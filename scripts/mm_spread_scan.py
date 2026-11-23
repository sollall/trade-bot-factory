"""
Hyperliquid の無期限先物の全銘柄について、「売値と買値の差」がメイカー手数料に勝っているかを一覧にする。

  python scripts/mm_spread_scan.py [--rounds 3] [--min-volume 1000000]

最良の買い・売りに並んで往復したとき、取れるのは差の全部、払うのはメイカー手数料 × 2。
「余り = 差 - 手数料 × 2」がプラスの銘柄だけが、速い MM の候補になりうる。
板の瞬間値を数回取って中央値を見るだけの当たりづけで、逆行 (刺さった後に不利に動くぶん) は入っていない。
発注はしない。公開マーケットデータの read-only 照会のみ。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tbf.http import UrllibHttp
from tbf.market.hyperliquid import _post


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3, help="板を取る回数 (中央値を使う)")
    ap.add_argument("--pause", type=float, default=20.0, help="回と回の間に待つ秒数")
    ap.add_argument("--min-volume", type=float, default=1_000_000, help="24 時間の売買代金の下限 (USD)")
    ap.add_argument("--maker-bps", type=float, default=1.5, help="メイカー手数料 (bps)。基本ティアの想定値")
    ap.add_argument("--out", default="reports/mm_study/spread_scan.json")
    a = ap.parse_args(argv)

    http = UrllibHttp()
    meta, ctxs = await _post(http, {"type": "metaAndAssetCtxs"})
    coins: dict[str, float] = {}
    for u, c in zip(meta["universe"], ctxs):
        if u.get("isDelisted"):
            continue
        vol = float(c.get("dayNtlVlm") or 0)
        if vol >= a.min_volume:
            coins[u["name"]] = vol
    print(f"対象 {len(coins)} 銘柄 (24h 売買代金 {a.min_volume:,.0f} USD 以上)、{a.rounds} 回取得")

    spreads: dict[str, list[float]] = {c: [] for c in coins}
    depth: dict[str, list[float]] = {c: [] for c in coins}
    for r in range(a.rounds):
        if r:
            await asyncio.sleep(a.pause)
        for coin in coins:
            book = await _post(http, {"type": "l2Book", "coin": coin})
            bids, asks = book["levels"]
            if not bids or not asks:
                continue
            bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
            mid = (bid + ask) / 2
            spreads[coin].append((ask - bid) / mid * 10_000)
            # 最良気配に並んでいる金額 (買いと売りの小さい方)
            depth[coin].append(min(float(bids[0]["sz"]) * bid, float(asks[0]["sz"]) * ask))
            await asyncio.sleep(0.12)

    rows: list[dict[str, Any]] = []
    for coin, vol in coins.items():
        if not spreads[coin]:
            continue
        s = statistics.median(spreads[coin])
        rows.append(
            {
                "coin": coin,
                "spread_bps": round(s, 2),
                "edge_bps": round(s - 2 * a.maker_bps, 2),
                "volume_usd": round(vol),
                "top_depth_usd": round(statistics.median(depth[coin])),
            }
        )
    rows.sort(key=lambda r: -r["edge_bps"])
    plus = [r for r in rows if r["edge_bps"] > 0]
    print(f"余りがプラス: {len(plus)}/{len(rows)} 銘柄 (手数料 往復 {2 * a.maker_bps}bps)")
    print("銘柄        差bps   余りbps   24h売買代金(百万USD)  最良気配の厚み(USD)")
    for r in rows:
        print(
            f"{r['coin']:10s} {r['spread_bps']:6.2f} {r['edge_bps']:8.2f} "
            f"{r['volume_usd'] / 1e6:14.1f} {r['top_depth_usd']:18,d}"
        )
    path = Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"maker_bps": a.maker_bps, "rounds": a.rounds, "rows": rows}, indent=1))
    print(f"\n保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
