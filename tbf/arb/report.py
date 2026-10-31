from __future__ import annotations

import statistics

from tbf.arb.store import ArbStore


def build_report(store: ArbStore, since: int, min_persist_polls: int) -> str:
    lines = []
    polls = store.query(
        "SELECT chain, COUNT(*) n, SUM(error IS NOT NULL) errors FROM polls WHERE time >= ? GROUP BY chain", (since,)
    )
    lines.append("== RPC")
    for p in polls:
        lat = [r[0] for r in store.query(
            "SELECT latency_ms FROM polls WHERE chain=? AND time>=? AND latency_ms IS NOT NULL ORDER BY latency_ms",
            (p["chain"], since),
        )]
        p50 = statistics.median(lat) if lat else 0
        p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) >= 20 else (lat[-1] if lat else 0)
        lines.append(f"{p['chain']:>10}: polls {p['n']}  errors {p['errors']}  latency p50 {p50:.0f}ms p95 {p95:.0f}ms")

    lines.append("\n== 価格差 (最小サイズ, 粗利 bps)")
    for s in store.query(
        """SELECT sell_chain, buy_chain, SUM(n) n, MIN(min_bps) lo, MAX(max_bps) hi, SUM(sum_bps)/SUM(n) avg
           FROM spreads WHERE minute >= ? GROUP BY sell_chain, buy_chain ORDER BY hi DESC""",
        (since,),
    ):
        lines.append(f"{s['sell_chain']:>10} -> {s['buy_chain']:<10} avg {s['avg']:7.2f}  min {s['lo']:7.2f}  max {s['hi']:7.2f}")

    lines.append(f"\n== 機会 (純利益が閾値以上だった連続区間)  ※ {min_persist_polls} ポーリング以上続いたものを「捕捉可能」とみなす")
    rows = store.query(
        """SELECT sell_chain, buy_chain, COUNT(*) n, SUM(polls >= ?) persist,
                  SUM(CASE WHEN polls >= ? THEN first_net_usd ELSE 0 END) capturable,
                  SUM(max_net_usd) upper, MAX(max_net_usd) best
           FROM episodes WHERE start >= ? GROUP BY sell_chain, buy_chain ORDER BY capturable DESC""",
        (min_persist_polls, min_persist_polls, since),
    )
    if not rows:
        lines.append("(まだありません)")
    total = 0.0
    for r in rows:
        total += r["capturable"]
        lines.append(
            f"{r['sell_chain']:>10} -> {r['buy_chain']:<10} 回数 {r['n']:>4}  捕捉可能 {r['persist']:>4}  "
            f"仮想利益 ${r['capturable']:.2f} (上限 ${r['upper']:.2f})  最大 ${r['best']:.2f}"
        )
    lines.append(f"\n捕捉可能な機会の仮想利益の合計: ${total:.2f}")
    return "\n".join(lines)
