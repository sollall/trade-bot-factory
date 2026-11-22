import json
import random
import re

import pytest

import tbf.http
from tbf.mm.book import Book, Trade, price_decimals, round_down, round_up
from tbf.mm.quoter import BASELINE_PARAMS, HISTORY_SEC, Context, Quote, baseline, parse_params
from tbf.mm.recorder import WEIGHT_LIMIT_PER_MIN, Recorder, check_budget, weight_per_min
from tbf.mm.replay import BUY, SELL, Sim, format_result, simulate
from tbf.mm.report import build_report
from tbf.mm.store import MmStore

# ---------------------------------------------------------------- 記録


class FakeHyperliquid:
    """info の応答を本物の形で真似る (2026-11 に実物で確かめた形):
    値段と数量は文字列、気配は片側 20 段、recentTrades は直近の 10 件だけで並び順は当てにならない"""

    def __init__(self):
        self.books = {"INJ": (5.8908, 5.8929)}
        self.trades = {"INJ": []}  # これまでに成立した約定のすべて (古い順)
        self.status = 200
        self.time = 1_000_000
        self.calls = []

    def trade(self, coin, n, side="A", px=5.8908):
        for _ in range(n):
            self.time += 7
            self.trades[coin].append({
                "coin": coin, "side": side, "px": str(px), "sz": "1.5", "time": self.time,
                "hash": "0x00", "tid": 10_000 + len(self.trades[coin]), "users": ["0xa", "0xb"],
            })

    async def request(self, method, url, headers=None, body=None):
        req = json.loads(body)
        self.calls.append(req["type"])
        if self.status != 200:
            return self.status, "rate limited"
        if req["type"] == "meta":
            return 200, json.dumps({"universe": [{"name": "INJ", "szDecimals": 1}, {"name": "OLD", "szDecimals": 0, "isDelisted": True}]})
        if req["type"] == "l2Book":
            bid, ask = self.books[req["coin"]]
            self.time += 1
            levels = [
                [{"px": f"{bid - i * 0.0001:.4f}", "sz": "67.9", "n": 1} for i in range(20)],
                [{"px": f"{ask + i * 0.0001:.4f}", "sz": "30.5", "n": 2} for i in range(20)],
            ]
            return 200, json.dumps({"coin": req["coin"], "time": self.time, "levels": levels})
        if req["type"] == "recentTrades":
            return 200, json.dumps(self.trades[req["coin"]][-10:][::-1])
        raise AssertionError(req)


def recorder(tmp_path, hl, **kw):
    store = MmStore(tmp_path / "mm.sqlite")
    return Recorder(hl, store, ["INJ"], clock=lambda: hl.time + 300, log=lambda m: None, **kw), store


async def test_record_dedupes_trades_and_keeps_top_levels(tmp_path):
    hl = FakeHyperliquid()
    rec, store = recorder(tmp_path, hl, depth=5)
    await rec.setup()
    assert store.sz_decimals("INJ") == 1
    hl.trade("INJ", 6)
    await rec.poll_trades()
    hl.trade("INJ", 4)  # 次の応答は 10 件で、うち 6 件は前回と同じ
    await rec.poll_trades()
    await rec.poll_trades()  # 何も増えていない
    assert store.query("SELECT COUNT(*) FROM trades")[0][0] == 10
    assert [r["new_rows"] for r in store.query("SELECT new_rows FROM polls ORDER BY rowid")] == [6, 4, 0]
    assert not store.query("SELECT * FROM gaps")

    await rec.poll_books()
    (book,) = store.books("INJ")
    assert len(book.bids) == len(book.asks) == 5  # 20 段のうち上位だけ残す
    assert (book.best_bid, book.best_ask, book.bids[0][1]) == (5.8908, 5.8929, 67.9)
    assert book.bids[1][0] < book.best_bid and book.asks[1][0] > book.best_ask


async def test_record_flags_possible_missed_trades(tmp_path):
    hl = FakeHyperliquid()
    rec, store = recorder(tmp_path, hl)
    hl.trade("INJ", 3)
    await rec.poll_trades()  # 初回は比べる相手が無いので取りこぼし扱いにしない
    last_seen = hl.time
    hl.trade("INJ", 25)  # 10 件しか返らないので 15 件は見えない
    await rec.poll_trades()
    (gap,) = store.query("SELECT * FROM gaps")
    assert gap["coin"] == "INJ" and gap["after"] == last_seen
    assert gap["before"] == hl.trades["INJ"][-10]["time"]  # 抜けたのはこの 2 つの時刻の間
    assert store.query("SELECT COUNT(*) FROM trades")[0][0] == 13
    hl.trade("INJ", 2)
    await rec.poll_trades()  # 重なりがあれば取りこぼしではない
    assert len(store.query("SELECT * FROM gaps")) == 1 and rec.gaps == 1

    # 止めて起動し直した場合も、止まっていた間の取りこぼしが分かる
    hl.trade("INJ", 30)
    rec2 = Recorder(hl, store, ["INJ"], clock=lambda: hl.time, log=lambda m: None)
    await rec2.poll_trades()
    assert len(store.query("SELECT * FROM gaps")) == 2


async def test_record_survives_rate_limit_and_logs_it(tmp_path, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(tbf.http.asyncio, "sleep", no_sleep)  # 取り直しの待ち時間を飛ばす
    hl = FakeHyperliquid()
    rec, store = recorder(tmp_path, hl)
    hl.status = 429
    await rec.poll_books()
    await rec.poll_trades()
    errors = store.query("SELECT kind, error, latency_ms FROM polls ORDER BY rowid")
    assert [e["kind"] for e in errors] == ["l2Book", "recentTrades"]
    assert all("429" in e["error"] and e["latency_ms"] is None for e in errors)
    assert rec.rate_limited  # run() はこれを見てしばらく休む
    hl.status = 200
    await rec.poll_books()
    assert len(store.books("INJ")) == 1


async def test_record_rejects_unknown_or_delisted_coin(tmp_path):
    hl = FakeHyperliquid()
    store = MmStore(tmp_path / "mm.sqlite")
    with pytest.raises(ValueError, match="OLD"):
        await Recorder(hl, store, ["INJ", "OLD"], log=lambda m: None).setup()


def test_weight_budget_stops_too_fast_settings():
    # 気配 2 + 約定 20。3 銘柄を 3 秒 / 8 秒なら 3 * (40 + 150) = 570
    assert weight_per_min(3, 3, 8) == 570
    assert check_budget(3, 3, 8) == 570
    with pytest.raises(ValueError, match="流量制限"):
        check_budget(5, 2, 5)  # 1500 > 上限の半分
    with pytest.raises(ValueError, match="流量制限"):
        check_budget(20, 1, 1, budget=1.0)  # 上限いっぱいまで許しても超える
    with pytest.raises(ValueError):
        check_budget(1, 5, 10, budget=1.5)  # 上限より多くは許さない
    assert weight_per_min(3, 2, 10) <= WEIGHT_LIMIT_PER_MIN / 2 < weight_per_min(4, 2, 10)  # 既定の間隔なら 3 銘柄まで


# ---------------------------------------------------------------- 再生

SEC = 1000


def flat_books(n=4, bid=99.9, ask=100.1, sz=100.0, step=10 * SEC):
    return [Book(i * step, ((bid, sz),), ((ask, sz),)) for i in range(n)]


def fixed(bid=None, ask=None, size=10.0):
    return lambda ctx: Quote(bid, size, ask, size)


def run(books, trades, quoter, **kw):
    kw.setdefault("latency_ms", 0)
    return simulate(books, trades, quoter, {}, sz_decimals=1, **kw)


def test_price_rounding_follows_tick_rule():
    assert price_decimals(5.8908, 1) == 4  # 有効 5 桁
    assert price_decimals(0.013183, 0) == 6  # 小数は 6 - szDecimals 桁まで
    assert price_decimals(0.0131, 2) == 4
    assert price_decimals(65432.1, 5) == 0
    assert round_down(5.89085, 4) == 5.8908 and round_up(5.89081, 4) == 5.8909
    assert round_down(5.8908, 4) == 5.8908 == round_up(5.8908, 4)  # 刻みちょうどは動かさない


def test_fill_a_only_when_trade_goes_through_and_only_its_size():
    # 買い 99.5 は記録した気配 (1 段) より外側。列が分からないので、値段を抜けた約定でしか約定しない
    books = flat_books()
    no_fill = [
        Trade(5 * SEC, "A", 99.6, 50),  # 自分の値段まで届いていない
        Trade(6 * SEC, "B", 99.4, 50),  # 買い手主導の約定は買い指値に当たらない
        Trade(7 * SEC, "A", 99.5, 50),  # ちょうどの値段。前の列が分からないので約定させない
    ]
    assert run(books, no_fill, fixed(bid=99.5)).fills == []

    res = run(books, no_fill + [Trade(8 * SEC, "A", 99.4, 3)], fixed(bid=99.5))
    (f,) = res.fills
    # 約定するのは自分の値段で、数量は抜けた約定の数量まで (10 枚出していても 3 枚)
    assert (f.side, f.px, f.size, f.kind, f.time) == (BUY, 99.5, 3, "through", 8 * SEC)

    # 売りは逆
    res = run(books, [Trade(5 * SEC, "B", 100.4, 50), Trade(6 * SEC, "A", 100.6, 50), Trade(7 * SEC, "B", 100.6, 4)], fixed(ask=100.5))
    (f,) = res.fills
    assert (f.side, f.px, f.size, f.kind) == (SELL, 100.5, 4, "through")


def test_fill_b_waits_for_queue_ahead():
    # 買い 99.9 は最良気配と同じ値段。置いた時点で 100 枚が前に並んでいる
    books = flat_books()
    res = run(books, [Trade(2 * SEC, "A", 99.9, 60)], fixed(bid=99.9))
    assert res.fills == []  # 前の列を 60 消化しただけ
    res = run(books, [Trade(2 * SEC, "A", 99.9, 60), Trade(3 * SEC, "A", 99.9, 45)], fixed(bid=99.9))
    (f,) = res.fills
    assert (f.px, f.size, f.kind) == (99.9, 5, "queue")  # 残りの列 40 の後ろの 5 だけ
    res = run(books, [Trade(2 * SEC, "A", 99.9, 60), Trade(3 * SEC, "A", 99.9, 45), Trade(4 * SEC, "A", 99.9, 100)], fixed(bid=99.9))
    assert [f.size for f in res.fills] == [5, 5]  # 出した 10 枚より多くは約定しない

    # 最良気配より内側なら前に誰もいない
    res = run(books, [Trade(2 * SEC, "A", 99.95, 4)], fixed(bid=99.95))
    assert [(f.size, f.kind) for f in res.fills] == [(4, "queue")]

    # 同じ時刻の約定は、成行が板を食い進む順 (売り手主導なら高い値段から) に流す。
    # 逆順だと、値段を抜けた約定で列を 0 にしてから p ちょうどの約定を全部自分のものにしてしまう
    sweep = [Trade(2 * SEC, "A", 99.8, 2), Trade(2 * SEC, "A", 99.9, 100)]
    assert [(f.size, f.kind) for f in run(books, sweep, fixed(bid=99.9)).fills] == [(2, "through")]


def test_requote_goes_to_back_of_queue():
    books = flat_books(n=5)
    trades = [Trade(5 * SEC, "A", 99.9, 95), Trade(35 * SEC, "A", 99.9, 50)]
    # 値段を変えなければ列の位置は保たれる: 95 消化して残り 5、次の 50 で約定
    assert [f.size for f in run(books, trades, fixed(bid=99.9)).fills] == [10]

    # 途中で 1 回値段を変えて戻すと最後尾 (前に 100) からやり直し。50 では届かない
    def moving(ctx):
        return Quote(bid=99.8 if ctx.now == 10 * SEC else 99.9, bid_size=10)

    res = run(books, trades, moving)
    assert res.fills == []
    assert (res.placed, res.canceled) == (3, 2)


def test_fill_c_when_book_crosses_resting_order():
    # 約定の記録が抜けていても、次の気配が自分の指値を抜けていたら約定とみなす
    books = [Book(0, ((99.9, 100),), ((100.1, 100),)), Book(10 * SEC, ((99.0, 100),), ((99.2, 100),)), Book(20 * SEC, ((99.0, 100),), ((99.2, 100),))]
    res = run(books, [], fixed(bid=99.9))
    (f,) = res.fills
    assert (f.px, f.size, f.kind, f.time) == (99.9, 10, "cross", 10 * SEC)
    assert res.inventory_pnl == pytest.approx(10 * (99.1 - 100.0))  # 抜けた分はそのまま在庫の損
    # 甘い側の比較用に (c) を外せる。外すと、約定の記録が無いかぎり約定しない
    assert run(books, [], fixed(bid=99.9), cross_fills=False).fills == []
    text = format_result("INJ", res, {}, maker_rate=0.00015, taker_rate=0.00045, latency_ms=0, trade_gaps=2)
    assert "(c) 気配が抜けていた    1 件" in text and "取りこぼしの可能性 2 回" in text
    assert "ファンディング          未計上" in text and "内訳の和との差 +0.000000" in text


def test_post_only_and_minimum_size():
    books = flat_books()
    res = run(books, [Trade(5 * SEC, "A", 99.0, 50)], fixed(bid=100.1))  # 最良売りに当たる買いは出せない
    assert res.fills == [] and res.placed == 0 and res.rejected == len(books)
    res = run(books, [Trade(5 * SEC, "A", 99.0, 50)], fixed(bid=99.9, size=0.05))  # 0.1 枚未満は 0 に丸まる
    assert res.fills == [] and res.placed == 0 and res.too_small == len(books)


def test_pnl_breakdown_adds_up():
    # 手で追える例: ミッド 100 のとき 99.9 で 10 枚買い、ミッドが 99 に下がる
    books = [Book(0, ((99.9, 100),), ((100.1, 100),)), Book(10 * SEC, ((99.9, 100),), ((100.1, 100),)),
             Book(20 * SEC, ((98.9, 100),), ((99.1, 100),))]
    once = lambda ctx: Quote(bid=99.9 if ctx.inventory == 0 else None, bid_size=10)  # noqa: E731
    res = run(books, [Trade(5 * SEC, "A", 99.8, 10)], once, maker_rate=0.00015)
    assert len(res.fills) == 1 and res.spread_pnl == pytest.approx(1.0)  # (100 - 99.9) * 10
    assert res.fee_pnl == pytest.approx(-99.9 * 10 * 0.00015)
    assert res.inventory_pnl == pytest.approx(-10.0)  # 10 枚 * (99 - 100)
    assert res.total_pnl == pytest.approx(10 * (99 - 99.9) - 99.9 * 10 * 0.00015)
    assert res.total_pnl == pytest.approx(res.spread_pnl + res.fee_pnl + res.inventory_pnl)
    assert res.position == 10 and res.max_abs_position == 10
    assert res.avg_position == pytest.approx(10 * 15 / 20)  # 20 秒のうち後ろの 15 秒持っていた
    # リベート (負の料率) なら手数料は受け取り
    assert run(books, [Trade(5 * SEC, "A", 99.8, 10)], once, maker_rate=-0.0001).fee_pnl > 0

    # 乱数の相場でも、現金の出入りから出した合計と内訳の和が一致する
    rng = random.Random(7)
    mid, books, trades = 100.0, [], []
    for i in range(400):
        mid = round(mid + rng.choice([-0.05, 0, 0, 0.05]), 2)
        bids = tuple((round(mid - 0.02 - 0.01 * j, 2), 20.0) for j in range(10))
        asks = tuple((round(mid + 0.02 + 0.01 * j, 2), 20.0) for j in range(10))
        books.append(Book(i * 3 * SEC, bids, asks))
        for _ in range(rng.randint(0, 3)):
            side = rng.choice("AB")
            px = round(mid + (0.02 if side == "B" else -0.02) + rng.choice([-0.02, -0.01, 0, 0.01]), 2)
            trades.append(Trade(i * 3 * SEC + rng.randint(1, 2999), side, px, rng.choice([5.0, 30.0])))
    res = simulate(books, trades, baseline, {**BASELINE_PARAMS, "half_spread_bps": 2}, sz_decimals=1, latency_ms=250)
    assert res.count(BUY) > 5 and res.count(SELL) > 5
    assert {f.kind for f in res.fills} >= {"through", "queue"}
    assert res.total_pnl == pytest.approx(res.spread_pnl + res.fee_pnl + res.inventory_pnl, abs=1e-6)
    assert res.fee_pnl == pytest.approx(-res.volume_usd * 0.00015)
    # 在庫の上限を超えた側は出さないので、超えるのは最後の 1 本ぶんまで
    assert res.max_abs_position_usd <= BASELINE_PARAMS["max_inventory_usd"] + BASELINE_PARAMS["size_usd"] * 1.01
    assert set(res.markouts) == {5, 30, 60}


def test_no_lookahead():
    # 気配 1 (t=10s) で相場が上がり、買い指値は 99.9 -> 100.9 に上がる
    books = [Book(0, ((99.9, 100),), ((100.1, 100),)), Book(10 * SEC, ((100.9, 100),), ((101.1, 100),)),
             Book(20 * SEC, ((100.9, 100),), ((101.1, 100),))]
    seen = []

    def follow(ctx):
        seen.append(ctx.now)
        return Quote(bid=ctx.book.best_bid, bid_size=10)

    # 9 秒の時点の売り 100.5 は、まだ 99.9 の指値にしか出会わない。10 秒の気配で決めた 100.9 には当てない
    res = run(books, [Trade(9 * SEC, "A", 100.5, 50)], follow)
    assert res.fills == []
    assert seen == [0, 10 * SEC, 20 * SEC]  # クォーターが受け取るのは、その時点の気配 1 つだけ
    # 気配と同じ時刻の約定も、その気配で決めた指値には当てない (気配のほうが後とみなす)
    assert run(books, [Trade(10 * SEC, "A", 100.5, 50)], follow).fills == []
    # 気配の後の約定なら当たる
    assert len(run(books, [Trade(10 * SEC + 1, "A", 100.5, 50)], follow).fills) == 1

    # 板に載るまでの遅れの間は、新しい指値では約定しない
    assert run(books, [Trade(200, "A", 99.0, 50)], fixed(bid=99.9), latency_ms=500).fills == []
    assert len(run(books, [Trade(600, "A", 99.0, 50)], fixed(bid=99.9), latency_ms=500).fills) == 1

    # 先のデータを書き換えても、それより前の約定は変わらない
    rng = random.Random(3)
    books = [Book(i * 3 * SEC, ((99.9, 10.0),), ((100.1, 10.0),)) for i in range(100)]
    trades = [Trade(rng.randint(1, 299_000), rng.choice("AB"), rng.choice([99.9, 100.1]), 8.0) for _ in range(150)]
    cut = 150 * SEC
    past = [t for t in trades if t.time <= cut]
    crash = [Book(b.time, ((50.0, 10.0),), ((50.2, 10.0),)) if b.time > cut else b for b in books]
    a = simulate(books, trades, baseline, BASELINE_PARAMS, sz_decimals=1, latency_ms=250)
    b = simulate(crash, past, baseline, BASELINE_PARAMS, sz_decimals=1, latency_ms=250)
    assert [f for f in a.fills if f.time <= cut] == [f for f in b.fills if f.time <= cut] != []


def test_markout_measures_adverse_selection():
    # 99.9 で買った 5 秒後にミッドが 99.5 まで下がる
    books = [Book(0, ((99.9, 100),), ((100.1, 100),)), Book(3 * SEC, ((99.9, 100),), ((100.1, 100),)),
             Book(6 * SEC, ((99.4, 100),), ((99.6, 100),)), Book(9 * SEC, ((99.4, 100),), ((99.6, 100),))]
    res = run(books, [Trade(1 * SEC, "A", 99.8, 10)], fixed(bid=99.9), horizons=(5, 60))
    assert res.edge_bps == pytest.approx(0.1 / 99.9 * 10_000)  # 約定した瞬間は +10 bps
    bps, n = res.markouts[5]
    assert n == 1 and bps == pytest.approx((99.5 - 99.9) / 99.9 * 10_000)  # 5 秒後は -40 bps
    assert 60 not in res.markouts  # 記録が 60 秒後まで無い約定は数えない


def test_recording_break_cancels_quotes():
    # 気配が 10 分途切れた。その間の約定は判定できないので、指値は無かったことにする
    books = [Book(0, ((99.9, 100),), ((100.1, 100),)), Book(600 * SEC, ((99.9, 100),), ((100.1, 100),)),
             Book(603 * SEC, ((99.9, 100),), ((100.1, 100),))]
    res = run(books, [Trade(300 * SEC, "A", 99.0, 50)], fixed(bid=99.9))
    assert res.fills == [] and res.breaks == 1
    assert res.observed_ms == 3 * SEC  # 途切れていた時間は数えない
    assert len(run(books, [Trade(601 * SEC, "A", 99.0, 50)], fixed(bid=99.9)).fills) == 1


def test_baseline_quoter_skews_and_stops_at_inventory_limit():
    book = Book(0, ((99.9, 100),), ((100.1, 100),))
    p = parse_params(BASELINE_PARAMS, ["half_spread_bps=5", "skew_bps=4", "max_inventory_usd=1000", "size_usd=200"])
    flat = baseline(Context.of(book, 0, p))
    assert (flat.bid, flat.ask) == (pytest.approx(99.95), pytest.approx(100.05))
    assert flat.bid_size == flat.ask_size == pytest.approx(2.0)
    long = baseline(Context.of(book, 5, p))  # 500 ドルの買い越し = 上限の半分 -> 両方の値段を 2 bps 下げる
    assert long.bid == pytest.approx(99.95 * (1 - 0.0002)) and long.ask == pytest.approx(100.05 * (1 - 0.0002))
    assert baseline(Context.of(book, 10, p)).bid is None and baseline(Context.of(book, 10, p)).ask is not None  # 上限。買いは出さない
    assert baseline(Context.of(book, -10, p)).ask is None and baseline(Context.of(book, -10, p)).bid is not None
    with pytest.raises(ValueError, match="half_spread"):
        parse_params(BASELINE_PARAMS, ["half_spred_bps=5"])  # 打ち間違いは黙って無視しない


async def test_report_summarises_market(tmp_path):
    hl = FakeHyperliquid()
    rec, store = recorder(tmp_path, hl)
    await rec.setup()
    for _ in range(5):
        await rec.poll_books()
        hl.time += 3000
        hl.trade("INJ", 2, side="B", px=5.8929)
        await rec.poll_trades()
    await rec.poll_books()
    hl.trade("INJ", 30)
    await rec.poll_trades()
    report = build_report(store)
    spread = (5.8929 - 5.8908) / 5.89185 * 10_000
    assert "== INJ" in report and f"中央値 {spread:6.2f}" in report
    assert f"= {spread / 2 - 1.5:+.2f} bps" in report  # 片側スプレッド − メイカー手数料
    assert "記録した 10 件" in report  # 最後の気配より後の約定は数えない
    assert "取りこぼしの可能性  1 回" in report
    assert build_report(MmStore(tmp_path / "empty.sqlite")) == "(まだ記録がありません)"


# ---------------------------------------------------------------- 実況の paper

from tbf.mm.__main__ import main as mm_main  # noqa: E402
from tbf.mm.paper import Paper, build_paper_report, load_run, run_result, run_span  # noqa: E402
from tbf.mm.quoter import QUOTERS  # noqa: E402

PAPER_PARAMS = {**BASELINE_PARAMS, "half_spread_bps": 2.0}


async def run_paper(tmp_path, seed, cycles, store=None, quoter="baseline", params=PAPER_PARAMS, **kw):
    """乱数の相場でポーリングを回す。気配は約定より細かく取り、ときどき 10 件を超える約定を出す"""
    rng = random.Random(seed)
    hl = FakeHyperliquid()
    hl.time = 1_700_000_000_000 + seed * 100_000_000
    store = store or MmStore(tmp_path / "mm.sqlite")
    rec = Recorder(hl, store, ["INJ"], clock=lambda: hl.time + 300, log=lambda m: None)
    await rec.setup()
    paper = Paper(store, ["INJ"], quoter, params, maker_rate=0.00015, latency_ms=250,
                  clock=lambda: hl.time + 300, log=lambda m: None, **kw)
    rec.on_book, rec.on_trades = paper.on_book, paper.on_trades
    bid = 5.8908
    for _ in range(cycles):
        for _ in range(rng.randint(2, 5)):
            bid = round(bid + rng.choice([-0.0025, -0.0004, 0, 0, 0.0004, 0.0025]), 4)
            hl.books["INJ"] = (bid, round(bid + 0.0021, 4))
            await rec.poll_books()
            hl.time += rng.randint(300, 2500)
            for _ in range(rng.choice([0, 0, 1, 2, 4])):
                side = rng.choice("AB")
                px = round(bid + (0.0021 if side == "B" else 0) + rng.choice([-0.0015, -0.0008, 0, 0.0008, 0.0015]), 4)
                hl.trade("INJ", 1, side=side, px=px)
        if rng.random() < 0.1:
            hl.trade("INJ", 14, side="A", px=round(bid - 0.001, 4))  # 10 件しか返らないので一部は見えない
        await rec.poll_trades()
    hl.books["INJ"] = (round(bid + 0.0004, 4), round(bid + 0.0025, 4))
    await rec.poll_books()  # この後に約定のポーリングが無いので、この気配は確定しないまま止まる
    paper.stop()
    return store, paper


def fill_keys(fills):
    return [(f.time, f.side, f.px, f.size, f.kind, f.mid) for f in fills]


async def test_paper_matches_replay_of_its_own_recording(tmp_path, capsys):
    store, paper = await run_paper(tmp_path, seed=1, cycles=150)
    live = run_result(store, paper.run, "INJ")
    first, last = run_span(store, paper.run, "INJ")
    books = store.books("INJ", first, last)
    assert len(books) > 300 and len(store.books("INJ")) > len(books)  # 最後の数件はまだ確定していない
    replayed = simulate(books, store.trades("INJ", first, last), baseline, PAPER_PARAMS, sz_decimals=1, latency_ms=250)

    assert len(live.fills) > 20 and {f.kind for f in live.fills} == {"through", "queue", "cross"}
    assert fill_keys(live.fills) == fill_keys(replayed.fills)
    for k in ("spread_pnl", "inventory_pnl", "fee_pnl", "total_pnl", "position", "max_abs_position", "avg_abs_position",
              "market_volume_usd", "placed", "canceled", "observed_ms", "books"):
        assert getattr(live, k) == pytest.approx(getattr(replayed, k), abs=1e-9), k
    assert live.total_pnl == pytest.approx(live.spread_pnl + live.fee_pnl + live.inventory_pnl, abs=1e-9)
    assert store.query("SELECT COUNT(*) FROM gaps")[0][0] > 0  # 取りこぼしがあっても、同じ記録なら同じ結果
    acct = store.query("SELECT * FROM paper_account WHERE run = ? ORDER BY minute DESC LIMIT 1", (paper.run,))[0]
    assert (acct["late_trades"], acct["late_books"]) == (0, 0)
    assert acct["equity"] == pytest.approx(live.total_pnl) and acct["position"] == live.position
    # 約定ごとの手数料の和が口座の手数料と合う
    assert store.query("SELECT SUM(fee) FROM paper_fills")[0][0] == pytest.approx(acct["fees"])

    # コマンドでも同じ突き合わせができる
    assert await mm_main(["replay", "--db", str(tmp_path / "mm.sqlite"), "--run", str(paper.run)]) == 0
    assert f"paper run {paper.run} との突き合わせ: 一致" in capsys.readouterr().out


async def test_paper_report_breaks_down_by_day_and_hour(tmp_path):
    store, paper = await run_paper(tmp_path, seed=2, cycles=120)
    live = run_result(store, paper.run, "INJ")
    text = build_paper_report(store, taker_rate=0.00045)
    assert f"== paper run {paper.run}  INJ  クォーター baseline  half_spread_bps=2" in text
    assert "== paper INJ" in text and "ファンディング          未計上" in text and "内訳の和との差 +0.000000" in text
    assert "(c) 気配が抜けていた" in text and "秒後" in text
    # 日ごと・時間帯ごとの損益を足すと合計になる
    day = [l.split() for l in text.split("日ごと (UTC)")[1].split("時間帯ごと")[0].splitlines()[1:] if l.strip()]
    hour = [l.split() for l in text.split("時間帯ごと (UTC の時)")[1].splitlines()[1:] if re.match(r"\s+\d\d 時", l)]
    assert sum(float(r[1]) for r in day) == pytest.approx(live.total_pnl, abs=0.002 * len(day))
    assert sum(float(r[2]) for r in hour) == pytest.approx(live.total_pnl, abs=0.002 * len(hour))
    assert sum(int(r[2]) for r in day) == sum(int(r[3]) for r in hour) == len(live.fills)
    assert "== paper INJ" not in build_paper_report(store, taker_rate=0.00045, brief=True)  # Webhook 用は短い

    # 末尾の一部だけを見ても内訳の足し算は合う。始点の在庫は始点のミッドで評価し直す
    mid_time = live.fills[len(live.fills) // 2].time
    part = run_result(store, paper.run, "INJ", since=mid_time)
    assert 0 < len(part.fills) < len(live.fills)
    assert part.total_pnl == pytest.approx(part.spread_pnl + part.fee_pnl + part.inventory_pnl, abs=1e-9)
    assert part.position == live.position
    windowed = build_paper_report(store, hours=(run_span(store, paper.run, "INJ")[1] - mid_time) / 3_600_000, taker_rate=0.00045)
    day = [l.split() for l in windowed.split("日ごと (UTC)")[1].split("時間帯ごと")[0].splitlines()[1:] if l.strip()]
    assert sum(int(r[2]) for r in day) == len(part.fills)  # 上の表と日ごとの表で、数える約定が同じ
    assert sum(float(r[1]) for r in day) == pytest.approx(part.total_pnl, abs=0.002 * len(day))
    assert build_paper_report(MmStore(tmp_path / "none.sqlite"), taker_rate=0.00045) == "(paper の run がありません)"


async def test_paper_restart_is_a_new_run_from_zero(tmp_path):
    store, first = await run_paper(tmp_path, seed=3, cycles=60)
    before = run_result(store, first.run, "INJ")
    assert before.position != 0 and load_run(store, first.run).stopped is not None
    books_before = len(store.books("INJ"))
    store, second = await run_paper(tmp_path, seed=4, cycles=60, store=store, cross_fills=False)
    assert second.run == first.run + 1
    assert len(store.books("INJ")) > books_before  # 記録は同じ sqlite の続きに足される
    # 新しい run は在庫 0 から。前の run の成績は変わらない
    after = run_result(store, second.run, "INJ")
    replayed = simulate(store.books("INJ", *run_span(store, second.run, "INJ")), store.trades("INJ", *run_span(store, second.run, "INJ")),
                        baseline, PAPER_PARAMS, sz_decimals=1, latency_ms=250, cross_fills=False)
    assert fill_keys(after.fills) == fill_keys(replayed.fills) and after.total_pnl == pytest.approx(replayed.total_pnl)
    assert fill_keys(run_result(store, first.run, "INJ").fills) == fill_keys(before.fills)
    assert load_run(store).id == second.run and load_run(store).cross_fills is False  # 設定は run ごとに残る
    assert "ほかに 1 個の run" in build_paper_report(store, taker_rate=0.00045)


def test_paper_waits_for_trades_and_counts_what_arrives_too_late(tmp_path):
    store = MmStore(tmp_path / "mm.sqlite")
    store.set_coin("INJ", 1, 0)
    QUOTERS["test_fixed"] = (fixed(bid=99.9), {})
    try:
        paper = Paper(store, ["INJ"], "test_fixed", {}, maker_rate=0.0, latency_ms=0, log=lambda m: None)
    finally:
        del QUOTERS["test_fixed"]
    b = flat_books(n=4, step=2 * SEC)
    paper.on_book("INJ", b[0], recv=500)
    paper.on_book("INJ", b[1], recv=2500)
    assert "INJ" not in paper.sims  # 約定のポーリングがまだなので、気配は確定しない (クォーターも呼ばれない)
    paper.on_book("INJ", b[2], recv=4500)
    # 4000 に取りにいった約定のポーリング。それより前に受け取った気配 0, 1 だけが確定する
    paper.on_trades("INJ", [Trade(1 * SEC, "A", 99.8, 3), Trade(3 * SEC, "A", 99.8, 4)], started=4000)
    assert paper.sims["INJ"].prev.time == 2 * SEC
    assert [(f.time, f.size) for f in paper.sims["INJ"].res.fills] == [(1 * SEC, 3)]  # 3 秒の約定は気配 2 を待つ
    # 気配 1 (2 秒) より前の時刻の約定が後から届いた。もう通り過ぎているので使えず、数だけ残す
    paper.on_book("INJ", b[3], recv=6500)
    paper.on_trades("INJ", [Trade(1500, "A", 99.8, 5)], started=7000)
    assert [(f.time, f.size) for f in paper.sims["INJ"].res.fills] == [(1 * SEC, 3), (3 * SEC, 4)]
    assert paper.late_trades["INJ"] == 1
    acct = store.query("SELECT late_trades, position FROM paper_account")[0]
    assert (acct["late_trades"], acct["position"]) == (1, 7)


async def test_paper_failure_stops_paper_but_not_recording(tmp_path):
    def boom(ctx):
        raise RuntimeError("quoter bug")

    hl = FakeHyperliquid()
    store = MmStore(tmp_path / "mm.sqlite")
    logs = []
    rec = Recorder(hl, store, ["INJ"], clock=lambda: hl.time + 300, log=logs.append)
    await rec.setup()
    QUOTERS["test_boom"] = (boom, {})
    try:
        paper = Paper(store, ["INJ"], "test_boom", {}, maker_rate=0.0, latency_ms=0, clock=lambda: hl.time, log=logs.append)
    finally:
        del QUOTERS["test_boom"]
    rec.on_book, rec.on_trades = paper.on_book, paper.on_trades
    for _ in range(3):
        await rec.poll_books()
        hl.trade("INJ", 2)
        await rec.poll_trades()
    assert "INJ" in paper.broken and any("paper を止めました" in m for m in logs)
    assert len(store.books("INJ")) == 3 and len(store.trades("INJ")) == 6  # 記録は欠けない
    assert store.query("SELECT COUNT(*) FROM polls WHERE error IS NOT NULL")[0][0] == 0


# ---------------------------------------------------------------- クォーターに渡る履歴と state


def history_quoter(ctx):
    """テスト用の最小のクォーター (戦略ではない)。履歴と state を全部使う:
    直近 30 秒のミッドの振れで幅を広げ、約定の売り買いの偏りと板の厚さの偏りで真ん中をずらす"""
    st = ctx.state
    st["calls"] = st.get("calls", 0) + 1
    st["ema"] = ctx.book.mid if "ema" not in st else st["ema"] * 0.9 + ctx.book.mid * 0.1
    mids = [b.mid for b in ctx.books if b.time >= ctx.now - 30_000]
    swing_bps = (max(mids) - min(mids)) / ctx.book.mid * 10_000
    recent = [t for t in ctx.trades if t.time >= ctx.now - 60_000]
    flow = sum(t.sz if t.side == "B" else -t.sz for t in recent) / (sum(t.sz for t in recent) or 1.0)
    bid_depth, ask_depth = sum(sz for _, sz in ctx.book.bids[:3]), sum(sz for _, sz in ctx.book.asks[:3])
    lean = (bid_depth - ask_depth) / (bid_depth + ask_depth)
    center = st["ema"] * (1 + (flow + lean) * 1e-4 - ctx.inventory * ctx.book.mid / 500 * 3e-4)
    half = (ctx.params["half_spread_bps"] + 0.5 * swing_bps) / 10_000
    size = 100 / ctx.book.mid
    return Quote(center * (1 - half), size, center * (1 + half), size)


async def test_paper_matches_replay_with_a_quoter_that_uses_history_and_state(tmp_path, monkeypatch):
    monkeypatch.setitem(QUOTERS, "test_history", (history_quoter, {"half_spread_bps": 1.0}))
    params = {"half_spread_bps": 1.0}
    store, paper = await run_paper(tmp_path, seed=5, cycles=150, quoter="test_history", params=params)
    live = run_result(store, paper.run, "INJ")
    first, last = run_span(store, paper.run, "INJ")
    replayed = simulate(store.books("INJ", first, last), store.trades("INJ", first, last), history_quoter, params,
                        sz_decimals=1, latency_ms=250)
    assert len(live.fills) > 20 and live.count(BUY) > 5 and live.count(SELL) > 5
    assert fill_keys(live.fills) == fill_keys(replayed.fills)
    for k in ("spread_pnl", "inventory_pnl", "fee_pnl", "total_pnl", "position", "placed", "canceled", "rejected"):
        assert getattr(live, k) == pytest.approx(getattr(replayed, k), abs=1e-9), k
    assert paper.sims["INJ"].state["calls"] > 300  # state は呼び出しをまたいで残る
    # 履歴を使っていること自体の確認: 履歴を渡さなければ結果が変わる
    blind = simulate(store.books("INJ", first, last), store.trades("INJ", first, last),
                     lambda ctx: history_quoter(Context.of(ctx.book, ctx.inventory, ctx.params, state=ctx.state)),
                     params, sz_decimals=1, latency_ms=250)
    assert fill_keys(blind.fills) != fill_keys(replayed.fills)


def test_quoter_never_sees_anything_after_the_decision():
    rng = random.Random(11)
    books = [Book(i * 3 * SEC, ((99.9, 10.0), (99.8, 10.0)), ((100.1, 10.0), (100.2, 10.0))) for i in range(400)]
    trades = [Trade(rng.randint(-5000, 1_199_000), rng.choice("AB"), rng.choice([99.9, 100.1]), 2.0, tid=i) for i in range(900)]
    by_time = sorted(trades, key=lambda t: t.time)
    calls = []

    def spy(ctx):
        calls.append(ctx)
        assert ctx.books[-1] is ctx.book and ctx.now == ctx.book.time
        assert all(b.time <= ctx.now for b in ctx.books) and all(t.time <= ctx.now for t in ctx.trades)
        assert [b.time for b in ctx.books] == sorted(b.time for b in ctx.books)
        assert [t.time for t in ctx.trades] == sorted(t.time for t in ctx.trades)
        return Quote(bid=99.9, bid_size=1, ask=100.1, ask_size=1)

    # 遅れ 0 なら毎回の気配で呼ばれる。遅れが気配の間隔より長いと、指値が載るまで呼ばれない
    simulate(books, trades, spy, {}, sz_decimals=1, latency_ms=0)
    assert [c.now for c in calls] == [b.time for b in books]
    for c in calls[1:]:
        # 判断の時点までの約定は漏れなく入り (履歴の長さの範囲で)、それより後は 1 件も入らない。
        # 最初の気配より前の約定は、paper でも再生でも渡さない
        want = [t for t in by_time if max(0, c.now - HISTORY_SEC * 1000 - 1) < t.time <= c.now]
        assert sorted(t.tid for t in c.trades) == sorted(t.tid for t in want)
        want_books = [b for b in books if c.now - HISTORY_SEC * 1000 <= b.time <= c.now]
        assert list(c.books) == want_books
    assert calls[0].trades == () and calls[0].books == (books[0],)
    assert max(len(c.books) for c in calls) == HISTORY_SEC // 3 + 1  # 長く回しても履歴は増え続けない
    assert all(c.state is calls[0].state for c in calls)  # 同じ実行の中では同じ state

    # 先のデータを書き換えても、それより前の判断に渡るものは変わらない
    cut = 600 * SEC
    before = [(c.now, c.books, c.trades, c.inventory) for c in calls if c.now <= cut]
    calls.clear()
    crash = [Book(b.time, ((50.0, 10.0),), ((50.2, 10.0),)) if b.time > cut else b for b in books]
    simulate(crash, [t for t in trades if t.time <= cut], spy, {}, sz_decimals=1, latency_ms=0)
    assert [(c.now, c.books, c.trades, c.inventory) for c in calls if c.now <= cut] == before

    calls.clear()
    simulate(books[:20], [], spy, {}, sz_decimals=1, latency_ms=7000)
    assert [c.now for c in calls] == [b.time for b in books[:20:3]]  # 載った (7 秒後) 次の気配 (9 秒) でまた決める
    assert len(calls[-1].books) == 19  # 呼ばれなかった間の気配も履歴には入っている


def test_quoter_state_is_per_simulation():
    def counting(ctx):
        ctx.state["n"] = ctx.state.get("n", 0) + 1
        return Quote()

    a = Sim(counting, {}, sz_decimals=1, latency_ms=0)
    b = Sim(counting, {}, sz_decimals=1, latency_ms=0)
    for book in flat_books(n=5):
        a.step(book)
    b.step(flat_books(n=1)[0])
    assert (a.state["n"], b.state["n"]) == (5, 1)  # 銘柄 (Sim) ごとに別。実行し直せば空から


# ---------------------------------------------------------------- クォーターの改善案 (vol_spread / lean / vol_lean / cooldown)

from tbf.mm.quoter import (  # noqa: E402
    COOLDOWN_PARAMS, FLOW_DELAY_MS, LEAN_PARAMS, VOL_LEAN_PARAMS, VOL_PARAMS,
    book_imbalance, cooldown, lean, realized_vol_bps, trade_flow, vol_lean, vol_spread,
)

NEW_QUOTERS = {"vol_spread": VOL_PARAMS, "lean": LEAN_PARAMS, "vol_lean": VOL_LEAN_PARAMS, "cooldown": COOLDOWN_PARAMS}


def walk_books(n, step_bps, *, dt=2 * SEC, start=0, mid=100.0, bid_sz=10.0, ask_sz=10.0):
    """ミッドが毎回 ±step_bps ずつ交互に動く気配 (スプレッド 2 bps、2 段)。実現ボラが計算で分かる"""
    out = []
    for i in range(n):
        m = mid * (1 + step_bps / 10_000 * (i % 2))
        out.append(Book(start + i * dt, ((m * 0.9999, bid_sz), (m * 0.9998, 50.0)), ((m * 1.0001, ask_sz), (m * 1.0002, 50.0))))
    return out


def ctx_at(books, inventory=0.0, params=None, trades=(), state=None):
    return Context(books[-1].time, books[-1], inventory, params, tuple(books), tuple(trades), {} if state is None else state)


def test_new_quoters_are_registered_and_baseline_is_untouched():
    assert BASELINE_PARAMS == {"half_spread_bps": 3.0, "size_usd": 100.0, "max_inventory_usd": 500.0, "skew_bps": 3.0}
    assert QUOTERS["baseline"] == (baseline, BASELINE_PARAMS)
    for name, params in NEW_QUOTERS.items():
        assert QUOTERS[name][1] is params
        added = set(params) - set(BASELINE_PARAMS)
        assert set(BASELINE_PARAMS) < set(params) and {k: params[k] for k in BASELINE_PARAMS} == BASELINE_PARAMS
        assert 1 <= len(added) <= (6 if name == "vol_lean" else 3), name  # 1 案あたり新しいパラメータは 3 個まで
    assert set(VOL_LEAN_PARAMS) == set(VOL_PARAMS) | set(LEAN_PARAMS)  # 合わせた案に独自のパラメータは無い


def test_realized_vol_needs_history_and_ignores_what_is_before_a_break():
    books = walk_books(31, 2.0)  # 2 秒ごとに 2 bps -> 5 秒あたり 2 x sqrt(5 / 2)
    assert realized_vol_bps(tuple(books[:1]), books[0].time, 60) is None
    assert realized_vol_bps(tuple(books[:15]), books[14].time, 60) is None  # 28 秒ぶんしか無い (窓の半分に届かない)
    assert realized_vol_bps(tuple(books[:16]), books[15].time, 60) == pytest.approx(2.0 * (5 / 2) ** 0.5, rel=1e-3)
    assert realized_vol_bps(tuple(books), books[-1].time, 60) == pytest.approx(2.0 * (5 / 2) ** 0.5, rel=1e-3)
    assert realized_vol_bps(tuple(walk_books(31, 0.0)), 60 * SEC, 60) == 0.0
    # 記録の切れ目 (15 秒超) より前は使わない。切れ目の直後は「履歴が足りない」に戻る
    later = walk_books(31, 2.0, start=books[-1].time + 20 * SEC)
    assert realized_vol_bps(tuple(books + later[:5]), later[4].time, 60) is None
    assert realized_vol_bps(tuple(books + later), later[-1].time, 60) is not None
    # 切れ目をまたいだ値動きを 1 歩として数えない
    jump = walk_books(31, 0.0, start=books[-1].time + 20 * SEC, mid=120.0)
    assert realized_vol_bps(tuple(walk_books(31, 0.0) + jump), jump[-1].time, 100) == 0.0


def test_vol_spread_waits_for_history_widens_when_rough_and_pauses_when_too_rough():
    p = dict(VOL_PARAMS)
    for q in (vol_spread, vol_lean):
        pp = dict(VOL_LEAN_PARAMS) if q is vol_lean else p
        assert q(ctx_at(walk_books(5, 1.0), 0, pp)) == Quote()  # 履歴が足りない間は出さない
    calm, rough, wild = walk_books(40, 0.5), walk_books(40, 3.0), walk_books(40, 6.0)
    vol = {k: realized_vol_bps(tuple(b), b[-1].time, 60) for k, b in (("calm", calm), ("rough", rough), ("wild", wild))}
    assert vol["calm"] < p["half_spread_bps"] < vol["rough"] < p["vol_pause_bps"] < vol["wild"]

    def half_bps(q):
        return (q.ask - q.bid) / (q.ask + q.bid) * 10_000

    q_calm, q_rough = vol_spread(ctx_at(calm, 0, p)), vol_spread(ctx_at(rough, 0, p))
    assert half_bps(q_calm) == pytest.approx(p["half_spread_bps"], rel=1e-6)  # 静かなら baseline と同じ幅
    assert (q_calm.bid, q_calm.ask) == pytest.approx((baseline(Context.of(calm[-1], 0, BASELINE_PARAMS)).bid,
                                                     baseline(Context.of(calm[-1], 0, BASELINE_PARAMS)).ask))
    assert half_bps(q_rough) == pytest.approx(vol["rough"], rel=1e-6) and half_bps(q_rough) > half_bps(q_calm)
    assert half_bps(vol_spread(ctx_at(rough, 0, {**p, "vol_mult": 2.0}))) == pytest.approx(2 * vol["rough"], rel=1e-6)
    assert half_bps(vol_spread(ctx_at(rough, 0, {**p, "vol_mult": 0.0}))) == pytest.approx(p["half_spread_bps"], rel=1e-6)
    # 荒すぎるときは在庫を増やす側を出さない。在庫を減らす側だけ残す
    stopped = vol_spread(ctx_at(wild, 0, p))
    assert stopped.bid is None and stopped.ask is None
    long, short = vol_spread(ctx_at(wild, 1.0, p)), vol_spread(ctx_at(wild, -1.0, p))
    assert long.bid is None and long.ask is not None and short.ask is None and short.bid is not None
    both = vol_spread(ctx_at(wild, 0, {**p, "vol_pause_bps": 0.0}))  # 0 なら止めない
    assert both.bid is not None and both.ask is not None


def test_lean_moves_the_center_toward_the_thick_side_and_the_aggressor_side():
    p = {**LEAN_PARAMS, "imb_bps": 2.0, "flow_bps": 1.0, "one_side_bps": 0.0}
    flat = walk_books(3, 0.0)
    even = lean(ctx_at(flat, 0, p))
    base = baseline(Context.of(flat[-1], 0, BASELINE_PARAMS))
    assert (even.bid, even.ask) == pytest.approx((base.bid, base.ask))  # 偏りが無ければ baseline と同じ

    bid_heavy, ask_heavy = walk_books(3, 0.0, bid_sz=30.0, ask_sz=10.0), walk_books(3, 0.0, bid_sz=10.0, ask_sz=30.0)
    assert book_imbalance(bid_heavy[-1]) == pytest.approx(0.5) and book_imbalance(ask_heavy[-1]) == pytest.approx(-0.5)
    assert book_imbalance(bid_heavy[-1], levels=2) == pytest.approx(20 / 140)
    up, down = lean(ctx_at(bid_heavy, 0, p)), lean(ctx_at(ask_heavy, 0, p))
    assert up.bid == pytest.approx(even.bid * 1.0001) and up.ask == pytest.approx(even.ask * 1.0001)  # 買いが厚い -> 上へ 1 bps
    assert down.bid == pytest.approx(even.bid * 0.9999) and down.ask == pytest.approx(even.ask * 0.9999)

    now = flat[-1].time
    buys = [Trade(now - 30 * SEC, "B", 100.0, 3.0), Trade(now - 20 * SEC, "A", 100.0, 1.0)]
    assert trade_flow(tuple(buys), now) == pytest.approx(0.5)
    assert trade_flow((), now) == 0.0  # 約定の履歴が無い間はどちらにも寄せない
    assert lean(ctx_at(flat, 0, p, trades=buys)).bid == pytest.approx(even.bid * 1.00005)  # 買い手主導が多い -> 上へ
    sells = [Trade(t.time, "A" if t.side == "B" else "B", t.px, t.sz) for t in buys]
    assert lean(ctx_at(flat, 0, p, trades=sells)).bid == pytest.approx(even.bid * 0.99995)
    # 直前の約定 (届くのが間に合わないはずのもの) と、窓より古い約定は数えない
    fresh = buys + [Trade(now - FLOW_DELAY_MS + 1, "A", 100.0, 500.0), Trade(now, "A", 100.0, 500.0)]
    old = [Trade(now - 61 * SEC, "A", 100.0, 500.0)] + buys
    assert trade_flow(tuple(fresh), now) == trade_flow(tuple(old), now) == pytest.approx(0.5)
    assert trade_flow(tuple(buys + [Trade(now - FLOW_DELAY_MS, "A", 100.0, 2.0)]), now) == pytest.approx(0.0)

    # 偏りが強いとき、踏まれる側 (上がりそうなら売り) は出さない
    one = {**p, "one_side_bps": 1.0}
    assert lean(ctx_at(bid_heavy, 0, one)).ask is None and lean(ctx_at(bid_heavy, 0, one)).bid == pytest.approx(up.bid)
    assert lean(ctx_at(ask_heavy, 0, one)).bid is None and lean(ctx_at(ask_heavy, 0, one)).ask == pytest.approx(down.ask)
    weak = lean(ctx_at(bid_heavy, 0, {**one, "imb_bps": 1.0}))  # 0.5 bps しかずれない -> 両側
    assert weak.bid is not None and weak.ask is not None

    # 合わせた案: 幅は荒さ、真ん中は偏り
    rough = walk_books(40, 3.0, bid_sz=30.0, ask_sz=10.0)
    vp = {**VOL_LEAN_PARAMS, "imb_bps": 2.0, "flow_bps": 0.0, "one_side_bps": 0.0}
    both, wide = vol_lean(ctx_at(rough, 0, vp)), vol_spread(ctx_at(rough, 0, VOL_PARAMS))
    assert both.bid == pytest.approx(wide.bid * 1.0001) and both.ask == pytest.approx(wide.ask * 1.0001)


@pytest.mark.parametrize("name", sorted(NEW_QUOTERS))
def test_new_quoters_keep_the_inventory_rules_of_baseline(name):
    fn, params = QUOTERS[name]
    books = walk_books(40, 0.5)
    mid = books[-1].mid
    flat = fn(ctx_at(books, 0, params))
    assert flat.bid < mid < flat.ask and flat.bid_size == flat.ask_size == pytest.approx(params["size_usd"] / mid)
    cap = params["max_inventory_usd"] / mid
    long, short = fn(ctx_at(books, cap, params)), fn(ctx_at(books, -cap, params))
    assert long.bid is None and long.ask is not None  # 上限。増やす側は出さない
    assert short.ask is None and short.bid is not None
    half = fn(ctx_at(books, cap / 2, params))  # 買い越しなら両方の値段を下げる (baseline と同じ幅だけ)
    assert half.bid == pytest.approx(flat.bid * (1 - params["skew_bps"] / 2 / 10_000))
    assert half.ask == pytest.approx(flat.ask * (1 - params["skew_bps"] / 2 / 10_000))


def test_cooldown_rests_the_side_that_just_filled():
    p, state = {**COOLDOWN_PARAMS, "cooldown_sec": 10.0}, {}
    books = walk_books(12, 0.0)

    def at(i, inventory):
        return cooldown(Context(books[i].time, books[i], inventory, p, tuple(books[: i + 1]), (), state))

    first = at(0, 0.0)
    base = baseline(Context.of(books[0], 0, BASELINE_PARAMS))
    assert (first.bid, first.ask) == pytest.approx((base.bid, base.ask))  # 約定が無い間は baseline と同じ
    bought = at(1, 0.5)  # t=2 秒に買いが約定していた -> 12 秒まで買いを休む
    assert bought.bid is None and bought.ask is not None
    assert at(5, 0.5).bid is None  # 10 秒
    assert at(6, 0.5).bid is not None  # 12 秒
    sold = at(7, 0.0)  # 売りが約定 -> 今度は売りを休む
    assert sold.ask is None and sold.bid is not None
    assert at(11, 0.0).ask is None and cooldown(Context(30 * SEC, books[-1], 0.0, p, (books[-1],), (), state)).ask is not None

    # 再生の中でも: 買いが約定したら、次の買い指値は 10 秒たつまで出ない
    flat = [Book(i * 2 * SEC, ((99.99, 100.0),), ((100.01, 100.0),)) for i in range(12)]
    trades = [Trade(1 * SEC, "A", 99.9, 5.0), Trade(5 * SEC, "A", 99.9, 5.0), Trade(15 * SEC, "A", 99.9, 5.0)]
    res = simulate(flat, trades, cooldown, p, sz_decimals=2, latency_ms=0)
    assert [(f.time, f.side) for f in res.fills] == [(1 * SEC, BUY), (15 * SEC, BUY)]
    assert len(simulate(flat, trades, baseline, BASELINE_PARAMS, sz_decimals=2, latency_ms=0).fills) == 3


@pytest.mark.parametrize("name", sorted(NEW_QUOTERS))
def test_new_quoters_do_not_change_past_decisions_when_the_future_changes(name):
    fn, params = QUOTERS[name]
    rng = random.Random(7)
    mid, books = 100.0, []
    for i in range(300):
        mid *= 1 + rng.gauss(0, 1.5) / 10_000
        books.append(Book(i * 2 * SEC, ((round(mid * 0.9999, 4), rng.uniform(1, 9)), (round(mid * 0.9997, 4), 20.0)),
                          ((round(mid * 1.0001, 4), rng.uniform(1, 9)), (round(mid * 1.0003, 4), 20.0))))
    trades = []
    for i in range(400):
        t = rng.randint(1, 599_000)
        b = books[t // (2 * SEC)]
        side = rng.choice("AB")
        trades.append(Trade(t, side, round((b.best_bid if side == "A" else b.best_ask) * (1 + rng.gauss(0, 2) / 10_000), 4), rng.uniform(0.5, 3), tid=i))
    cut = 300 * SEC

    def record(log):
        def wrapped(ctx):
            q = fn(ctx)
            log.append((ctx.now, q))
            return q
        return wrapped

    a_log, b_log = [], []
    a = simulate(books, trades, record(a_log), params, sz_decimals=1, latency_ms=250)
    crash = [Book(b.time, ((50.0, 10.0), (49.9, 1.0)), ((50.2, 1.0), (50.3, 9.0))) if b.time > cut else b for b in books]
    future = [Trade(t.time, "B", 50.2, 99.0, tid=t.tid) for t in trades if t.time > cut]
    b = simulate(crash, [t for t in trades if t.time <= cut] + future, record(b_log), params, sz_decimals=1, latency_ms=250)
    past = [x for x in a_log if x[0] <= cut]
    assert past == [x for x in b_log if x[0] <= cut] and len(past) == 151
    assert [f for f in a.fills if f.time <= cut] == [f for f in b.fills if f.time <= cut] != []
    assert a_log != b_log  # 先のデータが違えば、その後の判断は変わる (= 履歴を実際に見ている)
    assert any(q.bid is not None for _, q in past) and any(q.ask is not None for _, q in past)
    # 同じ記録なら何度流しても同じ (state が実行をまたいで残らない)
    again = simulate(books, trades, fn, params, sz_decimals=1, latency_ms=250)
    assert again.fills == a.fills and again.total_pnl == a.total_pnl
