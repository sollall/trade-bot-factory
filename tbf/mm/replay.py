"""再生: 記録した気配と約定を時刻順に流して、指値の出し方 (クォーター) を試す。

発注はしない。「この指値を出していたら、記録にある約定のうちどれが自分に当たったか」を仮定で決める。
仮定は、実際より良く見えない側に倒してある。

時間の扱い (先読みしない)
  - 気配 k を見てクォーターが値段を決める。使えるのは気配 k 以前の気配と約定 (直近の履歴)、今の在庫、
    クォーターが自分で持つ state だけ (tbf/mm/quoter.py の Context)。気配 k より後のものは渡さない。
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
  - 仕掛けた側が分からない約定 (GMO をポーリングで記録したとき。side が "U") は、値段が届いた側の指値に (a)(b) を当てる。

記録の取り方で変わる仮定 (配信で取った記録だけ。ポーリングの記録の再生結果は変えない)
  - 列 (b) は、記録にある約定に加えて、気配でも減らす (queue_from_book): 自分の値段に見えている数量が
    自分より前の列より少なくなったら、列をその数量まで減らす。自分の指値は記録の板に載っていないので、
    見えている数量はすべて他人のもので、自分より後に並んだ分も含む。だから前の列はそれより多くはない。
    ポーリングの記録では前の人の取消を数えない (列は記録にある約定でしか減らない) ままにしてある。
  - 配信の記録には向きの分からない約定が無い (GMO は仕掛けた側だけを受け取る)。

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
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from tbf.mm.book import Book, Trade, ceil_to, floor_to, price_decimals, round_down, round_up, trade_order
from tbf.mm.quoter import HISTORY_SEC, MAX_HISTORY, Context, Quote, Quoter
from tbf.mm.venues import (
    HYPERLIQUID_MAKER_RATE,
    HYPERLIQUID_MIN_NOTIONAL,
    Instrument,
    hyperliquid_instrument,
    step_decimals,
)

BUY, SELL = 1, -1

#: Hyperliquid のメイカー手数料率 (前からの名前。値と由来は tbf/mm/venues.py)。
#: ほかの会場は、会場の API が返す銘柄ごとの値を使う (Instrument.maker_rate)
DEFAULT_MAKER_RATE = HYPERLIQUID_MAKER_RATE
#: 指値が板に載るまでの遅れの既定。**仮定**。手元から info を 1 回叩く往復が 250〜300ms だったのでその程度に
#: 置いただけで、発注の経路で測った値ではない
DEFAULT_LATENCY_MS = 250
#: Hyperliquid の 1 注文の最小の代金 (前からの名前)。会場ごとの値は Instrument.min_notional / min_size
MIN_NOTIONAL_USD = HYPERLIQUID_MIN_NOTIONAL
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
    """シミュレータの成績。足し上げていく値をそのまま持ち、損益などはそこから出す
    (paper はこれを丸ごと sqlite に残し、後から同じ形に戻す)"""

    fills: list[Fill] = field(default_factory=list)
    spread_pnl: float = 0.0
    inventory_pnl: float = 0.0
    cash: float = 0.0  # 売買の現金の出入り (手数料は別)
    fees: float = 0.0  # 払った手数料 (リベートなら負)
    position: float = 0.0
    last_mid: float = 0.0
    max_abs_position: float = 0.0
    max_abs_position_usd: float = 0.0
    area: float = 0.0  # 在庫 x 時間 (ms)
    abs_area: float = 0.0
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
    def fee_pnl(self) -> float:
        return -self.fees

    @property
    def total_pnl(self) -> float:
        """現金の出入り + 残った在庫の評価 - 手数料 (内訳とは別に計算する)"""
        return self.cash + self.position * self.last_mid - self.fees

    @property
    def avg_position(self) -> float:
        """時間平均 (枚数)"""
        return self.area / self.observed_ms if self.observed_ms else 0.0

    @property
    def avg_abs_position(self) -> float:
        return self.abs_area / self.observed_ms if self.observed_ms else 0.0

    @property
    def volume_usd(self) -> float:
        return sum(f.px * f.size for f in self.fills)

    def count(self, side: int) -> int:
        return sum(1 for f in self.fills if f.side == side)


def shown_size(book: Book, side: int, px: float) -> Optional[float]:
    """その値段に今見えている数量。記録した段数より外側で分からないときは None"""
    levels = book.bids if side == BUY else book.asks
    if (px - levels[0][0]) * side > 0:
        return 0.0
    if (levels[-1][0] - px) * side > 0:
        return None
    return next((sz for p, sz in levels if p == px), 0.0)


def queue_ahead(book: Book, side: int, px: float) -> float:
    """その値段に今並んでいる数量 (= これから出す指値の前の列)"""
    levels = book.bids if side == BUY else book.asks
    if (px - levels[0][0]) * side > 0:
        return 0.0  # 最良気配より内側。自分が先頭
    if (levels[-1][0] - px) * side > 0:
        return math.inf  # 記録した段数より外側。列が分からない
    return next((sz for p, sz in levels if p == px), 0.0)


class Sim:
    """1 銘柄ぶんのシミュレータ。気配を 1 つずつ食わせる。

    再生 (simulate) も実況の paper (tbf/mm/paper.py) も、約定の判定はここだけを通る。
    step(book, trades) の trades は「前の気配より後、この気配以前」の約定。順は問わない。
    """

    def __init__(
        self,
        quoter: Quoter,
        params: dict[str, float],
        *,
        sz_decimals: Optional[int] = None,
        maker_rate: Optional[float] = None,
        latency_ms: int = DEFAULT_LATENCY_MS,
        max_book_gap_ms: int = MAX_BOOK_GAP_MS,
        cross_fills: bool = True,
        instrument: Optional[Instrument] = None,
        queue_from_book: bool = False,
    ) -> None:
        # 配信の記録 (気配が細かい) のときだけ使う: 自分より前の列を、その値段に今見えている数量で頭打ちにする
        self.queue_from_book = queue_from_book
        # 銘柄の決まりごと (刻み・最小数量・手数料)。sz_decimals だけ渡せば Hyperliquid の銘柄として扱う
        if instrument is None:
            if sz_decimals is None:
                raise ValueError("instrument か sz_decimals のどちらかが要る")
            instrument = hyperliquid_instrument("", sz_decimals)
        self.inst = instrument
        self._tick_nd = step_decimals(instrument.tick) if instrument.tick else 0
        self._size_nd = step_decimals(instrument.size_step) if instrument.size_step else 0
        self.quoter, self.params = quoter, params
        self.maker_rate = instrument.maker_rate if maker_rate is None else maker_rate
        self.latency_ms = latency_ms
        self.max_book_gap_ms, self.cross_fills = max_book_gap_ms, cross_fills
        self.res = Result()
        self.live: dict[int, Optional[Order]] = {BUY: None, SELL: None}
        self.pending: Optional[tuple[int, Quote, Book]] = None  # (板に載る時刻, 出したい指値, 決めたときの気配)
        self.prev: Optional[Book] = None
        self.clock = 0
        self.mid = 0.0  # 直前の気配のミッド
        # クォーターに渡す履歴と、クォーターが自由に使う入れ物。どちらもこの Sim (= 1 銘柄・1 回の実行) だけのもの
        self.history_ms = HISTORY_SEC * 1000
        self.book_history: deque[Book] = deque(maxlen=MAX_HISTORY)
        self.trade_history: deque[Trade] = deque(maxlen=MAX_HISTORY)
        self.state: dict = {}

    def _advance(self, t: int) -> None:
        r = self.res
        r.area += r.position * (t - self.clock)
        r.abs_area += abs(r.position) * (t - self.clock)
        r.observed_ms += t - self.clock
        self.clock = t

    def _fill(self, o: Order, size: float, t: int, kind: str) -> None:
        if size <= 0:
            return
        r = self.res
        self._advance(max(t, self.clock))
        o.remaining = round(o.remaining - size, 9)
        r.position = round(r.position + o.side * size, 9)
        r.cash -= o.side * size * o.px
        r.fees += self.maker_rate * size * o.px
        r.spread_pnl += o.side * (self.mid - o.px) * size
        r.fills.append(Fill(t, o.side, o.px, size, self.mid, kind))
        r.max_abs_position = max(r.max_abs_position, abs(r.position))
        r.max_abs_position_usd = max(r.max_abs_position_usd, abs(r.position) * self.mid)

    def _normalize(self, side: int, px: Optional[float], size: float, book: Book) -> Optional[tuple[float, float]]:
        if px is None or px <= 0:
            return None
        inst = self.inst
        # 買いは切り下げ、売りは切り上げ (どちらもミッドから遠い側)。刻みが粗い銘柄では、これで最良気配と
        # 同じ値段に並ぶことがある。そのときは queue_ahead がその値段の数量を「前の列」として数える
        if inst.tick is None:  # Hyperliquid: 値段の桁で刻みが変わる
            nd = price_decimals(px, inst.sz_decimals)  # type: ignore[arg-type]
            px = round_down(px, nd) if side == BUY else round_up(px, nd)
            size = round_down(size, inst.sz_decimals)  # type: ignore[arg-type]
        else:
            px = floor_to(px, inst.tick, self._tick_nd) if side == BUY else ceil_to(px, inst.tick, self._tick_nd)
            size = floor_to(size, inst.size_step, self._size_nd)
        if px <= 0 or size <= 0 or size < inst.min_size or px * size < inst.min_notional:
            self.res.too_small += 1
            return None
        if (side == BUY and px >= book.best_ask) or (side == SELL and px <= book.best_bid):
            self.res.rejected += 1
            return None
        return px, size

    def _activate_due(self, t: int) -> None:
        if not self.pending or self.pending[0] > t:
            return
        _, quote, book = self.pending
        self.pending = None
        for side, want in ((BUY, self._normalize(BUY, quote.bid, quote.bid_size, book)),
                           (SELL, self._normalize(SELL, quote.ask, quote.ask_size, book))):
            cur = self.live[side]
            if cur and cur.remaining > 0 and want and want[0] == cur.px:
                continue  # 値段が同じなら置いたまま (列の位置を保つ)
            if cur and cur.remaining > 0:
                self.res.canceled += 1
            self.live[side] = None
            if want:
                self.live[side] = Order(side, want[0], want[1], queue_ahead(book, side, want[0]))
                self.res.placed += 1

    def _match(self, tr: Trade) -> None:
        if tr.side == "A":
            o = self.live[BUY]
        elif tr.side == "B":
            o = self.live[SELL]
        else:
            # 仕掛けた側が分からない約定 (GMO)。値段が届いた側の指値に当てる: 自分の買い値以下で成立したなら
            # 買い指値に、売り値以上なら売り指値に。買い値 < 売り値なので両方に当たることは無い。
            # どちらが仕掛けたにせよ、自分の指値の値段かそれより不利な値段で市場が成立した、という事実だけを使う
            bid, ask = self.live[BUY], self.live[SELL]
            o = bid if bid and bid.remaining > 0 and tr.px <= bid.px else ask if ask and ask.remaining > 0 and tr.px >= ask.px else None
        if any(self.live.values()):
            self.res.market_volume_usd += tr.px * tr.sz
        if not o or o.remaining <= 0:
            return
        if (tr.px - o.px) * o.side < 0:  # (a) 自分の値段を抜けた
            o.queue = 0.0
            self._fill(o, min(o.remaining, tr.sz), tr.time, "through")
        elif tr.px == o.px:  # (b) 自分の値段ちょうど。まず前の列が消化される
            eaten = min(o.queue, tr.sz)
            o.queue -= eaten
            self._fill(o, min(o.remaining, tr.sz - eaten), tr.time, "queue")

    def step(self, book: Book, trades: list[Trade] = ()) -> list[Fill]:  # type: ignore[assignment]
        """気配を 1 つ進める。この 1 歩で付いた約定を返す"""
        r, prev = self.res, self.prev
        before = len(r.fills)
        r.books += 1
        if prev is None:
            self.clock, self.mid = book.time, book.mid  # 最初の気配より前の約定は、当てる指値が無い
        elif book.time <= prev.time:
            raise ValueError(f"気配の時刻が戻った: {prev.time} -> {book.time}")
        elif book.time - prev.time > self.max_book_gap_ms:
            # 記録の切れ目。見えない間のことは判定できないので、指値は前の気配の時点で取り下げたことにする
            r.breaks += 1
            r.canceled += sum(1 for o in self.live.values() if o and o.remaining > 0)
            self.live[BUY] = self.live[SELL] = None
            self.pending = None
            self.clock = book.time
        else:
            for tr in sorted(trades, key=trade_order):
                if not prev.time < tr.time <= book.time:
                    raise ValueError(f"約定の時刻 {tr.time} が気配の間 ({prev.time}, {book.time}] に無い")
                self._activate_due(tr.time)
                self._match(tr)
            self._activate_due(book.time)
            if self.queue_from_book:
                # 自分の指値は記録の板に載っていないので、その値段に見えている数量はすべて他人のもの。
                # 自分より後に並んだ分も含むから、「自分より前の列」はそれより多くはない (前の人の取消で列が減ったことが分かる)
                for o in (self.live[BUY], self.live[SELL]):
                    if o and o.remaining > 0:
                        shown = shown_size(book, o.side, o.px)
                        if shown is not None:
                            o.queue = min(o.queue, shown)
            for o in (self.live[BUY], self.live[SELL]):  # (c) 値段が自分の指値を抜けていたら約定
                if self.cross_fills and o and o.remaining > 0 and (
                    (o.side == BUY and book.best_ask <= o.px) or (o.side == SELL and book.best_bid >= o.px)
                ):
                    self._fill(o, o.remaining, book.time, "cross")
            self._advance(book.time)
        if prev is not None:
            r.inventory_pnl += r.position * (book.mid - self.mid)
            self.mid = book.mid
        # 履歴に足すのは、この気配と、この気配以前の約定だけ (step の約束で trades は book.time 以前)。
        # 記録の切れ目の間の約定は約定の判定には使わないが、見えていたものなので履歴には入れる
        if prev is not None:
            self.trade_history.extend(sorted((t for t in trades if prev.time < t.time <= book.time), key=trade_order))
        self.book_history.append(book)
        for h in (self.book_history, self.trade_history):
            while h and h[0].time < book.time - self.history_ms:
                h.popleft()
        if self.pending is None:
            ctx = Context(book.time, book, r.position, self.params, tuple(self.book_history), tuple(self.trade_history), self.state)
            self.pending = (book.time + self.latency_ms, self.quoter(ctx), book)
            self._activate_due(book.time)
        self.prev = book
        r.last_mid = self.mid
        return r.fills[before:]


def add_markouts(res: Result, books: list[Book], horizons: tuple[int, ...], max_book_gap_ms: int = MAX_BOOK_GAP_MS) -> None:
    """約定から N 秒後のミッドで測り直す。約定した瞬間の取り分より小さくなった分が逆選択。
    books は時刻順で、約定より後のぶんまで入っていること"""
    times = [b.time for b in books]
    weight = sum(f.size * f.px for f in res.fills)
    res.edge_bps = sum(f.side * (f.mid - f.px) * f.size for f in res.fills) / weight * 10_000 if weight else 0.0
    res.markouts = {}
    for h in horizons:
        num = den = 0.0
        n = 0
        for f in res.fills:
            target = f.time + h * 1000
            i = bisect.bisect_left(times, target)  # N 秒たった後の最初の気配 (N 秒より手前では測らない)
            if i == len(books) or books[i].time - target > max_book_gap_ms:
                continue  # 記録がそこまで無い、または切れ目の中
            num += f.side * (books[i].mid - f.px) * f.size
            den += f.size * f.px
            n += 1
        if den:
            res.markouts[h] = (num / den * 10_000, n)


def simulate(
    books: list[Book],
    trades: list[Trade],
    quoter: Quoter,
    params: dict[str, float],
    *,
    sz_decimals: Optional[int] = None,
    maker_rate: Optional[float] = None,
    latency_ms: int = DEFAULT_LATENCY_MS,
    horizons: tuple[int, ...] = (5, 30, 60),
    max_book_gap_ms: int = MAX_BOOK_GAP_MS,
    cross_fills: bool = True,
    instrument: Optional[Instrument] = None,
    queue_from_book: bool = False,
) -> Result:
    """books は時刻順。trades の順は問わない。仮定はモジュールの docstring を参照"""
    sim = Sim(quoter, params, sz_decimals=sz_decimals, maker_rate=maker_rate, latency_ms=latency_ms,
              max_book_gap_ms=max_book_gap_ms, cross_fills=cross_fills, instrument=instrument, queue_from_book=queue_from_book)
    trades = sorted(trades, key=trade_order)
    ti = 0
    for k, book in enumerate(books):
        start = ti
        while ti < len(trades) and trades[ti].time <= book.time:
            ti += 1
        sim.step(book, trades[start:ti] if k > 0 else [])
    add_markouts(sim.res, books, horizons, max_book_gap_ms)
    return sim.res


def format_result(
    coin: str, res: Result, params: dict[str, float], *, maker_rate: float, taker_rate: float, latency_ms: int,
    trade_gaps: int, quoter: str = "baseline", cross_fills: bool = True, instrument: Optional[Instrument] = None,
    queue_from_book: bool = False,
) -> str:
    """金額は銘柄の建値の通貨 (instrument.quote)。instrument を省くと Hyperliquid (USD)"""
    sym = instrument.symbol if instrument else "$"
    quote = instrument.quote if instrument else "USD"
    hours = res.observed_ms / 3_600_000
    lines = [f"== 再生 {coin}  クォーター {quoter}  " + " ".join(f"{k}={v:g}" for k, v in params.items())]
    lines.append(
        f"記録 {hours:.2f} 時間 (気配 {res.books} 件、切れ目 {res.breaks} 回、約定の取りこぼしの可能性 {trade_gaps} 回)  "
        f"メイカー手数料 {maker_rate * 10_000:+.2f} bps  板に載るまで {latency_ms} ms"
    )
    traded = sum(f.px * f.size for f in res.fills if f.kind != "cross")
    share = traded / res.market_volume_usd * 100 if res.market_volume_usd else 0.0
    lines.append(f"約定 {len(res.fills)} 件 (買 {res.count(BUY)} / 売 {res.count(SELL)})  代金 {sym}{res.volume_usd:,.0f}")
    for kind, label in (("through", "(a) 値段を抜けた    "), ("queue", "(b) 列を消化して    "), ("cross", "(c) 気配が抜けていた")):
        fs = [f for f in res.fills if f.kind == kind]
        usd = sum(f.px * f.size for f in fs)
        line = f"  {label} {len(fs):>4} 件  {sym}{usd:>9,.0f}"
        if usd:
            line += f"  瞬間の取り分 {sum(f.side * (f.mid - f.px) * f.size for f in fs) / usd * 10_000:+6.2f} bps"
        lines.append(line)
    lines.append(
        f"  (a)+(b) は、指値を出していた間の市場の約定代金 {sym}{res.market_volume_usd:,.0f} の {share:.1f}%。"
        "(c) は記録にある約定ではないので、この割合に入らない"
    )
    if queue_from_book:
        lines.append("  ※ 配信の記録: 自分より前の列 (b) は、その値段に見えている数量を上限に、気配でも減らしている")
    if not cross_fills:
        lines.append("  ※ --no-cross-fill: (c) を外した甘い側の結果。値段が指値を抜けても、約定の記録が無ければ約定させていない")
    lines.append(
        f"指値: 出した {res.placed} 回  取り消した {res.canceled} 回  "
        f"相手の気配に当たるので出さず {res.rejected} 回  小さすぎて出さず {res.too_small} 回"
    )
    lines.append(f"\n損益 ({quote})")
    lines.append(f"  スプレッドの取り分   {res.spread_pnl:+10.4f}")
    lines.append(f"  手数料               {res.fee_pnl + 0.0:+10.4f}")
    lines.append(f"  在庫の評価損益       {res.inventory_pnl:+10.4f}   (残った在庫は最後のミッド {res.last_mid:g} で評価)")
    lines.append("  ファンディング          未計上")
    parts = res.spread_pnl + res.fee_pnl + res.inventory_pnl
    lines.append(f"  合計                 {res.total_pnl:+10.4f}   (内訳の和との差 {round(res.total_pnl - parts, 6) + 0.0:+.6f})")
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
    lines.append(f"  最大      {res.max_abs_position:g} 枚 ({sym}{res.max_abs_position_usd:,.0f})")
    lines.append(f"  時間平均  {res.avg_position:+g} 枚 (絶対値の平均 {res.avg_abs_position:g} 枚)")
    close_cost = abs(res.position) * res.last_mid * taker_rate
    lines.append(
        f"  最後      {res.position:+g} 枚 ({sym}{res.position * res.last_mid:+,.0f})  "
        f"テイカーで閉じるなら手数料 {sym}{close_cost:.4f} + スプレッドの半分が別にかかる (上の損益には未計上)"
    )
    if instrument and not instrument.can_short:
        # 現物は在庫が無いと売れないが、再生はそれを守らせていない。どれだけ頼っているかを出す
        pos = res.position - sum(f.side * f.size for f in res.fills)
        n = usd = 0.0
        for f in res.fills:
            if f.side == SELL and pos - f.size < -1e-9:
                n += 1
                usd += f.px * f.size
            pos += f.side * f.size
        lines.append(
            f"\n※ 現物なので、実際には在庫が無いと売れない。この再生はそれを守らせていない (実際より甘い)。"
            f"在庫を 0 より下に押し下げた売りが {n:.0f} 件 ({sym}{usd:,.0f}) 含まれる。"
            "最初に在庫を持って始めれば出せるが、その在庫の値動きは別に負う"
        )
    return "\n".join(lines)
