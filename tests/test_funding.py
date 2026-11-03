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
        if "zklighter" in url:
            if "orderBookDetails" in url:
                return fixture("lighter_orderBookDetails")
            if "funding-rates" in url:
                return fixture("lighter_funding-rates")
            if "orderBookOrders" in url:
                mid = int(re.search(r"market_id=(\d+)", url).group(1))
                return fixture(f"lighter_orderBookOrders_{'BTC' if mid == 1 else 'ETH'}")
        if "paradex" in url:
            sym = "BTC" if "BTC" in url else "ETH"
            if "summary" in url:
                return fixture(f"paradex_summary_{sym}")
            if "orderbook" in url:
                return fixture(f"paradex_orderbook_{sym}")
        if "dydx" in url:
            sym = "BTC" if "BTC" in url else "ETH"
            if "perpetualMarkets" in url:
                return fixture(f"dydx_market_{sym}")
            if "orderbooks" in url:
                return fixture(f"dydx_orderbook_{sym}")
        if "binance" in url:
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
