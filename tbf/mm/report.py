"""記録した市場の様子の集計 (銘柄ごと)"""

from __future__ import annotations

from tbf.mm.replay import DEFAULT_MAKER_RATE, MAX_BOOK_GAP_MS
from tbf.mm.store import MmStore


def quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * q))]


def build_report(store: MmStore, since: int = 0, maker_rate: float = DEFAULT_MAKER_RATE) -> str:
    maker_bps = maker_rate * 10_000
    lines = []
    for coin in store.coins():
        books = store.books(coin, since)
        if len(books) < 2:
            continue
        start, end = books[0].time, books[-1].time
        # 記録が途切れていた時間は「1 時間あたり」の分母に入れない
        steps = [b.time - a.time for a, b in zip(books, books[1:])]
        hours = sum(s for s in steps if s <= MAX_BOOK_GAP_MS) / 3_600_000
        covered = [(a.time, b.time) for a, b in zip(books, books[1:]) if b.time - a.time <= MAX_BOOK_GAP_MS]
        trades = [t for t in store.trades(coin, start) if t.time <= end]
        # 約定は時刻順、covered も時刻順なので 1 回なめれば足りる
        seen, i = [], 0
        for t in trades:
            while i < len(covered) and covered[i][1] < t.time:
                i += 1
            if i < len(covered) and covered[i][0] < t.time:
                seen.append(t)
        spreads = sorted(b.spread_bps for b in books)
        bid_usd = sorted(b.bids[0][1] * b.best_bid for b in books)
        ask_usd = sorted(b.asks[0][1] * b.best_ask for b in books)
        notional = sum(t.px * t.sz for t in seen)
        buys = sum(t.px * t.sz for t in seen if t.side == "B")
        med = quantile(spreads, 0.5)
        lines.append(f"== {coin}  記録 {hours:.2f} 時間  気配 {len(books)} 件 (間隔の中央値 {quantile(sorted(steps), 0.5) / 1000:.1f} 秒)")
        lines.append(
            f"  スプレッド (bps)   中央値 {med:6.2f}  p25 {quantile(spreads, 0.25):6.2f}  p75 {quantile(spreads, 0.75):6.2f}"
        )
        lines.append(
            f"  片側スプレッド − メイカー手数料 ({maker_bps:+.2f} bps) = {med / 2 - maker_bps:+.2f} bps  "
            "※ 最良気配に並んで約定し、その後ミッドが動かない場合の上限。逆選択は replay で見る"
        )
        lines.append(
            f"  最良気配の厚さ     買 中央値 ${quantile(bid_usd, 0.5):,.0f} (p25 ${quantile(bid_usd, 0.25):,.0f})  "
            f"売 中央値 ${quantile(ask_usd, 0.5):,.0f} (p25 ${quantile(ask_usd, 0.25):,.0f})"
        )
        if hours > 0:
            lines.append(
                f"  約定               {len(seen) / hours:,.0f} 件/時  ${notional / hours:,.0f}/時  "
                f"(記録した {len(seen)} 件、買い手主導が代金の {buys / notional * 100 if notional else 0:.0f}%)"
            )
        gaps = store.gap_count(coin, start, end)
        tp = store.query(
            "SELECT COUNT(*) FROM polls WHERE coin = ? AND kind = 'recentTrades' AND error IS NULL AND time >= ?", (coin, since)
        )[0][0]
        lines.append(
            f"  約定の取りこぼしの可能性  {gaps} 回 / 約定のポーリング {tp} 回  "
            "※ 抜けた件数は分からない。上の件数と代金は下限"
        )
        for kind, label in (("l2Book", "気配"), ("recentTrades", "約定")):
            p = store.query(
                "SELECT COUNT(*) n, COALESCE(SUM(error IS NOT NULL), 0) errors FROM polls WHERE coin = ? AND kind = ? AND time >= ?",
                (coin, kind, since),
            )[0]
            lat = [r[0] for r in store.query(
                "SELECT latency_ms FROM polls WHERE coin = ? AND kind = ? AND time >= ? AND latency_ms IS NOT NULL ORDER BY latency_ms",
                (coin, kind, since),
            )]
            lines.append(
                f"  {label}のポーリング   {p['n']} 回  エラー {p['errors']}  "
                f"遅延 p50 {quantile(lat, 0.5):.0f}ms p95 {quantile(lat, 0.95):.0f}ms"
            )
        lines.append("")
    return "\n".join(lines).rstrip() or "(まだ記録がありません)"
