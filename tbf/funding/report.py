"""集計レポート

net_apr(hold) は保存していない。gross と往復コスト (bps) の平均から導く:
    net_apr(hold) = gross_apr - cost_bps / 10000 * 365 / hold
breakeven_days も同じ 2 つから導く (平均の比。比の平均ではない)。
"""

from __future__ import annotations

import statistics
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

from tbf.funding.config import FundingConfig
from tbf.funding.spread import BPS, DAYS_PER_YEAR
from tbf.funding.store import FundingStore


def net_apr(gross_apr: float, cost_bps: float, hold_days: float) -> float:
    return gross_apr - cost_bps / BPS * DAYS_PER_YEAR / hold_days


def breakeven_days(gross_apr: float, cost_bps: float) -> Optional[float]:
    if gross_apr <= 0:
        return None
    return cost_bps / (gross_apr * BPS / DAYS_PER_YEAR)


def _width(s: str) -> int:
    """端末上の表示幅。日本語は 2 文字ぶん"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    """表示幅で揃える (f-string の :<n は日本語がずれる)"""
    fill = " " * max(0, width - _width(s))
    return fill + s if right else s + fill


def _row(cells: list[tuple[str, int]]) -> str:
    """(文字列, 幅) の列を表示幅で揃えて 1 行にする。幅が負なら右寄せ"""
    return "".join(_pad(s, abs(w), right=w < 0) for s, w in cells)


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:+.2f}%"


def _days(x: Optional[float]) -> str:
    if x is None:
        return "なし"
    return f"{x:.1f}日" if x < 10_000 else ">1万日"


def _hours(sec: float) -> str:
    return f"{sec / 3600:.2f}h"


# ------------------------------------------------------------------ 集計の素

@dataclass(frozen=True)
class PairStat:
    short_venue: str
    long_venue: str
    symbol: str
    size_usd: float
    n: int
    avg_gross: float
    min_gross: float
    max_gross: float
    avg_cost_bps: float
    min_cost_bps: float
    max_cost_bps: float
    # 入った瞬間の 2 会場の価格差 (bps)。net_apr には含めていない
    avg_basis_bps: float = 0.0
    min_basis_bps: float = 0.0
    max_basis_bps: float = 0.0

    @property
    def label(self) -> str:
        return f"S:{self.short_venue} / L:{self.long_venue}"

    def net(self, hold_days: float) -> float:
        return net_apr(self.avg_gross, self.avg_cost_bps, hold_days)

    @property
    def breakeven(self) -> Optional[float]:
        return breakeven_days(self.avg_gross, self.avg_cost_bps)


def pair_stats(store: FundingStore, since: int, until: int, size_usd: Optional[float] = None) -> list[PairStat]:
    sql = """SELECT short_venue, long_venue, symbol, size_usd, SUM(n) n,
                    SUM(sum_gross)/SUM(n) avg_gross, MIN(min_gross) min_gross, MAX(max_gross) max_gross,
                    SUM(sum_cost_bps)/SUM(n) avg_cost, MIN(min_cost_bps) min_cost, MAX(max_cost_bps) max_cost,
                    SUM(sum_basis_bps)/SUM(n) avg_basis, MIN(min_basis_bps) min_basis, MAX(max_basis_bps) max_basis
             FROM spread_buckets WHERE bucket >= ? AND bucket < ?"""
    params: tuple = (since, until)
    if size_usd is not None:
        sql += " AND size_usd = ?"
        params = (since, until, size_usd)
    sql += " GROUP BY short_venue, long_venue, symbol, size_usd"
    return [
        PairStat(
            r["short_venue"], r["long_venue"], r["symbol"], r["size_usd"], r["n"],
            r["avg_gross"], r["min_gross"], r["max_gross"], r["avg_cost"], r["min_cost"], r["max_cost"],
            r["avg_basis"], r["min_basis"], r["max_basis"],
        )
        for r in store.query(sql, params)
    ]


def episode_stats(store: FundingStore, since: int, size_usd: Optional[float] = None) -> dict[tuple, tuple[int, float, float]]:
    """(short, long, symbol, size) -> (エピソード数, 最長継続秒, 最大 net_apr)"""
    sql = """SELECT short_venue, long_venue, symbol, size_usd, COUNT(*) n,
                    MAX(end - start) longest, MAX(max_net_apr) best
             FROM episodes WHERE start >= ?"""
    params: tuple = (since,)
    if size_usd is not None:
        sql += " AND size_usd = ?"
        params = (since, size_usd)
    sql += " GROUP BY short_venue, long_venue, symbol, size_usd"
    return {
        (r["short_venue"], r["long_venue"], r["symbol"], r["size_usd"]): (r["n"], r["longest"] / 1000, r["best"])
        for r in store.query(sql, params)
    }


# ------------------------------------------------------------------ ウォークフォワード

@dataclass(frozen=True)
class WfWindow:
    index: int
    start: int
    end: int
    # 前の窓 (IS) で一番良かったペア
    picked: Optional[tuple[str, str, str]]
    picked_is_net: Optional[float]
    # そのペアをこの窓で持った場合 (OOS)
    oos_net: Optional[float]
    # この窓を後から見たときの最良ペアとその値
    best: Optional[tuple[str, str, str]]
    best_net: Optional[float]

    @property
    def efficiency(self) -> Optional[float]:
        if self.oos_net is None or self.best_net is None or self.best_net <= 0:
            return None
        return self.oos_net / self.best_net


def _window_nets(store: FundingStore, start: int, end: int, size_usd: float, hold: float) -> dict[tuple[str, str, str], float]:
    return {
        (s.short_venue, s.long_venue, s.symbol): s.net(hold)
        for s in pair_stats(store, start, end, size_usd)
    }


def walkforward(
    store: FundingStore, since: int, until: int, windows: int, size_usd: float, hold: float
) -> list[WfWindow]:
    """「前の窓で最良だったペアを次の窓で持つ」を窓ごとに評価する

    全期間を 1 回通した平均は、後からペアを選べる前提なので実運用の見込みにはならない。
    知りたいのは「選んだ時点では分からない状態で選んだペアが、次の期間でどうだったか」。
    """
    if windows < 2 or until <= since:
        return []
    width = (until - since) // windows
    if width <= 0:
        return []
    out: list[WfWindow] = []
    prev: dict[tuple[str, str, str], float] = {}
    for i in range(windows):
        ws, we = since + i * width, since + (i + 1) * width
        nets = _window_nets(store, ws, we, size_usd, hold)
        best_key = max(nets, key=lambda k: nets[k]) if nets else None
        picked = max(prev, key=lambda k: prev[k]) if prev else None
        out.append(
            WfWindow(
                index=i + 1,
                start=ws,
                end=we,
                picked=picked,
                picked_is_net=prev.get(picked) if picked else None,
                oos_net=nets.get(picked) if picked else None,
                best=best_key,
                best_net=nets.get(best_key) if best_key else None,
            )
        )
        prev = nets
    return out


# ------------------------------------------------------------------ 符号反転

@dataclass(frozen=True)
class FlipStat:
    venue: str
    symbol: str
    n: int
    avg_apr: float
    min_apr: float
    max_apr: float
    neg_ratio: float
    flips: int
    buckets: int


def funding_flips(store: FundingStore, since: int, until: int) -> list[FlipStat]:
    """会場・銘柄ごとのファンディング年率の水準と、符号が反転した回数

    反転するとショート側が受け取りから支払いに回るので、解消ルールの設計に必要。
    """
    rows = store.query(
        """SELECT venue, symbol, bucket, n, sum_apr, sum_apr/n avg_apr, min_apr, max_apr, neg_n
           FROM funding_buckets WHERE bucket >= ? AND bucket < ? ORDER BY venue, symbol, bucket""",
        (since, until),
    )
    grouped: dict[tuple[str, str], list] = {}
    for r in rows:
        grouped.setdefault((r["venue"], r["symbol"]), []).append(r)
    out = []
    for (venue, symbol), rs in sorted(grouped.items()):
        n = sum(r["n"] for r in rs)
        flips = 0
        last_sign = 0
        for r in rs:
            sign = 1 if r["avg_apr"] > 0 else (-1 if r["avg_apr"] < 0 else 0)
            if sign != 0 and last_sign != 0 and sign != last_sign:
                flips += 1
            if sign != 0:
                last_sign = sign
        out.append(
            FlipStat(
                venue=venue,
                symbol=symbol,
                n=n,
                avg_apr=sum(r["sum_apr"] for r in rs) / n,
                min_apr=min(r["min_apr"] for r in rs),
                max_apr=max(r["max_apr"] for r in rs),
                neg_ratio=sum(r["neg_n"] for r in rs) / n,
                flips=flips,
                buckets=len(rs),
            )
        )
    return out


def pair_sign_flips(store: FundingStore, since: int, until: int, size_usd: float) -> dict[tuple[str, str, str], int]:
    """会場ペアの粗スプレッドの符号が反転した回数 (= 受け取り側と支払い側が入れ替わった回数)"""
    rows = store.query(
        """SELECT short_venue, long_venue, symbol, bucket, sum_gross/n avg_gross
           FROM spread_buckets WHERE bucket >= ? AND bucket < ? AND size_usd = ?
           ORDER BY short_venue, long_venue, symbol, bucket""",
        (since, until, size_usd),
    )
    flips: dict[tuple[str, str, str], int] = {}
    last: dict[tuple[str, str, str], int] = {}
    for r in rows:
        key = (r["short_venue"], r["long_venue"], r["symbol"])
        sign = 1 if r["avg_gross"] > 0 else (-1 if r["avg_gross"] < 0 else 0)
        if sign != 0:
            if last.get(key, 0) not in (0, sign):
                flips[key] = flips.get(key, 0) + 1
            last[key] = sign
        flips.setdefault(key, 0)
    return flips


# ------------------------------------------------------------------ 本体


def build_report(store: FundingStore, cfg: FundingConfig, since: int, until: Optional[int] = None) -> str:
    until = until or int(time.time() * 1000)
    hold = cfg.rank_hold_days
    ref = cfg.ref_size_usd
    L: list[str] = []

    # ---- 0. 計測期間
    first = store.get_meta("first_run_ms")
    cov = store.query(
        "SELECT MIN(bucket) lo, MAX(bucket) hi, COUNT(DISTINCT bucket) nb FROM spread_buckets WHERE bucket >= ? AND bucket < ?",
        (since, until),
    )[0]
    L.append("== 0. 計測期間とカバー率")
    if cov["lo"] is None:
        L.append("まだデータがありません (run を起動してください)")
        return "\n".join(L)
    span_ms = cov["hi"] - cov["lo"] + store.bucket_ms
    expected = max(1, int(span_ms / store.bucket_ms))
    L.append(
        f"最初の run: {_ts(int(first)) if first else '不明'}   "
        f"集計対象: {_ts(cov['lo'])} 〜 {_ts(cov['hi'])} ({span_ms / 86_400_000:.2f}日)"
    )
    L.append(
        f"バケット: {cov['nb']}/{expected} ({cov['nb'] / expected * 100:.1f}%)  "
        f"バケット幅 {store.bucket_ms / 1000:.0f}s  "
        f"ランキング基準: 片脚 ${ref:,.0f} / 保有 {hold:g}日 / 機会の閾値 net_apr >= {cfg.min_net_apr * 100:.1f}%"
    )
    L.append("※ 粗スプレッドではなく、往復の taker 手数料とスリッページを引いた net_apr で並べています")

    # ---- 1. 会場ペア別ランキング
    stats = sorted(pair_stats(store, since, until, ref), key=lambda s: s.net(hold), reverse=True)
    eps = episode_stats(store, since, ref)
    L.append(f"\n== 1. 会場ペア別ランキング (片脚 ${ref:,.0f}, net_apr({hold:g}d) 降順)")
    w1 = [32, 5, -9, -9, -9, -9, -9, -10, -6, -9, -7]
    L.append(_row(list(zip(
        ["ショート / ロング", "銘柄", f"net{hold:g}d", "粗 avg", "粗 min", "粗 max", "往復bps",
         "breakeven", "回数", "最長", "観測"], w1))))
    if not stats:
        L.append("(まだありません)")
    for s in stats:
        n_ep, longest, _ = eps.get((s.short_venue, s.long_venue, s.symbol, s.size_usd), (0, 0.0, 0.0))
        L.append(_row(list(zip(
            [s.label, s.symbol, _pct(s.net(hold)), _pct(s.avg_gross), _pct(s.min_gross), _pct(s.max_gross),
             f"{s.avg_cost_bps:.2f}", _days(s.breakeven), str(n_ep), _hours(longest), str(s.n)], w1))))

    # ---- 1b. 会場間の価格差 (net_apr に含めていないので別建てで出す)
    L.append(f"\n== 1b. 入った瞬間の会場間の価格差 (片脚 ${ref:,.0f}) — net_apr には含めていません")
    L.append("+ は「安い会場をショート / 高い会場をロング」= 建てた瞬間の含み損。解消時の価格差が同じなら相殺される")
    wb = [32, 5, -12, -11, -11, -17]
    L.append(_row(list(zip(["ショート / ロング", "銘柄", "価格差 avg", "min", "max", "往復bps との比"], wb))))
    for s in stats[:10]:
        ratio = f"{s.avg_basis_bps / s.avg_cost_bps:.2f}x" if s.avg_cost_bps else "n/a"
        L.append(_row(list(zip(
            [s.label, s.symbol, f"{s.avg_basis_bps:+.2f}", f"{s.min_basis_bps:+.2f}",
             f"{s.max_basis_bps:+.2f}", ratio], wb))))

    # ---- 2. 保有期間別
    L.append(f"\n== 2. 保有期間別の net_apr (片脚 ${ref:,.0f}, 上位 8 ペア)")
    w2 = [32, 5] + [-10] * len(cfg.hold_days)
    L.append(_row(list(zip(["ショート / ロング", "銘柄"] + [f"net{h:g}d" for h in cfg.hold_days], w2))))
    for s in stats[:8]:
        L.append(_row(list(zip([s.label, s.symbol] + [_pct(s.net(h)) for h in cfg.hold_days], w2))))

    # ---- 3. 銘柄別・サイズ別
    L.append("\n== 3. 銘柄別・サイズ別の内訳 (各サイズで net_apr が最良のペア)")
    w3 = [5, -11, 34, -9, -9, -9, -10, -7]
    L.append(_row(list(zip(
        ["銘柄", "片脚USD", "  最良ペア", f"net{hold:g}d", "粗avg", "往復bps", "breakeven", "評価可"], w3))))
    all_stats = pair_stats(store, since, until)
    for sym in cfg.symbols:
        for size in cfg.sizes_usd:
            cands = [s for s in all_stats if s.symbol == sym and s.size_usd == size]
            if not cands:
                miss = store.query(
                    "SELECT SUM(n) n FROM depth_misses WHERE bucket >= ? AND symbol = ? AND size_usd = ?",
                    (since, sym, size),
                )[0]["n"]
                L.append(_row(list(zip([sym, f"{size:,.0f}", f"  評価できた組み合わせなし (板不足 {miss or 0} 回)"], w3))))
                continue
            b = max(cands, key=lambda s: s.net(hold))
            L.append(_row(list(zip(
                [sym, f"{size:,.0f}", f"  {b.label}", _pct(b.net(hold)), _pct(b.avg_gross),
                 f"{b.avg_cost_bps:.2f}", _days(b.breakeven), str(b.n)], w3))))

    # ---- 4. 稼働性
    L.append("\n== 4. 会場ごとの稼働性 (レイテンシは 1 リクエストあたり = ポーリング合計 / calls)")
    w4 = [14, -7, -8, -8, -6, -9, -9, -11]
    L.append(_row(list(zip(["会場", "poll", "エラー", "率", "429", "p50", "p95", "calls/poll"], w4))))
    for p in store.query(
        """SELECT venue, COUNT(*) n, SUM(ok = 0) errors, SUM(rate_limited) limited, SUM(calls) calls
           FROM polls WHERE time >= ? AND time <= ? GROUP BY venue ORDER BY venue""",
        (since, until),
    ):
        # latency_ms は 1 ポーリングで投げた calls 本の合計なので、1 本あたりに割り戻す
        lat = [
            r[0]
            for r in store.query(
                """SELECT latency_ms / NULLIF(calls, 0) FROM polls
                   WHERE venue=? AND time>=? AND time<=? AND latency_ms IS NOT NULL AND calls > 0
                   ORDER BY latency_ms / NULLIF(calls, 0)""",
                (p["venue"], since, until),
            )
        ]
        p50 = statistics.median(lat) if lat else 0.0
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0.0
        L.append(_row(list(zip(
            [p["venue"], str(p["n"]), str(p["errors"]), f"{p['errors'] / p['n'] * 100:.2f}%",
             str(p["limited"]), f"{p50:.0f}ms", f"{p95:.0f}ms", f"{p['calls'] / p['n']:.1f}"], w4))))
    err = store.query(
        """SELECT venue, error, COUNT(*) n FROM polls WHERE time >= ? AND error IS NOT NULL
           GROUP BY venue, error ORDER BY n DESC LIMIT 5""",
        (since,),
    )
    for e in err:
        L.append(f"  エラー例 {e['venue']}: {e['n']} 回  {e['error'][:110]}")

    # ---- 5. ファンディングの水準と符号反転
    L.append("\n== 5. ファンディング年率の水準と符号反転")
    w5 = [14, 5, -9, -9, -9, -10, -9, -8]
    L.append(_row(list(zip(["会場", "銘柄", "avg", "min", "max", "負の割合", "符号反転", "区間数"], w5))))
    tradable = {v.name for v in cfg.tradable}
    flips = funding_flips(store, since, until)
    for f in flips:
        tag = "" if f.venue in tradable else "  (参照)"
        L.append(_row(list(zip(
            [f.venue, f.symbol, _pct(f.avg_apr), _pct(f.min_apr), _pct(f.max_apr),
             f"{f.neg_ratio * 100:.1f}%", str(f.flips), str(f.buckets)], w5))) + tag)
    L.append(f"\n会場ペアの粗スプレッドの符号反転 (片脚 ${ref:,.0f}, 反転 = 受け取り側と支払い側が入れ替わった)")
    pf = pair_sign_flips(store, since, until, ref)
    for (sh, lo, sym), n in sorted(pf.items(), key=lambda kv: -kv[1])[:10]:
        L.append(f"  S:{sh} / L:{lo} {sym}: {n} 回")
    if not pf:
        L.append("  (まだありません)")

    # ---- 6. ウォークフォワード
    L.append(f"\n== 6. ウォークフォワード ({cfg.walkforward_windows} 窓, 片脚 ${ref:,.0f}, net_apr({hold:g}d))")
    L.append("前の窓で最良だったペアを次の窓で持った場合 (OOS) と、後から見た最良ペア (hindsight) の比較")
    w6 = [4, 30, 34, -9, -9, -11, -9]
    L.append(_row(list(zip(["窓", "期間", "選んだペア (前窓最良)", "IS", "OOS", "後見最良", "効率"], w6))))
    wf = walkforward(store, since, until, cfg.walkforward_windows, ref, hold)
    if len(wf) < 2:
        L.append("(窓を作るにはデータが足りません)")
    for w in wf:
        picked = f"S:{w.picked[0]} / L:{w.picked[1]} {w.picked[2]}" if w.picked else "(なし)"
        eff = "n/a" if w.efficiency is None else f"{w.efficiency * 100:.1f}%"
        L.append(_row(list(zip(
            [str(w.index), f"{_ts(w.start)} - {_ts(w.end)}", picked,
             _pct(w.picked_is_net), _pct(w.oos_net), _pct(w.best_net), eff], w6))))
    oos = [w.oos_net for w in wf if w.oos_net is not None]
    effs = [w.efficiency for w in wf if w.efficiency is not None]
    if oos:
        L.append(
            f"OOS の中央値 {statistics.median(oos) * 100:+.2f}%  黒字だった窓 {sum(1 for x in oos if x > 0)}/{len(oos)}"
            + (f"  効率の中央値 {statistics.median(effs) * 100:.1f}%" if effs else "")
        )

    # ---- 7. 板不足
    L.append("\n== 7. 板が足りずに評価できなかった (会場, 銘柄, サイズ)")
    rows = store.query(
        """SELECT venue, symbol, size_usd, SUM(n) n FROM depth_misses WHERE bucket >= ? AND bucket < ?
           GROUP BY venue, symbol, size_usd ORDER BY size_usd, venue, symbol""",
        (since, until),
    )
    if not rows:
        L.append("(なし — 全ての会場で全サイズを板で約定できた)")
    for r in rows:
        L.append(f"  {r['venue']:<14}{r['symbol']:<5}片脚 ${r['size_usd']:>10,.0f}  {r['n']} 回")
    return "\n".join(L)


def _ts(ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000))
