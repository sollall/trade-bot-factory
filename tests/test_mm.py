import dataclasses
import json
import random
import re
import time
from datetime import datetime, timezone

import pytest

import tbf.http
import tbf.mm.venues
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


# ---------------------------------------------------------------- 会場 (GMO コイン / bitbank)

from tbf.mm.venues import (  # noqa: E402
    Bitbank,
    Gmo,
    Instrument,
    RateLimited,
    hyperliquid_instrument,
    parse_bitbank_book,
    parse_bitbank_instruments,
    parse_bitbank_trades,
    parse_gmo_book,
    parse_gmo_instruments,
    parse_gmo_trades,
    scale_defaults,
)

# 2026-11-22 に実物の応答から切り出した小さな例 (値はそのまま、件数だけ減らした)
GMO_SYMBOLS = {"status": 0, "responsetime": "2026-11-22T21:54:21.652Z", "data": [
    {"symbol": "SOL", "minOrderSize": "0.01", "maxOrderSize": "500", "sizeStep": "0.01", "tickSize": "1", "takerFee": "0.0009", "makerFee": "-0.0003"},
    {"symbol": "ETH_JPY", "minOrderSize": "0.01", "maxOrderSize": "100", "sizeStep": "0.01", "tickSize": "1", "takerFee": "0", "makerFee": "0"},
    {"symbol": "XRP_JPY", "minOrderSize": "10", "maxOrderSize": "100000", "sizeStep": "10", "tickSize": "0.001", "takerFee": "0", "makerFee": "0"},
]}
GMO_BOOK = {"status": 0, "responsetime": "2026-11-22T21:54:21.196Z", "data": {
    # 売りは安い順、買いは高い順で返る。気配そのものに時刻は無い
    "asks": [{"price": "272605", "size": "0.12"}, {"price": "272642", "size": "0.2"}, {"price": "272643", "size": "0.4"}],
    "bids": [{"price": "272463", "size": "0.26"}, {"price": "272462", "size": "0.3"}, {"price": "272459", "size": "0.02"}],
    "symbol": "ETH_JPY"}}
GMO_TRADES = {"status": 0, "responsetime": "2026-11-22T21:54:22.411Z", "data": {"pagination": {"count": 100, "currentPage": 1}, "list": [
    # 新しい順。約定に id が無い。同じ時刻・値段・数量で BUY と SELL が並ぶことがある
    {"price": "272625", "side": "BUY", "size": "0.1", "timestamp": "2026-11-22T21:54:11.702Z"},
    {"price": "272625", "side": "SELL", "size": "0.1", "timestamp": "2026-11-22T21:54:11.702Z"},
    {"price": "272623", "side": "BUY", "size": "0.4", "timestamp": "2026-11-22T21:54:08.286Z"},
    {"price": "272623", "side": "BUY", "size": "0.4", "timestamp": "2026-11-22T21:54:08.286Z"},
]}}
BITBANK_PAIRS = {"success": 1, "data": {"pairs": [
    {"name": "xrp_jpy", "base_asset": "xrp", "quote_asset": "jpy", "maker_fee_rate_quote": "-0.0002", "taker_fee_rate_quote": "0.0012",
     "unit_amount": "0.0001", "price_digits": 3, "amount_digits": 4, "is_enabled": True},
    {"name": "btc_jpy", "base_asset": "btc", "quote_asset": "jpy", "maker_fee_rate_quote": "0", "taker_fee_rate_quote": "0.001",
     "unit_amount": "0.0001", "price_digits": 0, "amount_digits": 4, "is_enabled": True},
    {"name": "old_jpy", "base_asset": "old", "quote_asset": "jpy", "maker_fee_rate_quote": "0", "taker_fee_rate_quote": "0",
     "unit_amount": "1", "price_digits": 0, "amount_digits": 0, "is_enabled": False},
]}}
BITBANK_DEPTH = {"success": 1, "data": {
    "asks": [["146.860", "24218.9341"], ["146.861", "6766.4808"], ["146.868", "6766.4808"]],
    "bids": [["146.859", "343.9556"], ["146.832", "329.3210"], ["146.831", "60.3380"]],
    "asks_over": "13028174.9339", "bids_under": "21447146.5792", "timestamp": 1795384483940, "sequenceId": "35495873891"}}
BITBANK_TRADES = {"success": 1, "data": {"transactions": [
    {"transaction_id": 1241108853, "side": "buy", "price": "146.860", "amount": "0.0001", "executed_at": 1795384471762},
    {"transaction_id": 1241108852, "side": "sell", "price": "146.859", "amount": "4.2560", "executed_at": 1795384464768},
]}}


def test_gmo_responses_are_read_as_recorded_form():
    inst = parse_gmo_instruments(GMO_SYMBOLS)
    sol, eth = inst["SOL"], inst["ETH_JPY"]
    assert (sol.tick, sol.size_step, sol.min_size, sol.maker_rate, sol.taker_rate) == (1.0, 0.01, 0.01, -0.0003, 0.0009)
    assert (sol.quote, sol.venue, sol.symbol) == ("JPY", "gmo", "¥")
    assert sol.can_short is False and eth.can_short is True  # 現物は売りから入れない。*_JPY はレバレッジ
    assert inst["XRP_JPY"].tick == 0.001 and inst["XRP_JPY"].size_step == 10

    book = parse_gmo_book(GMO_BOOK, depth=2)
    assert book.time == 1795384461196  # responsetime (気配に時刻が無いので)
    assert book.bids == ((272463.0, 0.26), (272462.0, 0.3)) and book.asks == ((272605.0, 0.12), (272642.0, 0.2))
    shuffled = {**GMO_BOOK, "data": {"asks": GMO_BOOK["data"]["asks"][::-1], "bids": GMO_BOOK["data"]["bids"][::-1]}}
    assert parse_gmo_book(shuffled, depth=2) == book  # 並び順は応答に頼らない

    trades = parse_gmo_trades(GMO_TRADES)
    # 同じ時刻・値段・数量の BUY と SELL は 1 つの約定の両側。1 件にまとめ、向きは不明 ("U") にする。
    # 対にならない行だけ、BUY を買い手主導として読む
    assert [(t.time, t.side, t.px, t.sz) for t in trades] == [
        (1795384448286, "B", 272623.0, 0.4), (1795384448286, "B", 272623.0, 0.4), (1795384451702, "U", 272625.0, 0.1)]
    assert len({(t.time, t.tid) for t in trades}) == 3  # 同じ中身が 2 件あっても別の約定として残る
    assert [t.tid for t in parse_gmo_trades(GMO_TRADES)] == [t.tid for t in trades]  # 同じ応答なら同じ id (重複として落とせる)
    # 同じ中身の組が減っても、残ったほうは既に記録した id と重なる (新しい約定として二重に数えない)
    cut = {**GMO_TRADES, "data": {"list": GMO_TRADES["data"]["list"][:3]}}
    assert {(t.time, t.tid) for t in parse_gmo_trades(cut)} < {(t.time, t.tid) for t in trades}
    # 上限いっぱい (100 行) の応答は、いちばん古い時刻で対が切れているかもしれない。その時刻の行は使わない
    # (片方だけ残った SELL を「売り手主導の別の約定」として数えないため)
    rows = [{"price": "100", "side": s, "size": "1", "timestamp": f"2026-11-22T21:54:{10 + i:02d}.000Z"} for i in range(49, 0, -1) for s in ("BUY", "SELL")]
    full = {"status": 0, "data": {"list": rows + [{"price": "99", "side": "BUY", "size": "2", "timestamp": "2026-11-22T21:54:10.000Z"},
                                                   {"price": "99", "side": "SELL", "size": "2", "timestamp": "2026-11-22T21:54:10.000Z"}]}}
    assert len(full["data"]["list"]) == 100 and [t.side for t in parse_gmo_trades(full)] == ["U"] * 49
    full["data"]["list"].pop()  # 100 行に満たなければ全部使う
    assert [(t.side, t.px) for t in parse_gmo_trades(full)][:1] == [("B", 99.0)]

    with pytest.raises(RateLimited):
        parse_gmo_trades({"status": 4, "messages": [{"message_code": "ERR-5003", "message_string": "Requests are too many."}]})
    with pytest.raises(RuntimeError, match="ERR-5106"):
        parse_gmo_book({"status": 1, "messages": [{"message_code": "ERR-5106", "message_string": "Invalid request parameter."}]}, 5)


def test_bitbank_responses_are_read_as_recorded_form():
    inst = parse_bitbank_instruments(BITBANK_PAIRS)
    xrp = inst["xrp_jpy"]
    assert (xrp.tick, xrp.size_step, xrp.min_size, xrp.maker_rate, xrp.taker_rate) == (0.001, 0.0001, 0.0001, -0.0002, 0.0012)
    assert inst["btc_jpy"].tick == 1.0 and inst["btc_jpy"].maker_rate == 0.0
    assert xrp.can_short is False and xrp.quote == "JPY" and "old_jpy" not in inst  # 停止中の銘柄は無いものとして扱う

    book = parse_bitbank_book(BITBANK_DEPTH, depth=2)
    assert (book.time, book.bids, book.asks) == (1795384483940, ((146.859, 343.9556), (146.832, 329.321)), ((146.86, 24218.9341), (146.861, 6766.4808)))
    trades = parse_bitbank_trades(BITBANK_TRADES)
    # side は taker 側 (公式ドキュメント)。buy = 買い手主導 = 売り板 (146.860) で成立
    assert [(t.time, t.side, t.px, t.sz, t.tid) for t in trades] == [
        (1795384471762, "B", 146.86, 0.0001, 1241108853), (1795384464768, "A", 146.859, 4.256, 1241108852)]
    with pytest.raises(RateLimited):
        parse_bitbank_trades({"success": 0, "data": {"code": 10009}})
    with pytest.raises(RuntimeError, match="10000"):
        parse_bitbank_book({"success": 0, "data": {"code": 10000}}, 5)


class FakeDomestic:
    """GMO / bitbank の公開 API を URL で振り分けて真似る。約定は直近の limit 件だけ返す (新しい順)"""

    def __init__(self):
        self.time = 1_795_384_000_000
        self.status = 200
        self.fills = []  # (time, taker が買いか, 値段, 数量) 古い順
        self.book = (11159, 11160)
        self.twins = False  # GMO の実物: 1 つの約定が BUY と SELL の 2 行で返る (大半がこの形)

    def trade(self, n, buy=False, px="11159", sz="0.5", same_ms=False):
        for _ in range(n):
            self.time += 0 if same_ms else 7
            self.fills.append((self.time, buy, px, sz))

    async def request(self, method, url, headers=None, body=None):
        assert method == "GET" and body is None
        if self.status != 200:
            return self.status, "Too Many Requests"
        iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"  # noqa: E731
        bid, ask = self.book
        if "/public/v1/symbols" in url:
            return 200, json.dumps(GMO_SYMBOLS)
        if "/public/v1/orderbooks" in url:
            self.time += 1
            return 200, json.dumps({"status": 0, "responsetime": iso(self.time), "data": {
                "asks": [{"price": str(ask + i), "size": "20.5"} for i in range(30)],
                "bids": [{"price": str(bid - i), "size": "7.25"} for i in range(30)]}})
        if "/public/v1/trades" in url:
            rows = [{"price": p, "side": side, "size": s, "timestamp": iso(t)} for t, b, p, s in self.fills
                    for side in (("SELL", "BUY") if self.twins else ("BUY",) if b else ("SELL",))]
            return 200, json.dumps({"status": 0, "responsetime": iso(self.time), "data": {"list": rows[::-1][:100]}})
        if url.endswith("/spot/pairs"):
            return 200, json.dumps(BITBANK_PAIRS)
        if url.endswith("/depth"):
            self.time += 1
            return 200, json.dumps({"success": 1, "data": {
                "asks": [[f"{146.860 + i / 1000:.3f}", "6766.4808"] for i in range(200)],
                "bids": [[f"{146.859 - i / 1000:.3f}", "343.9556"] for i in range(200)], "timestamp": self.time}})
        if url.endswith("/transactions"):
            rows = [{"transaction_id": 5000 + i, "side": "buy" if b else "sell", "price": p, "amount": s, "executed_at": t}
                    for i, (t, b, p, s) in enumerate(self.fills)][-60:]
            return 200, json.dumps({"success": 1, "data": {"transactions": rows[::-1]}})
        raise AssertionError(url)


async def test_record_gmo_dedupes_without_trade_ids_and_flags_gaps(tmp_path):
    http = FakeDomestic()
    store = MmStore(tmp_path / "gmo.sqlite")
    rec = Recorder(http, store, ["SOL"], venue=Gmo(), min_gap=0, clock=lambda: http.time + 300, log=lambda m: None)
    await rec.setup()
    assert store.venue() == "gmo" and store.instrument("SOL").maker_rate == -0.0003 and store.sz_decimals("SOL") is None
    http.trade(3)
    http.trade(2, same_ms=True)  # 同じミリ秒・同じ中身の約定が 3 件 (id が無いので中身では区別できない)
    await rec.poll_trades()
    http.trade(4, buy=True, px="11160")
    await rec.poll_trades()
    await rec.poll_trades()
    assert [r["new_rows"] for r in store.query("SELECT new_rows FROM polls ORDER BY rowid")] == [5, 4, 0]
    assert len(store.trades("SOL")) == 9 and not store.query("SELECT * FROM gaps")
    assert [t.side for t in store.trades("SOL")] == ["A"] * 5 + ["B"] * 4
    http.trade(130)  # 100 件しか返らない (うち、いちばん古い時刻の 1 件は使わない)
    await rec.poll_trades()
    assert len(store.query("SELECT * FROM gaps")) == 1 and len(store.trades("SOL")) == 108
    # 実物に多い形: 1 つの約定が BUY と SELL の 2 行で返る。1 件として残し、出来高を 2 重に数えない
    http.twins = True
    http.trade(3, buy=True, px="11160", sz="2")
    await rec.poll_trades()
    await rec.poll_trades()
    assert [(t.side, t.sz) for t in store.trades("SOL")[-3:]] == [("U", 2.0)] * 3

    await rec.poll_books()
    (book,) = store.books("SOL")
    assert (book.best_bid, book.best_ask, len(book.bids)) == (11159.0, 11160.0, 10)

    http.status = 429  # 取り直さずに記録し、しばらく休む
    await rec.poll_books()
    assert rec.rate_limited and "429" in store.query("SELECT error FROM polls WHERE error IS NOT NULL")[0][0]


async def test_record_bitbank_and_refuse_mixing_venues(tmp_path):
    http = FakeDomestic()
    store = MmStore(tmp_path / "bb.sqlite")
    rec = Recorder(http, store, ["xrp_jpy"], venue=Bitbank(), min_gap=0, clock=lambda: http.time + 300, log=lambda m: None)
    await rec.setup()
    http.trade(70, px="146.859", sz="1.5")  # 60 件しか返らない
    await rec.poll_trades()
    http.trade(5, buy=True, px="146.860", sz="2.0")
    await rec.poll_books()
    await rec.poll_trades()
    await rec.poll_books()
    assert len(store.trades("xrp_jpy")) == 65 and not store.query("SELECT * FROM gaps")
    assert store.books("xrp_jpy")[0].bids[1] == (146.858, 343.9556)
    report = build_report(store)
    assert "会場 bitbank" in report and "メイカー手数料 (-2.00 bps)" in report  # 会場の値 (リベート) が既定になる
    assert "メイカー手数料 (+1.00 bps)" in build_report(store, maker_rate=0.0001)

    # 違う会場の記録を同じ db に足そうとしたら止める。無い銘柄・停止中の銘柄も止める
    with pytest.raises(ValueError, match="bitbank の記録"):
        await Recorder(http, store, ["SOL"], venue=Gmo(), min_gap=0, log=lambda m: None).setup()
    with pytest.raises(ValueError, match="bitbank の記録"):
        await Recorder(FakeHyperliquid(), store, ["INJ"], log=lambda m: None).setup()
    with pytest.raises(ValueError, match="old_jpy"):
        await Recorder(http, MmStore(tmp_path / "x.sqlite"), ["old_jpy"], venue=Bitbank(), min_gap=0, log=lambda m: None).setup()


async def test_db_without_venue_is_read_as_hyperliquid(tmp_path):
    # 会場を残す前の版で作った db: meta も instruments も無く、coins に数量の小数桁だけがある
    store = MmStore(tmp_path / "old.sqlite")
    store.set_coin("INJ", 1, 0)
    store.add_book("INJ", flat_books(n=1)[0], 0)
    store.conn.execute("DELETE FROM meta")
    store.commit()
    assert store.venue() == "hyperliquid"
    inst = store.instrument("INJ")
    assert inst == hyperliquid_instrument("INJ", 1) and (inst.quote, inst.tick, inst.maker_rate, inst.can_short) == ("USD", None, 0.00015, True)
    with pytest.raises(ValueError, match="hyperliquid の記録"):
        await Recorder(FakeDomestic(), store, ["SOL"], venue=Gmo(), min_gap=0, log=lambda m: None).setup()
    hl = FakeHyperliquid()
    await Recorder(hl, store, ["INJ"], clock=lambda: hl.time, log=lambda m: None).setup()  # 同じ会場なら続きに足せる
    assert store.venue() == "hyperliquid" and store.sz_decimals("INJ") == 1


def test_weight_budget_is_per_venue():
    # Hyperliquid は重み (気配 2、約定 20)、国内は回数。上限はどれも裏取りが足りないので、既定は半分まで
    assert check_budget(5, 3, 10, venue=Gmo()) == 130  # 5 * (20 + 6)
    with pytest.raises(ValueError, match="回数 216 > 使ってよい量 150"):
        check_budget(6, 2, 10, venue=Gmo())
    assert check_budget(4, 2, 10, venue=Bitbank()) == 144
    with pytest.raises(ValueError, match="流量制限"):
        check_budget(9, 2, 10, venue=Bitbank())
    assert weight_per_min(3, 3, 8) == 570  # 会場を省くと今までどおり Hyperliquid


SOL_SPOT = Instrument("SOL", "gmo", "JPY", tick=1.0, size_step=0.01, min_size=0.01, maker_rate=-0.0003, taker_rate=0.0009, can_short=False)


def coarse_books(n=4):
    """スプレッドが刻み 1 つ (11159 / 11160)。GMO の SOL 現物の実物と同じ形"""
    return [Book(i * 10 * SEC, ((11159.0, 7.25), (11158.0, 3.0)), ((11160.0, 20.5), (11161.0, 4.0))) for i in range(n)]


def test_coarse_tick_rounds_onto_best_quote_and_waits_in_queue():
    run_sol = lambda trades, quoter, **kw: simulate(coarse_books(), trades, quoter, {}, instrument=SOL_SPOT, latency_ms=0, **kw)  # noqa: E731
    # スプレッドの内側 (11159.6) に出したくても、刻みに丸めると最良買い 11159 に並ぶ。前に 7.25 枚いる
    inside = lambda ctx: Quote(bid=11159.6, bid_size=1.004, ask=11159.4, ask_size=1.004)  # noqa: E731
    res = run_sol([Trade(2 * SEC, "A", 11159.0, 7.0)], inside)
    assert res.fills == [] and (res.placed, res.rejected) == (2, 0)  # 売りも 11160 に切り上がって最良売りに並ぶ
    res = run_sol([Trade(2 * SEC, "A", 11159.0, 7.0), Trade(3 * SEC, "A", 11159.0, 0.75)], inside)
    (f,) = res.fills
    assert (f.px, f.size, f.kind) == (11159.0, 0.5, "queue")  # 残りの列 0.25 の後ろの 0.5 だけ
    # 売り側も同じ: 最良売り 11160 の 20.5 枚の後ろ。数量は刻み 0.01 に切り下げて 1.00
    res = run_sol([Trade(2 * SEC, "B", 11160.0, 21.0), Trade(3 * SEC, "B", 11160.0, 5.0)], inside)
    assert [(f.side, f.px, f.size, f.kind) for f in res.fills] == [(SELL, 11160.0, 0.5, "queue"), (SELL, 11160.0, 0.5, "queue")]
    # 値段を抜けた約定 (a) は列に関係なく約定する
    assert [(f.px, f.kind) for f in run_sol([Trade(2 * SEC, "A", 11158.0, 0.3)], inside).fills] == [(11159.0, "through")]

    # 相手の気配に当たる値段は出せない (11160.2 の買いは 11160 に丸まり、最良売りと同じ)
    crossing = lambda ctx: Quote(bid=11160.2, bid_size=1)  # noqa: E731
    res = run_sol([Trade(2 * SEC, "A", 11158.0, 5.0)], crossing)
    assert res.fills == [] and res.placed == 0 and res.rejected == 4
    # 最小数量 (0.01) に届かない指値は出さない
    tiny = lambda ctx: Quote(bid=11159.0, bid_size=0.009)  # noqa: E731
    assert run_sol([], tiny).too_small == 4
    # 最小数量が数量の刻みより大きい銘柄もある (刻みに丸めて残っても、最小に届かなければ出さない)
    big_min = dataclasses.replace(SOL_SPOT, min_size=0.05)
    assert simulate(coarse_books(), [], lambda ctx: Quote(bid=11159.0, bid_size=0.03), {}, instrument=big_min, latency_ms=0).too_small == 4
    # 手数料は銘柄の値 (リベート) が既定。上書きもできる
    res = run_sol([Trade(2 * SEC, "A", 11158.0, 0.3)], inside)
    assert res.fee_pnl == pytest.approx(11159.0 * 0.3 * 0.0003) and res.fee_pnl > 0
    assert run_sol([Trade(2 * SEC, "A", 11158.0, 0.3)], inside, maker_rate=0.0001).fee_pnl < 0

    # 刻みが 10 の累乗でなくても、小数の刻みでも丸めは刻みの倍数になる
    for tick, want_bid, want_ask in ((0.5, 11159.5, 11160.0), (0.001, 11159.6, 11159.601), (5.0, 11155.0, 11160.0)):
        inst = dataclasses.replace(SOL_SPOT, tick=tick)
        wide = [Book(i * SEC, ((11100.0, 5.0),), ((11200.0, 5.0),)) for i in range(2)]
        sim = Sim(lambda ctx: Quote(bid=11159.6, bid_size=1, ask=11159.6004, ask_size=1), {}, instrument=inst, latency_ms=0)
        sim.step(wide[0])
        assert (sim.live[BUY].px, sim.live[SELL].px) == (want_bid, want_ask), tick

    # Hyperliquid の規則 (有効 5 桁) でも同じ値段に丸まり、同じように列に並ぶ
    hl = simulate(coarse_books(), [Trade(2 * SEC, "A", 11159.0, 7.0), Trade(3 * SEC, "A", 11159.0, 0.75)], inside, {},
                  sz_decimals=2, latency_ms=0)
    assert [(f.px, f.size, f.kind) for f in hl.fills] == [(11159.0, 0.5, "queue")]


def test_trades_of_unknown_side_fill_whichever_quote_the_price_reached():
    # GMO の約定は仕掛けた側が分からない ("U")。値段が届いた側の指値にだけ当てる
    both = fixed(bid=99.9, ask=100.1)
    one = lambda t: run(flat_books(), [t], both).fills  # noqa: E731
    assert one(Trade(5 * SEC, "U", 100.0, 5)) == []  # 自分の指値の間で成立した約定は、どちらにも当たらない
    assert [(f.side, f.px, f.size, f.kind) for f in one(Trade(5 * SEC, "U", 99.8, 3))] == [(BUY, 99.9, 3, "through")]
    assert [(f.side, f.px, f.size, f.kind) for f in one(Trade(5 * SEC, "U", 100.2, 4))] == [(SELL, 100.1, 4, "through")]
    # ちょうどの値段なら前の列 (100 枚) を先に消化する
    assert one(Trade(5 * SEC, "U", 99.9, 60)) == []
    fills = run(flat_books(), [Trade(5 * SEC, "U", 100.1, 60), Trade(6 * SEC, "U", 100.1, 45)], both).fills
    assert [(f.side, f.size, f.kind) for f in fills] == [(SELL, 5, "queue")]
    # 向きが分かっている約定の扱いは変わらない: 売り値以上で成立した「売り手主導」は売り指値に当てない
    assert one(Trade(5 * SEC, "A", 100.2, 4)) == []
    res = run(flat_books(), [Trade(5 * SEC, "U", 99.8, 3)], both)
    assert res.market_volume_usd == pytest.approx(99.8 * 3)  # 1 件として数える


def test_output_uses_venue_currency_and_warns_about_spot_shorting():
    res = simulate(coarse_books(), [Trade(2 * SEC, "B", 11161.0, 0.4), Trade(12 * SEC, "A", 11158.0, 0.1)],
                   lambda ctx: Quote(bid=11159.0, bid_size=1, ask=11160.0, ask_size=1), {}, instrument=SOL_SPOT, latency_ms=0)
    assert [(f.side, f.size) for f in res.fills] == [(SELL, 0.4), (BUY, 0.1)]
    text = format_result("SOL", res, {}, maker_rate=-0.0003, taker_rate=0.0009, latency_ms=0, trade_gaps=0, instrument=SOL_SPOT)
    assert "損益 (JPY)" in text and "代金 ¥5,580" in text and "$" not in text and "メイカー手数料 -3.00 bps" in text
    assert "現物なので" in text and "在庫を 0 より下に押し下げた売りが 1 件 (¥4,464)" in text
    lev = dataclasses.replace(SOL_SPOT, coin="SOL_JPY", can_short=True)
    assert "現物なので" not in format_result("SOL_JPY", res, {}, maker_rate=0, taker_rate=0, latency_ms=0, trade_gaps=0, instrument=lev)
    usd = format_result("INJ", res, {}, maker_rate=0.00015, taker_rate=0.00045, latency_ms=0, trade_gaps=0)
    assert "損益 (USD)" in usd and "代金 $5,580" in usd and "現物なので" not in usd  # 会場を渡さなければ今までどおり

    # 金額のパラメータの既定値は建値の桁に合わせる (為替ではなく大まかな倍率)。bps などはそのまま
    assert scale_defaults(BASELINE_PARAMS, "JPY") == {**BASELINE_PARAMS, "size_usd": 15_000.0, "max_inventory_usd": 75_000.0}
    assert scale_defaults(BASELINE_PARAMS, "USD") == BASELINE_PARAMS


async def test_paper_on_gmo_uses_per_coin_fees_and_matches_replay(tmp_path, capsys):
    http = FakeDomestic()
    db = tmp_path / "gmo.sqlite"
    store = MmStore(db)
    rec = Recorder(http, store, ["SOL", "ETH_JPY"], venue=Gmo(), min_gap=0, clock=lambda: http.time + 300, log=lambda m: None)
    await rec.setup()
    params = parse_params(scale_defaults(BASELINE_PARAMS, "JPY"), ["half_spread_bps=0.3"])
    paper = Paper(store, ["SOL", "ETH_JPY"], "baseline", params, latency_ms=250, clock=lambda: http.time + 300, log=lambda m: None)
    http.twins = True  # 約定の向きは分からない ("U")
    rec.on_book, rec.on_trades = paper.on_book, paper.on_trades
    rng = random.Random(9)
    bid = 11159
    for _ in range(120):
        for _ in range(3):
            bid += rng.choice([-2, -1, 0, 0, 1, 2])
            http.book = (bid, bid + 1)
            await rec.poll_books()
            http.time += rng.randint(500, 2500)
            for _ in range(rng.choice([0, 1, 3])):
                buy = rng.random() < 0.5
                http.trade(1, buy=buy, px=str(bid + (1 if buy else 0) + rng.choice([-1, 0, 0, 1])), sz=rng.choice(["0.5", "3", "30"]))
        await rec.poll_trades()
    paper.stop()
    cfg = load_run(store)
    assert (cfg.rate("SOL"), cfg.rate("ETH_JPY")) == (-0.0003, 0.0)  # 会場の値 (銘柄ごと) を run に残す
    sol = run_result(store, paper.run, "SOL")
    assert len(sol.fills) > 10 and {"queue", "through"} <= {f.kind for f in sol.fills}
    assert sol.fee_pnl == pytest.approx(sol.volume_usd * 0.0003) and run_result(store, paper.run, "ETH_JPY").fee_pnl == 0
    report = build_paper_report(store, taker_rate=0.0)
    assert "SOL -3.00 ETH_JPY +0.00 bps" in report and "(JPY。" in report and "現物なので" in report and "$" not in report
    assert await mm_main(["replay", "--db", str(db), "--run", str(paper.run)]) == 0
    out = capsys.readouterr().out
    assert out.count("突き合わせ: 一致") == 2 and "損益 (JPY)" in out
    # 記録から読む再生も、手数料と金額の既定値を会場に合わせる。--fee-bps で上書きできる
    assert await mm_main(["replay", "--db", str(db), "--coin", "SOL"]) == 0
    out = capsys.readouterr().out
    assert "size_usd=15000" in out and "メイカー手数料 -3.00 bps" in out and "現物なので" in out
    assert await mm_main(["replay", "--db", str(db), "--coin", "SOL", "--fee-bps", "1"]) == 0
    assert "メイカー手数料 +1.00 bps" in capsys.readouterr().out


async def test_requests_are_spaced_on_venues_that_reject_bursts():
    """GMO は、合計が少なくても続けて投げると ERR-5003 を返す (実測)。1 回ずつ間隔を空けて投げる"""
    starts = []

    class Timed(FakeDomestic):
        async def request(self, method, url, headers=None, body=None):
            starts.append(time.monotonic())
            return await super().request(method, url, headers, body)

    http = Timed()
    assert Gmo.min_gap_sec > 0 and Bitbank.min_gap_sec > 0 and tbf.mm.venues.Hyperliquid.min_gap_sec == 0
    rec = Recorder(http, MmStore(":memory:"), ["SOL", "ETH_JPY", "XRP_JPY"], venue=Gmo(), min_gap=0.03, log=lambda m: None)
    await rec.poll_trades()
    await rec.poll_books()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) == 6 and min(gaps) >= 0.029


# ---------------------------------------------------------------- WebSocket (配信での記録)

import asyncio  # noqa: E402
import base64  # noqa: E402
import io  # noqa: E402
import struct  # noqa: E402

from tbf.mm import ws as wslib  # noqa: E402
from tbf.mm.streams import BitbankStream, GmoStream, StreamError, StreamRecorder  # noqa: E402
from tbf.mm.ws import OP_CLOSE, OP_CONT, OP_PING, OP_PONG, OP_TEXT, WebSocket, WebSocketError, accept_key, encode_frame, read_frame  # noqa: E402


async def frames_of(data: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    out = []
    while not reader.at_eof():
        out.append(await read_frame(reader))
    return out


async def test_websocket_frames_follow_rfc6455():
    # RFC 6455 の例: 握手の鍵、マスク無しの "Hello"、マスクつきの "Hello"
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert encode_frame(OP_TEXT, b"Hello") == bytes.fromhex("810548656c6c6f")
    assert encode_frame(OP_TEXT, b"Hello", mask=bytes.fromhex("37fa213d")) == bytes.fromhex("818537fa213d7f9f4d5158")
    assert await frames_of(bytes.fromhex("818537fa213d7f9f4d5158")) == [(True, OP_TEXT, b"Hello")]  # 読むときはマスクを外す
    # 長さの 3 通りの書き方 (7 ビット / 16 ビット / 64 ビット) の境目
    for n in (0, 1, 125, 126, 65535, 65536, 70000):
        payload = bytes(i % 251 for i in range(n))
        for mask in (None, b"\x01\x02\x03\x04"):
            frame = encode_frame(OP_TEXT, payload, mask=mask)
            assert len(frame) == n + (2 if n < 126 else 4 if n < 65536 else 10) + (4 if mask else 0)
            assert await frames_of(frame) == [(True, OP_TEXT, payload)]
    # 分割 (FIN が立っていない断片 + 続き) と、間に挟まる制御フレーム
    data = encode_frame(OP_TEXT, b"ab", fin=False) + encode_frame(OP_PING, b"p") + encode_frame(OP_CONT, b"cd")
    assert await frames_of(data) == [(False, OP_TEXT, b"ab"), (True, OP_PING, b"p"), (True, OP_CONT, b"cd")]
    with pytest.raises(WebSocketError, match="予約ビット"):
        await frames_of(bytes([0xC1, 0x00]))  # 圧縮などの拡張は扱わない
    with pytest.raises(asyncio.IncompleteReadError):
        await frames_of(encode_frame(OP_TEXT, b"Hello")[:4])  # 途中で切れた


class WsServer:
    """標準ライブラリだけの最小の WebSocket サーバ (テスト用)。handler(send, recv, raw) に 1 接続ぶんの台本を書く"""

    def __init__(self, handler, reject=False, bad_accept=False):
        self.handler, self.reject, self.bad_accept = handler, reject, bad_accept
        self.client_frames = []  # (opcode, 中身, マスクされていたか)
        self.connections = 0

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/stream?x=1"
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    async def _serve(self, reader, writer):
        self.connections += 1
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            self.request_line = head.split("\r\n")[0]
            if self.reject:
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 2\r\n\r\nno")
                await writer.drain()
                return
            key = next(l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.lower().startswith("sec-websocket-key"))
            accept = "bm90IHRoZSByaWdodCBvbmU=" if self.bad_accept else accept_key(key)
            writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())

            async def send(text=None, *, opcode=OP_TEXT, payload=b"", fin=True):
                writer.write(encode_frame(opcode, text.encode() if text is not None else payload, fin=fin))
                await writer.drain()

            async def recv():
                b = await asyncio.wait_for(reader.readexactly(2), 3)  # 返事が来ないときに黙って待ち続けない
                n = b[1] & 0x7F
                if n == 126:
                    n = struct.unpack("!H", await reader.readexactly(2))[0]
                masked = bool(b[1] & 0x80)
                mask = await reader.readexactly(4) if masked else b""
                payload = wslib.apply_mask(await reader.readexactly(n), mask) if masked else await reader.readexactly(n)
                self.client_frames.append((b[0] & 0x0F, payload, masked))
                return b[0] & 0x0F, payload

            await self.handler(send, recv, writer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def test_websocket_client_against_local_server():
    async def script(send, recv, writer):
        assert await recv() == (OP_TEXT, b'{"command":"subscribe"}')
        await send("he", fin=False)  # 分割された 1 通の途中に ping が挟まる
        await send(opcode=OP_PING, payload=b"are-you-there")
        await send(opcode=OP_CONT, payload="llo ".encode(), fin=False)
        await send(opcode=OP_CONT, payload="気配".encode())
        assert await recv() == (OP_PONG, b"are-you-there")  # ping と同じ中身の pong が返る
        await send("x" * 70000)  # 64 ビットの長さ
        await send(opcode=OP_CLOSE, payload=struct.pack("!H", 1001))
        assert (await recv())[0] == OP_CLOSE

    async with WsServer(script) as server:
        ws = await WebSocket.connect(server.url)
        assert server.request_line == "GET /stream?x=1 HTTP/1.1"
        await ws.send('{"command":"subscribe"}')
        assert await asyncio.wait_for(ws.recv(), 3) == "hello 気配"
        assert await asyncio.wait_for(ws.recv(), 3) == "x" * 70000
        assert await asyncio.wait_for(ws.recv(), 3) is None and ws.close_code == 1001  # 相手が閉じた
        await ws.close()
        await asyncio.sleep(0.05)
        assert server.client_frames and all(masked for _, _, masked in server.client_frames)  # 送るフレームは必ずマスクする

    async with WsServer(None, reject=True) as server:
        with pytest.raises(WebSocketError, match="403"):
            await WebSocket.connect(server.url)

    async def hang_up(send, recv, writer):
        await send("one")
        writer.transport.abort()  # close の挨拶なしに切れる

    async with WsServer(hang_up) as server:
        ws = await WebSocket.connect(server.url)
        assert await ws.recv() == "one"
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
            await ws.recv()
        await ws.close()
    with pytest.raises(WebSocketError):
        await WebSocket.connect("https://example.invalid/")  # ws:// か wss:// だけ

    # 握手の応答が合わない相手 (WebSocket を話さない中継など) にはつながない
    async with WsServer(None, bad_accept=True) as server:
        with pytest.raises(WebSocketError, match="Sec-WebSocket-Accept"):
            await WebSocket.connect(server.url)

    async def stray_continuation(send, recv, writer):
        await send(opcode=OP_CONT, payload=b"tail without head")
        await asyncio.sleep(0.2)

    async with WsServer(stray_continuation) as server:
        ws = await WebSocket.connect(server.url)
        with pytest.raises(WebSocketError, match="分割フレーム"):
            await asyncio.wait_for(ws.recv(), 3)
        await ws.close()


async def test_websocket_through_connect_proxy(monkeypatch):
    """HTTPS プロキシ越し: まず CONNECT で相手までの管を開け、その中で握手する"""
    async def script(send, recv, writer):
        await send("via proxy")
        await recv()

    seen = []
    async with WsServer(script) as server:
        async def proxy(reader, writer):
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            seen.append(head.split("\r\n")[0])
            if "blocked" in head:
                writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            up_r, up_w = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")

            async def pipe(src, dst):
                try:
                    while data := await src.read(4096):
                        dst.write(data)
                        await dst.drain()
                except ConnectionError:
                    pass
                dst.close()

            await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))

        p = await asyncio.start_server(proxy, "127.0.0.1", 0)
        port = p.sockets[0].getsockname()[1]
        for name in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{port}")
        assert wslib.proxy_for("ws://feed.test:8123/x") == ("127.0.0.1", port)
        ws = await WebSocket.connect("ws://feed.test:8123/x")
        assert await ws.recv() == "via proxy" and seen == ["CONNECT feed.test:8123 HTTP/1.1"]
        await ws.close()
        with pytest.raises(WebSocketError, match="プロキシが CONNECT blocked.test:80 を通さない"):
            await WebSocket.connect("ws://blocked.test/")
        monkeypatch.setenv("no_proxy", "feed.test")  # NO_PROXY に入っている相手には直結する
        assert wslib.proxy_for("ws://feed.test:8123/x") is None
        p.close()


# 2026-11-23 に実物の配信から切り出した例 (段数だけ減らした)
GMO_WS_BOOK = '{"channel":"orderbooks","asks":[{"price":"9788676","size":"0.04"},{"price":"9788727","size":"0.04"},{"price":"9788931","size":"0.003"}],"bids":[{"price":"9784167","size":"0.02"},{"price":"9783977","size":"0.02"},{"price":"9783900","size":"0.1"}],"symbol":"BTC_JPY","timestamp":"2026-11-23T19:08:18.783Z"}'
GMO_WS_TRADE = '{"channel":"trades","price":"9784194","side":"SELL","size":"0.02","symbol":"BTC_JPY","timestamp":"2026-11-23T19:08:27.096Z"}'
BB_OPEN = '0{"sid":"eqepxGz9SKeMrCyMBmk-","upgrades":[],"pingInterval":25000,"pingTimeout":20000,"maxPayload":1000000}'


def bb_msg(room, data):
    return "42" + json.dumps(["message", {"room_name": room, "message": {"data": data}}])


def test_gmo_stream_reads_books_and_taker_side_trades():
    st = GmoStream(["BTC_JPY", "ETH_JPY"], depth=2)
    start = st.start()
    # 購読の命令は 1 秒に 1 回まで (公式)。約定は TAKER_ONLY で、仕掛けた側の 1 通だけを受け取る
    assert [json.loads(t) for t, _ in start][:2] == [
        {"command": "subscribe", "channel": "orderbooks", "symbol": "BTC_JPY"},
        {"command": "subscribe", "channel": "trades", "symbol": "BTC_JPY", "option": "TAKER_ONLY"}]
    assert len(start) == 4 and all(gap >= 1.0 for _, gap in start)
    ((kind, coin, book),) = st.feed(GMO_WS_BOOK, 0)
    assert (kind, coin, book.time) == ("book", "BTC_JPY", 1795460898783)
    assert book.bids == ((9784167.0, 0.02), (9783977.0, 0.02)) and book.asks == ((9788676.0, 0.04), (9788727.0, 0.04))
    ((_, _, (t1,)),) = st.feed(GMO_WS_TRADE, 0)
    assert (t1.time, t1.side, t1.px, t1.sz) == (1795460907096, "A", 9784194.0, 0.02)  # SELL = 売り手主導。向きが分かる
    ((_, _, (t2,)),) = st.feed(GMO_WS_TRADE, 0)  # 同じ時刻・同じ中身の約定がもう 1 件 (id が無いので通し番号で分ける)
    ((_, _, (t3,)),) = st.feed(GMO_WS_TRADE.replace('"SELL"', '"BUY"'), 0)
    assert len({t1.tid, t2.tid, t3.tid}) == 3 and t3.side == "B"
    assert st.feed(GMO_WS_BOOK.replace("BTC_JPY", "SOL"), 0) == []  # 頼んでいない銘柄は捨てる
    assert st.feed('{"channel":"ticker","symbol":"BTC_JPY","last":"1"}', 0) == []
    with pytest.raises(StreamError, match="ERR-5003"):
        st.feed('{"error":"ERR-5003 Request too many."}', 0)


def test_bitbank_stream_handshake_and_book_from_whole_plus_diffs():
    st = BitbankStream(["xrp_jpy"], depth=2)
    assert st.start() == [] and st.feed(BB_OPEN, 0) == [("send", "40")]  # Engine.IO が開いたら socket.io の接続を頼む
    joins = st.feed('40{"sid":"1YfR82Il91wcr9ZHBmk_"}', 0)
    assert [e[1] for e in joins] == ['42["join-room", "transactions_xrp_jpy"]', '42["join-room", "depth_whole_xrp_jpy"]',
                                     '42["join-room", "depth_diff_xrp_jpy"]']
    assert st.feed("2", 0) == [("send", "3")]  # サーバーの ping には pong

    ((_, coin, trades),) = st.feed(bb_msg("transactions_xrp_jpy", {"transactions": [
        {"transaction_id": 1241163071, "side": "sell", "price": "151.410", "amount": "0.6440", "executed_at": 1795460906279},
        {"transaction_id": 1241163073, "side": "buy", "price": "151.411", "amount": "2500.0000", "executed_at": 1795460906280}]}), 0)
    assert coin == "xrp_jpy" and [(t.time, t.side, t.px, t.sz, t.tid) for t in trades] == [
        (1795460906279, "A", 151.41, 0.644, 1241163071), (1795460906280, "B", 151.411, 2500.0, 1241163073)]

    # 公式の手順の例: diff{3}, diff{5}, diff{6}, diff{8}, whole{5} の順に届いたら、whole で置き換えてから 6 と 8 だけを当て直す
    diff = lambda s, b, a, t: st.feed(bb_msg("depth_diff_xrp_jpy", {"a": a, "b": b, "t": t, "s": str(s)}), 0)  # noqa: E731
    assert diff(3, [["151.409", "999"]], [], 1000) == []  # 板の全体が来るまでは何も出さない
    assert diff(5, [["151.300", "5"]], [], 1001) == []
    assert diff(6, [["151.410", "0"], ["151.408", "70"]], [], 1002) == []
    assert diff(8, [], [["151.411", "20"], ["151.412", "0"]], 1003) == []
    ((_, _, book),) = st.feed(bb_msg("depth_whole_xrp_jpy", {
        "asks": [["151.411", "36118.9371"], ["151.412", "50"], ["151.420", "250"]],
        "bids": [["151.410", "4930.2439"], ["151.400", "2500"], ["151.300", "5"]], "timestamp": 1001, "sequenceId": "5"}), 0)
    assert book.bids == ((151.408, 70.0), (151.4, 2500.0)) and book.asks == ((151.411, 20.0), (151.42, 250.0))
    assert book.time == 1003  # 当て直した差分のうち最後の時刻
    assert diff(7, [["151.409", "1"]], [], 1004) == []  # 古い順番の差分は当てない
    ((_, _, book),) = diff(9, [["151.409", "3"]], [], 1005)
    assert book.bids == ((151.409, 3.0), (151.408, 70.0)) and book.time == 1005
    # 差分の途中で買いと売りが逆転して見える間は、気配として残さない
    assert diff(10, [["151.411", "1"]], [], 1006) == []
    with pytest.raises(StreamError):
        st.feed('44{"message":"invalid namespace"}', 0)


class FakeInstruments:
    """StreamRecorder.setup が銘柄の情報を取りにいく REST だけ真似る"""

    async def request(self, method, url, headers=None, body=None):
        return 200, json.dumps(BITBANK_PAIRS if url.endswith("/spot/pairs") else GMO_SYMBOLS)


async def test_stream_recorder_keeps_changes_reconnects_and_logs_gaps(tmp_path, monkeypatch):
    whole = lambda bid, t, seq: bb_msg("depth_whole_xrp_jpy", {  # noqa: E731
        "asks": [[f"{bid + 0.001 + i / 1000:.3f}", "50"] for i in range(8)],
        "bids": [[f"{bid - i / 1000:.3f}", "40"] for i in range(8)], "timestamp": t, "sequenceId": str(seq)})
    tx = lambda tid, side, px, t: bb_msg("transactions_xrp_jpy", {"transactions": [  # noqa: E731
        {"transaction_id": tid, "side": side, "price": px, "amount": "3.0000", "executed_at": t}]})

    async def script(send, recv, writer):
        await send(BB_OPEN)
        assert await recv() == (OP_TEXT, b"40")
        await send('40{"sid":"x"}')
        rooms = [json.loads((await recv())[1][2:])[1] for _ in range(3)]
        assert rooms == ["transactions_xrp_jpy", "depth_whole_xrp_jpy", "depth_diff_xrp_jpy"]
        if server.connections == 1:
            await send(whole(146.859, 1000, 10))
            await send(bb_msg("depth_diff_xrp_jpy", {"a": [], "b": [["146.700", "9"]], "t": 1100, "s": "11"}))  # 上位 5 段は変わらない
            await send(bb_msg("depth_diff_xrp_jpy", {"a": [], "b": [["146.859", "25"]], "t": 1200, "s": "12"}))  # 最良買いの数量が変わる
            await send(tx(501, "sell", "146.859", 1250))
            await send("2")
            assert await recv() == (OP_TEXT, b"3")
            writer.transport.abort()  # 回線が切れる
        else:
            await send(whole(146.900, 5000, 30))
            await send(tx(502, "buy", "146.901", 5100))
            await send(tx(502, "buy", "146.901", 5100))  # 同じ約定がもう一度来ても 1 件
            await asyncio.sleep(5)

    async with WsServer(script) as server:
        monkeypatch.setattr(BitbankStream, "url", server.url)
        store = MmStore(tmp_path / "ws.sqlite")
        logs = []
        now = [10_000]

        def clock():
            now[0] += 1
            return now[0]

        rec = StreamRecorder(store, Bitbank(), ["xrp_jpy"], depth=5, clock=clock, log=logs.append, backoff=(0.05, 0.2))
        await rec.setup(FakeInstruments())
        assert (store.venue(), store.source()) == ("bitbank", "ws") and store.instrument("xrp_jpy").maker_rate == -0.0002
        await rec.run(max_seconds=1.0)

    books = store.books("xrp_jpy")
    assert [b.time for b in books] == [1000, 1200, 5000]  # 上位の段が変わったときだけ残す (1100 は残さない)
    assert len(books[0].bids) == 5 and books[1].bids[0] == (146.859, 25.0) and books[2].best_bid == 146.9
    assert [(t.time, t.side, t.tid) for t in store.trades("xrp_jpy")] == [(1250, "A", 501), (5100, "B", 502)]
    assert (rec.reconnects, rec.books_seen, rec.books_kept, rec.trades_kept) == (1, 4, 3, 2)
    # 切れていた間の約定は取れていない。つなぎ直したときに gaps に残す
    (gap,) = store.query("SELECT * FROM gaps")
    assert gap["coin"] == "xrp_jpy" and gap["after"] < gap["before"]
    polls = store.query("SELECT kind, error FROM polls ORDER BY rowid")
    assert [p["kind"] for p in polls] == ["ws"] * 3 and [p["error"] is None for p in polls] == [True, False, True]
    assert any("つなぎ直します" in m for m in logs)
    assert store.query("SELECT MIN(recv - time) FROM books")[0][0] > 0  # 受け取った時刻と取引所の時刻の両方が残る
    report = build_report(store)
    assert "配信の切断 1 回" in report and "配信の遅れ" in report and "ポーリング" not in report

    # 配信の記録とポーリングの記録は同じ db に混ぜない (再生の仮定が違う)
    with pytest.raises(ValueError, match=r"配信 \(ws\) の記録"):
        await Recorder(FakeDomestic(), store, ["xrp_jpy"], venue=Bitbank(), min_gap=0, log=lambda m: None).setup()
    rest = MmStore(tmp_path / "rest.sqlite")
    r = Recorder(FakeDomestic(), rest, ["xrp_jpy"], venue=Bitbank(), min_gap=0, log=lambda m: None)
    await r.setup()
    await r.poll_books()
    assert rest.source() == "rest"
    with pytest.raises(ValueError, match=r"ポーリング \(rest\) の記録"):
        await StreamRecorder(rest, Bitbank(), ["xrp_jpy"], log=lambda m: None).setup(FakeInstruments())
    with pytest.raises(ValueError, match="未対応"):
        StreamRecorder(rest, tbf.mm.venues.Hyperliquid(), ["INJ"])


def test_queue_shrinks_with_the_book_only_for_stream_recordings():
    # 買い 99.9 に並ぶ (前に 100 枚)。次の気配で、その値段に見えている数量が 20 枚に減った (前の人が取り消した)
    books = [Book(0, ((99.9, 100.0),), ((100.1, 100.0),)), Book(10 * SEC, ((99.9, 20.0),), ((100.1, 100.0),)),
             Book(20 * SEC, ((99.9, 20.0),), ((100.1, 100.0),)), Book(30 * SEC, ((99.9, 20.0),), ((100.1, 100.0),))]
    trades = [Trade(15 * SEC, "A", 99.9, 25)]
    # ポーリングの記録 (今までどおり): 列は約定でしか減らないので、25 枚の売りでは届かない
    assert run(books, trades, fixed(bid=99.9)).fills == []
    # 配信の記録: 見えている数量 (20) が前の列の上限。25 枚の売りのうち 5 枚が自分に当たる
    res = run(books, trades, fixed(bid=99.9), queue_from_book=True)
    assert [(f.px, f.size, f.kind) for f in res.fills] == [(99.9, 5, "queue")]
    # 見えている数量が増えても列は増えない (後から並んだ人は自分の後ろ)
    grown = [books[0], Book(10 * SEC, ((99.9, 500.0),), ((100.1, 100.0),)), *books[2:]]
    assert [f.size for f in run(grown, [Trade(15 * SEC, "A", 99.9, 105)], fixed(bid=99.9), queue_from_book=True).fills] == [5]
    # その値段が記録した段数の外に出て見えなくなったら、分からないので減らさない
    away = [books[0], Book(10 * SEC, ((100.0, 5.0),), ((100.2, 100.0),)), Book(20 * SEC, ((100.0, 5.0),), ((100.2, 100.0),))]
    assert run(away, [Trade(15 * SEC, "A", 99.9, 25)], fixed(bid=99.9), queue_from_book=True).fills == []
    text = format_result("x", res, {}, maker_rate=0, taker_rate=0, latency_ms=0, trade_gaps=0, queue_from_book=True)
    assert "配信の記録" in text and "配信の記録" not in format_result("x", res, {}, maker_rate=0, taker_rate=0, latency_ms=0, trade_gaps=0)


async def test_paper_on_stream_matches_replay(tmp_path, capsys):
    db = tmp_path / "ws.sqlite"
    store = MmStore(db)
    now = [1_795_460_000_000]
    rec = StreamRecorder(store, Gmo(), ["XRP_JPY"], depth=5, clock=lambda: now[0], log=lambda m: None)
    await rec.setup(FakeInstruments())
    params = parse_params(scale_defaults(BASELINE_PARAMS, "JPY"), ["half_spread_bps=0.15", "skew_bps=0.2"])
    paper = Paper(store, ["XRP_JPY"], "baseline", params, latency_ms=250, interval=0, trades_interval=0, depth=5,
                  clock=lambda: now[0], log=lambda m: None)
    rec.on_book, rec.on_trades = paper.on_book, paper.on_trades
    stream = GmoStream(["XRP_JPY"], 5)
    rng = random.Random(21)
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"  # noqa: E731
    bid = 151300
    for _ in range(1500):
        now[0] += rng.randint(200, 700)
        bid += rng.choice([-4, -1, 0, 0, 1, 4])
        t = now[0] - 90  # 取引所の時刻は受け取りより少し前
        msgs = [json.dumps({"channel": "orderbooks", "symbol": "XRP_JPY", "timestamp": iso(t),
                            "bids": [{"price": f"{(bid - i) / 1000:.3f}", "size": str(rng.choice([30, 200, 500]))} for i in range(8)],
                            "asks": [{"price": f"{(bid + 3 + i) / 1000:.3f}", "size": str(rng.choice([30, 200, 500]))} for i in range(8)]})]
        for _ in range(rng.choice([0, 0, 1, 2])):
            side = rng.choice(["BUY", "SELL"])
            px = bid + (3 if side == "BUY" else 0) + rng.choice([-3, -1, 0, 0, 1, 3])
            msgs.append(json.dumps({"channel": "trades", "symbol": "XRP_JPY", "side": side, "price": f"{px / 1000:.3f}",
                                    "size": str(rng.choice([10, 100, 400])), "timestamp": iso(t + rng.randint(1, 80))}))
        for m in msgs:
            rec.handle(stream.feed(m, now[0]), now[0])
        rec.settle(now[0])
    paper.stop()
    live = run_result(store, paper.run, "XRP_JPY")
    assert len(live.fills) > 30 and {"through", "queue"} <= {f.kind for f in live.fills}
    assert all(t.side in "AB" for t in store.trades("XRP_JPY"))  # 配信の記録には向きの分からない約定が無い
    acct = store.query("SELECT late_trades, late_books FROM paper_account ORDER BY minute DESC LIMIT 1")[0]
    assert (acct["late_trades"], acct["late_books"]) == (0, 0)
    assert await mm_main(["replay", "--db", str(db), "--run", str(paper.run)]) == 0
    out = capsys.readouterr().out
    assert "突き合わせ: 一致" in out and "配信の記録" in out
    assert "配信で記録" in build_paper_report(store, taker_rate=0.0)
    # 配信の記録だから列を気配でも減らしている。その仮定を外すと結果が変わる (= 仮定が効いていて、記録の種類で切り替わっている)
    first, last = run_span(store, paper.run, "XRP_JPY")
    plain = simulate(store.books("XRP_JPY", first, last), store.trades("XRP_JPY", first, last), baseline, params,
                     instrument=store.instrument("XRP_JPY"), latency_ms=250)
    assert fill_keys(plain.fills) != fill_keys(live.fills)
