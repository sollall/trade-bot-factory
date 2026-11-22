"""クォーター: 気配と約定の履歴、自分の在庫を見て、どこに指値を出すかを決める関数。

書き方 (これだけ読めば書ける。docs/mm.md の「クォーターの書き方」にも同じことがある):

    def my_quoter(ctx: Context) -> Quote:
        ...
        return Quote(bid=..., bid_size=..., ask=..., ask_size=...)   # None の側は出さない

    QUOTERS["my_quoter"] = (my_quoter, {"half_spread_bps": 3.0, ...})   # 名前 -> (関数, パラメータの既定値)

再生 (replay) でも実況の paper でも、同じ Sim (replay.py) が同じ Context を組み立てて呼ぶ。
呼ばれるのは気配が 1 つ進むたび (前に決めた指値がまだ板に載っていない間は呼ばれない)。

渡るもの (Context)
  now        判断の時刻 (取引所の時刻、ms)。今の気配の時刻と同じ
  book       今の気配。bids は高い順、asks は安い順の (値段, 数量)。記録した上位の段 (既定 10 段) が入っている
  inventory  今の在庫 (枚数。売り越しは負)。now までの約定で決まった分
  params     パラメータ (既定値に --param を重ねたもの)。数値だけ
  books      気配の履歴。古い順で、最後が今の気配 (books[-1] is book)。直近 HISTORY_SEC 秒ぶん
  trades     市場の約定の履歴。古い順。時刻が now 以前で、直近 HISTORY_SEC 秒ぶん。
             side は仕掛けた側 ("B" = 買い手主導、"A" = 売り手主導)。自分の疑似約定は入っていない
  state      この銘柄・この実行だけの dict。最初は空。指数移動平均など、次の呼び出しまで持ちたい値を入れる

履歴に入るのは now 以前のものだけ (先読みできない)。now より後の気配・約定は、次の呼び出しで初めて見える。
履歴は上限つき (HISTORY_SEC 秒、かつ MAX_HISTORY 件)。それより長い記憶が要るなら state に自分で畳み込む。
実行の最初は履歴が短い (1 件から始まる)。足りないときの扱い (出さない、など) を自分で決めること。

守ること (破ると、再生と paper の結果が合わなくなるか、成績が嘘になる)
  - 同じ Context には同じ Quote を返す。今の時計 (time.time)、乱数、ファイルやネットワークを使わない
  - 覚えておきたい値は ctx.state に入れる。モジュールの変数やクロージャに持たない
    (銘柄をまたいで混ざり、再生をやり直すたびに結果が変わる)
  - ctx.book / ctx.books / ctx.trades / ctx.params を書き換えない
  - 履歴が途切れていることがある (記録の切れ目、約定の取りこぼし)。時刻の差を見て、連続していると決めつけない
  - 約定の履歴は REST の記録なので抜けがある (docs/mm.md)。件数や代金は下限として扱う
  - state は実行をまたがない。paper を起動し直すと空から始まる

返したあとに Sim がやること: 刻みへの丸め (買いは切り下げ、売りは切り上げ)、数量の丸め、
最小の代金に届かない指値と、相手の気配に当たる指値 (ポストオンリー) の取り下げ。
値段が前と同じなら指値は置いたまま (列の位置を保つ)。変えると列の最後尾に並び直す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from tbf.mm.book import Book, Trade

#: クォーターに渡す履歴の長さ (秒)。これより古い気配・約定は渡さない
HISTORY_SEC = 600
#: 履歴の件数の上限 (気配・約定それぞれ)。長く回してもメモリが増え続けないように。超えたら古いものから落とす
MAX_HISTORY = 5000


@dataclass(frozen=True)
class Quote:
    """bid / ask が None なら、その側は出さない。数量は枚数"""

    bid: Optional[float] = None
    bid_size: float = 0.0
    ask: Optional[float] = None
    ask_size: float = 0.0


@dataclass(frozen=True)
class Context:
    """クォーターに渡すもの。意味はモジュールの docstring を参照"""

    now: int
    book: Book
    inventory: float
    params: dict[str, float]
    books: tuple[Book, ...] = ()
    trades: tuple[Trade, ...] = ()
    state: dict = field(default_factory=dict)

    @classmethod
    def of(cls, book: Book, inventory: float = 0.0, params: Optional[dict[str, float]] = None, **kw) -> "Context":
        """テスト用: 気配 1 枚から作る (履歴はその 1 枚だけ)"""
        kw.setdefault("books", (book,))
        return cls(book.time, book, inventory, params or {}, **kw)


Quoter = Callable[[Context], Quote]

#: 基準クォーターの既定値。--param k=v で上書きする。採用の判断に使った値ではなく、出発点
BASELINE_PARAMS: dict[str, float] = {
    "half_spread_bps": 3.0,  # 基準の値段から片側に離す幅
    "size_usd": 100.0,  # 片側 1 本の大きさ
    "max_inventory_usd": 500.0,  # 在庫がこれ以上になったら、増やす側は出さない
    "skew_bps": 3.0,  # 在庫が上限のとき、両方の値段を在庫を減らす向きにずらす幅 (在庫に比例)
}


def baseline(ctx: Context) -> Quote:
    """基準: ミッド ± half_spread_bps。買い越しなら両方の値段を下げ (買いにくく売りやすく)、
    売り越しなら上げる。在庫が上限に達した側は出さない。履歴も state も使わない"""
    book, inventory, params = ctx.book, ctx.inventory, ctx.params
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
