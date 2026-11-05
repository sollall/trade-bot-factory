"""tbf/carry のテスト。HTTP は合成レスポンスでスタブする (ネットワークには出ない)

重点は「社内で実際に踏んだ罠」の回帰:
  - spotMetaAndAssetCtxs を位置で zip しないこと
  - l2Book 20 段で届かないサイズをゼロ埋めしないこと
  - fundingHistory の 500 件ページング
そして資本規約 (分母は常に C = 1.33N) と保守側の証拠金モデル。
"""

import json
import math
from pathlib import Path

import pytest

from tbf.carry.config import load_config
from tbf.carry.hl import Depth, HlError, InfoClient, round_hour
from tbf.carry.position import (
    Entry,
    annualized,
    avg_fill_px,
    basis_bps,
    mark,
    max_drawdown,
    stats,
    walk,
)
from tbf.carry.recorder import Recorder, Snapshot, build_entry, floor_to, verify_identity
from tbf.carry.report import (
    build_report,
    cost_series,
    daily_rows,
    window_rows,
    write_daily_csv,
)
from tbf.carry.store import CarryStore

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "carry.toml"


# ---------------------------------------------------------------- 合成レスポンス


def spot_meta():
    return {
        "universe": [
            {"tokens": [1, 0], "name": "@0", "index": 0, "isCanonical": True},
            {"tokens": [150, 0], "name": "@107", "index": 107, "isCanonical": False},
        ],
        "tokens": [
            {"name": "USDC", "index": 0, "szDecimals": 8, "fullName": None},
            {"name": "PURR", "index": 1, "szDecimals": 0, "fullName": None},
        ]
        + [{"name": f"T{i}", "index": i, "szDecimals": 2, "fullName": f"t{i}"} for i in range(2, 150)]
        + [
            {
                "name": "HYPE", "index": 150, "szDecimals": 2, "weiDecimals": 8,
                "tokenId": "0x0d01dc56dcaaca66ad901c959b4011ec", "fullName": "Hyperliquid",
            }
        ],
    }


def spot_ctxs():
    """実物と同じく universe と長さが違い、位置も一致しない並び"""
    return [
        {"coin": "@0", "markPx": "0.2", "midPx": "0.2", "dayNtlVlm": "1000"},
        {"coin": "@105", "markPx": "1.0", "midPx": "1.0", "dayNtlVlm": "5"},
        {"coin": "@107", "markPx": "100.0", "midPx": "100.0", "dayNtlVlm": "74400000"},
        {"coin": "@999", "markPx": "7.0", "midPx": "7.0", "dayNtlVlm": "9"},
    ]


def perp_meta_ctxs():
    universe = [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40, "marginTableId": 1},
                {"name": "HYPE", "szDecimals": 2, "maxLeverage": 10, "marginTableId": 52}]
    ctxs = [
        {"funding": "0.00001", "markPx": "60000", "midPx": "60000", "oraclePx": "60000",
         "premium": "0", "openInterest": "1", "dayNtlVlm": "1"},
        {"funding": "0.0000125", "markPx": "100.0", "midPx": "100.0", "oraclePx": "100.0",
         "premium": "-0.0001", "openInterest": "22164946", "dayNtlVlm": "1079300968"},
    ]
    return [universe, ctxs]


def book(mid=100.0, size=200.0, levels=20, tick=0.01):
    """20 段の板。各段 size 枚 (= mid*size USD) 置く"""
    bids = [{"px": f"{mid - tick * (i + 1):.4f}", "sz": f"{size}", "n": 1} for i in range(levels)]
    asks = [{"px": f"{mid + tick * (i + 1):.4f}", "sz": f"{size}", "n": 1} for i in range(levels)]
    return {"coin": "x", "time": 1_700_000_000_000, "levels": [bids, asks]}


class FakeHttp:
    """info エンドポイントの type でルーティングする"""

    def __init__(self, funding_hours=24, thin=False, start_ms=1_700_000_000_000):
        self.calls = []
        self.funding_hours = funding_hours
        self.thin = thin
        self.start_ms = start_ms
        self.status = 200

    async def request(self, method, url, headers=None, body=None):
        req = json.loads(body)
        self.calls.append(req["type"])
        if self.status != 200:
            return self.status, "boom"
        return 200, json.dumps(self._route(req))

    def _route(self, req):
        t = req["type"]
        if t == "spotMeta":
            return spot_meta()
        if t == "spotMetaAndAssetCtxs":
            return [spot_meta(), spot_ctxs()]
        if t == "metaAndAssetCtxs":
            u, c = perp_meta_ctxs()
            return [{"universe": u}, c]
        if t == "l2Book":
            sz = 0.5 if self.thin else 200.0
            return book(size=sz)
        if t == "marginTable":
            return {"description": "tiered 10x (2)",
                    "marginTiers": [{"lowerBound": "0.0", "maxLeverage": 10}]}
        if t == "userFees":
            return {"feeSchedule": {"cross": "0.00045", "spotCross": "0.0007"}}
        if t == "fundingHistory":
            # 毎時グリッド上の点を返す。実物と同じく 1 レスポンス 500 件上限、time は +31ms ずれる
            start, end = int(req["startTime"]), int(req["endTime"])
            ts = (start // 3_600_000 + 1) * 3_600_000
            out = []
            while ts <= end and len(out) < 500:
                out.append({"coin": "HYPE", "fundingRate": "0.0000125",
                            "premium": "-0.0001", "time": ts + 31})
                ts += 3_600_000
            return out
        raise AssertionError(f"unexpected type {t}")


@pytest.fixture
def cfg():
    return load_config(CONFIG)


# ---------------------------------------------------------------- 設定 (資本規約)


def test_config_holds_the_capital_convention(cfg):
    assert cfg.capital_usd == 10_000
    assert cfg.spot_notional_usd == 7_500
    assert cfg.perp_margin_usd == 2_500
    assert cfg.capital_ratio == pytest.approx(4 / 3)
    assert cfg.spot_counts_as_perp_margin is False


def test_config_rejects_a_broken_capital_convention(tmp_path):
    raw = CONFIG.read_text().replace("perp_margin_usd = 2_500", "perp_margin_usd = 5_000")
    p = tmp_path / "carry.toml"
    p.write_text(raw)
    with pytest.raises(ValueError, match="資本規約"):
        load_config(p)


def test_config_rejects_counting_spot_as_margin(tmp_path):
    raw = CONFIG.read_text().replace(
        "spot_counts_as_perp_margin = false", "spot_counts_as_perp_margin = true"
    )
    p = tmp_path / "carry.toml"
    p.write_text(raw)
    with pytest.raises(ValueError, match="未確定"):
        load_config(p)


# ---------------------------------------------------------------- 板を歩く


def test_avg_fill_px_walks_the_book():
    levels = [(100.0, 1.0), (101.0, 1.0)]
    # $100 なら 1 段目だけ
    assert avg_fill_px(levels, 100.0) == pytest.approx(100.0)
    # $201 で 2 段 (100*1 + 101*1) -> 平均は数量加重
    assert avg_fill_px(levels, 201.0) == pytest.approx(201.0 / (1 + 1))


def test_avg_fill_px_returns_none_when_the_book_is_too_thin():
    """足りない板で「食べられたところまでの平均」を返すとスリッページを小さく見せてしまう"""
    assert avg_fill_px([(100.0, 1.0)], 10_000.0) is None


def test_walk_marks_an_unfilled_size_as_unmeasured():
    d = Depth(((100.0, 1.0),), 1)
    f = walk(d, 10_000.0, 100.0, "buy", 7.0)
    assert f.ok is False
    assert f.avg_px is None and f.slip_bps is None and f.cost_bps is None
    assert f.depth_usd == pytest.approx(100.0)


def test_walk_slippage_is_adverse_on_both_sides():
    asks = Depth(((101.0, 100.0),), 1)
    bids = Depth(((99.0, 100.0),), 1)
    assert walk(asks, 1_000.0, 100.0, "buy", 0).slip_bps == pytest.approx(100.0)
    assert walk(bids, 1_000.0, 100.0, "sell", 0).slip_bps == pytest.approx(100.0)


# ---------------------------------------------------------------- 既知の罠の回帰


async def test_spot_ctx_joins_by_coin_not_by_position():
    """位置で zip すると @107 に @105 の値が付く。coin 結合ならそれが起きない"""
    http = FakeHttp()
    client = InfoClient(http, "http://x")
    ctx = await client.spot_ctx("@107")
    assert ctx["coin"] == "@107"
    assert float(ctx["markPx"]) == 100.0  # 位置 zip なら @105 の 1.0 を拾ってしまう


async def test_spot_ctx_raises_when_the_pair_has_no_context():
    client = InfoClient(FakeHttp(), "http://x")
    with pytest.raises(HlError, match="@42"):
        await client.spot_ctx("@42")


async def test_perp_ctx_refuses_to_zip_mismatched_lengths():
    class Bad(FakeHttp):
        def _route(self, req):
            if req["type"] == "metaAndAssetCtxs":
                u, c = perp_meta_ctxs()
                return [{"universe": u}, c[:1]]
            return super()._route(req)

    client = InfoClient(Bad(), "http://x")
    with pytest.raises(HlError, match="長さが一致しません"):
        await client.perp_ctx("HYPE")


async def test_funding_history_pages_past_the_500_limit():
    http = FakeHttp()
    client = InfoClient(http, "http://x")
    start = 700 * 3_600_000
    pts = await client.funding_history("HYPE", start, start + 700 * 3_600_000)
    assert len(pts) == 700
    assert http.calls.count("fundingHistory") >= 2
    # time は毎時 +31ms でずれているが、毎時丁度に丸められている
    assert all(p.time % 3_600_000 == 0 for p in pts)


def test_round_hour():
    assert round_hour(3_600_000 + 31) == 3_600_000
    assert round_hour(3_600_000 - 31) == 3_600_000


def test_verify_identity_rejects_a_different_token(cfg):
    meta = spot_meta()
    meta["tokens"][150] = {**meta["tokens"][150], "fullName": "Pump"}
    with pytest.raises(HlError, match="宣言した"):
        verify_identity(meta, cfg)


def test_verify_identity_rejects_a_repointed_pair(cfg):
    meta = spot_meta()
    meta["universe"][1] = {**meta["universe"][1], "tokens": [1, 0]}
    with pytest.raises(HlError, match="tokens"):
        verify_identity(meta, cfg)


def test_verify_identity_accepts_the_real_shape(cfg):
    assert "HYPE/USDC" in verify_identity(spot_meta(), cfg)[0]


# ---------------------------------------------------------------- 建玉と評価


def fake_snapshot(cfg, mid=100.0, size=200.0, now=1_700_000_000_000):
    from tbf.carry.hl import Book as HlBook

    raw = book(mid=mid, size=size)
    client = InfoClient(FakeHttp(), "http://x")
    bids = tuple(sorted(((float(l["px"]), float(l["sz"])) for l in raw["levels"][0]), reverse=True))
    asks = tuple(sorted((float(l["px"]), float(l["sz"])) for l in raw["levels"][1]))
    b = HlBook("@107", now, Depth(bids, len(bids)), Depth(asks, len(asks)))
    p = HlBook("HYPE", now, Depth(bids, len(bids)), Depth(asks, len(asks)))
    u, c = perp_meta_ctxs()
    return Snapshot(now, spot_ctxs()[2], u[1], c[1], b, p)


def test_build_entry_uses_walked_prices_and_floors_the_size(cfg):
    snap = fake_snapshot(cfg)
    entry, notes = build_entry(snap, cfg, 2)
    assert notes == []
    assert entry is not None
    # 現物は売り板を食べて買うので、mid より高い平均約定になる
    assert entry.spot_px > 100.0
    # perp は買い板を食べて売るので、mid より安い
    assert entry.perp_px < 100.0
    assert entry.size == floor_to(cfg.spot_notional_usd / entry.spot_px, 2)
    assert entry.spot_notional_usd == pytest.approx(cfg.spot_notional_usd, rel=1e-3)
    # 片道コスト = 手数料 11.5bps + 両脚のスリッページ
    assert entry.entry_cost_bps == pytest.approx(
        cfg.spot_taker_bps + cfg.perp_taker_bps + entry.spot_slip_bps + entry.perp_slip_bps
    )


def test_build_entry_refuses_a_book_it_cannot_walk(cfg):
    snap = fake_snapshot(cfg, size=0.5)  # 20 段で $1,000 程度しかない
    entry, notes = build_entry(snap, cfg, 2)
    assert entry is None
    assert any("歩き切れません" in n for n in notes)


def simple_entry(size=75.0, spot_px=100.0, perp_px=100.0, margin=2_500.0, fee=0.0):
    return Entry(
        time=0, size=size, spot_px=spot_px, perp_px=perp_px,
        spot_notional_usd=size * spot_px, perp_notional_usd=size * perp_px,
        margin_usd=margin, capital_usd=10_000.0,
        spot_fee_bps=fee, perp_fee_bps=fee, spot_slip_bps=0.0, perp_slip_bps=0.0,
        spot_mid=spot_px, perp_mid=perp_px,
    )


def test_mark_is_delta_neutral_when_the_basis_does_not_move():
    e = simple_entry()
    m = mark(e, 1, 120.0, 120.0, 0.0, 0.05, 0.20)
    assert m.mtm_usd == pytest.approx(0.0)
    assert m.net_pnl_usd == pytest.approx(0.0)


def test_mark_charges_the_basis_move_against_the_position():
    e = simple_entry()
    # perp だけ 1 ドル上がる = ベーシスが広がる -> ショート脚の含み損
    m = mark(e, 1, 100.0, 101.0, 0.0, 0.05, 0.20)
    assert m.mtm_usd == pytest.approx(-75.0)
    assert m.basis_bps == pytest.approx(basis_bps(101.0, 100.0))


def test_mark_adds_funding_and_subtracts_entry_cost():
    e = simple_entry(fee=10.0)  # 片脚 10bps
    m = mark(e, 1, 100.0, 100.0, 5.0, 0.05, 0.20)
    cost = 2 * 10.0 / 10_000 * 7_500
    assert m.entry_cost_usd == pytest.approx(cost)
    assert m.net_pnl_usd == pytest.approx(5.0 - cost)


def test_margin_ratio_ignores_the_spot_leg_and_liquidates_on_the_way_up():
    """保守側モデル: 現物の含み益は証拠金に入らない。$7,500/$2,500 なら +27% で清算"""
    e = simple_entry()
    assert mark(e, 1, 100.0, 100.0, 0.0, 0.05, 0.20).margin_ratio == pytest.approx(1 / 3)
    trigger = (2_500.0 + 7_500.0) / (7_500.0 * 1.05) - 1  # = 0.2698
    # ベーシスは動かさず、HYPE が素直に上がった場合 (両脚同じ価格)
    def at(move):
        px = 100.0 * (1 + move)
        return mark(e, 1, px, px, 0.0, 0.05, 0.20)

    assert at(trigger - 0.001).liquidated is False
    assert at(trigger + 0.001).liquidated is True
    # 現物側は同じだけ含み益が出ているので、2 脚合計の評価損益はゼロのまま清算する
    assert at(trigger + 0.001).mtm_usd == pytest.approx(0.0, abs=1e-9)
    # 20% 割れは +11.1% で来る
    warn = (2_500.0 + 7_500.0) / (7_500.0 * 1.20) - 1
    assert at(warn - 0.001).margin_warn is False
    assert at(warn + 0.001).margin_warn is True


def test_margin_ratio_is_rescued_by_funding_received():
    e = simple_entry()
    without = mark(e, 1, 100.0, 126.0, 0.0, 0.05, 0.20)
    with_funding = mark(e, 1, 100.0, 126.0, 200.0, 0.05, 0.20)
    assert with_funding.margin_ratio > without.margin_ratio


# ---------------------------------------------------------------- 指標


def test_stats_matches_a_hand_computed_sharpe():
    rets = [0.001, 0.002, 0.003]
    st = stats(rets)
    assert st.n == 3
    assert st.mean == pytest.approx(0.002)
    assert st.std == pytest.approx(0.001)
    assert st.sharpe == pytest.approx(2 * math.sqrt(365))
    assert st.std_se == pytest.approx(0.001 / math.sqrt(4))
    assert st.sharpe_low < st.sharpe < st.sharpe_high


def test_stats_needs_two_points():
    assert stats([0.001]) is None


def test_max_drawdown():
    assert max_drawdown([0, 10, 4, 12]) == pytest.approx(6)
    assert max_drawdown([0, -5, -3]) == pytest.approx(5)


def test_annualized_divides_by_capital():
    # C に対して 30 日で 1% -> 年率 12.17%
    assert annualized(100.0, 10_000.0, 30) == pytest.approx(0.01 * 365 / 30)
    assert annualized(100.0, 10_000.0, 0) is None


# ---------------------------------------------------------------- 記録と報告


async def run_recorder(tmp_path, polls=2, thin=False, clock_start=1_700_000_000_000, step=300_000):
    cfg = load_config(CONFIG)
    store = CarryStore(tmp_path / "a1.sqlite")
    http = FakeHttp(thin=thin)
    t = {"now": clock_start}

    def clock():
        t["now"] += step
        return t["now"]

    rec = Recorder(cfg, http, store, clock=clock, log=lambda m: None)
    for _ in range(polls):
        await rec.poll_once()
    return cfg, store, rec


async def test_recorder_records_entry_samples_fills_and_funding(tmp_path):
    # ファンディングは 1 時間刻みなので、1 ポーリング 1 時間で回す
    cfg, store, rec = await run_recorder(tmp_path, polls=3, step=3_600_000)
    assert rec.entry is not None
    assert store.load_entry().size == rec.entry.size
    n_samples = store.query("SELECT COUNT(*) AS n FROM samples")[0]["n"]
    assert n_samples >= 3
    # サイズ 4 × 脚 2 × 方向 2 = 16 行 / ポーリング
    assert store.query("SELECT COUNT(*) AS n FROM fills")[0]["n"] == 3 * 16
    assert store.query("SELECT COUNT(*) AS n FROM polls WHERE ok = 1")[0]["n"] == 3
    # ファンディングは建玉より後の時間だけ積む
    rows = store.query("SELECT time, rate, amount_usd, nominal_amount_usd FROM funding ORDER BY time")
    assert rows and all(int(r["time"]) > rec.entry.time for r in rows)
    # 名目 $7,500 に対する受取と、建玉の時価名目に対する受取の両方を残す
    for r in rows:
        assert r["nominal_amount_usd"] == pytest.approx(float(r["rate"]) * cfg.spot_notional_usd)
        assert r["amount_usd"] == pytest.approx(float(r["rate"]) * rec.entry.size * 100.0)
    assert store.funding_cum_usd() > 0


async def test_recorder_does_not_open_a_position_on_a_thin_book(tmp_path):
    cfg, store, rec = await run_recorder(tmp_path, polls=2, thin=True)
    assert rec.entry is None
    assert store.load_entry() is None
    assert store.query("SELECT COUNT(*) AS n FROM samples")[0]["n"] == 0
    bad = store.query("SELECT error FROM polls WHERE ok = 0")
    assert bad and "entry not established" in bad[0]["error"]


async def test_recorder_keeps_the_entry_across_a_restart(tmp_path):
    cfg, store, rec = await run_recorder(tmp_path, polls=1)
    first = store.load_entry()
    store2 = CarryStore(tmp_path / "a1.sqlite")
    rec2 = Recorder(cfg, FakeHttp(), store2, clock=lambda: 1_700_000_900_000, log=lambda m: None)
    assert rec2.entry is not None
    assert rec2.entry.time == first.time
    assert rec2.entry.spot_px == first.spot_px


async def test_recorder_logs_a_failed_poll_without_crashing(tmp_path):
    cfg = load_config(CONFIG)
    store = CarryStore(tmp_path / "a1.sqlite")
    http = FakeHttp()
    http.status = 503
    rec = Recorder(cfg, http, store, clock=lambda: 1_700_000_000_000, log=lambda m: None)
    await rec.poll_once()
    row = store.query("SELECT ok, error FROM polls")[0]
    assert row["ok"] == 0 and "503" in row["error"]


async def test_cost_series_pairs_both_legs(tmp_path):
    cfg, store, rec = await run_recorder(tmp_path, polls=2)
    series = cost_series(store, cfg.spot_notional_usd)
    assert series
    for d in series.values():
        assert d["entry"] > cfg.spot_taker_bps + cfg.perp_taker_bps  # 手数料 + スリッページ
        assert d["exit"] > 0


async def test_report_withholds_judgment_before_the_declared_window(tmp_path):
    cfg, store, rec = await run_recorder(tmp_path, polls=4)
    out = build_report(store, cfg, rec.entry.time + 86_400_000)
    assert "判定しない" in out
    assert "30 日" in out
    # 禁止語が入っていないこと
    for word in ("有望", "良さそう", "期待できる", "可能性がある"):
        assert word not in out


async def test_report_rejects_on_a_simulated_liquidation(tmp_path):
    cfg, store, rec = await run_recorder(tmp_path, polls=2)
    e = store.load_entry()
    # 31 日ぶんの日次サンプルを足し、途中で perp が +40% 跳ねた日を入れる
    t = e.time
    for day in range(1, 32):
        t = e.time + day * 86_400_000
        perp = 100.0 * (1.4 if day == 10 else 1.0)
        m = mark(e, t, 100.0, perp, 0.5 * day, cfg.maintenance_margin_frac, cfg.margin_warn_frac)
        store.add_sample(m, {})
    store.commit()
    out = build_report(store, cfg, t + 3_600_000)
    assert "**却下**" in out
    assert "清算" in out


async def test_report_and_csv_cover_the_four_items(tmp_path):
    cfg, store, rec = await run_recorder(tmp_path, polls=3)
    out = build_report(store, cfg, rec.entry.time + 2 * 86_400_000)
    for header in ("(i) 実効往復コスト", "(ii) 実現ファンディング累計",
                   "(iii) ベーシスの最大逸脱と証拠金", "(iv) 現物板上位 20 段の深さ"):
        assert header in out
    assert "C = $10,000" in out
    rows = daily_rows(store, cfg)
    p = write_daily_csv(rows, tmp_path / "a1_daily.csv")
    text = p.read_text().splitlines()
    assert text[0].startswith("date,samples")
    assert len(text) == len(rows) + 1


async def test_daily_returns_divide_by_capital(tmp_path):
    cfg, store, rec = await run_recorder(tmp_path, polls=2)
    e = store.load_entry()
    for day in (1, 2):
        m = mark(e, e.time + day * 86_400_000, 100.0, 100.0, 10.0 * day,
                 cfg.maintenance_margin_frac, cfg.margin_warn_frac)
        store.add_sample(m, {})
    store.commit()
    rows = daily_rows(store, cfg)
    last = rows[-1]
    assert last.ret == pytest.approx(10.0 / cfg.capital_usd)
    assert last.funding_usd == pytest.approx(10.0)


async def test_window_apr_uses_elapsed_time_not_row_count(tmp_path):
    """UTC 日で切ると初日・最終日が部分日になる。行数を日数として年率化してはいけない

    建玉 12:00 から 24 時間ぶん記録すると UTC 日の行は 2 行できるが、実際の経過は 1 日。
    行数で割ると年率が半分になる (この run で実際に出ていたズレの回帰)。
    """
    # 建玉を 12:00 UTC に置く (1_700_000_000_000 は 2023-11-14 22:13:20 UTC なので明示的に作る)
    noon = 1_700_000_000_000 - (1_700_000_000_000 % 86_400_000) + 43_200_000
    cfg, store, rec = await run_recorder(tmp_path, polls=2, clock_start=noon - 300_000)
    e = store.load_entry()
    # 当日 23:00 と翌日 12:00 の 2 本。net は現物/perp 同値でファンディングだけが積む
    for t, fund in ((e.time + 11 * 3_600_000, 1.0), (e.time + 24 * 3_600_000, 2.0)):
        store.add_sample(
            mark(e, t, 100.0, 100.0, fund, cfg.maintenance_margin_frac, cfg.margin_warn_frac), {}
        )
    store.commit()
    rows = daily_rows(store, cfg)
    assert len(rows) == 2
    assert sum(r.span_days for r in rows) == pytest.approx(1.0, abs=1e-6)

    pnl = rows[-1].net_pnl_usd
    by_elapsed = annualized(pnl, cfg.capital_usd, 1.0)
    by_row_count = annualized(pnl, cfg.capital_usd, 2.0)
    lines = window_rows(rows, cfg)
    assert f"{by_elapsed * 100:+.2f}%" in lines[-1]
    assert f"{by_row_count * 100:+.2f}%" not in lines[-1]
    # 行数 (2) と実日数 (1.00) を両方出していること
    assert "| 2 | 1.00 |" in lines[-1]


def test_sigma_phases_exposes_how_much_the_hourly_grid_matters():
    """5 分サンプルから 1 時間刻みを作る位相は 12 通り。1 つだけ出すと恣意的な σ になる"""
    from tbf.carry.report import _sigma_phases

    # 5 分おきに ±1 を交互に振る（完全な平均回帰）。位相で σ が変わる系列
    v = [(-1.0) ** i for i in range(300)]
    phases = _sigma_phases(v, 12)
    assert len(phases) == 12
    # 偶数刻みで間引くので全位相で変化 0 -> σ 0。位相を 1 つ選んでも値は同じ
    assert max(phases) == pytest.approx(0.0)


def test_mean_reverting_basis_breaks_root_t_scaling():
    """σ を √t で伸ばす前提が崩れていれば、刻み別の表で 実測/RW < 1 として見える"""
    from tbf.carry.report import _autocorr1, _sigma_scaling_table

    v = [3.0 * (-1.0) ** i + 0.01 * i for i in range(300)]
    ch = [b - a for a, b in zip(v, v[1:])]
    assert _autocorr1(ch) < -0.5
    table = _sigma_scaling_table(v)
    hourly = next(line for line in table if line.startswith("| 1時間"))
    ratio = float(hourly.rstrip(" |").rsplit("|", 1)[1])
    assert ratio < 1.0


async def test_funding_is_not_refetched_for_hours_already_recorded(tmp_path):
    """保存時刻は毎時丁度に丸めるが API の time は毎時 +数十ms。`last+1` から取ると毎回 1 件返る

    二重計上は INSERT OR REPLACE で起きないが、「1 時間ぶん追加」とログに出続け、
    同じ 1 時間ぶんを毎回取りに行って API 呼び出しを無駄に使う。
    """
    cfg = load_config(CONFIG)
    store = CarryStore(tmp_path / "a1.sqlite")
    http = FakeHttp()
    t = {"now": 1_700_000_000_000}

    def clock():
        t["now"] += 3_600_000
        return t["now"]

    rec = Recorder(cfg, http, store, clock=clock, log=lambda m: None)
    # ファンディングは 1 時間刻みなので 1 ポーリング 1 時間で回す
    for _ in range(3):
        await rec.poll_once()
    client = InfoClient(http, cfg.info_url)
    n_rows = store.query("SELECT COUNT(*) AS n FROM funding")[0]["n"]
    assert n_rows >= 1
    # 同じ時刻でもう一度回す -> 新しい時間は無いので 0 件、行数も増えない
    assert await rec.update_funding(client, t["now"]) == 0
    assert await rec.update_funding(client, t["now"]) == 0
    assert store.query("SELECT COUNT(*) AS n FROM funding")[0]["n"] == n_rows
    # 1 時間進めれば 1 件だけ増える
    assert await rec.update_funding(client, t["now"] + 3_600_000) == 1
    assert store.query("SELECT COUNT(*) AS n FROM funding")[0]["n"] == n_rows + 1
