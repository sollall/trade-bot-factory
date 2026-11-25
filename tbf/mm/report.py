"""記録した市場の様子の集計 (銘柄ごと)"""

from __future__ import annotations

from array import array
from typing import Optional

from tbf.mm.book import trade_order
from tbf.mm.replay import DEFAULT_MAKER_RATE, MAX_BOOK_GAP_MS
from tbf.mm.store import FOREVER, MmStore


def quantile(sorted_values, q: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * q))]


def _sorted(values: array) -> array:
    """並べ替えた写し。同じ詰まった形で持つ (100 万件でも 8MB。Python のリストのままだと数倍になる)"""
    return array(values.typecode, sorted(values))


def build_report(store: MmStore, since: int = 0, maker_rate: Optional[float] = None, until: int = FOREVER) -> str:
    """maker_rate を省くと、銘柄ごとに会場の値 (記録を始めたときに API から取ったもの) を使う。
    since〜until (どちらも含む) の気配と、その間の約定だけを数える。
    気配は最良気配だけを 1 件ずつ読み、数字の列として持つ (板の全段をメモリに載せない)"""
    lines = []
    venue = store.venue()
    if venue != "hyperliquid" and store.coins():
        lines += [f"会場 {venue}  (金額は建値の通貨)", ""]
    for coin in store.coins():
        inst = store.instrument(coin)
        sym = inst.symbol if inst else "$"
        maker_bps = (maker_rate if maker_rate is not None else inst.maker_rate if inst else DEFAULT_MAKER_RATE) * 10_000
        times, lag, ticks = array("q"), array("q"), array("q")
        spreads, bid_usd, ask_usd = array("d"), array("d"), array("d")
        mid = 0.0
        for t, recv, bid, ask, bid_sz, ask_sz in store.iter_tops(coin, since, until):
            mid = (bid + ask) / 2
            times.append(t)
            lag.append(recv - t)
            spreads.append((ask - bid) / mid * 10_000)
            bid_usd.append(bid_sz * bid)
            ask_usd.append(ask_sz * ask)
            if inst and inst.tick:
                ticks.append(round((ask - bid) / inst.tick))
        if len(times) < 2:
            continue
        n_books = len(times)
        start, end = times[0], times[-1]
        # 記録が途切れていた時間は「1 時間あたり」の分母に入れない
        steps = array("q", (times[k] - times[k - 1] for k in range(1, n_books)))
        hours = sum(s for s in steps if s <= MAX_BOOK_GAP_MS) / 3_600_000
        trades = sorted(store.iter_trades(coin, start, end), key=trade_order)
        # 数えるのは、途切れていない気配の間 (前の気配より後、次の気配以前) に入った約定だけ。
        # 約定も気配も時刻順なので 1 回なめれば足りる。i は「時刻が約定以降」の最初の気配
        seen, i = [], 1
        for t in trades:
            while i < n_books and times[i] < t.time:
                i += 1
            if i < n_books and times[i - 1] < t.time and steps[i - 1] <= MAX_BOOK_GAP_MS:
                seen.append(t)
        spreads, bid_usd, ask_usd = _sorted(spreads), _sorted(bid_usd), _sorted(ask_usd)
        notional = sum(t.px * t.sz for t in seen)
        buys = sum(t.px * t.sz for t in seen if t.side == "B")
        unknown = sum(t.px * t.sz for t in seen if t.side == "U")
        med = quantile(spreads, 0.5)
        lines.append(f"== {coin}  記録 {hours:.2f} 時間  気配 {n_books} 件 (間隔の中央値 {quantile(_sorted(steps), 0.5) / 1000:.1f} 秒)")
        lines.append(
            f"  スプレッド (bps)   中央値 {med:6.2f}  p25 {quantile(spreads, 0.25):6.2f}  p75 {quantile(spreads, 0.75):6.2f}"
        )
        if inst and inst.tick:
            # 刻みが粗い銘柄では、スプレッドが刻み 1 つに張り付く。そこでは内側に指値を出せず、最良気配の列に並ぶしかない
            ticks = _sorted(ticks)  # mid は最後の気配のミッド
            lines.append(
                f"  刻み {inst.tick:g} (= {inst.tick / mid * 10_000:.3f} bps)   スプレッドは刻み {quantile(ticks, 0.5)} 個ぶん (中央値)、"
                f"刻み 1 個だった割合 {sum(1 for t in ticks if t <= 1) / len(ticks) * 100:.0f}%   "
                f"最小数量 {inst.min_size:g} (= {sym}{inst.min_size * mid:,.0f})"
            )
        lines.append(
            f"  片側スプレッド − メイカー手数料 ({maker_bps:+.2f} bps) = {med / 2 - maker_bps:+.2f} bps  "
            "※ 最良気配に並んで約定し、その後ミッドが動かない場合の上限。逆選択は replay で見る"
        )
        lines.append(
            f"  最良気配の厚さ     買 中央値 {sym}{quantile(bid_usd, 0.5):,.0f} (p25 {sym}{quantile(bid_usd, 0.25):,.0f})  "
            f"売 中央値 {sym}{quantile(ask_usd, 0.5):,.0f} (p25 {sym}{quantile(ask_usd, 0.25):,.0f})"
        )
        if hours > 0:
            lines.append(
                f"  約定               {len(seen) / hours:,.0f} 件/時  {sym}{notional / hours:,.0f}/時  "
                f"(記録した {len(seen)} 件、買い手主導が代金の {buys / notional * 100 if notional else 0:.0f}%"
                + (f"、向きが分からない約定が {unknown / notional * 100:.0f}%" if unknown else "") + ")"
            )
        gaps = store.gap_count(coin, start, end)
        tp = store.query(
            "SELECT COUNT(*) FROM polls WHERE coin = ? AND kind = 'recentTrades' AND error IS NULL AND time >= ? AND time <= ?",
            (coin, since, until)
        )[0][0]
        lines.append(
            f"  約定の取りこぼしの可能性  {gaps} 回 / 約定のポーリング {tp} 回  "
            "※ 抜けた件数は分からない。上の件数と代金は下限"
        )
        if store.source() == "ws":
            # 配信の記録: ポーリングは無い。受け取りの遅れ (手元の時計 − 取引所の時刻。時計のずれを含む) と切断の回数を出す
            lines.pop()
            lag = _sorted(lag)
            tlag = [r[0] for r in store.query(
                "SELECT recv - time FROM trades WHERE coin = ? AND time >= ? AND time <= ? ORDER BY 1", (coin, start, until))]
            drops = store.query(
                "SELECT COUNT(*) FROM polls WHERE kind = 'ws' AND error IS NOT NULL AND time >= ? AND time <= ?", (since, until))[0][0]
            lines.append(f"  約定の取りこぼしの可能性  {gaps} 回 (配信が切れていた間)  配信の切断 {drops} 回")
            lines.append(
                f"  配信の遅れ         気配 p50 {quantile(lag, 0.5):.0f}ms p95 {quantile(lag, 0.95):.0f}ms  "
                f"約定 p50 {quantile(tlag, 0.5):.0f}ms p95 {quantile(tlag, 0.95):.0f}ms  ※ 受け取った時刻 − 取引所の時刻"
            )
            lines.append("")
            continue
        for kind, label in (("l2Book", "気配"), ("recentTrades", "約定")):
            p = store.query(
                "SELECT COUNT(*) n, COALESCE(SUM(error IS NOT NULL), 0) errors FROM polls WHERE coin = ? AND kind = ? AND time >= ? AND time <= ?",
                (coin, kind, since, until),
            )[0]
            lat = [r[0] for r in store.query(
                "SELECT latency_ms FROM polls WHERE coin = ? AND kind = ? AND time >= ? AND time <= ? AND latency_ms IS NOT NULL "
                "ORDER BY latency_ms", (coin, kind, since, until),
            )]
            lines.append(
                f"  {label}のポーリング   {p['n']} 回  エラー {p['errors']}  "
                f"遅延 p50 {quantile(lat, 0.5):.0f}ms p95 {quantile(lat, 0.95):.0f}ms"
            )
        lines.append("")
    return "\n".join(lines).rstrip() or "(まだ記録がありません)"
