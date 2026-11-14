"""TRA-65 のシャドーモード計測。

依頼票が明示した「必ずテストを書く」対象:
  半スプレッド取り分 / 逆選択の計算 / 約定率の保守・楽観の 2 推定 / 年率換算
に加えて、穴をゼロ埋めしないこと・宣言グリッドを設定で動かせないこと・
収集が注文系 API を叩かないことも固定する。
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest

from tbf.jpmaker import declared
from tbf.jpmaker.collect import Collector
from tbf.jpmaker.config import load_config
from tbf.jpmaker.hedge import HlSample, hedge_cost, slippage_ratio, walk_levels
from tbf.jpmaker.measure import Cell, PairData, economics, load, run_all, window_bounds
from tbf.jpmaker.report import build_report, walkforward
from tbf.jpmaker.sim import (
    BookSample,
    Fill,
    Trade,
    adverse_loss_jpy,
    best_price_runs,
    mid_at,
    simulate,
    tape_side_sanity,
)
from tbf.jpmaker.store import JpMakerStore

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_700_000_000_000


def book(n: int, bid: float, ask: float, bid_size: float = 100.0, ask_size: float = 100.0, start: int = T0, step: int = 1000):
    return [BookSample(start + i * step, bid, bid_size, ask, ask_size) for i in range(n)]


# --- 1. 最良気配の滞在時間 ---
def test_best_price_runs_splits_on_change_and_gap():
    samples = book(3, 100.0, 101.0) + book(2, 100.5, 101.0, start=T0 + 3000) + book(2, 100.5, 101.0, start=T0 + 60_000)
    runs = best_price_runs(samples, "buy")
    assert [r.closed_by for r in runs] == ["change", "gap", "end"]
    # 値段が動いて閉じた区間だけが滞在時間。穴をまたいで「続いていた」ことにしない
    assert runs[0].seconds == pytest.approx(3.0)
    assert runs[0].price == 100.0
    assert runs[1].price == 100.5 and runs[1].seconds == pytest.approx(1.0)
    # ask 側は動いていないので 1 区間 (末尾で打ち切り) + 穴
    assert [r.closed_by for r in best_price_runs(samples, "sell")] == ["gap", "end"]


# --- 2. 約定率 (保守 / 楽観の 2 推定) ---
def test_fill_estimates_conservative_vs_optimistic():
    # bid 100 に 100 枚 (= 10,000 円) 見えている。そこへ成行売り 15,000 円が来る
    samples = book(4, 100.0, 101.0, bid_size=100.0)
    trades = [Trade(T0 + 1500, "sell", 100.0, 150.0)]  # 15,000 円

    opt = simulate("p", samples, trades, "buy", 50_000.0, "optimistic")
    assert len(opt.fills) == 1
    # 自分が先頭なら 15,000 円まるごと取れる
    assert opt.fills[0].notional_jpy == pytest.approx(15_000.0)

    con = simulate("p", samples, trades, "buy", 50_000.0, "conservative")
    # 前に 10,000 円居たので、自分に届くのは 5,000 円だけ
    assert len(con.fills) == 1
    assert con.fills[0].notional_jpy == pytest.approx(5_000.0)
    # 2 推定を 1 つに丸めていないこと
    assert con.fills[0].notional_jpy < opt.fills[0].notional_jpy
    assert con.taker_notional_jpy == pytest.approx(15_000.0)


def test_fill_capped_by_order_size_and_requote_resets_queue():
    samples = book(2, 100.0, 101.0, bid_size=0.0) + book(2, 99.0, 101.0, bid_size=0.0, start=T0 + 2000)
    # 1 件目で 5 万円いっぱい約定し、置き直してからもう一度約定する
    trades = [Trade(T0 + 500, "sell", 100.0, 1000.0), Trade(T0 + 2500, "sell", 99.0, 1000.0)]
    res = simulate("p", samples, trades, "buy", 50_000.0, "optimistic")
    assert [f.notional_jpy for f in res.fills] == [pytest.approx(50_000.0), pytest.approx(50_000.0)]
    assert [f.price for f in res.fills] == [100.0, 99.0]
    assert res.requotes >= 2  # 約定しきって出し直した分 + 値段が動いた分


def test_trade_on_wrong_side_or_through_price_is_not_a_fill():
    samples = book(3, 100.0, 101.0, bid_size=0.0)
    # 成行買い (ask を食う) は買い指値を約定させない
    assert simulate("p", samples, [Trade(T0 + 500, "buy", 101.0, 10.0)], "buy", 50_000.0, "optimistic").fills == []
    # 自分の指値より高い値段で売れた = 自分の列には届いていない
    assert simulate("p", samples, [Trade(T0 + 500, "sell", 100.5, 10.0)], "buy", 50_000.0, "optimistic").fills == []
    # 自分の指値より安い値段まで board が掘られたら約定する
    assert len(simulate("p", samples, [Trade(T0 + 500, "sell", 99.0, 10.0)], "buy", 50_000.0, "optimistic").fills) == 1


def test_fills_during_gap_are_not_credited():
    samples = book(2, 100.0, 101.0, bid_size=0.0) + book(2, 100.0, 101.0, bid_size=0.0, start=T0 + 120_000)
    trades = [Trade(T0 + 60_000, "sell", 100.0, 100.0)]
    res = simulate("p", samples, trades, "buy", 50_000.0, "optimistic")
    # 穴の中の約定は自分の約定に数えない (指値が乗っていたか分からない)
    assert res.fills == []
    assert res.unarmed_notional_jpy == pytest.approx(10_000.0)
    # 穴は標本と標本の間の生の長さで数える (T0+1,000 -> T0+120,000)
    assert res.skipped_gap_ms == 119_000


# --- 半スプレッド取り分 ---
def test_half_spread_both_sides():
    buy = Fill(T0, "p", "buy", 99.0, 10_000.0, 100.0, "optimistic")
    sell = Fill(T0, "p", "sell", 101.0, 10_000.0, 100.0, "optimistic")
    assert buy.half_spread_ratio == pytest.approx(0.01)
    assert sell.half_spread_ratio == pytest.approx(0.01)
    # 中値より不利な値段で約定したら取り分はマイナス
    assert Fill(T0, "p", "buy", 101.0, 1.0, 100.0, "optimistic").half_spread_ratio == pytest.approx(-0.01)


# --- 3. 逆選択 ---
def test_adverse_selection_sign_and_unmeasured():
    samples = book(1, 99.0, 101.0) + book(1, 98.0, 100.0, start=T0 + 60_000)  # 中値 100 -> 99
    times = [s.time for s in samples]
    buy = Fill(T0, "p", "buy", 99.0, 10_000.0, 100.0, "optimistic")
    # 買った直後に 1% 下がった = 100 円の損
    assert adverse_loss_jpy(buy, samples, times, 60) == pytest.approx(100.0)
    sell = Fill(T0, "p", "sell", 101.0, 10_000.0, 100.0, "optimistic")
    # 売った直後に下がったのは得 (損がマイナス)
    assert adverse_loss_jpy(sell, samples, times, 60) == pytest.approx(-100.0)
    # 地平の標本が無ければ **ゼロではなく未測定**
    assert adverse_loss_jpy(buy, samples, times, 1800) is None


def test_mid_at_does_not_return_stale_price():
    samples = book(1, 99.0, 101.0)
    times = [s.time for s in samples]
    assert mid_at(samples, times, T0 + 1000, 3.0) == pytest.approx(100.0)
    # 許容を超えて古い標本をそのまま返すと「値段が動かなかった」ことにされる
    assert mid_at(samples, times, T0 + 600_000, 60.0) is None
    assert mid_at([], [], T0, 10.0) is None


def test_tape_side_sanity_detects_reversed_side():
    samples = book(5, 99.0, 101.0)
    times = [s.time for s in samples]
    normal = [Trade(T0 + 1000, "sell", 99.0, 1.0), Trade(T0 + 2000, "buy", 101.0, 1.0)]
    s = tape_side_sanity(samples, times, normal)
    assert s["sell_rel_bps"] < 0 < s["buy_rel_bps"]
    reversed_tape = [Trade(T0 + 1000, "sell", 101.0, 1.0), Trade(T0 + 2000, "buy", 99.0, 1.0)]
    r = tape_side_sanity(samples, times, reversed_tape)
    assert r["sell_rel_bps"] > 0 > r["buy_rel_bps"]


# --- 4. ヘッジ ---
def test_walk_levels_and_slippage():
    bids = [(100.0, 1.0), (99.0, 2.0), (98.0, 10.0)]
    avg, filled = walk_levels(bids, 2.0)
    assert filled == pytest.approx(2.0)
    assert avg == pytest.approx((100.0 * 1 + 99.0 * 1) / 2)
    # 記録した深さで足りなければ足りた分だけ
    assert walk_levels(bids, 100.0)[1] == pytest.approx(13.0)

    s = HlSample(T0, bids=bids, asks=[(102.0, 1.0), (103.0, 5.0)])
    assert s.mid == pytest.approx(101.0)
    assert slippage_ratio(s, "sell", 1.0) == pytest.approx((101.0 - 100.0) / 101.0)
    assert slippage_ratio(s, "buy", 1.0) == pytest.approx((102.0 - 101.0) / 101.0)
    # 板が足りなければ **0 ではなく None** (推定で埋めない)
    assert slippage_ratio(s, "sell", 999.0) is None


def test_hedge_cost_includes_taker_legs_and_reports_drift():
    deep = [(100.0, 1000.0)]
    at_fill = HlSample(T0, bids=deep, asks=[(100.0, 1000.0)])
    later = HlSample(T0 + 2000, bids=[(99.0, 1000.0)], asks=[(99.0, 1000.0)])
    samples = [at_fill, later]
    times = [s.time for s in samples]
    fill = Fill(T0, "p", "buy", 100.0, 10_000.0, 100.0, "conservative")
    h = hedge_cost(fill, samples, times, delay_ms=2000, tolerance_sec=3.0)
    assert h["slippage"] == pytest.approx(0.0)
    assert h["taker"] == pytest.approx(declared.HL_TAKER_FEE * declared.HL_TAKER_LEGS)
    assert h["cost_ratio"] == pytest.approx(h["slippage"] + h["taker"])
    # 在庫を売ってヘッジする間に中値が下がった = 時間差の間の値動きは損側
    assert h["drift"] == pytest.approx(0.01)
    assert h["used_delay_ms"] == pytest.approx(2000)
    # 板の記録が無い時刻は None (ゼロ埋めしない)
    assert hedge_cost(fill, samples, times, delay_ms=600_000, tolerance_sec=10.0) is None


# --- 純益と年率換算 ---
def test_net_formula_and_annualisation():
    data = PairData(pair="p")
    data.samples = book(1, 99.0, 101.0) + book(1, 99.0, 101.0, start=T0 + 60_000)
    deep = [(100.0, 100_000.0)]
    data.hl = [HlSample(T0, deep, deep), HlSample(T0 + 1300, deep, deep)]
    fill = Fill(T0, "p", "buy", 99.0, 100_000.0, 100.0, "conservative")
    econ = economics(data, [fill], delay_ms=1300)[0]
    assert econ.measured
    # 粗利 = 10 万円 x (1% + 0.02%)
    assert econ.gross_jpy == pytest.approx(100_000.0 * (0.01 + declared.BITBANK_MAKER_REBATE))
    assert econ.adverse_jpy == pytest.approx(0.0)  # 中値が動いていない
    assert econ.hedge_jpy == pytest.approx(100_000.0 * declared.HL_TAKER_FEE * declared.HL_TAKER_LEGS)
    assert econ.net_jpy == pytest.approx(econ.gross_jpy - econ.hedge_jpy)

    # 年率 = 純益 / 投下資本 x (1 年 / 実測秒数)。穴はゼロ埋めせず分母からも外す
    cell = Cell(scope="p", size_jpy=50_000.0, mode="conservative", n_pairs=1)
    cell.gross_jpy, cell.measured_sec = 1_000.0, 86_400.0
    cap = declared.capital_jpy(50_000.0, 1)
    assert cell.capital_jpy == pytest.approx(50_000.0 * 2 * 1.5)
    assert cell.apy == pytest.approx(1_000.0 / cap * 365)
    half = Cell(scope="p", size_jpy=50_000.0, mode="conservative", n_pairs=1)
    half.gross_jpy, half.measured_sec = 1_000.0, 43_200.0
    assert half.apy == pytest.approx(cell.apy * 2)  # 半日で同じ額なら年率は 2 倍
    assert Cell(scope="p", size_jpy=50_000.0, mode="conservative").apy is None  # 実測秒数 0 なら未測定


def test_unmeasured_fill_is_excluded_not_zero_filled():
    data = PairData(pair="p")
    data.samples = book(1, 99.0, 101.0)
    data.hl = []  # ヘッジ先の記録が無い
    fill = Fill(T0, "p", "buy", 99.0, 100_000.0, 100.0, "conservative")
    econ = economics(data, [fill], delay_ms=1300)[0]
    assert econ.hedge is None and econ.measured is False and econ.net_jpy is None


# --- 窓の切り方 ---
def test_window_bounds_align_to_utc_midnight():
    day = 86_400_000
    since = 1_700_000_000_000
    wins = window_bounds(since, since + int(2.5 * day), 1.0)
    assert len(wins) == 4  # 端数の窓が前後に出る
    assert wins[0][0] == since and wins[-1][1] == since + int(2.5 * day)
    assert all(b % day == 0 for _, b in wins[:-1])  # 境界は UTC 0 時
    assert all(a < b for a, b in wins)


def test_short_window_is_not_judged(tmp_path):
    """3 分しか測っていない区間を「1 日窓」として合否判定に使わないこと"""
    store = JpMakerStore(tmp_path / "db.sqlite")
    for pair, coin in declared.PAIRS.items():
        for i in range(180):
            t = T0 + i * 1000
            store.add_book(t, t, pair, 99.0, 100.0, 101.0, 100.0, None)
            store.add_hl_book(t, t, coin, [["1.0", "100000"]], [["1.01", "100000"]])
    store.commit()
    since, until = T0, T0 + 180_000
    m = run_all(load(store, declared.PAIRS, since, until), since, until, 1.0, 1250.0)
    rows = walkforward(m, 50_000.0, 1.0)
    assert len(rows) == 1
    # カバレッジは足りている (1 秒格子でほぼ埋まっている) が、窓幅が 1 日の 0.2% しかない
    assert rows[0].coverage >= declared.MIN_WINDOW_COVERAGE
    assert rows[0].span_ratio < declared.MIN_WINDOW_SPAN_RATIO
    assert rows[0].usable is False
    text = build_report(store, m)
    assert "判定から除外" in text and "判定不能" in text
    # 判定可能な窓が無いときに「不合格」とも「合格」とも書かないこと
    assert "**合格**" not in text


def test_stability_rule_counts_windows_below_floor():
    # 床を割る窓が半分を超えたら不合格、という線が窓の列から出せること
    apys = [0.30, 0.01, -0.05, 0.02]
    below = sum(1 for a in apys if a < declared.FLOOR_APY)
    assert below / len(apys) > declared.MAX_BELOW_FLOOR_RATIO
    assert declared.PASS_APY == pytest.approx(declared.FLOOR_APY * 3)


# --- 設定が宣言グリッドを動かせないこと ---
def test_config_rejects_grid_changes(tmp_path):
    base = (ROOT / "jp_maker.toml").read_text()
    cfg = load_config(ROOT / "jp_maker.toml")
    assert set(cfg.pair_names) == set(declared.PAIRS)

    # 成績の良い銘柄だけ残す操作を設定ファイルからできてしまってはいけない
    dropped = base.replace('[[pairs]]\npair = "op_jpy"\nhedge_coin = "OP"\n', "")
    p = tmp_path / "dropped.toml"
    p.write_text(dropped)
    with pytest.raises(ValueError):
        load_config(p)

    p2 = tmp_path / "size.toml"
    p2.write_text(base.replace("sizes_jpy = [50000.0, 200000.0]", "sizes_jpy = [10000.0]"))
    with pytest.raises(ValueError):
        load_config(p2)

    p3 = tmp_path / "fast.toml"
    p3.write_text(base.replace("book_poll_sec = 1.0", "book_poll_sec = 0.1"))
    with pytest.raises(ValueError):
        load_config(p3)

    p4 = tmp_path / "hedge.toml"
    p4.write_text(base.replace('hedge_coin = "RENDER"', 'hedge_coin = "BTC"'))
    with pytest.raises(ValueError):
        load_config(p4)


# --- 収集 (HTTP はフィクスチャ) ---
class FakeHttp:
    """公開エンドポイントだけを真似る。注文系を叩いたら即座に落とす"""

    FORBIDDEN = ("/user/", "/order", "/assets", "/withdraw", "/exchange")

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.fail_depth = False
        self.tape = [
            {"transaction_id": 1, "side": "sell", "price": "100.0", "amount": "1.0", "executed_at": T0},
            {"transaction_id": 2, "side": "buy", "price": "101.0", "amount": "2.0", "executed_at": T0 + 1000},
        ]

    async def request(self, method, url, headers=None, body=None):
        for bad in self.FORBIDDEN:
            assert bad not in url, f"注文系 API を叩いている: {url}"
        assert "api.bitbank.cc" not in url, f"private API のホストを叩いている: {url}"
        self.calls.append((method, url))
        if url.endswith("/depth"):
            if self.fail_depth:
                return 429, "too many requests"
            return 200, json.dumps(
                {
                    "success": 1,
                    "data": {
                        "bids": [["100.0", "10.0"], ["99.0", "5.0"]],
                        "asks": [["101.0", "7.0"]],
                        "timestamp": T0,
                        "sequenceId": "42",
                    },
                }
            )
        if url.endswith("/transactions"):
            return 200, json.dumps({"success": 1, "data": {"transactions": self.tape}})
        if "hyperliquid" in url:
            assert json.loads(body)["type"] == "l2Book"
            return 200, json.dumps(
                {
                    "coin": json.loads(body)["coin"],
                    "time": T0,
                    "levels": [[{"px": "1.0", "sz": "10", "n": 1}], [{"px": "1.01", "sz": "10", "n": 1}]],
                }
            )
        raise AssertionError(f"想定外の URL: {url}")


def test_collector_records_and_never_touches_order_api(tmp_path):
    cfg = load_config(ROOT / "jp_maker.toml")
    store = JpMakerStore(tmp_path / "db.sqlite")
    http = FakeHttp()
    col = Collector(cfg, http, store, log=lambda _: None, clock=lambda: T0)

    assert asyncio.run(col.poll_book("render_jpy")) is True
    assert asyncio.run(col.poll_tape("render_jpy")) is True
    assert asyncio.run(col.poll_hl("RENDER")) is True
    store.commit()

    row = store.query("SELECT * FROM book")[0]
    assert (row["bid"], row["bid_size"], row["ask"], row["ask_size"]) == (100.0, 10.0, 101.0, 7.0)
    assert store.query("SELECT COUNT(*) n FROM trades")[0]["n"] == 2
    assert json.loads(store.query("SELECT bids_json FROM hl_book")[0]["bids_json"]) == [["1.0", "10"]]
    assert all(m == "GET" or "hyperliquid" in u for m, u in http.calls)

    # 同じテープを 2 回取っても重複しない
    asyncio.run(col.poll_tape("render_jpy"))
    assert store.query("SELECT COUNT(*) n FROM trades")[0]["n"] == 2

    # 429 は polls に記録して False を返す (呼び出し側がバックオフする)
    http.fail_depth = True
    assert asyncio.run(col.poll_book("render_jpy")) is False
    assert store.query("SELECT COUNT(*) n FROM polls WHERE status=429")[0]["n"] == 1


def test_tape_overflow_is_flagged_not_filled(tmp_path):
    cfg = load_config(ROOT / "jp_maker.toml")
    store = JpMakerStore(tmp_path / "db.sqlite")
    http = FakeHttp()
    col = Collector(cfg, http, store, log=lambda _: None, clock=lambda: T0)
    asyncio.run(col.poll_tape("render_jpy"))
    # 返ってきた全件が未知 = その手前が 60 件の窓から落ちた可能性がある
    http.tape = [
        {"transaction_id": 10, "side": "sell", "price": "100.0", "amount": "1.0", "executed_at": T0 + 500_000},
        {"transaction_id": 11, "side": "sell", "price": "100.0", "amount": "1.0", "executed_at": T0 + 501_000},
    ]
    asyncio.run(col.poll_tape("render_jpy"))
    store.commit()
    assert store.query("SELECT COUNT(*) n FROM tape_overflow")[0]["n"] == 1


def test_report_runs_end_to_end(tmp_path):
    """記録 -> 集計 -> レポートが通り、窓ごとの行と合否が出ること

    2.2 日ぶんを 30 秒格子で作る (窓幅の下限を満たすので実際に判定まで走る)。
    """
    step = 30_000
    span = int(2.2 * 86_400_000)
    store = JpMakerStore(tmp_path / "db.sqlite")
    for pair, coin in declared.PAIRS.items():
        for i in range(span // step):
            t = T0 + i * step
            store.add_book(t, t, pair, 99.0, 0.0, 101.0, 0.0, None)  # 前に誰も居ない板
            store.add_hl_book(t, t, coin, [["1.0", "100000"]], [["1.01", "100000"]])
        # 10 分ごとに成行売りが来る (買い指値が約定する)
        store.add_trades(
            pair,
            [
                {"transaction_id": i, "side": "sell", "price": "99.0", "amount": "100.0",
                 "executed_at": T0 + i * 600_000}
                for i in range(1, span // 600_000)
            ],
        )
        store.add_poll(T0, "hyperliquid", "l2Book", 250.0, 200, None)
        store.add_poll(T0, "bitbank", "depth", 120.0, 200, None)
    store.commit()

    since, until = T0, T0 + span
    data = load(store, declared.PAIRS, since, until)
    assert len(data["render_jpy"].samples) == span // step
    m = run_all(data, since, until, step / 1000, 1250.0)
    rows = walkforward(m, 50_000.0, 1.0)
    assert len(rows) >= 3
    judged = [r for r in rows if r.usable]
    assert judged, "1 日窓が判定に使えていない"
    assert judged[0].n_measured > 0 and judged[0].declared_apy is not None
    assert judged[0].efficiency is not None and not math.isnan(judged[0].net_jpy)
    # 2 日目以降は「前の窓で選んだ銘柄」の OOS も出る
    assert any(r.prev_best_pair for r in rows)
    text = build_report(store, m)
    for marker in ("== 1.", "== 2.", "== 3.", "== 4.", "== 5.", "== 6.", "== 7."):
        assert marker in text
    # 判定まで到達していること (合格 / 不合格 のどちらかが書かれる)
    assert "**合格**" in text or "**不合格**" in text
    assert "判定不能" not in text
