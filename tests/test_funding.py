"""tbf/funding のテスト。HTTP は tests/fixtures/funding の実レスポンス (間引いたもの) でスタブする"""

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from tbf.funding.config import load_config
from tbf.funding.report import breakeven_days, build_report, net_apr, pair_sign_flips, walkforward
from tbf.funding.scanner import Scanner
from tbf.funding.spread import (
    EpisodeTracker,
    avg_fill_price,
    evaluate,
    slippage_bps,
)
from tbf.funding.store import FundingStore
from tbf.funding.venues import (
    Book,
    Session,
    SymbolSnapshot,
    VenueClient,
    VenueError,
    VenueSnapshot,
    annualize,
    check_all,
)

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures" / "funding"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())


# ---------------------------------------------------------------- HTTP スタブ


class FakeHttp:
    """URL と body から fixture を引く。ネットワークには出ない"""

    def __init__(self):
        self.down = set()          # ここに入れた会場は 503 を返す
        self.rate_limited = set()  # ここに入れた会場は 429 を返す
        self.calls = []

    async def request(self, method, url, headers=None, body=None):
        self.calls.append((method, url))
        for venue in self.down:
            if venue in url:
                return 503, "service unavailable"
        for venue in self.rate_limited:
            if venue in url:
                return 429, "too many requests"
        return 200, json.dumps(self._route(method, url, body))

    def _route(self, method, url, body):
        if "hyperliquid" in url:
            req = json.loads(body)
            if req["type"] == "metaAndAssetCtxs":
                return fixture("hyperliquid_metaAndAssetCtxs")
            if req["type"] == "l2Book":
                return fixture(f"hyperliquid_l2Book_{req['coin']}")
            if req["type"] == "fundingHistory":
                return fixture(f"hyperliquid_fundingHistory_{req['coin']}")
            if req["type"] == "candleSnapshot":
                return fixture(f"hyperliquid_candleSnapshot_{req['req']['coin']}")
        if "zklighter" in url:
            if "orderBookDetails" in url:
                return fixture("lighter_orderBookDetails")
            if "funding-rates" in url:
                return fixture("lighter_funding-rates")
            if "fundings" in url:
                mid = int(re.search(r"market_id=(\d+)", url).group(1))
                return fixture(f"lighter_fundings_{'BTC' if mid == 1 else 'ETH'}")
            if "orderBookOrders" in url:
                mid = int(re.search(r"market_id=(\d+)", url).group(1))
                return fixture(f"lighter_orderBookOrders_{'BTC' if mid == 1 else 'ETH'}")
        if "paradex" in url:
            sym = "BTC" if "BTC" in url else "ETH"
            if "summary" in url:
                return fixture(f"paradex_summary_{sym}")
            if "klines" in url:
                return fixture(f"paradex_klines_{sym}")
            if "funding/data" in url:
                # 実物と同じく「end_at 以前の直近 1 行」を返す (累積インデックスのサンプル)
                rows = sorted(fixture(f"paradex_funding_data_{sym}")["results"], key=lambda r: r["created_at"])
                m = re.search(r"end_at=(\d+)", url)
                if m:
                    rows = [r for r in rows if r["created_at"] <= int(m.group(1))]
                return {"results": rows[-1:]}
            if "orderbook" in url:
                return fixture(f"paradex_orderbook_{sym}")
        if "dydx" in url:
            sym = "BTC" if "BTC" in url else "ETH"
            if "perpetualMarkets" in url:
                return fixture(f"dydx_market_{sym}")
            if "historicalFunding" in url:
                return fixture(f"dydx_historicalFunding_{sym}")
            if "orderbooks" in url:
                return fixture(f"dydx_orderbook_{sym}")
        if "binance" in url:
            if "fundingInfo" in url:
                return fixture("binance_fundingInfo")
            return fixture(f"binance_premiumIndex_{'BTC' if 'BTCUSDT' in url else 'ETH'}")
        raise AssertionError(f"fixture がありません: {method} {url} {body}")


def cfg_and_client():
    cfg = load_config(ROOT / "funding.toml", ROOT / "data" / "venues.toml")
    http = FakeHttp()
    return cfg, VenueClient(http, cfg, clock=lambda: 1_000_000), http


# ---------------------------------------------------------------- 年率正規化


def test_annualize_normalizes_venue_intervals():
    # 1 時間あたり 0.0000125 (Hyperliquid の典型値) -> 年率 10.95%
    assert annualize(0.0000125, 1) == pytest.approx(0.1095, abs=1e-4)
    # 8 時間あたり 0.0001 (Binance / Paradex の建て) は同じ水準になる
    assert annualize(0.0001, 8) == pytest.approx(annualize(0.0000125, 1))
    # 建てを間違えると 8 倍ずれる (これを防ぐのが funding_interval_hours)
    assert annualize(0.0001, 1) == pytest.approx(8 * annualize(0.0001, 8))
    with pytest.raises(ValueError):
        annualize(0.0001, 0)


def test_settlement_spacing_matches_the_registry_interval():
    """ファンディングの支払い履歴が取れる会場は、支払い間隔 == 設定の建て であることを確かめる

    レートの建てを間違えるのがこのスクリーナーで一番効く事故なので、
    「レジストリにそう書いてある」ではなく「API の履歴がそうなっている」で固定する。
    """
    cfg = load_config(ROOT / "funding.toml", ROOT / "data" / "venues.toml")
    # hyperliquid: fundingHistory の time は ms
    hl = sorted(int(f["time"]) for f in fixture("hyperliquid_fundingHistory_BTC"))
    # 記録時刻には数十 ms のばらつきがあるので秒に丸めて比べる
    gaps = {round((b - a) / 1000) for a, b in zip(hl, hl[1:])}
    assert gaps == {3600}, gaps
    assert cfg.venue("hyperliquid").funding_interval_hours == 1

    # dydx: historicalFunding の effectiveAt は ISO8601
    from datetime import datetime

    ts = sorted(
        datetime.fromisoformat(f["effectiveAt"].replace("Z", "+00:00")).timestamp()
        for f in fixture("dydx_historicalFunding_BTC")["historicalFunding"]
    )
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    assert all(abs(g - 3600) < 5 for g in gaps), gaps
    assert cfg.venue("dydx").funding_interval_hours == 1
    # 履歴のレートと perpetualMarkets の nextFundingRate が同じ桁 (= 同じ建て) であること
    hist = [float(f["rate"]) for f in fixture("dydx_historicalFunding_BTC")["historicalFunding"]]
    nxt = float(fixture("dydx_market_BTC")["markets"]["BTC-USD"]["nextFundingRate"])
    assert 0.1 < nxt / (sum(hist) / len(hist)) < 10, (nxt, hist)


def test_lighter_funding_rates_endpoint_is_8h_denominated():
    """Lighter の 2 つの endpoint の建てが食い違わないことを fixture で固定する

    /api/v1/funding-rates の rate は 8 時間ぶんの分数、
    /api/v1/fundings (resolution=1h) の rate は 1 時間あたりの％。
    同時刻のデータなので、前者 / 8 == 後者 / 100 になるはず。
    """
    rates = fixture("lighter_funding-rates")["funding_rates"]
    per_8h = next(r["rate"] for r in rates if r["exchange"] == "lighter" and r["symbol"] == "BTC")
    hourly_pct = [float(f["rate"]) for f in fixture("lighter_fundings_BTC")["fundings"]]
    assert per_8h / 8 == pytest.approx(sum(hourly_pct) / len(hourly_pct) / 100, rel=0.05)
    # /fundings の value / mark_price も同じ 1 時間あたりのレートになる
    f = fixture("lighter_fundings_BTC")["fundings"][0]
    mark = float(next(d for d in fixture("lighter_orderBookDetails")["order_book_details"] if d["symbol"] == "BTC")["mark_price"])
    assert float(f["value"]) / mark == pytest.approx(per_8h / 8, rel=0.05)


# ---------------------------------------------------------------- スリッページ


def test_avg_fill_price_walks_the_book():
    asks = ((100.0, 1.0), (110.0, 1.0), (120.0, 10.0))
    # $100 なら最良気配だけで足りる
    assert avg_fill_price(asks, 100) == pytest.approx(100.0)
    # $210 = 1 枚 @100 + 1 枚 @110 -> 平均 105
    assert avg_fill_price(asks, 210) == pytest.approx(105.0)
    # 板の合計は 100 + 110 + 1200 = $1410。超えたら None (「食べられるぶんの平均」は返さない)
    assert avg_fill_price(asks, 1410) == pytest.approx(1410 / (1 + 1 + 10))
    assert avg_fill_price(asks, 1411) is None
    assert avg_fill_price((), 100) is None
    assert avg_fill_price(asks, 0) is None


def test_slippage_bps_is_signed_against_us():
    asks = ((100.0, 1.0), (110.0, 1.0))
    bids = ((98.0, 1.0), (90.0, 1.0))
    # 買い: 基準 100 に対し平均 105 -> 500bps 不利
    assert slippage_bps(asks, 210, 100.0, buy=True) == pytest.approx(500.0)
    # 売り: 基準 100 に対し平均 94 -> 600bps 不利
    assert slippage_bps(bids, 188, 100.0, buy=False) == pytest.approx(600.0)
    # 最良気配ぴったりなら、基準 (mid) との差だけ残る
    assert slippage_bps(asks, 100, 100.0, buy=True) == pytest.approx(0.0)
    assert slippage_bps(asks, 1_000_000, 100.0, buy=True) is None


# ---------------------------------------------------------------- 実効スプレッド


def _snap(venue, symbol, apr, interval_hours=1.0, px=100.0, depth=1000.0):
    """年率 apr になる生レートを逆算して、板が十分厚いスナップショットを作る"""
    rate = apr / (24 * 365) * interval_hours
    sz = depth / px
    return VenueSnapshot(
        venue, 1_000_000, 1.0, 1,
        {symbol: SymbolSnapshot(symbol, rate, interval_hours, px, Book(((px, sz),), ((px, sz),)))},
    )


def test_effective_spread_breakeven_and_net_apr():
    cfg, _, _ = cfg_and_client()
    # ショート側 20% / ロング側 5% -> 粗 15%。板は 1 本・同値なのでスリッページ 0
    cfg = replace(cfg, symbols=["BTC"], sizes_usd=[1_000.0], ref_size_usd=1_000.0)
    snaps = [_snap("hyperliquid", "BTC", 0.20), _snap("dydx", "BTC", 0.05, interval_hours=1.0)]
    spreads, misses = evaluate(snaps, cfg)
    assert not misses
    s = next(x for x in spreads if x.short_venue == "hyperliquid" and x.long_venue == "dydx")
    assert s.gross_apr == pytest.approx(0.15)
    # 片道 = taker(hyperliquid 4.5) + taker(dydx 5.0) = 9.5bps、往復 19bps
    assert s.entry_cost_bps == pytest.approx(9.5)
    assert s.exit_cost_bps == pytest.approx(9.5)
    assert s.cost_bps == pytest.approx(19.0)
    # breakeven = 19bps / (15% の 1 日ぶん)
    assert s.breakeven_days == pytest.approx(19.0 / (0.15 * 10_000 / 365))
    assert s.breakeven_days == pytest.approx(4.62, abs=0.01)
    # net_apr(hold) = 粗 - 往復コストを hold 日で年率に割り戻したぶん
    assert s.net_apr(14) == pytest.approx(0.15 - 0.0019 * 365 / 14)
    assert s.net_apr(7) < s.net_apr(14) < s.net_apr(30)
    # 逆向き (ショート dydx / ロング hyperliquid) は粗がマイナスなので永遠に黒字にならない
    r = next(x for x in spreads if x.short_venue == "dydx")
    assert r.gross_apr == pytest.approx(-0.15)
    assert r.breakeven_days is None
    assert r.net_apr(14) < 0


def test_basis_is_measured_but_kept_out_of_net_apr():
    """会場間の価格差は記録するが、依頼票の実効スプレッドの式には入れない"""
    cfg, _, _ = cfg_and_client()
    cfg = replace(cfg, symbols=["BTC"], sizes_usd=[1_000.0], ref_size_usd=1_000.0)
    # hyperliquid が 100.0、dydx が 101.0 -> 安い方 (hyperliquid) をショートすると +100bps の含み損
    cheap = _snap("hyperliquid", "BTC", 0.20, px=100.0)
    rich = _snap("dydx", "BTC", 0.05, px=101.0)
    spreads, _ = evaluate([cheap, rich], cfg)
    s = next(x for x in spreads if x.short_venue == "hyperliquid")
    r = next(x for x in spreads if x.short_venue == "dydx")
    assert s.basis_bps == pytest.approx(100.0)
    assert r.basis_bps == pytest.approx(-100.0 * 100 / 101)   # 基準が逆になるので対称ではない
    # net_apr / breakeven は価格差に影響されない (式どおり)
    assert s.net_apr(14) == pytest.approx(net_apr(s.gross_apr, s.cost_bps, 14))
    assert s.cost_bps == pytest.approx(19.0)


def test_store_adds_new_columns_to_an_existing_db(tmp_path):
    """計測中の sqlite にも列を後から足せる (計測をやり直さない)"""
    import sqlite3

    from tbf.funding.store import ADDED_COLUMNS

    path = tmp_path / "old.sqlite"
    store = FundingStore(path, 1.0)
    _fill_window(store, 0, "a", "b", "BTC", 1_000.0, 0.10)
    store.commit()
    # 列を足す前の状態を作り直す
    store.conn.close()
    conn = sqlite3.connect(path)
    for name, _ in ADDED_COLUMNS["spread_buckets"]:
        conn.execute(f"ALTER TABLE spread_buckets DROP COLUMN {name}")
    conn.commit()
    conn.close()

    reopened = FundingStore(path, 1.0)
    have = {r["name"] for r in reopened.conn.execute("PRAGMA table_info(spread_buckets)")}
    assert all(name in have for name, _ in ADDED_COLUMNS["spread_buckets"])
    # 既存の行は残っていて、新しい列は 0 で埋まる
    (row,) = reopened.query("SELECT n, sum_gross, sum_basis_bps FROM spread_buckets")
    assert row["n"] == 1 and row["sum_gross"] == pytest.approx(0.10) and row["sum_basis_bps"] == 0.0
    # 足した後も書き込めること
    _fill_window(reopened, 1_000, "a", "b", "BTC", 1_000.0, 0.20)
    reopened.commit()
    assert reopened.query("SELECT COUNT(*) FROM spread_buckets")[0][0] == 2


def test_report_formulas_match_spread_formulas():
    cfg, _, _ = cfg_and_client()
    cfg = replace(cfg, symbols=["BTC"], sizes_usd=[1_000.0], ref_size_usd=1_000.0)
    spreads, _ = evaluate([_snap("hyperliquid", "BTC", 0.20), _snap("dydx", "BTC", 0.05)], cfg)
    s = spreads[0]
    assert net_apr(s.gross_apr, s.cost_bps, 14) == pytest.approx(s.net_apr(14))
    assert breakeven_days(s.gross_apr, s.cost_bps) == pytest.approx(s.breakeven_days)
    assert breakeven_days(-0.01, 10) is None


def test_bigger_size_eats_the_edge():
    cfg, _, _ = cfg_and_client()
    cfg = replace(cfg, symbols=["BTC"], sizes_usd=[1_000.0, 5_000.0, 50_000.0], ref_size_usd=1_000.0)
    # 板: $1,000 は最良気配で足りるが、$5,000 は 1% 離れたところまで食う。$50,000 は板が無い
    px = 100.0
    thin = VenueSnapshot(
        "hyperliquid", 1_000_000, 1.0, 1,
        {"BTC": SymbolSnapshot("BTC", 0.20 / (24 * 365), 1.0, px,
                               Book(((99.0, 50.0), (90.0, 100.0)), ((101.0, 50.0), (110.0, 100.0))))},
    )
    spreads, misses = evaluate([thin, _snap("dydx", "BTC", 0.05, depth=1e9)], cfg)
    by_size = {s.size_usd: s for s in spreads if s.short_venue == "hyperliquid"}
    assert 50_000.0 not in by_size                              # 板不足で評価しない
    assert any(m.size_usd == 50_000.0 and m.venue == "hyperliquid" for m in misses)
    assert by_size[5_000.0].cost_bps > by_size[1_000.0].cost_bps   # サイズを上げるとコストが増える
    assert by_size[5_000.0].net_apr(14) < by_size[1_000.0].net_apr(14)


# ---------------------------------------------------------------- アダプタ


async def test_adapters_parse_real_shapes():
    cfg, client, _ = cfg_and_client()
    for v in cfg.venues:
        snap = await client.fetch(v)
        assert set(snap.symbols) == {"BTC", "ETH"}, v.name
        for sym, s in snap.symbols.items():
            assert s.mark > 0, (v.name, sym)
            # どの会場も同じ時刻の実データなので、年率は常識的な範囲に収まるはず
            assert -1.0 < s.funding_apr < 1.0, (v.name, sym, s.funding_apr)
            if v.reference_only:
                assert s.book is None
                continue
            assert s.book and s.book.bids and s.book.asks, (v.name, sym)
            # 板は bid 降順 / ask 昇順に並び替えられている
            assert list(s.book.bids) == sorted(s.book.bids, key=lambda l: -l[0]), (v.name, sym)
            assert list(s.book.asks) == sorted(s.book.asks, key=lambda l: l[0]), (v.name, sym)
            assert s.book.bids[0][0] < s.book.asks[0][0], (v.name, sym)
            assert s.book.mid == pytest.approx((s.book.bids[0][0] + s.book.asks[0][0]) / 2)


async def test_all_venues_agree_on_the_level_after_normalization():
    """建てを正しく正規化できていれば、同時刻の 5 会場の年率は近い値に揃う

    揃わない場合は funding_interval_hours の設定ミスを疑う (8 倍ずれる)。
    """
    cfg, client, _ = cfg_and_client()
    aprs = {}
    for v in cfg.venues:
        snap = await client.fetch(v)
        aprs[v.name] = snap.symbols["BTC"].funding_apr
    lo, hi = min(aprs.values()), max(aprs.values())
    assert hi - lo < 0.10, aprs   # 年率 10%pt 以内に収まる


async def test_check_all_reports_every_venue():
    cfg, client, _ = cfg_and_client()
    res = await check_all(client)
    # Paradex は API 自身が建てを返すので突き合わせできる
    assert res.api_intervals["paradex"] == 8.0
    assert res.ok == {v.name for v in cfg.venues} and not res.errors and not res.config_errors
    res.raise_for_check(cfg)
    res.raise_for_run(cfg)
    text = "\n".join(res.lines)
    for v in cfg.venues:
        assert v.name in text
    assert "参照" in text and "対象" in text


async def test_check_all_detects_interval_mismatch():
    cfg, client, _ = cfg_and_client()
    broken = [replace(v, funding_interval_hours=1.0) if v.name == "paradex" else v for v in cfg.venues]
    client.cfg = cfg = replace(cfg, venues=broken)
    res = await check_all(client)
    # 建ての食い違いは設定の誤りなので check でも run でも落とす
    assert res.config_errors
    for raise_for in (res.raise_for_check, res.raise_for_run):
        with pytest.raises(VenueError, match="funding_interval_hours の食い違い"):
            raise_for(cfg)


async def test_tradable_venue_down_fails_check_but_run_keeps_going():
    cfg, client, http = cfg_and_client()
    http.down.add("zklighter")
    res = await check_all(client)
    assert "lighter" in res.errors
    # check は設定の点検なので落とす
    with pytest.raises(VenueError, match="lighter"):
        res.raise_for_check(cfg)
    # run は残り 3 会場でペアが作れるので続ける (7 日計測を 1 会場の障害で止めない)
    res.raise_for_run(cfg)
    assert res.ok_tradable(cfg) == ["hyperliquid", "paradex", "dydx"]


async def test_run_stops_when_fewer_than_two_tradable_venues_are_up():
    cfg, client, http = cfg_and_client()
    http.down.update({"zklighter", "paradex", "dydx"})
    res = await check_all(client)
    with pytest.raises(VenueError, match="2 つ以上つながりません"):
        res.raise_for_run(cfg)


async def test_reference_only_venue_down_is_only_a_warning():
    cfg, client, http = cfg_and_client()
    http.down.add("binance")   # 地域制限でつながらない状況
    res = await check_all(client)
    assert "binance" in res.errors
    # 参照のみの会場は計測を止める理由にならない
    res.raise_for_check(cfg)
    res.raise_for_run(cfg)
    assert "参照のみなので計測は続行" in "\n".join(res.lines)


async def test_session_marks_rate_limit():
    http = FakeHttp()
    http.rate_limited.add("hyperliquid")
    s = Session(http)
    with pytest.raises(VenueError) as e:
        await s.json("POST", "https://api.hyperliquid.xyz/info", json.dumps({"type": "metaAndAssetCtxs"}))
    assert e.value.rate_limited and e.value.status == 429
    assert s.rate_limited and s.calls == 1


# ---------------------------------------------------------------- エピソード


def _spread_at(net_target, hold=14.0):
    """net_apr(hold) が net_target になるスプレッドを 1 つ作る"""
    from tbf.funding.spread import Spread

    cost = 10.0
    gross = net_target + cost / 10_000 * 365 / hold
    return Spread("a", "b", "BTC", 1_000.0, gross, cost / 2, cost / 2, gross, 0.0)


def test_episode_tracker_measures_duration():
    tr = EpisodeTracker(min_net_apr=0.05, hold_days=14.0)
    assert tr.update(0, [_spread_at(0.10)]) == []
    assert tr.update(60_000, [_spread_at(0.20)]) == []
    assert tr.update(120_000, [_spread_at(0.12)]) == []
    # 閾値を割ったのでエピソードが閉じる。閉じた時点は含めない
    (ep,) = tr.update(180_000, [_spread_at(0.01)])
    assert (ep.start, ep.end, ep.polls) == (0, 120_000, 3)
    assert ep.duration_sec == 120.0
    assert ep.first_net_apr == pytest.approx(0.10)
    assert ep.max_net_apr == pytest.approx(0.20)
    assert ep.min_breakeven_days is not None
    assert not tr.open
    # 閾値を超えたまま停止した場合も、そこまでの継続時間として取り出せる
    tr.update(240_000, [_spread_at(0.10)])
    (open_ep,) = tr.flush(300_000)
    assert open_ep.polls == 1 and not tr.open


# ---------------------------------------------------------------- scanner


def scanner_for(tmp_path, cfg, client, clock):
    store = FundingStore(tmp_path / "funding.sqlite", cfg.spread_bucket_sec)
    return Scanner(cfg, client, store, clock=clock, log=lambda m: None), store


class FakeClient:
    """年率を直接指定してスナップショットを返す。scanner のロジックだけを見るため"""

    def __init__(self, cfg, aprs, now):
        self.cfg = cfg
        self.aprs = aprs       # venue -> 年率
        self.now = now
        self.last = {}
        self.fail = set()      # 503 を返す会場
        self.limited = set()   # 429 を返す会場

    async def fetch(self, v):
        limited = v.name in self.limited
        self.last[v.name] = (12.0, 1, limited)
        if limited:
            raise VenueError("HTTP 429 (rate limited)", 429, True)
        if v.name in self.fail:
            raise VenueError("HTTP 503 down", 503, False)
        snap = _snap(v.name, "BTC", self.aprs[v.name], depth=1e9)
        return VenueSnapshot(v.name, self.now(), 12.0, 1, snap.symbols)


async def test_scanner_records_funding_spreads_and_episodes(tmp_path):
    cfg, _, _ = cfg_and_client()
    cfg = replace(
        cfg, symbols=["BTC"], sizes_usd=[1_000.0], ref_size_usd=1_000.0,
        venues=[v for v in cfg.venues if v.name in ("hyperliquid", "dydx")],
    )
    t = [1_700_000_000_000]
    client = FakeClient(cfg, {"hyperliquid": 0.30, "dydx": 0.05}, lambda: t[0])
    scanner, store = scanner_for(tmp_path, cfg, client, lambda: t[0])

    for _ in range(3):
        await scanner.poll_once()
        t[0] += 60_000
    client.aprs["hyperliquid"] = 0.05   # 差が消える
    await scanner.poll_once()

    # ファンディングは会場ごとに記録される
    rows = store.query("SELECT venue, SUM(n) n FROM funding_buckets GROUP BY venue ORDER BY venue")
    assert {r["venue"]: r["n"] for r in rows} == {"dydx": 4, "hyperliquid": 4}
    # スプレッドは両向き記録される
    pairs = store.query("SELECT DISTINCT short_venue, long_venue FROM spread_buckets")
    assert {(p["short_venue"], p["long_venue"]) for p in pairs} == {("hyperliquid", "dydx"), ("dydx", "hyperliquid")}
    # エピソードは儲かる向きだけ
    eps = store.query("SELECT * FROM episodes")
    assert len(eps) == 1
    assert (eps[0]["short_venue"], eps[0]["long_venue"]) == ("hyperliquid", "dydx")
    assert eps[0]["polls"] == 3 and eps[0]["end"] - eps[0]["start"] == 120_000
    assert eps[0]["max_gross_apr"] == pytest.approx(0.25)
    assert not store.query("SELECT * FROM depth_misses")

    text = build_report(store, cfg, 0, until=t[0] + 1)
    assert "会場ペア別ランキング" in text
    assert "S:hyperliquid / L:dydx" in text
    assert "符号反転" in text and "ウォークフォワード" in text
    assert "0.00h" not in text.split("最長")[0]


async def test_scanner_survives_venue_failure_and_backs_off(tmp_path):
    cfg, _, _ = cfg_and_client()
    cfg = replace(
        cfg, symbols=["BTC"], sizes_usd=[1_000.0], ref_size_usd=1_000.0, poll_interval_sec=1.0,
        venues=[replace(v, poll_interval_sec=1.0) for v in cfg.venues if v.name in ("hyperliquid", "dydx", "lighter")],
    )
    t = [1_700_000_000_000]
    client = FakeClient(cfg, {"hyperliquid": 0.30, "dydx": 0.05, "lighter": 0.10}, lambda: t[0])
    client.limited = {"lighter"}
    scanner, store = scanner_for(tmp_path, cfg, client, lambda: t[0])
    await scanner.poll_once()

    assert store.query("SELECT COUNT(*) FROM polls WHERE ok = 0")[0][0] == 1
    assert store.query("SELECT COUNT(*) FROM polls WHERE rate_limited = 1")[0][0] == 1
    # 落ちた会場はペアに入らない -> 残り 2 会場の 2 向きだけ
    assert store.query("SELECT COUNT(DISTINCT short_venue || long_venue) FROM spread_buckets")[0][0] == 2
    # 429 を受けた会場は間隔が 4 倍に伸びる
    assert scanner.next_due["lighter"] - t[0] == 4_000
    assert scanner.next_due["dydx"] - t[0] == 1_000

    text = build_report(store, cfg, 0, until=t[0] + 1)
    assert "エラー例 lighter" in text
    # 稼働性のレイテンシは 1 リクエストあたりに割り戻している (FakeClient は 1 call / 12ms)
    assert "12ms" in text and "1 リクエストあたり" in text


async def test_poll_schedule_tolerates_clock_jitter(tmp_path):
    """ループが 1ms 早く回っただけで 1 周まるごとスキップされないこと

    厳密な >= で判定していると、実効の取得間隔が倍になって計測密度が落ちる。
    """
    cfg, _, _ = cfg_and_client()
    cfg = replace(
        cfg, symbols=["BTC"], sizes_usd=[1_000.0], ref_size_usd=1_000.0,
        venues=[replace(v, poll_interval_sec=15.0) for v in cfg.venues if v.name in ("hyperliquid", "dydx")],
    )
    t = [1_700_000_000_000]
    client = FakeClient(cfg, {"hyperliquid": 0.30, "dydx": 0.05}, lambda: t[0])
    scanner, store = scanner_for(tmp_path, cfg, client, lambda: t[0])
    await scanner.poll_once()
    t[0] += 15_000 - 1          # 1ms 早い
    assert {v.name for v in scanner.due(t[0])} == {"hyperliquid", "dydx"}
    await scanner.poll_once()
    assert store.query("SELECT COUNT(*) FROM polls")[0][0] == 4
    # 許容幅を超えて早い場合はちゃんと待つ
    t[0] += 1_000
    assert scanner.due(t[0]) == []


async def test_stale_snapshot_is_excluded(tmp_path):
    cfg, _, _ = cfg_and_client()
    cfg = replace(
        cfg, symbols=["BTC"], sizes_usd=[1_000.0], ref_size_usd=1_000.0, max_stale_sec=60.0,
        venues=[replace(v, poll_interval_sec=30.0) for v in cfg.venues if v.name in ("hyperliquid", "dydx")],
    )
    t = [1_700_000_000_000]
    client = FakeClient(cfg, {"hyperliquid": 0.30, "dydx": 0.05}, lambda: t[0])
    scanner, store = scanner_for(tmp_path, cfg, client, lambda: t[0])
    await scanner.poll_once()
    assert len(scanner.latest) == 2

    # dydx だけ落として、max_stale_sec を超えるまで時間を進める
    client.fail = {"dydx"}
    t[0] += 30_000
    await scanner.poll_once()
    assert store.query("SELECT COUNT(*) FROM spread_buckets")[0][0] == 2   # まだ使える
    t[0] += 60_000
    before = store.query("SELECT SUM(n) n FROM spread_buckets")[0]["n"]
    await scanner.poll_once()
    # 古くなった dydx は計算から外れるので、ペアが作れず n は増えない
    assert store.query("SELECT SUM(n) n FROM spread_buckets")[0]["n"] == before


# ---------------------------------------------------------------- ウォークフォワード


def _fill_window(store, bucket, short, long, symbol, size, gross, cost=10.0):
    from tbf.funding.spread import Spread

    store.add_spread(bucket, Spread(short, long, symbol, size, gross, cost / 2, cost / 2, gross, 0.0))


async def test_walkforward_scores_the_previous_windows_pick(tmp_path):
    """窓 1 で最良だったペアが窓 2 では劣後する、という形を作って効率を確かめる"""
    store = FundingStore(tmp_path / "f.sqlite", 1.0)
    size = 1_000.0
    # 窓 1 (0-999ms): A が 30%, B が 10%
    for ms in (0, 500):
        _fill_window(store, ms, "a", "x", "BTC", size, 0.30)
        _fill_window(store, ms, "b", "x", "BTC", size, 0.10)
    # 窓 2 (1000-1999ms): A は 1% に落ち、B が 20% で最良
    for ms in (1000, 1500):
        _fill_window(store, ms, "a", "x", "BTC", size, 0.01)
        _fill_window(store, ms, "b", "x", "BTC", size, 0.20)
    store.commit()

    w1, w2 = walkforward(store, 0, 2000, windows=2, size_usd=size, hold=14.0)
    assert w1.picked is None            # 前の窓が無いので選べない
    assert w1.best == ("a", "x", "BTC")
    assert w2.picked == ("a", "x", "BTC")
    assert w2.best == ("b", "x", "BTC")
    assert w2.picked_is_net == pytest.approx(net_apr(0.30, 10.0, 14))
    assert w2.oos_net == pytest.approx(net_apr(0.01, 10.0, 14))
    assert w2.best_net == pytest.approx(net_apr(0.20, 10.0, 14))
    # OOS はマイナス、効率もマイナス -> 後知恵の最良値では運用の見込みを測れない
    assert w2.oos_net < 0 < w2.best_net
    assert w2.efficiency == pytest.approx(w2.oos_net / w2.best_net)
    assert walkforward(store, 0, 2000, windows=1, size_usd=size, hold=14.0) == []


def test_pair_sign_flips_counts_side_swaps(tmp_path):
    store = FundingStore(tmp_path / "f.sqlite", 0.001)   # バケット幅 1ms = 1 ポーリング 1 バケット
    for ms, gross in ((0, 0.10), (1, 0.05), (2, -0.03), (3, -0.01), (4, 0.02)):
        _fill_window(store, ms, "a", "b", "BTC", 1_000.0, gross)
    store.commit()
    assert pair_sign_flips(store, 0, 10, 1_000.0)[("a", "b", "BTC")] == 2


# ---------------------------------------------------------------- 設定


def test_config_reads_the_registry():
    cfg = load_config(ROOT / "funding.toml", ROOT / "data" / "venues.toml")
    assert [v.name for v in cfg.tradable] == ["hyperliquid", "lighter", "paradex", "dydx"]
    assert [v.name for v in cfg.venues if v.reference_only] == ["binance"]
    assert cfg.venue("lighter").funding_interval_hours == 8
    assert cfg.venue("hyperliquid").funding_interval_hours == 1
    assert cfg.ref_size_usd in cfg.sizes_usd
    assert cfg.rank_hold_days in cfg.hold_days
    # 会場ごとにポーリング間隔を上書きできる (BAN 回避)
    assert cfg.venue("binance").poll_interval_sec != cfg.poll_interval_sec


def test_config_rejects_unusable_setups(tmp_path):
    venues = (ROOT / "data" / "venues.toml").read_text()
    base = (ROOT / "funding.toml").read_text()

    def write(name, text):
        p = tmp_path / name
        p.write_text(text)
        return p

    only_one = write("v1.toml", venues.split("[[venues]]")[0] + "[[venues]]" + venues.split("[[venues]]")[1])
    with pytest.raises(ValueError, match="2 つ以上必要"):
        load_config(write("f1.toml", base), only_one)

    with pytest.raises(ValueError, match="ref_size_usd"):
        load_config(write("f2.toml", base.replace("ref_size_usd = 5_000", "ref_size_usd = 7_777")), ROOT / "data" / "venues.toml")

    with pytest.raises(ValueError, match="schema_version"):
        load_config(write("f3.toml", base), write("v3.toml", venues.replace("schema_version = 1", "schema_version = 2")))

    with pytest.raises(ValueError, match="symbols"):
        load_config(write("f4.toml", base.replace('symbols = ["BTC", "ETH"]', 'symbols = ["DOGE"]')), ROOT / "data" / "venues.toml")


async def test_unknown_adapter_is_rejected():
    cfg, client, _ = cfg_and_client()
    v = replace(cfg.venue("hyperliquid"), adapter="nope")
    with pytest.raises(VenueError, match="unknown adapter"):
        await client.fetch(v)


HOUR = 3_600_000


# ---------------------------------------------------------------- 建ての自動検証


async def test_check_verifies_the_funding_interval_on_every_venue():
    """建てを API 側から確かめる (レジストリにそう書いてあるでは不十分)

    建てを取り違えると年率が 8 倍ずれる。Paradex だけ自動チェックがあり、
    Lighter の検算は fixture に残っているだけ、dydx と Binance は無検証だった。
    """
    cfg, client, _ = cfg_and_client()
    res = await check_all(client)
    assert res.config_errors == []
    # 全ての会場で API 側から建てが読めていること
    assert res.api_intervals == {
        "hyperliquid": 1.0,
        "lighter": 8.0,
        "paradex": 8.0,
        "dydx": 1.0,
        "binance": 8.0,
    }


async def test_check_fails_when_the_registry_interval_is_wrong():
    """設定の建てを 8h -> 1h に変えたら、API との食い違いとして止まること"""
    cfg, client, http = cfg_and_client()
    broken = [replace(v, funding_interval_hours=1) if v.name == "lighter" else v for v in cfg.venues]
    client.cfg = replace(cfg, venues=broken)
    res = await check_all(client)
    assert any("lighter" in e and "検証に失敗" in e for e in res.config_errors), res.config_errors
    with pytest.raises(VenueError):
        res.raise_for_check(client.cfg)


async def test_binance_funding_interval_comes_from_funding_info():
    """Binance は銘柄によって 8h / 4h がある。fundingInfo に出るのは既定と違う銘柄だけ"""
    from tbf.funding.venues import Session, verify_binance

    cfg, client, http = cfg_and_client()
    v = cfg.venue("binance")
    assert (await verify_binance(Session(http), v, "BTCUSDT")).hours == 8.0
    # fixture に入れた 4h の銘柄は 4h として読めること (BTC と同じ既定にしない)
    four = [r for r in fixture("binance_fundingInfo") if r["fundingIntervalHours"] == 4]
    assert four, "4h の銘柄を fixture に入れておく"
    assert (await verify_binance(Session(http), v, four[0]["symbol"])).hours == 4.0


def test_hyperliquid_book_is_capped_at_20_levels():
    """l2Book は片側 20 本までしか返さない。book_depth = 100 は効いていない"""
    cfg = load_config(ROOT / "funding.toml", ROOT / "data" / "venues.toml")
    hl = cfg.venue("hyperliquid")
    assert hl.book_depth == 100 and hl.max_book_levels == 20
    assert hl.effective_book_depth == 20
    book = fixture("hyperliquid_l2Book_BTC")
    assert len(book["levels"][0]) <= 20 and len(book["levels"][1]) <= 20
    # 上限を宣言していない会場は book_depth のまま
    assert cfg.venue("dydx").effective_book_depth == cfg.venue("dydx").book_depth


# ---------------------------------------------------------------- 履歴 (確定ファンディング)


def _fixture_window(venue):
    """その会場の fixture に入っている時刻をちょうど覆う範囲 (ms)

    fixture は会場ごとに別の時点で取ったものなので、範囲は会場ごとに出す。
    (全部まとめると Paradex のサンプル点が 90 日ぶんになって遅い)
    """
    from datetime import datetime

    if venue == "hyperliquid":
        times = [int(f["time"]) for f in fixture("hyperliquid_fundingHistory_BTC")]
    elif venue == "lighter":
        times = [int(f["timestamp"]) * 1000 for f in fixture("lighter_fundings_BTC")["fundings"]]
    elif venue == "paradex":
        times = [int(r["created_at"]) for r in fixture("paradex_funding_data_BTC")["results"]]
        times += [int(r[0]) for r in fixture("paradex_klines_BTC")["results"]]
    elif venue == "dydx":
        times = [
            int(datetime.fromisoformat(f["effectiveAt"].replace("Z", "+00:00")).timestamp() * 1000)
            for f in fixture("dydx_historicalFunding_BTC")["historicalFunding"]
        ]
    else:
        raise AssertionError(venue)
    return min(times) - HOUR, max(times) + HOUR


async def test_history_normalizes_sign_and_interval_across_venues():
    """会場ごとに建ても符号の表し方も違うので、「ロングが払うとき +」の 1 時間率に揃える"""
    from tbf.funding.history import fetch_history
    from tbf.funding.venues import Session

    cfg, client, http = cfg_and_client()
    for name in ("hyperliquid", "lighter", "paradex", "dydx"):
        v = cfg.venue(name)
        if name == "paradex":
            # fixture は 2 点しか無いので、サンプル間隔を縮めて両方を踏ませる
            v = replace(v, history_sample_hours=1.0)
        lo, hi = _fixture_window(name)
        points = await fetch_history(Session(http), v, "BTC", lo, hi)
        assert points, name
        assert all(p.hours > 0 for p in points), name
        # fixture はどれも +10% 前後の平らな期間なので、年率が同じオーダーに乗ること
        aprs = [p.apr for p in points]
        assert 0.0 < sum(aprs) / len(aprs) < 0.5, (name, aprs[:3])
        assert points == sorted(points, key=lambda p: p.time)


async def test_lighter_history_sign_comes_from_direction():
    """Lighter の /fundings は rate に符号を持たず direction で表す

    ここを取り違えると支払い側と受け取り側が丸ごと入れ替わる。
    同じレートで direction だけ変えたら符号が反転することを固定する。
    """
    from tbf.funding.history import history_lighter
    from tbf.funding.venues import Session

    cfg = load_config(ROOT / "funding.toml", ROOT / "data" / "venues.toml")
    v = cfg.venue("lighter")

    class Flipped(FakeHttp):
        def __init__(self, direction):
            super().__init__()
            self.direction = direction

        def _route(self, method, url, body):
            payload = super()._route(method, url, body)
            if "fundings" in url and "funding-rates" not in url:
                payload = json.loads(json.dumps(payload))
                for f in payload["fundings"]:
                    f["direction"] = self.direction
            return payload

    assert all(f["direction"] == "long" for f in fixture("lighter_fundings_BTC")["fundings"])
    lo, hi = _fixture_window("lighter")
    longs = await history_lighter(Session(Flipped("long")), v, "BTC", lo, hi)
    shorts = await history_lighter(Session(Flipped("short")), v, "BTC", lo, hi)
    assert all(p.rate > 0 for p in longs)
    assert all(p.rate < 0 for p in shorts)
    assert [p.rate for p in longs] == [-p.rate for p in shorts]
    # mark 価格は符号に関係なく同じ (value / |rate|)
    assert [p.mark for p in longs] == [p.mark for p in shorts]


async def test_paradex_history_uses_the_cumulative_index():
    """Paradex は連続課金。累積 funding_index の差を mark で割ると分数になる"""
    from tbf.funding.history import history_paradex
    from tbf.funding.venues import Session

    cfg, client, http = cfg_and_client()
    v = cfg.venue("paradex")
    lo, hi = _fixture_window("paradex")
    points = await history_paradex(Session(http), v, "BTC-USD-PERP", lo, hi, sample_hours=1.0)
    rows = fixture("paradex_funding_data_BTC")["results"]
    rows = sorted(rows, key=lambda r: r["created_at"])
    mark = float(fixture("paradex_klines_BTC")["results"][0][4])
    expected = (float(rows[-1]["funding_index"]) - float(rows[0]["funding_index"])) / mark
    assert len(points) == len(rows) - 1
    assert sum(p.rate for p in points) == pytest.approx(expected, rel=1e-6)
    # funding_rate_8h と同じオーダーに乗ること (建てを取り違えていないことの確認)
    quoted_8h = float(rows[-1]["funding_rate_8h"])
    per_8h = sum(p.rate for p in points) / sum(p.hours for p in points) * 8
    assert 0.2 < per_8h / quoted_8h < 5, (per_8h, quoted_8h)


def test_history_round_trips_through_the_store(tmp_path):
    from tbf.funding.history import FundingPoint

    store = FundingStore(tmp_path / "f.sqlite", 300.0)
    pts = [FundingPoint(3_600_000 * i, 1.0, 1e-5 * i, 100.0 + i) for i in range(1, 5)]
    assert store.add_history("hyperliquid", "BTC", pts) == 4
    # 同じ時刻は上書き (backfill を何度流しても増えない)
    store.add_history("hyperliquid", "BTC", pts)
    got = store.history(0, 1 << 62)
    assert list(got) == [("hyperliquid", "BTC")]
    assert [p.rate for p in got[("hyperliquid", "BTC")]] == [p.rate for p in pts]
    (cov,) = store.history_coverage()
    assert (cov["n"], cov["marks"]) == (4, 4)


# ---------------------------------------------------------------- 確定ファンディングでの主指標


def _points(start_hour, hourly_rates, mark=100.0):
    """1 時間刻みの確定ファンディングを作る"""
    from tbf.funding.history import FundingPoint

    marks = mark if isinstance(mark, list) else [mark] * len(hourly_rates)
    return [
        FundingPoint((start_hour + i + 1) * HOUR, 1.0, r, marks[i])
        for i, r in enumerate(hourly_rates)
    ]


def test_realized_sums_actual_funding_instead_of_extrapolating():
    """主指標は「実際に確定したファンディングの合計 − 往復コスト」

    ライブの net_apr は「今の予測レートが hold 日続く」という外挿なので、
    途中でレートが変わった期間の損益は再現しない。
    """
    from tbf.funding.realized import build_series, realize

    hours = 48
    # 前半は年率 +36.5% 相当 (1e-4/h) だが、後半は 0 に落ちる
    short = [1e-4] * 24 + [0.0] * 24
    series = build_series({("a", "BTC"): _points(0, short), ("b", "BTC"): _points(0, [0.0] * hours)})
    t = realize(series, ("a", "b", "BTC"), 0, 2.0, cost_bps=0.0)
    # 実際に確定したのは 24 時間ぶんの 1e-4 = 24bps
    assert t.gross == pytest.approx(24e-4)
    assert t.net_apr == pytest.approx(24e-4 * 365 / 2)
    # 建てた瞬間のレート (1e-4/h = 年率 87.6%) をそのまま伸ばすと倍の見込みになる
    assert t.net_apr < 1e-4 * 24 * 365 * 0.6

    # 往復コストは保有期間 1 回ぶんだけ引く (年率に按分しない)
    with_cost = realize(series, ("a", "b", "BTC"), 0, 2.0, cost_bps=12.0)
    assert with_cost.net == pytest.approx(t.gross - 12.0 / 10_000)


def test_realized_skips_entries_without_enough_coverage():
    from tbf.funding.realized import build_series, realize

    # ロング側は後半 24 時間ぶんのデータが無い
    series = build_series(
        {("a", "BTC"): _points(0, [1e-4] * 48), ("b", "BTC"): _points(0, [0.0] * 24)}
    )
    assert realize(series, ("a", "b", "BTC"), 0, 2.0, 0.0, min_coverage=0.9) is None
    partial = realize(series, ("a", "b", "BTC"), 0, 2.0, 0.0, min_coverage=0.4)
    assert partial is not None and partial.coverage == pytest.approx(0.5)


def test_realized_basis_is_the_relative_price_move_and_stays_out_of_net():
    """basis は「建てた瞬間の水準」ではなく「保有期間中の変化」が損益になる"""
    from tbf.funding.realized import build_series, realize

    # ショート側は動かず、ロング側が 1% 上がる -> ロング脚の含み益
    series = build_series(
        {
            ("a", "BTC"): _points(0, [0.0] * 24, mark=[100.0] * 24),
            ("b", "BTC"): _points(0, [0.0] * 24, mark=[100.0] * 12 + [101.0] * 12),
        }
    )
    t = realize(series, ("a", "b", "BTC"), 0, 1.0, cost_bps=0.0)
    assert t.basis_pnl == pytest.approx(0.01, abs=1e-9)
    # net には入れない (依頼票の式どおり)。合計は別に出す
    assert t.net == pytest.approx(0.0)
    assert t.net_apr_with_basis == pytest.approx(0.01 * 365)


def test_walkforward_window_equals_the_hold_period():
    """窓の長さと保有期間を一致させる

    以前は窓 1 日なのに保有 14 日としてコストを按分していて、
    評価している期間と保有期間が一致していなかった。
    """
    from tbf.funding.realized import build_series, walkforward_realized

    hold = 1.0
    series = build_series(
        {
            ("a", "BTC"): _points(0, [1e-4] * 96),
            ("b", "BTC"): _points(0, [0.0] * 96),
        }
    )
    wins = walkforward_realized(series, [("a", "b", "BTC")], 0, 96 * HOUR, hold, {}, 0.0)
    assert [w.hold_days for w in wins] == [hold] * len(wins)
    for w in wins:
        assert w.end - w.start == int(hold * 24) * HOUR


def test_walkforward_charges_the_switch_cost_only_when_the_pair_changes():
    """乗り換えコストを課さないと、ペアが頻繁に入れ替わるほど OOS が良く見える"""
    from tbf.funding.realized import build_series, walkforward_realized

    hold = 1.0
    cost = {("a", "b", "BTC"): 100.0, ("b", "a", "BTC"): 100.0}
    # a は 1 日目だけ高く、2 日目以降は b のほうが高い -> 窓ごとに最良ペアが入れ替わる
    a = [2e-4] * 24 + [0.0] * 24 + [0.0] * 24 + [0.0] * 24
    b = [0.0] * 24 + [2e-4] * 24 + [2e-4] * 24 + [2e-4] * 24
    series = build_series({("a", "BTC"): _points(0, a), ("b", "BTC"): _points(0, b)})
    keys = [("a", "b", "BTC"), ("b", "a", "BTC")]
    wins = walkforward_realized(series, keys, 0, 96 * HOUR, hold, cost, 100.0)
    switched = [w for w in wins if w.picked and w.switched]
    kept = [w for w in wins if w.picked and not w.switched]
    assert switched, "ペアが入れ替わる窓があること"
    assert kept, "同じペアを持ち続ける窓があること"
    # 乗り換えた窓は往復 100bps を引かれ、持ち続けた窓は引かれない
    for w in switched:
        assert w.oos_net_apr == pytest.approx((w.oos_gross_apr or 0) - 100.0 / 10_000 * 365 / hold)
    for w in kept:
        assert w.oos_net_apr == pytest.approx(w.oos_gross_apr)


def test_walkforward_efficiency_compares_oos_with_hindsight():
    from tbf.funding.realized import build_series, walkforward_realized

    hold = 1.0
    a = [1e-4] * 24 + [1e-4] * 24 + [0.0] * 24
    b = [0.0] * 24 + [0.0] * 24 + [3e-4] * 24
    series = build_series({("a", "BTC"): _points(0, a), ("b", "BTC"): _points(0, b)})
    keys = [("a", "b", "BTC"), ("b", "a", "BTC")]
    wins = walkforward_realized(series, keys, 0, 72 * HOUR, hold, {}, 0.0)
    last = wins[-1]
    # 前の窓の最良 (a ショート) を持ったが、この窓の最良は反対側だった
    assert last.picked == ("a", "b", "BTC")
    assert last.best == ("b", "a", "BTC")
    assert last.efficiency is not None and last.efficiency < 0


def test_capital_apr_halves_at_full_collateral():
    """net_apr は片脚の建玉に対する率。資本は 2 会場ぶん必要"""
    from tbf.funding.realized import capital_apr

    assert capital_apr(0.10, leverage=1.0) == pytest.approx(0.05)
    assert capital_apr(0.10, leverage=2.0) == pytest.approx(0.10)
    with pytest.raises(ValueError):
        capital_apr(0.10, leverage=0)


# ---------------------------------------------------------------- 価格の鮮度


def test_stale_prices_are_excluded_from_pairs():
    """ファンディングは 3 分古くても困らないが、価格差とスリッページは混ぜられない"""
    from tbf.funding.spread import stale_price_venues

    cfg = load_config(ROOT / "funding.toml", ROOT / "data" / "venues.toml")
    assert cfg.max_price_stale_sec < cfg.max_stale_sec
    now = 10_000_000
    fresh = _snap("hyperliquid", "BTC", 0.20)
    fresh = replace(fresh, time=now - 5_000)
    old = replace(_snap("lighter", "BTC", 0.02), time=now - 120_000)
    # max_stale_sec (180s) では両方生きているが、価格の鮮度 (30s) では lighter は古い
    assert stale_price_venues([fresh, old], cfg, now) == {"lighter"}
    spreads, _ = evaluate([fresh, old], cfg, now)
    assert spreads == []
    # 鮮度を緩めればペアになる
    spreads, _ = evaluate([fresh, old], replace(cfg, max_price_stale_sec=300), now)
    assert spreads


def test_spread_records_the_fetch_time_lag():
    cfg = load_config(ROOT / "funding.toml", ROOT / "data" / "venues.toml")
    now = 10_000_000
    a = replace(_snap("hyperliquid", "BTC", 0.20), time=now)
    b = replace(_snap("lighter", "BTC", 0.02), time=now - 7_000)
    spreads, _ = evaluate([a, b], cfg, now)
    assert spreads and all(s.lag_ms == 7_000 for s in spreads)


def test_stale_data_holds_the_episode_open_instead_of_splitting_it():
    """片方の会場のデータが古くなっただけで閉じると、最長継続時間が短く出る"""
    tr = EpisodeTracker(min_net_apr=0.05, hold_days=14.0, max_pending_sec=180.0)
    tr.update(0, [_spread_at(0.10)])
    # a のデータが古くなって計算から外れた -> 閉じずに保留
    assert tr.update(60_000, [], stale_venues={"a"}) == []
    assert tr.update(120_000, [], stale_venues={"a"}) == []
    # 戻ってきたら同じエピソードとして続く (別エピソードにしない)
    assert tr.update(180_000, [_spread_at(0.10)]) == []
    (ep,) = tr.update(240_000, [_spread_at(0.01)])
    assert ep.start == 0 and ep.end == 180_000
    assert ep.duration_sec == 180.0
    assert ep.pending_polls == 2

    # 閾値を割ったとき (データはある) は従来どおり閉じる
    tr2 = EpisodeTracker(min_net_apr=0.05, hold_days=14.0)
    tr2.update(0, [_spread_at(0.10)])
    assert len(tr2.update(60_000, [_spread_at(0.01)])) == 1

    # 保留の上限を超えたら閉じる (ずっと見えないものを続いていたとは言えない)
    tr3 = EpisodeTracker(min_net_apr=0.05, hold_days=14.0, max_pending_sec=60.0)
    tr3.update(0, [_spread_at(0.10)])
    assert tr3.update(30_000, [], stale_venues={"a"}) == []
    assert len(tr3.update(120_000, [], stale_venues={"a"})) == 1


def test_efficiency_never_exceeds_hindsight_when_holding_pays_no_cost():
    """後から見た最良も「今持っているペアはコストなし」の土台で選ぶ

    OOS 側だけ乗り換えコストを免除すると、持ち続けた窓で OOS > 後見最良 になり
    効率が 100% を超えてしまう (良く見えるだけで、比として意味がない)。
    """
    from tbf.funding.realized import build_series, walkforward_realized

    hold = 1.0
    cost = {("a", "b", "BTC"): 200.0, ("b", "a", "BTC"): 200.0}
    # a ショートがずっと最良。ただし 3 日目は差が小さくなる
    a = [2e-4] * 24 + [2e-4] * 24 + [2e-6] * 24
    series = build_series({("a", "BTC"): _points(0, a), ("b", "BTC"): _points(0, [0.0] * 72)})
    keys = [("a", "b", "BTC"), ("b", "a", "BTC")]
    wins = walkforward_realized(series, keys, 0, 72 * HOUR, hold, cost, 200.0)
    effs = [w.efficiency for w in wins if w.efficiency is not None]
    assert effs, "効率の出る窓があること"
    assert all(e <= 1.0 + 1e-9 for e in effs), effs
