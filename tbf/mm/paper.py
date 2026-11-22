"""実況の paper: 気配と約定を記録しながら、同じクォーターをその場で動かして疑似的に約定を付ける。

発注はしない。鍵も要らない。約定の判定は再生と同じ Sim (tbf/mm/replay.py) だけを通るので、
paper が貯めた記録を後から `replay --run ID` で流すと、同じ結果になる。

そのために、気配は受け取ってすぐには処理しない。約定のポーリングは気配より間隔が長く、
気配 k を受け取った時点では「気配 k 以前の約定」がまだ全部は届いていないからである。
気配 k は、それを受け取った後に始めた約定のポーリングが成功してから処理する (= 確定)。
成績は実際の時計より最大で約定の間隔ぶん遅れて付くが、クォーターが見るのは気配 k と
その時点までの約定で決まる在庫だけで、再生と同じ (先読みしない)。

replay と食い違いうるのは次の 2 つで、起きた回数を paper_account に残す:
  - late_trades: 確定した気配より前の時刻の約定が、後から届いた。paper はもう通り過ぎているので使えない
    (replay は記録に残った時刻どおりに使う)
  - late_books: 処理済みの気配より前の時刻の気配が、後から届いた

止めて起動し直すと新しい run になり、在庫は 0 から始まる (前の run の在庫は引き継がない)。
記録 (books / trades) は同じ sqlite の続きに足される。
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Callable, Optional

from tbf.mm.book import Book, Trade
from tbf.mm.quoter import QUOTERS
from tbf.mm.replay import Fill, Result, Sim, add_markouts, format_result
from tbf.mm.store import MmStore
from tbf.mm.venues import Instrument

_STATS_SKIP = ("fills", "markouts", "edge_bps")
#: 窓 (--hours) で区切るとき、始点の値を引けば窓の中のぶんになる項目
_ADDITIVE = ("spread_pnl", "inventory_pnl", "fees", "area", "abs_area", "observed_ms", "books", "breaks",
             "placed", "canceled", "rejected", "too_small", "market_volume_usd")


@dataclasses.dataclass(frozen=True)
class RunConfig:
    id: int
    started: int
    stopped: Optional[int]
    coins: list[str]
    quoter: str
    params: dict[str, float]
    maker_rate: float
    latency_ms: int
    cross_fills: bool
    interval: float
    trades_interval: float
    depth: int
    #: 銘柄ごとのメイカー手数料率 (会場の値を使うと銘柄で違う)。無い銘柄は maker_rate
    maker_rates: dict = dataclasses.field(default_factory=dict)

    def rate(self, coin: str) -> float:
        return self.maker_rates.get(coin, self.maker_rate)


def load_run(store: MmStore, run: Optional[int] = None) -> Optional[RunConfig]:
    """run を省くと最後の run"""
    rows = store.query(
        "SELECT * FROM paper_runs WHERE id = COALESCE(?, (SELECT MAX(id) FROM paper_runs))", (run,))
    if not rows:
        return None
    r = rows[0]
    return RunConfig(
        r["id"], r["started"], r["stopped"], r["coins"].split(","), r["quoter"], json.loads(r["params"]),
        r["maker_rate"], r["latency_ms"], bool(r["cross_fills"]), r["interval"], r["trades_interval"], r["depth"],
        {f["coin"]: f["maker_rate"] for f in store.query("SELECT coin, maker_rate FROM paper_run_fees WHERE run = ?", (r["id"],))},
    )


class Paper:
    """Recorder の on_book / on_trades につなぐ。1 銘柄に 1 つの Sim を持つ"""

    def __init__(
        self,
        store: MmStore,
        coins: list[str],
        quoter: str,
        params: dict[str, float],
        *,
        maker_rate: Optional[float] = None,
        latency_ms: int,
        cross_fills: bool = True,
        interval: float = 2.0,
        trades_interval: float = 10.0,
        depth: int = 10,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        log: Callable[[str], None] = print,
    ) -> None:
        self.store, self.coins, self.log, self.clock = store, coins, log, clock
        # maker_rate を省くと、銘柄ごとに会場の値 (記録を始めたときに API から取ったもの) を使う
        self.rates = {c: self._instrument(c).maker_rate if maker_rate is None else maker_rate for c in coins}
        fn = QUOTERS[quoter][0]
        self._make = lambda coin: Sim(
            fn, params, instrument=self._instrument(coin), maker_rate=self.rates[coin],
            latency_ms=latency_ms, cross_fills=cross_fills,
        )
        self.sims: dict[str, Sim] = {}
        self.first_book: dict[str, int] = {}
        self.books: dict[str, list[tuple[int, Book]]] = {c: [] for c in coins}  # まだ確定していない (受け取った時刻, 気配)
        self.trades: dict[str, list[Trade]] = {c: [] for c in coins}  # まだ使っていない約定
        self.late_trades = {c: 0 for c in coins}
        self.late_books = {c: 0 for c in coins}
        self.broken: set[str] = set()
        self.run = store.conn.execute(
            "INSERT INTO paper_runs (started, coins, quoter, params, maker_rate, latency_ms, cross_fills, interval, trades_interval, depth)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (clock(), ",".join(coins), quoter, json.dumps(params), self.rates[coins[0]], latency_ms, int(cross_fills),
             interval, trades_interval, depth),
        ).lastrowid
        store.conn.executemany("INSERT INTO paper_run_fees VALUES (?,?,?)", [(self.run, c, r) for c, r in self.rates.items()])
        store.commit()

    def _instrument(self, coin: str) -> Instrument:
        inst = self.store.instrument(coin)
        if inst is None:
            raise ValueError(f"{coin} の銘柄の情報が記録に無い (Recorder.setup を先に呼ぶ)")
        return inst

    def on_book(self, coin: str, book: Book, recv: int) -> None:
        if coin not in self.broken:
            self.books[coin].append((recv, book))

    def on_trades(self, coin: str, new: list[Trade], started: int) -> None:
        if coin in self.broken:
            return
        self.trades[coin].extend(new)
        try:
            self._settle(coin, started)
        except Exception as e:  # noqa: BLE001  記録は続ける。成績は信用できないので、この銘柄の paper は止める
            self.broken.add(coin)
            self.log(f"[{coin}] paper を止めました (記録は続けます): {type(e).__name__}: {e}")

    def _settle(self, coin: str, started: int) -> None:
        """started より前に受け取った気配を確定する。それ以前の約定は、もう全部届いているとみなせる"""
        ready = sorted((b for recv, b in self.books[coin] if recv <= started), key=lambda b: b.time)
        if not ready:
            return
        self.books[coin] = [(recv, b) for recv, b in self.books[coin] if recv > started]
        sim = self.sims.get(coin) or self.sims.setdefault(coin, self._make(coin))
        for book in ready:
            if sim.prev and book.time <= sim.prev.time:
                self.late_books[coin] += 1
                continue
            batch = [t for t in self.trades[coin] if t.time <= book.time]
            self.trades[coin] = [t for t in self.trades[coin] if t.time > book.time]
            if sim.prev:
                late = sum(1 for t in batch if t.time <= sim.prev.time)
                self.late_trades[coin] += late
                batch = [t for t in batch if t.time > sim.prev.time]
            else:
                batch = []  # 最初の気配より前の約定は、当てる指値が無い
                self.first_book[coin] = book.time
            for f in sim.step(book, batch):
                self.store.conn.execute(
                    "INSERT INTO paper_fills VALUES (?,?,?,?,?,?,?,?,?)",
                    (self.run, coin, f.time, f.side, f.px, f.size, f.kind, self.rates[coin] * f.px * f.size, f.mid),
                )
        self._snapshot(coin)

    def _snapshot(self, coin: str) -> None:
        sim = self.sims[coin]
        r = sim.res
        stats = {k: v for k, v in dataclasses.asdict(r).items() if k not in _STATS_SKIP}
        t = sim.prev.time  # type: ignore[union-attr]
        self.store.conn.execute(
            "INSERT OR REPLACE INTO paper_account VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run, coin, t // 60_000 * 60_000, t, self.first_book[coin], r.position, r.cash, r.fees, r.last_mid,
             r.total_pnl, self.late_trades[coin], self.late_books[coin], json.dumps(stats)),
        )

    def stop(self) -> None:
        self.store.conn.execute("UPDATE paper_runs SET stopped = ? WHERE id = ?", (self.clock(), self.run))
        self.store.commit()


# ---------------------------------------------------------------- 成績の読み出し


def _stats(row) -> Result:
    return Result(**json.loads(row["stats"]))


def run_span(store: MmStore, run: int, coin: str) -> Optional[tuple[int, int]]:
    """この run がその銘柄で処理した気配の (最初, 最後) の時刻。replay --run はこの区間を流す"""
    rows = store.query(
        "SELECT first_book, time FROM paper_account WHERE run = ? AND coin = ? ORDER BY minute DESC LIMIT 1", (run, coin))
    return (rows[0][0], rows[0][1]) if rows else None


def run_result(store: MmStore, run: int, coin: str, since: int = 0, horizons: tuple[int, ...] = (5, 30, 60)) -> Optional[Result]:
    """paper の成績を、再生と同じ Result の形に戻す。since より後のぶんだけにもできる"""
    last = store.query(
        "SELECT * FROM paper_account WHERE run = ? AND coin = ? ORDER BY minute DESC LIMIT 1", (run, coin))
    if not last:
        return None
    res = _stats(last[0])
    base = store.query(
        "SELECT * FROM paper_account WHERE run = ? AND coin = ? AND time < ? ORDER BY minute DESC LIMIT 1", (run, coin, since))
    start = last[0]["first_book"]
    if base:
        b = _stats(base[0])
        start = base[0]["time"]
        for k in _ADDITIVE:
            setattr(res, k, getattr(res, k) - getattr(b, k))
        res.cash -= b.cash + b.position * b.last_mid  # 始点の在庫は始点のミッドで買ったものとして数える
    res.fills = [
        Fill(r["time"], r["side"], r["px"], r["size"], r["mid"], r["kind"])
        for r in store.query(
            "SELECT * FROM paper_fills WHERE run = ? AND coin = ? AND time > ? ORDER BY time, rowid", (run, coin, start))
    ]
    if base:
        pos, res.max_abs_position, res.max_abs_position_usd = b.position, abs(b.position), abs(b.position) * b.last_mid
        for f in res.fills:
            pos += f.side * f.size
            res.max_abs_position = max(res.max_abs_position, abs(pos))
            res.max_abs_position_usd = max(res.max_abs_position_usd, abs(pos) * f.mid)
    add_markouts(res, store.books(coin, start), horizons)
    return res


def _buckets(store: MmStore, run: int, coins: list[str], width: int, since: int) -> dict[int, list[float]]:
    """width (ms) ごとの [損益, 約定の件数, 約定代金]。損益は口座のスナップショットの差 (評価損益を含む)"""
    out: dict[int, list[float]] = {}
    for coin in coins:
        prev = 0.0
        for r in store.query("SELECT time, equity FROM paper_account WHERE run = ? AND coin = ? ORDER BY minute", (run, coin)):
            if r["time"] >= since:
                out.setdefault(r["time"] // width, [0.0, 0, 0.0])[0] += r["equity"] - prev
            prev = r["equity"]
        # 窓の始点は run_result と同じ (since より前の最後のスナップショット)。約定の数え方を揃える
        base = store.query(
            "SELECT COALESCE(MAX(time), 0) FROM paper_account WHERE run = ? AND coin = ? AND time < ?", (run, coin, since))[0][0]
        for r in store.query("SELECT time, px, size FROM paper_fills WHERE run = ? AND coin = ? AND time > ?", (run, coin, base)):
            b = out.setdefault(r["time"] // width, [0.0, 0, 0.0])
            b[1] += 1
            b[2] += r["px"] * r["size"]
    return out


def _utc(ms: int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return time.strftime(fmt, time.gmtime(ms / 1000))


def build_paper_report(
    store: MmStore, run: Optional[int] = None, hours: Optional[float] = None, *, taker_rate: float,
    horizons: tuple[int, ...] = (5, 30, 60), brief: bool = False,
) -> str:
    """brief は Webhook 用 (銘柄ごとの詳しい内訳を省く)"""
    cfg = load_run(store, run)
    if cfg is None:
        return "(paper の run がありません)" if run is None else f"(run {run} がありません)"
    spans = {c: run_span(store, cfg.id, c) for c in cfg.coins}
    end = max((s[1] for s in spans.values() if s), default=0)
    since = 0 if hours is None else end - int(hours * 3_600_000)
    insts = {c: store.instrument(c) for c in cfg.coins}
    first = next((i for i in insts.values() if i), None)
    sym, quote = (first.symbol, first.quote) if first else ("$", "USD")
    rates = {cfg.rate(c) for c in cfg.coins}
    fee_text = (f"{rates.pop() * 10_000:+.2f} bps" if len(rates) == 1
                else " ".join(f"{c} {cfg.rate(c) * 10_000:+.2f}" for c in cfg.coins) + " bps")
    lines = [
        f"== paper run {cfg.id}  {', '.join(cfg.coins)}  クォーター {cfg.quoter}  "
        + " ".join(f"{k}={v:g}" for k, v in cfg.params.items()),
        f"開始 {_utc(cfg.started)} UTC  " + (f"停止 {_utc(cfg.stopped)} UTC" if cfg.stopped else "停止の記録なし (動作中か、異常終了)")
        + f"  処理した気配の末尾 {_utc(end) if end else '-'} UTC",
        f"メイカー手数料 {fee_text}  板に載るまで {cfg.latency_ms} ms  "
        f"約定の仮定 (c) {'あり' if cfg.cross_fills else 'なし (甘い側)'}  "
        f"気配 {cfg.interval:g} 秒 / 約定 {cfg.trades_interval:g} 秒" + (f"  ※ 末尾から {hours:g} 時間ぶん" if hours is not None else ""),
    ]
    others = store.query("SELECT COUNT(*) FROM paper_runs")[0][0] - 1
    if others:
        lines.append(f"(ほかに {others} 個の run があります。--run ID で選べます。run どうしの成績は足しません)")
    total = [0.0, 0.0, 0.0, 0.0, 0, 0.0]  # 取り分, 手数料, 在庫, 合計, 約定の件数, 約定代金
    details = []
    lines.append("\n銘柄      約定 (a)/(b)/(c)    代金   取り分   手数料     在庫     合計   瞬間→5秒後(bps)  最後の在庫  食い違い")
    for coin in cfg.coins:
        res = run_result(store, cfg.id, coin, since, horizons)
        if res is None:
            lines.append(f"{coin:<8}  (まだ確定した気配がありません)")
            continue
        acct = store.query(
            "SELECT late_trades, late_books FROM paper_account WHERE run = ? AND coin = ? ORDER BY minute DESC LIMIT 1", (cfg.id, coin))[0]
        k = [sum(1 for f in res.fills if f.kind == kind) for kind in ("through", "queue", "cross")]
        mo = res.markouts.get(min(horizons), (float("nan"), 0))[0]
        lines.append(
            f"{coin:<8} {len(res.fills):>4} {k[0]:>3}/{k[1]}/{k[2]:<3} {sym}{res.volume_usd:>8,.0f} {res.spread_pnl:+8.3f} {res.fee_pnl + 0.0:+8.3f} "
            f"{res.inventory_pnl:+8.3f} {res.total_pnl:+8.3f}   {res.edge_bps:+5.2f} → {mo:+5.2f}   {sym}{res.position * res.last_mid:+8,.0f}  "
            f"遅れた約定 {acct['late_trades']} / 気配 {acct['late_books']}"
        )
        for i, v in enumerate((res.spread_pnl, res.fee_pnl, res.inventory_pnl, res.total_pnl, len(res.fills), res.volume_usd)):
            total[i] += v
        span = spans[coin]
        details.append(format_result(
            coin, res, cfg.params, maker_rate=cfg.rate(coin), latency_ms=cfg.latency_ms,
            taker_rate=insts[coin].taker_rate if insts[coin] else taker_rate, instrument=insts[coin],
            trade_gaps=store.gap_count(coin, max(since, span[0]), span[1]), quoter=cfg.quoter, cross_fills=cfg.cross_fills,
        ).replace("== 再生 ", "== paper ", 1))
    lines.append(
        f"{'合計':<6} {total[4]:>4}           {sym}{total[5]:>8,.0f} {total[0]:+8.3f} {total[1] + 0.0:+8.3f} {total[2]:+8.3f} {total[3]:+8.3f}"
        f"   ({quote}。ファンディングと、残った在庫を閉じる費用は未計上)"
    )

    lines.append("\n日ごと (UTC)              損益    約定    代金")
    for b, (pnl, n, usd) in sorted(_buckets(store, cfg.id, cfg.coins, 86_400_000, since).items()):
        lines.append(f"  {_utc(b * 86_400_000, '%Y-%m-%d')}        {pnl:+9.3f}  {n:>5}  {sym}{usd:>9,.0f}")
    by_hour: dict[int, list[float]] = {}
    for b, vals in _buckets(store, cfg.id, cfg.coins, 3_600_000, since).items():
        agg = by_hour.setdefault(b % 24, [0.0, 0, 0.0, 0])
        for i in range(3):
            agg[i] += vals[i]
        agg[3] += 1
    lines.append("\n時間帯ごと (UTC の時)       損益    約定    代金   (その時を含んだ回数)")
    for h, (pnl, n, usd, days) in sorted(by_hour.items()):
        lines.append(f"  {h:02d} 時             {pnl:+9.3f}  {n:>5}  {sym}{usd:>9,.0f}   ({days})")
    lines.append("  ※ 損益は口座の評価額の差 (持っている在庫の値動きを含む)。1 分ごとのスナップショットから出している")
    if not brief:
        lines += ["", *("\n\n".join(details).split("\n"))]
    return "\n".join(lines)

