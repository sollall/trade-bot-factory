"""記録した板とテープから「指値を両側に出していたら何が起きたか」を組み立てる。

ここは**純関数だけ**。HTTP も sqlite も触らない (テストでフィクスチャを流し込めるように)。

測るのは依頼票の 1〜3:
  1. 最前列に並べ続けられるか  -> `best_price_runs`
  2. 約定するか              -> `simulate` (保守側 / 楽観側の 2 推定)
  3. 逆選択                   -> `adverse_loss_jpy`
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from tbf.jpmaker import declared

# 標本が途切れた扱いにする間隔。1 秒格子なので 3 秒空いたら「連続していない」とみなす。
MAX_SAMPLE_GAP_SEC = 3.0


@dataclass(frozen=True)
class BookSample:
    time: int  # ms
    bid: float
    bid_size: float
    ask: float
    ask_size: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def best(self, side: str) -> tuple[float, float]:
        """自分の side ("buy" なら bid 側) の最良気配 (値段, 数量)"""
        return (self.bid, self.bid_size) if side == "buy" else (self.ask, self.ask_size)


@dataclass(frozen=True)
class Trade:
    time: int  # ms (executed_at)
    side: str  # bitbank の side = テイカーの向き (declared.TAPE_SIDE_IS_TAKER)
    price: float
    amount: float

    @property
    def notional_jpy(self) -> float:
        return self.price * self.amount


@dataclass(frozen=True)
class Fill:
    """自分の指値が約定したと仮定した 1 件"""

    time: int
    pair: str
    side: str  # 自分の向き ("buy" = 買い指値が約定)
    price: float  # 自分の指値の値段
    notional_jpy: float
    mid_at_fill: float
    mode: str

    @property
    def half_spread_ratio(self) -> float:
        """中値に対して取れた半スプレッド (比)。買いなら中値より安く買えた分"""
        if self.mid_at_fill <= 0:
            return 0.0
        edge = (self.mid_at_fill - self.price) if self.side == "buy" else (self.price - self.mid_at_fill)
        return edge / self.mid_at_fill


@dataclass(frozen=True)
class Run:
    """最良気配の値段が変わらずに続いた区間"""

    start: int
    end: int
    price: float
    closed_by: str  # "change" = 値段が動いた / "gap" = 標本が途切れた / "end" = 記録の末尾

    @property
    def seconds(self) -> float:
        return (self.end - self.start) / 1000.0


@dataclass
class SimResult:
    fills: list[Fill] = field(default_factory=list)
    requotes: int = 0  # 置き直した回数 (値段が動いた + 約定しきって出し直した)
    armed_ms: int = 0  # 指値が板に乗っていた時間
    taker_notional_jpy: float = 0.0  # 指値が乗っている間に自分の値段に届いたテイカー注文の総額 (母数)
    unarmed_notional_jpy: float = 0.0  # 指値を乗せられていない間に同じ値段に届いた総額 (取り逃し)
    skipped_gap_ms: int = 0  # 標本が途切れて指値を乗せられなかった時間

    @property
    def filled_notional_jpy(self) -> float:
        return sum(f.notional_jpy for f in self.fills)


def best_price_runs(samples: list[BookSample], side: str, max_gap_sec: float = MAX_SAMPLE_GAP_SEC) -> list[Run]:
    """最良気配の値段が同じまま続いた区間を並べる (依頼票 1 の滞在時間)

    標本が `max_gap_sec` 以上途切れたら区間を打ち切り、`closed_by="gap"` を立てる。
    **穴をまたいで「続いていた」ことにしない。**
    """
    runs: list[Run] = []
    if not samples:
        return runs
    start = samples[0].time
    price = samples[0].best(side)[0]
    prev_t = samples[0].time
    for s in samples[1:]:
        if (s.time - prev_t) / 1000.0 > max_gap_sec:
            runs.append(Run(start, prev_t, price, "gap"))
            start, price, prev_t = s.time, s.best(side)[0], s.time
            continue
        p = s.best(side)[0]
        if p != price:
            runs.append(Run(start, s.time, price, "change"))
            start, price = s.time, p
        prev_t = s.time
    runs.append(Run(start, prev_t, price, "end"))
    return runs


def _is_aggressor_against(my_side: str, trade: Trade, quote_px: float) -> bool:
    """そのテイカー注文が自分の指値の値段に届いたか"""
    if my_side == "buy":
        return trade.side == "sell" and trade.price <= quote_px + 1e-12
    return trade.side == "buy" and trade.price >= quote_px - 1e-12


def simulate(
    pair: str,
    samples: list[BookSample],
    trades: list[Trade],
    side: str,
    size_jpy: float,
    mode: str,
    max_gap_sec: float = MAX_SAMPLE_GAP_SEC,
) -> SimResult:
    """最良気配と同値に `size_jpy` の指値を出し続けたときの約定を組み立てる

    mode:
      "conservative" 指値を置いた瞬間に見えていた数量が全部自分の前に居たとする
      "optimistic"   自分が先頭 (前に誰も居ない) とする

    値段が動いたら置き直す = **列の最後尾に戻る** (残っていた未約定分は消える)。
    約定しきった場合も、次の標本で出し直す (= 置き直し 1 回)。
    """
    if mode not in declared.FILL_MODES:
        raise ValueError(f"unknown mode: {mode}")
    res = SimResult()
    if not samples:
        return res

    sample_times = [s.time for s in samples]
    quote_px: float | None = None
    quote_mid = 0.0
    ahead_jpy = 0.0
    remaining_jpy = 0.0
    placed_at = 0

    ti = 0
    trades = sorted(trades, key=lambda t: t.time)
    prev_sample_t = samples[0].time

    for si, s in enumerate(samples):
        gap_ms = s.time - prev_sample_t
        if si > 0 and gap_ms / 1000.0 > max_gap_sec:
            # 記録に穴がある。その間に何が起きたか分からないので指値を降ろし、穴の時間を別に数える
            res.skipped_gap_ms += gap_ms
            quote_px = None
        elif si > 0 and quote_px is not None:
            res.armed_ms += gap_ms
        prev_sample_t = s.time

        px, sz = s.best(side)
        if quote_px is None or px != quote_px:
            if quote_px is not None:
                res.requotes += 1
            quote_px = px
            quote_mid = s.mid
            ahead_jpy = px * sz if mode == "conservative" else 0.0
            remaining_jpy = size_jpy
            placed_at = s.time
        else:
            quote_mid = s.mid
            # 同じ値段のまま。見えている数量が減ったぶんは自分の前の列が短くなったとみなす
            # (増えたぶんは自分より後ろに並んだ注文なので無視する)
            ahead_jpy = min(ahead_jpy, px * sz)

        # 次の標本までに起きた約定を処理する
        next_t = sample_times[si + 1] if si + 1 < len(sample_times) else None
        while ti < len(trades) and (next_t is None or trades[ti].time < next_t):
            t = trades[ti]
            ti += 1
            # 直前の標本から max_gap 以上あとの約定は、板がどうなっていたか分からない
            # (次の標本までが穴)。指値が乗っていたことにしない。
            fresh = (t.time - s.time) / 1000.0 <= max_gap_sec
            armed = quote_px is not None and remaining_jpy > 0 and t.time >= placed_at and fresh
            ref_px = quote_px if quote_px is not None else px
            if not _is_aggressor_against(side, t, ref_px):
                continue
            if not armed:
                # 指値が乗っていない間に来た注文。約定の母数には入れず、取り逃しとして別に数える
                res.unarmed_notional_jpy += t.notional_jpy
                continue
            notional = t.notional_jpy
            res.taker_notional_jpy += notional
            if mode == "conservative":
                eaten = min(ahead_jpy, notional)
                ahead_jpy -= eaten
                notional -= eaten
            if notional <= 0:
                continue
            fill = min(remaining_jpy, notional)
            res.fills.append(
                Fill(time=t.time, pair=pair, side=side, price=quote_px, notional_jpy=fill, mid_at_fill=quote_mid, mode=mode)
            )
            remaining_jpy -= fill
            if remaining_jpy <= 1e-9:
                # 出しきった。次の標本で最良気配に出し直す (= 列の最後尾から)
                quote_px = None
                res.requotes += 1
                break
    return res


def mid_at(samples: list[BookSample], times: list[int], target_ms: int, tolerance_sec: float) -> float | None:
    """`target_ms` に最も近い標本の中値。許容を超えて離れていたら None (= 未測定)

    **古い標本をそのまま返さない。**返してしまうと「値段が動かなかった」ことにされる
    (TRA-62 で踏んだ穴と同じ)。
    """
    if not samples:
        return None
    i = bisect_left(times, target_ms)
    best: tuple[float, float] | None = None
    for j in (i - 1, i):
        if 0 <= j < len(samples):
            d = abs(samples[j].time - target_ms) / 1000.0
            if best is None or d < best[0]:
                best = (d, samples[j].mid)
    if best is None or best[0] > tolerance_sec:
        return None
    return best[1]


def adverse_loss_jpy(fill: Fill, samples: list[BookSample], times: list[int], horizon_sec: int) -> float | None:
    """逆選択損 (JPY, **正なら損**)。未測定なら None

    買いが約定した直後に中値が下がったら、半スプレッドで取った分を値動きで失っている。
      損 = -(自分の向き) x (中値の変化率) x 約定金額
    """
    future = mid_at(samples, times, fill.time + horizon_sec * 1000, declared.adverse_tolerance_sec(horizon_sec))
    if future is None or fill.mid_at_fill <= 0:
        return None
    drift = (future - fill.mid_at_fill) / fill.mid_at_fill
    sign = 1.0 if fill.side == "buy" else -1.0
    return -sign * drift * fill.notional_jpy


def tape_side_sanity(samples: list[BookSample], times: list[int], trades: list[Trade]) -> dict[str, float]:
    """`side` がテイカーの向きだという宣言の検算

    side="sell" (成行売り) の約定は中値より **下** に寄っているはず。
    平均の符号が逆なら、テープの side の意味が逆で、買い側と売り側の約定率が入れ替わる。
    """
    out = {"sell_rel_bps": 0.0, "buy_rel_bps": 0.0, "n_sell": 0.0, "n_buy": 0.0, "n_unmatched": 0.0}
    acc = {"sell": [], "buy": []}
    for t in trades:
        mid = mid_at(samples, times, t.time, MAX_SAMPLE_GAP_SEC)
        if mid is None or t.side not in acc:
            out["n_unmatched"] += 1
            continue
        acc[t.side].append((t.price - mid) / mid * 10_000)
    for side in ("sell", "buy"):
        v = acc[side]
        out[f"n_{side}"] = float(len(v))
        out[f"{side}_rel_bps"] = sum(v) / len(v) if v else 0.0
    return out
