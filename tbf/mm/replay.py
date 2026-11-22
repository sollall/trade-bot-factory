"""再生: 記録した気配と約定を時刻順に流して、指値の出し方 (クォーター) を試す。

発注はしない。「この指値を出していたら、記録にある約定のうちどれが自分に当たったか」を仮定で決める。
仮定は、実際より良く見えない側に倒してある。

時間の扱い (先読みしない)
  - 気配 k を見てクォーターが値段を決める。使えるのは気配 k と今の在庫だけ。
  - 決めた指値が板に載るのは latency_ms 後。それまでは前の指値が残っている。
    次の指値がまだ載っていない間は、新しい判断をしない。
  - 取引所の時刻が気配 k+1 以前の約定は、気配 k+1 より先に、その時点で板に載っている指値に当てる
    (ポーリングの間隔より細かい前後関係は分からないので、約定の時刻だけを信じる)。

約定の仮定 (買い指値 p の場合。売りは逆)
  (a) 売り手主導の約定が p より安く出たら約定する。ただし数量はその約定の数量まで
      (記録にある売りの量より多くは買えない)。
  (b) p ちょうどの売り手主導の約定は、まず自分より前の列を消化し、残りがあればその分だけ約定する。
      列は、指値を決めたときの気配で p に並んでいた数量。減るのは記録にある約定でだけ
      (前にいる人の取消では減らさない)。
      p が最良気配より内側なら列は 0。記録した段数より外側なら列は分からないので、(a) でしか約定しない。
  (c) 次の気配で相手側の最良気配が p 以下になっていたら、残りはすべて p で約定したとみなす。
      約定の記録に抜けがあっても、値段が自分の指値を抜けたのに「約定しなかった」ことにしないため。
      これはかなり不利な側の仮定。約定が無いまま気配だけが動くことは多く (他のメイカーの出し直し)、
      実際にはその全部で古い指値が拾われるわけではない。気配の間隔が粗いほど (c) が増え、結果を支配する。
      cross_fills=False (--no-cross-fill) で外せる。外したほうは甘い側なので、2 つの間に実際があると読む。
  - 値段が同じなら指値は置いたままで、列の位置も保つ。値段が変わったら取り消して出し直し、列の最後尾。
  - 相手の気配に当たる値段 (買い >= 最良売り) は出さない (ポストオンリーで弾かれる扱い)。

損益の内訳 (足すと合計になる)
  - スプレッドの取り分: 約定ごとの (その直前の気配のミッド - 約定値段) x 数量 (売りは符号が逆)
  - 手数料: 約定代金 x メイカー手数料率
  - 在庫の評価損益: 持っていた在庫 x その間のミッドの動き。残った在庫は最後のミッドで評価する
  - ファンディング: **未計上**

まだ甘いところは docs/mm.md の「限界」に書いてある。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Optional

from tbf.mm.book import Book, Trade, price_decimals, round_down, round_up, trade_order
from tbf.mm.quoter import Quote, Quoter

BUY, SELL = 1, -1

#: メイカー手数料率の既定 (+0.015%)。Hyperliquid の無期限・基本ティア。
#: 2026-11-22 に公開 API の {"type":"userFees"} が返す料率表 (feeSchedule.add = 0.00015) で確かめた。
#: 公式ドキュメントの手数料ページでは **未確認** (この環境から届かない)。出来高や
#: ステーキングの割引、紹介の割引は入れていない。
DEFAULT_MAKER_RATE = 0.00015
#: 指値が板に載るまでの遅れの既定。**仮定**。手元から info を 1 回叩く往復が 250〜300ms だったのでその程度に
#: 置いただけで、発注の経路で測った値ではない
DEFAULT_LATENCY_MS = 250
#: 1 注文の最小の代金 (USD)。これ未満は出さない。Hyperliquid は 10 ドルのはずだが **未確認**
MIN_NOTIONAL_USD = 10.0
#: 気配がこれ以上途切れたら記録の切れ目とみなし、指値をいったん全部取り下げる (見えない間は約定を判定できない)
MAX_BOOK_GAP_MS = 60_000


@dataclass
class Order:
    side: int
    px: float
    remaining: float
    queue: float  # 自分より前に並んでいる数量。分からないときは inf


@dataclass(frozen=True)
class Fill:
    time: int
    side: int
    px: float
    size: float
    mid: float  # 約定の直前の気配のミッド
    kind: str  # "through" = (a)、"queue" = (b)、"cross" = (c)


@dataclass
class Result:
    fills: list[Fill] = field(default_factory=list)
    spread_pnl: float = 0.0
    fee_pnl: float = 0.0
    inventory_pnl: float = 0.0
    total_pnl: float = 0.0  # 現金の出入り + 残った在庫の評価 - 手数料 (内訳とは別に計算する)
    position: float = 0.0
    last_mid: float = 0.0
    max_abs_position: float = 0.0
    max_abs_position_usd: float = 0.0
    avg_position: float = 0.0  # 時間平均 (枚数)
    avg_abs_position: float = 0.0
    observed_ms: int = 0  # 記録の切れ目を除いた長さ
    books: int = 0
    breaks: int = 0
    placed: int = 0
    canceled: int = 0
    rejected: int = 0  # 相手の気配に当たるので出さなかった回数
    too_small: int = 0  # 最小の代金に届かず出さなかった回数
    market_volume_usd: float = 0.0  # 指値を出していた間に市場で成立した代金
    #: 約定から N 秒後のミッドで測った取り分 (bps、数量で加重)。N -> (値, 数えた約定の件数)
    markouts: dict[int, tuple[float, int]] = field(default_factory=dict)
    edge_bps: float = 0.0  # 約定した瞬間の取り分 (bps、数量で加重)

    @property
    def volume_usd(self) -> float:
        return sum(f.px * f.size for f in self.fills)

    def count(self, side: int) -> int:
        return sum(1 for f in self.fills if f.side == side)


def queue_ahead(book: Book, side: int, px: float) -> float:
    """その値段に今並んでいる数量 (= これから出す指値の前の列)"""
    levels = book.bids if side == BUY else book.asks
    if (px - levels[0][0]) * side > 0:
        return 0.0  # 最良気配より内側。自分が先頭
    if (levels[-1][0] - px) * side > 0:
        return math.inf  # 記録した段数より外側。列が分からない
    return next((sz for p, sz in levels if p == px), 0.0)


def simulate(
    books: list[Book],
    trades: list[Trade],
    quoter: Quoter,
    params: dict[str, float],
    *,
    sz_decimals: int,
    maker_rate: float = DEFAULT_MAKER_RATE,
    latency_ms: int = DEFAULT_LATENCY_MS,
    horizons: tuple[int, ...] = (5, 30, 60),
    max_book_gap_ms: int = MAX_BOOK_GAP_MS,
    cross_fills: bool = True,
) -> Result:
    """books は時刻順。trades の順は問わない。仮定はモジュールの docstring を参照"""
    res = Result(books=len(books))
    if not books:
        return res
    trades = sorted(trades, key=trade_order)
    live: dict[int, Optional[Order]] = {BUY: None, SELL: None}
    pending: Optional[tuple[int, Quote, Book]] = None  # (板に載る時刻, 出したい指値, 決めたときの気配)
    pos = cash = fees = 0.0
    area = abs_area = 0.0  # 在庫 x 時間
    clock = books[0].time
    mid = books[0].mid  # 直前の気配のミッド

    def advance(t: int) -> None:
        nonlocal clock, area, abs_area
        area += pos * (t - clock)
        abs_area += abs(pos) * (t - clock)
        res.observed_ms += t - clock
        clock = t

    def fill(o: Order, size: float, t: int, kind: str) -> None:
        nonlocal pos, cash, fees
        if size <= 0:
            return
        advance(max(t, clock))
        o.remaining = round(o.remaining - size, 9)
        pos = round(pos + o.side * size, 9)
        cash -= o.side * size * o.px
        fees += maker_rate * size * o.px
        res.spread_pnl += o.side * (mid - o.px) * size
        res.fills.append(Fill(t, o.side, o.px, size, mid, kind))
        res.max_abs_position = max(res.max_abs_position, abs(pos))
        res.max_abs_position_usd = max(res.max_abs_position_usd, abs(pos) * mid)

    def normalize(side: int, px: Optional[float], size: float, book: Book) -> Optional[tuple[float, float]]:
        if px is None or px <= 0:
            return None
        nd = price_decimals(px, sz_decimals)
        px = round_down(px, nd) if side == BUY else round_up(px, nd)
        size = round_down(size, sz_decimals)
        if px <= 0 or px * size < MIN_NOTIONAL_USD:
            res.too_small += 1
            return None
        if (side == BUY and px >= book.best_ask) or (side == SELL and px <= book.best_bid):
            res.rejected += 1
            return None
        return px, size

    def activate(quote: Quote, book: Book) -> None:
        for side, want in ((BUY, normalize(BUY, quote.bid, quote.bid_size, book)),
                           (SELL, normalize(SELL, quote.ask, quote.ask_size, book))):
            cur = live[side]
            if cur and cur.remaining > 0 and want and want[0] == cur.px:
                continue  # 値段が同じなら置いたまま (列の位置を保つ)
            if cur and cur.remaining > 0:
                res.canceled += 1
            live[side] = None
            if want:
                live[side] = Order(side, want[0], want[1], queue_ahead(book, side, want[0]))
                res.placed += 1

    def activate_due(t: int) -> None:
        nonlocal pending
        if pending and pending[0] <= t:
            activate(pending[1], pending[2])
            pending = None

    def match(tr: Trade) -> None:
        o = live[BUY] if tr.side == "A" else live[SELL]
        if any(live.values()):
            res.market_volume_usd += tr.px * tr.sz
        if not o or o.remaining <= 0:
            return
        if (tr.px - o.px) * o.side < 0:  # (a) 自分の値段を抜けた
            o.queue = 0.0
            fill(o, min(o.remaining, tr.sz), tr.time, "through")
        elif tr.px == o.px:  # (b) 自分の値段ちょうど。まず前の列が消化される
            eaten = min(o.queue, tr.sz)
            o.queue -= eaten
            fill(o, min(o.remaining, tr.sz - eaten), tr.time, "queue")

    ti = 0
    while ti < len(trades) and trades[ti].time <= books[0].time:
        ti += 1  # 最初の気配より前の約定は、当てる指値が無い
    for k, book in enumerate(books):
        if k > 0 and book.time - books[k - 1].time > max_book_gap_ms:
            # 記録の切れ目。見えない間のことは判定できないので、指値は前の気配の時点で取り下げたことにする
            res.breaks += 1
            res.canceled += sum(1 for o in live.values() if o and o.remaining > 0)
            live[BUY] = live[SELL] = None
            pending = None
            while ti < len(trades) and trades[ti].time <= book.time:
                ti += 1
            clock = book.time
        elif k > 0:
            while ti < len(trades) and trades[ti].time <= book.time:
                activate_due(trades[ti].time)
                match(trades[ti])
                ti += 1
            activate_due(book.time)
            for o in (live[BUY], live[SELL]):  # (c) 値段が自分の指値を抜けていたら約定
                if cross_fills and o and o.remaining > 0 and (
                    (o.side == BUY and book.best_ask <= o.px) or (o.side == SELL and book.best_bid >= o.px)
                ):
                    fill(o, o.remaining, book.time, "cross")
            advance(book.time)
        if k > 0:
            res.inventory_pnl += pos * (book.mid - mid)
            mid = book.mid
        if pending is None:
            pending = (book.time + latency_ms, quoter(book, pos, params), book)
            activate_due(book.time)

    res.position, res.last_mid = pos, mid
    res.fee_pnl = -fees
    res.total_pnl = cash + pos * mid - fees
    if res.observed_ms:
        res.avg_position, res.avg_abs_position = area / res.observed_ms, abs_area / res.observed_ms

    # 約定から N 秒後のミッドで測り直す。約定した瞬間の取り分より小さくなった分が逆選択
    times = [b.time for b in books]
    weight = sum(f.size * f.px for f in res.fills)
    if weight:
        res.edge_bps = sum(f.side * (f.mid - f.px) / f.px * 10_000 * f.size * f.px for f in res.fills) / weight
    for h in horizons:
        num = den = 0.0
        n = 0
        for f in res.fills:
            target = f.time + h * 1000
            i = bisect.bisect_left(times, target)  # N 秒たった後の最初の気配 (N 秒より手前では測らない)
            if i == len(books) or books[i].time - target > max_book_gap_ms:
                continue  # 記録がそこまで無い、または切れ目の中
            later = books[i]
            num += f.side * (later.mid - f.px) / f.px * 10_000 * f.size * f.px
            den += f.size * f.px
            n += 1
        if den:
            res.markouts[h] = (num / den, n)
    return res


def format_result(
    coin: str, res: Result, params: dict[str, float], *, maker_rate: float, taker_rate: float, latency_ms: int,
    trade_gaps: int, quoter: str = "baseline", cross_fills: bool = True,
) -> str:
    hours = res.observed_ms / 3_600_000
    lines = [f"== 再生 {coin}  クォーター {quoter}  " + " ".join(f"{k}={v:g}" for k, v in params.items())]
    lines.append(
        f"記録 {hours:.2f} 時間 (気配 {res.books} 件、切れ目 {res.breaks} 回、約定の取りこぼしの可能性 {trade_gaps} 回)  "
        f"メイカー手数料 {maker_rate * 10_000:+.2f} bps  板に載るまで {latency_ms} ms"
    )
    traded = sum(f.px * f.size for f in res.fills if f.kind != "cross")
    share = traded / res.market_volume_usd * 100 if res.market_volume_usd else 0.0
    lines.append(f"約定 {len(res.fills)} 件 (買 {res.count(BUY)} / 売 {res.count(SELL)})  代金 ${res.volume_usd:,.0f}")
    for kind, label in (("through", "(a) 値段を抜けた    "), ("queue", "(b) 列を消化して    "), ("cross", "(c) 気配が抜けていた")):
        fs = [f for f in res.fills if f.kind == kind]
        usd = sum(f.px * f.size for f in fs)
        line = f"  {label} {len(fs):>4} 件  ${usd:>9,.0f}"
        if usd:
            line += f"  瞬間の取り分 {sum(f.side * (f.mid - f.px) * f.size for f in fs) / usd * 10_000:+6.2f} bps"
        lines.append(line)
    lines.append(
        f"  (a)+(b) は、指値を出していた間の市場の約定代金 ${res.market_volume_usd:,.0f} の {share:.1f}%。"
        "(c) は記録にある約定ではないので、この割合に入らない"
    )
    if not cross_fills:
        lines.append("  ※ --no-cross-fill: (c) を外した甘い側の結果。値段が指値を抜けても、約定の記録が無ければ約定させていない")
    lines.append(
        f"指値: 出した {res.placed} 回  取り消した {res.canceled} 回  "
        f"相手の気配に当たるので出さず {res.rejected} 回  小さすぎて出さず {res.too_small} 回"
    )
    lines.append("\n損益 (USD)")
    lines.append(f"  スプレッドの取り分   {res.spread_pnl:+10.4f}")
    lines.append(f"  手数料               {res.fee_pnl:+10.4f}")
    lines.append(f"  在庫の評価損益       {res.inventory_pnl:+10.4f}   (残った在庫は最後のミッド {res.last_mid:g} で評価)")
    lines.append("  ファンディング          未計上")
    parts = res.spread_pnl + res.fee_pnl + res.inventory_pnl
    lines.append(f"  合計                 {res.total_pnl:+10.4f}   (内訳の和との差 {res.total_pnl - parts:+.6f})")
    if hours > 0:
        lines.append(f"  1 時間あたり         {res.total_pnl / hours:+10.4f}")
    if res.volume_usd:
        lines.append(f"  約定代金あたり       {res.total_pnl / res.volume_usd * 10_000:+10.2f} bps")

    lines.append("\n逆選択 (約定 1 件の取り分。bps、代金で加重、手数料を引く前。約定値段に対するミッドの位置)")
    lines.append(f"  約定した瞬間  {res.edge_bps:+7.2f}")
    for h, (bps, n) in sorted(res.markouts.items()):
        lines.append(f"  {h:>3} 秒後     {bps:+7.2f}   ({n} 件。瞬間との差 {bps - res.edge_bps:+.2f} が逆選択)")
    if res.fills and not res.markouts:
        lines.append("  (N 秒後まで記録が続いている約定がありません)")

    lines.append("\n在庫")
    lines.append(f"  最大      {res.max_abs_position:g} 枚 (${res.max_abs_position_usd:,.0f})")
    lines.append(f"  時間平均  {res.avg_position:+g} 枚 (絶対値の平均 {res.avg_abs_position:g} 枚)")
    close_cost = abs(res.position) * res.last_mid * taker_rate
    lines.append(
        f"  最後      {res.position:+g} 枚 (${res.position * res.last_mid:+,.0f})  "
        f"テイカーで閉じるなら手数料 ${close_cost:.4f} + スプレッドの半分が別にかかる (上の損益には未計上)"
    )
    return "\n".join(lines)
