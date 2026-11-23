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
             side は仕掛けた側 ("B" = 買い手主導、"A" = 売り手主導)。自分の疑似約定は入っていない。
             GMO コインの記録では side が "U" (不明) の約定が大半。向きを使うなら "U" を売り買いどちらにも
             数えないこと (`== "B"` でなければ売り、と書くと全部が売りに見える)
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


# ---------------------------------------------------------------------------------------------
# 改善案 (一次評価のための候補。採否は決まっていない)。
#
# どれも在庫の扱い (在庫に比例して値段をずらす・上限で片側を止める) は baseline と同じで、変えるのは
# 「真ん中をどこに置くか」「幅をどうするか」「どの側を出さないか」だけ。比べやすいように 1 案 1 仕掛けにしてある。
# 下の「決め打ち」の定数は、名前のとおり固定で使っている作りの一部 (スイープしていない)。
# 既定値はすべて出発点で、採用の判断に使った値ではない。

#: 荒さを測るときの決め打ち。実現ボラは「この秒数のあいだにミッドが動く幅 (1σ、bps)」に直して返す。
#: 5 秒は、逆選択を測る一番短い時間 (replay の --horizons の既定) に合わせただけ
VOL_HORIZON_SEC = 5.0
#: 気配の間がこれより空いていたら記録の切れ目とみなし、それより前は荒さの計算に使わない
VOL_MAX_STEP_MS = 15_000
#: 窓のうちこの割合より短い時間しか気配が続いていなければ「履歴が足りない」(= 出さない)
VOL_MIN_COVERAGE = 0.5
#: 板の偏りを見る段数。最良気配の 1 段だけ。1 / 3 / 5 / 10 段を前半の記録 (27 分) で比べ、5 秒先のミッドの動きと
#: 3 銘柄とも同じ向きだったのが 1 段だけだったので 1 にした (相関は 0.06〜0.15 と弱い)。合わせ込みの余地がある値
IMB_LEVELS = 1
#: 約定の売り買いの偏りを見る窓 (秒)
FLOW_WINDOW_SEC = 60.0
#: 直前のこの時間の約定は偏りの計算に使わない。ctx.trades には now ちょうどの約定まで入るが、
#: 実際の bot には届くのに時間がかかるので、直前の約定に頼ると実際より良く見える (docs/mm.md)
FLOW_DELAY_MS = 500


def realized_vol_bps(books: tuple[Book, ...], now: int, window_sec: float) -> Optional[float]:
    """直近 window_sec 秒の実現ボラ。VOL_HORIZON_SEC 秒あたりのミッドの動き (1σ、bps) に直して返す。
    気配の間隔はまちまち (1.5〜3 秒) なので、2 乗和を経過時間で割ってから直す。
    記録の切れ目より前は使わない。続いている時間が窓の VOL_MIN_COVERAGE に満たなければ None (履歴が足りない)"""
    start = now - window_sec * 1000
    sumsq = 0.0
    covered = 0
    newer: Optional[Book] = None
    for b in reversed(books):
        if b.time > now:
            continue
        if b.time < start:
            break
        if newer is not None:
            dt = newer.time - b.time
            if dt > VOL_MAX_STEP_MS:
                break
            if dt > 0:
                move = (newer.mid / b.mid - 1) * 10_000
                sumsq += move * move
                covered += dt
        newer = b
    if covered <= 0 or covered < window_sec * 1000 * VOL_MIN_COVERAGE:
        return None
    return (sumsq / covered * VOL_HORIZON_SEC * 1000) ** 0.5


def book_imbalance(book: Book, levels: int = IMB_LEVELS) -> float:
    """板の厚さの偏り。上位 levels 段の (買い − 売り) / (買い + 売り)。+1 = 買いだけが厚い、−1 = 売りだけが厚い"""
    bid = sum(sz for _, sz in book.bids[:levels])
    ask = sum(sz for _, sz in book.asks[:levels])
    return (bid - ask) / (bid + ask) if bid + ask > 0 else 0.0


def trade_flow(trades: tuple[Trade, ...], now: int, window_sec: float = FLOW_WINDOW_SEC) -> float:
    """約定の売り買いの偏り。直近 window_sec 秒の (買い手主導 − 売り手主導) / 合計 (代金)。+1 = 買いばかり。
    直前 FLOW_DELAY_MS の約定は使わない。約定が 1 件も無ければ 0 (どちらにも寄せない)。
    約定の記録には抜けがあり、件数も少ない (1 分に数件) ので、1 件だけで ±1 になることがある"""
    newest, oldest = now - FLOW_DELAY_MS, now - window_sec * 1000
    buy = sell = 0.0
    for t in reversed(trades):
        if t.time > newest:
            continue
        if t.time < oldest:
            break
        if t.side == "B":
            buy += t.px * t.sz
        elif t.side == "A":  # "U" (向きが分からない約定。GMO) はどちらにも数えない
            sell += t.px * t.sz
    return (buy - sell) / (buy + sell) if buy + sell > 0 else 0.0


def _quote_around(ctx: Context, center: float, half_bps: float, *, bid_ok: bool = True, ask_ok: bool = True) -> Quote:
    """center ± half_bps に出す。在庫の扱いは baseline と同じ (在庫に比例してずらす・上限で増やす側を止める)。
    center = ミッド、half_bps = half_spread_bps なら baseline と同じ指値になる"""
    params, mid = ctx.params, ctx.book.mid
    inv_usd = ctx.inventory * mid
    cap = params["max_inventory_usd"]
    center = center * (1 - params["skew_bps"] / 10_000 * (inv_usd / cap if cap > 0 else 0.0))
    half = half_bps / 10_000
    size = params["size_usd"] / mid
    return Quote(
        bid=center * (1 - half) if bid_ok and inv_usd < cap else None,
        bid_size=size,
        ask=center * (1 + half) if ask_ok and inv_usd > -cap else None,
        ask_size=size,
    )


def _vol_half(ctx: Context) -> tuple[Optional[float], bool]:
    """(幅 bps, 新しく在庫を増やしてよいか)。履歴が足りない間は (None, False)"""
    params = ctx.params
    vol = realized_vol_bps(ctx.books, ctx.now, params["vol_window_sec"])
    if vol is None:
        return None, False
    calm = not (params["vol_pause_bps"] > 0 and vol > params["vol_pause_bps"])
    return max(params["half_spread_bps"], params["vol_mult"] * vol), calm


def _lean_bps(ctx: Context) -> float:
    """真ん中をずらす幅 (bps)。正 = 上がりそう (買いが厚い・買い手主導が多い) なので、両方の値段を上げる"""
    params = ctx.params
    return params["imb_bps"] * book_imbalance(ctx.book) + params["flow_bps"] * trade_flow(ctx.trades, ctx.now)


def _vol_lean(ctx: Context, *, use_vol: bool, use_lean: bool) -> Quote:
    params, mid = ctx.params, ctx.book.mid
    half, bid_ok, ask_ok = params["half_spread_bps"], True, True
    if use_vol:
        vol_half, calm = _vol_half(ctx)
        if vol_half is None:
            return Quote()  # 履歴が足りない間は出さない
        half = vol_half
        if not calm:  # 荒すぎる。在庫を減らす側だけ残す (在庫を抱えたまま両方を引くと、出口が無くなる)
            bid_ok, ask_ok = ctx.inventory < 0, ctx.inventory > 0
    center = mid
    if use_lean:
        lean = _lean_bps(ctx)
        center = mid * (1 + lean / 10_000)
        if params["one_side_bps"] > 0:  # 偏りが強いとき、踏まれる側 (上がりそうなら売り) は出さない
            ask_ok = ask_ok and lean < params["one_side_bps"]
            bid_ok = bid_ok and lean > -params["one_side_bps"]
    return _quote_around(ctx, center, half, bid_ok=bid_ok, ask_ok=ask_ok)


#: 既定値は出発点で、採用の判断に使った値ではない (baseline と同じ扱い)
VOL_PARAMS: dict[str, float] = {
    **BASELINE_PARAMS,
    "vol_window_sec": 60.0,  # 荒さを測る窓
    "vol_mult": 1.0,  # 幅 = max(half_spread_bps, vol_mult x 直近の実現ボラ (5 秒あたり bps))。0 なら広げない
    "vol_pause_bps": 6.0,  # 実現ボラがこれを超えたら、在庫を増やす側は出さない。0 以下なら止めない
}
LEAN_PARAMS: dict[str, float] = {
    **BASELINE_PARAMS,
    "imb_bps": 1.0,  # 板の偏りが ±1 のとき、真ん中をずらす幅
    "flow_bps": 0.5,  # 約定の偏りが ±1 のとき、真ん中をずらす幅
    "one_side_bps": 1.0,  # ずらす幅の合計がこれ以上なら、踏まれる側は出さない。0 以下なら両側とも出す
}
VOL_LEAN_PARAMS: dict[str, float] = {**VOL_PARAMS, **{k: LEAN_PARAMS[k] for k in ("imb_bps", "flow_bps", "one_side_bps")}}
COOLDOWN_PARAMS: dict[str, float] = {
    **BASELINE_PARAMS,
    "cooldown_sec": 10.0,  # 約定した側を、この秒数は出さない
}


def vol_spread(ctx: Context) -> Quote:
    """荒さで幅を変える: 幅 = max(half_spread_bps, vol_mult x 直近 vol_window_sec 秒の実現ボラ)。
    実現ボラが vol_pause_bps を超えたら、在庫を増やす側は出さない。真ん中はミッド (baseline と同じ)。
    履歴が窓の半分に満たない間 (実行の最初、記録の切れ目の直後) は出さない"""
    return _vol_lean(ctx, use_vol=True, use_lean=False)


def lean(ctx: Context) -> Quote:
    """真ん中をずらす: ミッドから imb_bps x 板の偏り + flow_bps x 約定の偏り だけ動かす (幅は固定)。
    ずれが one_side_bps 以上なら、踏まれる側は出さない。約定の履歴が無い間は板の偏りだけで動く"""
    return _vol_lean(ctx, use_vol=False, use_lean=True)


def vol_lean(ctx: Context) -> Quote:
    """vol_spread と lean を合わせたもの (幅は荒さで、真ん中は偏りで)"""
    return _vol_lean(ctx, use_vol=True, use_lean=True)


def cooldown(ctx: Context) -> Quote:
    """約定した側をしばらく休む: 買いが約定したら cooldown_sec 秒は買いを出さない (売りも同じ)。ほかは baseline と同じ。
    同じ向きに続けて拾われて在庫が積み上がるのを抑える狙い。約定は、前に呼ばれたときからの在庫の変化で知る (state)"""
    st, now = ctx.state, ctx.now
    last = st.get("inventory", ctx.inventory)
    if ctx.inventory > last + 1e-12:
        st["bid_until"] = now + ctx.params["cooldown_sec"] * 1000
    elif ctx.inventory < last - 1e-12:
        st["ask_until"] = now + ctx.params["cooldown_sec"] * 1000
    st["inventory"] = ctx.inventory
    return _quote_around(
        ctx, ctx.book.mid, ctx.params["half_spread_bps"],
        bid_ok=now >= st.get("bid_until", 0), ask_ok=now >= st.get("ask_until", 0),
    )


# ---------------------------------------------------------------------------------------------
# 国内 2 会場 (GMO コインのレバレッジ、bitbank の現物) の配信の記録から作った候補。採否は決まっていない。
# どちらも「どの側を出さないか」を変えるだけで、値段の決め方は 1 つずつ。既定値は出発点で、採用の判断に使った値ではない。

#: trade_pause が約定を見るとき、直前のこの時間の約定は「まだ届いていない」として使わない。
#: 配信の約定が手元に届くまでの実測 (GMO コインで 90ms 前後。docs/mm.md) に合わせた決め打ち
TRADE_PAUSE_DELAY_MS = 100

TRADE_PAUSE_PARAMS: dict[str, float] = {
    **BASELINE_PARAMS,
    "pause_sec": 3.0,  # 成行が出たあと、その成行に食われる側をこの秒数は出さない。0 以下なら休まない (= baseline)
}


def trade_pause(ctx: Context) -> Quote:
    """成行が出た直後は、食われる側を休む: 直近 pause_sec 秒に売り手主導の約定があれば買いを、買い手主導の約定があれば
    売りを出さない (両方あれば両方)。ほかは baseline と同じ (値段も在庫の扱いも)。在庫を減らす側でも休む。

    GMO のレバレッジの配信の記録で、baseline の約定のうち「直前 1 秒以内に同じ向きの成行が出ていたもの」(約定の 3 割前後) が、
    ほかの約定より 5 秒後か 60 秒後の取り分が悪かったのを受けたもの。成行は続けて出る。cooldown は自分が約定したあとに休むが、これは市場の約定を見て休む。
    直前 TRADE_PAUSE_DELAY_MS の約定は使わない。約定の向きが要るので、向きが分からない記録 (GMO のポーリング。side が "U") では
    何も休まず baseline と同じになる。呼ばれるのは気配が進むときだけなので、成行を見てから引くまでに気配 1 回ぶん遅れる"""
    params, now = ctx.params, ctx.now
    newest, oldest = now - TRADE_PAUSE_DELAY_MS, now - params["pause_sec"] * 1000
    bid_ok = ask_ok = True
    for t in reversed(ctx.trades):
        if t.time > newest:
            continue
        if t.time < oldest:
            break
        if t.side == "A":
            bid_ok = False
        elif t.side == "B":
            ask_ok = False
    return _quote_around(ctx, ctx.book.mid, params["half_spread_bps"], bid_ok=bid_ok, ask_ok=ask_ok)


JOIN_PARAMS: dict[str, float] = {
    "size_usd": BASELINE_PARAMS["size_usd"],  # 片側 1 本の大きさ
    "max_inventory_usd": BASELINE_PARAMS["max_inventory_usd"],  # 在庫がこれ以上になったら、増やす側は出さない
    "min_imb": -0.5,  # 自分の側から見た板の偏り (最良気配 1 段) がこれ未満なら並ばない。-1 なら常に並ぶ
    "long_only": 0.0,  # 1 なら現物として動く: 売りは持っている分だけ (在庫 0 から売りで入らない)
}


def join(ctx: Context) -> Quote:
    """最良気配に並ぶ: 買いは最良買いと同じ値段、売りは最良売りと同じ値段に出す (列の後ろに付く。内側には出さない)。
    スプレッドが刻み 1 つに張り付いた板 (bitbank の主要な銘柄) のためのもの。ミッドからの幅も、在庫で値段をずらすことも無い
    (刻み 1 つの板ではずらす先が無い)。在庫は「上限で増やす側を止める」だけで抑える。

    並ぶのは自分の側の列が相手より薄すぎないときだけ: 板の偏り (買いが厚い = +) が、買いは min_imb 以上、売りは -min_imb 以下。
    薄い側の列は食い尽くされやすく、そこで約定するとそのまま値段が飛ぶ (bitbank の記録で、並んだ約定の 5 秒後の取り分は
    自分の側が薄いほど悪かった)。条件から外れたら指値を引くので、戻ったときは列の最後尾から並び直しになる。
    履歴も state も使わない"""
    book, params = ctx.book, ctx.params
    mid = book.mid
    inv_usd = ctx.inventory * mid
    cap = params["max_inventory_usd"]
    size = ask_size = params["size_usd"] / mid
    imb = book_imbalance(book)
    bid_ok = inv_usd < cap and imb >= params["min_imb"]
    ask_ok = inv_usd > -cap and -imb >= params["min_imb"]
    if params["long_only"] > 0:  # 現物: 持っている分より多くは売らない
        ask_size = min(size, max(ctx.inventory, 0.0))
        ask_ok = ask_ok and ask_size > 0
    return Quote(
        bid=book.best_bid if bid_ok else None,
        bid_size=size,
        ask=book.best_ask if ask_ok else None,
        ask_size=ask_size,
    )


QUOTERS: dict[str, tuple[Quoter, dict[str, float]]] = {
    "baseline": (baseline, BASELINE_PARAMS),
    "vol_spread": (vol_spread, VOL_PARAMS),
    "lean": (lean, LEAN_PARAMS),
    "vol_lean": (vol_lean, VOL_LEAN_PARAMS),
    "cooldown": (cooldown, COOLDOWN_PARAMS),
    "trade_pause": (trade_pause, TRADE_PAUSE_PARAMS),
    "join": (join, JOIN_PARAMS),
}


def parse_params(defaults: dict[str, float], overrides: list[str]) -> dict[str, float]:
    """["half_spread_bps=4", ...] を既定値に重ねる。知らない名前は打ち間違いなので止める"""
    out = dict(defaults)
    for item in overrides:
        key, sep, value = item.partition("=")
        if not sep or key not in defaults:
            raise ValueError(f"--param {item!r}: k=v の形で、k は {', '.join(defaults)} のどれか")
        out[key] = float(value)
    return out
