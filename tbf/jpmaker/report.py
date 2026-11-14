"""レポート。依頼票の 1〜5 とウォークフォワードの窓、最後に合否の線への当てはめ。

主指標は **窓ごとの行**。全期間 1 回通した値は参考として 1 行だけ出す。
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass

from tbf.jpmaker import declared
from tbf.jpmaker.measure import (
    DAY_MS,
    Measurement,
    cells_for_window,
    coverage,
    percentile,
    window_bounds,
)
from tbf.jpmaker.sim import best_price_runs, tape_side_sanity
from tbf.jpmaker.store import JpMakerStore

# これより長い穴は 1 件ずつ列挙する。短いものは日ごとに件数と合計時間で出す。
GAP_LIST_THRESHOLD_SEC = 60.0


def _ts(ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000))


def _pct(x: float | None) -> str:
    return "  n/a  " if x is None else f"{x * 100:7.2f}%"


def _bps(x: float | None) -> str:
    return "   n/a" if x is None else f"{x * 10_000:6.1f}"


# --- 1. 最前列に並べ続けられるか ---
def section_residency(m: Measurement) -> str:
    out = ["== 1. 最良気配の滞在時間と置き直し回数 (1 秒標本)"]
    out.append("銘柄        側   区間数  滞在 中央値   p90    最長   置き直し/時  打ち切り(穴/末尾)")
    for pair, d in m.data.items():
        hours = (m.until - m.since) / 3_600_000
        for side in ("buy", "sell"):
            runs = best_price_runs(d.samples, side)
            closed = [r.seconds for r in runs if r.closed_by == "change"]
            censored = sum(1 for r in runs if r.closed_by != "change")
            if not closed:
                out.append(f"{pair:<11} {side:<4} {'(値段が動いた区間なし)':>20}  打ち切り {censored}")
                continue
            med = statistics.median(closed)
            out.append(
                f"{pair:<11} {side:<4} {len(closed):>6}  {med:>9.1f}s {percentile(closed, 0.9):>6.1f}s "
                f"{max(closed):>6.1f}s {len(closed) / hours if hours else 0:>10.1f}  {censored:>8}"
            )
    return "\n".join(out)


# --- 2. 約定率 (保守 / 楽観の 2 推定) ---
def section_fills(m: Measurement) -> str:
    days = (m.until - m.since) / DAY_MS
    out = ["== 2. 約定率 (自分の値段に届いたテイカー注文のうち取れた割合)", "   保守側 = 置いた瞬間の最良数量が全部前に居た / 楽観側 = 自分が先頭"]
    out.append("銘柄        サイズ   推定          件数   約定額/日      届いた額/日   取得率   取り逃し/日")
    for pair in m.data:
        for size in declared.SIZES_JPY:
            for mode in declared.FILL_MODES:
                n = filled = taker = unarmed = 0.0
                for side in ("buy", "sell"):
                    r = m.sims[(pair, side, size, mode)]
                    n += len(r.fills)
                    filled += r.filled_notional_jpy
                    taker += r.taker_notional_jpy
                    unarmed += r.unarmed_notional_jpy
                ratio = filled / taker if taker > 0 else None
                out.append(
                    f"{pair:<11} {size / 10_000:>5.0f}万 {mode:<13} {int(n):>5} "
                    f"{filled / days if days else 0:>12,.0f} {taker / days if days else 0:>14,.0f}  "
                    f"{_pct(ratio)} {unarmed / days if days else 0:>12,.0f}"
                )
    return "\n".join(out)


# --- 3. 逆選択 ---
def section_adverse(m: Measurement) -> str:
    out = [
        "== 3. 逆選択 (約定の直後に中値がどちらへ動いたか。正 = 取った半スプレッドを値動きで失う)",
        f"   純益の式に入れるのは {declared.ADVERSE_HORIZON_FOR_NET_SEC} 秒の列だけ (事前宣言)。他は感度。",
        f"   対象は **{declared.DECISION_MODE} 側 (判定に使う推定)** の約定。単位は約定金額に対する bps (金額重み付き平均)。",
    ]
    head = "銘柄        サイズ  半スプレッド " + " ".join(f"{h:>5}s" for h in declared.ADVERSE_HORIZONS_SEC) + "   未測定件数"
    out.append(head)
    for pair in m.data:
        for size in declared.SIZES_JPY:
            econs = [e for side in ("buy", "sell") for e in m.econs[(pair, side, size, declared.DECISION_MODE)]]
            if not econs:
                out.append(f"{pair:<11} {size / 10_000:>5.0f}万  ({declared.DECISION_MODE} 側の約定なし)")
                continue
            notional = sum(e.fill.notional_jpy for e in econs)
            hs = sum(e.half_spread * e.fill.notional_jpy for e in econs) / notional if notional else 0.0
            cols, miss = [], 0
            for h in declared.ADVERSE_HORIZONS_SEC:
                vals = [(e.adverse[h], e.fill.notional_jpy) for e in econs if e.adverse[h] is not None]
                miss += len(econs) - len(vals)
                if not vals:
                    cols.append("   n/a")
                    continue
                den = sum(w for _, w in vals)
                cols.append(f"{sum(v for v, _ in vals) / den * 10_000:6.1f}" if den else "   n/a")
            out.append(f"{pair:<11} {size / 10_000:>5.0f}万  {_bps(hs)}      " + " ".join(cols) + f"   {miss:>6}")
    return "\n".join(out)


# --- 4. 在庫ヘッジの実コスト ---
def section_hedge(m: Measurement) -> str:
    out = [
        "== 4. 在庫ヘッジ (Hyperliquid perp を成行。bps は約定金額に対する比)",
        f"   ヘッジまでの時間差は **下限** {m.delay_ms:.0f}ms "
        f"(約定検知 {declared.FILL_DETECTION_FLOOR_MS:.0f}ms + l2Book 往復 p50 の実測)。実際はこれより遅い。",
        f"   対象は {declared.DECISION_MODE} 側の約定。",
        "銘柄        サイズ  スリッページ p50   p95   テイカー  時間差中の値動き p50  板不足  使った時間差 p50",
    ]
    for pair in m.data:
        for size in declared.SIZES_JPY:
            econs = [e for side in ("buy", "sell") for e in m.econs[(pair, side, size, declared.DECISION_MODE)]]
            got = [e.hedge for e in econs if e.hedge]
            short = sum(1 for e in econs if e.hedge is None)
            if not got:
                out.append(f"{pair:<11} {size / 10_000:>5.0f}万  (測れた約定なし)  板不足/未測定 {short}")
                continue
            slips = [h["slippage"] for h in got]
            drifts = [abs(h["drift"]) for h in got]
            delays = [h["used_delay_ms"] for h in got]
            out.append(
                f"{pair:<11} {size / 10_000:>5.0f}万  {_bps(statistics.median(slips))} {_bps(percentile(slips, 0.95))} "
                f"  {_bps(declared.HL_TAKER_FEE * declared.HL_TAKER_LEGS)}  {_bps(statistics.median(drifts))}        "
                f"{short:>5}  {statistics.median(delays):>12.0f}ms"
            )
    return "\n".join(out)


# --- 5. 稼働性 ---
def section_uptime(store: JpMakerStore, m: Measurement) -> str:
    out = ["== 5. 稼働性"]
    out.append("会場         エンドポイント   取得数  エラー  429  レイテンシ p50   p95")
    for r in store.query(
        """SELECT venue, endpoint, COUNT(*) n, SUM(error IS NOT NULL) errors,
                  SUM(status = 429) rl FROM polls WHERE time>=? AND time<? GROUP BY venue, endpoint""",
        (m.since, m.until),
    ):
        lat = [
            x[0]
            for x in store.query(
                "SELECT latency_ms FROM polls WHERE venue=? AND endpoint=? AND time>=? AND time<? "
                "AND latency_ms IS NOT NULL ORDER BY latency_ms",
                (r["venue"], r["endpoint"], m.since, m.until),
            )
        ]
        p50 = statistics.median(lat) if lat else 0
        out.append(
            f"{r['venue']:<12} {r['endpoint']:<15} {r['n']:>6} {r['errors'] or 0:>7} {r['rl'] or 0:>4}  "
            f"{p50:>10.0f}ms {percentile(lat, 0.95):>7.0f}ms"
        )

    out.append("\n-- 記録の穴 (ゼロ埋めしない。年率の分母からも外す)")
    for pair, d in m.data.items():
        times = d.times
        cov, n = coverage(times, m.since, m.until, m.poll_sec)
        gaps = []
        for a, b in zip(times, times[1:]):
            if (b - a) / 1000.0 > m.poll_sec * 3:
                gaps.append((a, b, (b - a) / 1000.0))
        long_gaps = [g for g in gaps if g[2] >= GAP_LIST_THRESHOLD_SEC]
        short = [g for g in gaps if g[2] < GAP_LIST_THRESHOLD_SEC]
        out.append(
            f"{pair:<11} 標本 {n:>7}  カバレッジ {cov * 100:5.1f}%  穴 {len(gaps)} 件 "
            f"(合計 {sum(g[2] for g in gaps) / 60:.1f} 分)"
        )
        for a, b, s in sorted(long_gaps, key=lambda g: -g[2]):
            out.append(f"            未測定: {_ts(a)} -> {_ts(b)} ({s / 60:.1f} 分)")
        if short:
            out.append(
                f"            60 秒未満の穴 {len(short)} 件 (合計 {sum(g[2] for g in short) / 60:.1f} 分, "
                f"最長 {max(g[2] for g in short):.0f} 秒) — 個別には列挙しない"
            )

    ovf = store.query(
        "SELECT pair, COUNT(*) n FROM tape_overflow WHERE time>=? AND time<? GROUP BY pair", (m.since, m.until)
    )
    if ovf:
        out.append("\n-- テープの取りこぼしの疑い (transactions が最新 60 件しか返さないため)")
        for r in ovf:
            out.append(f"{r['pair']:<11} {r['n']} 回")
    else:
        out.append("\n-- テープの取りこぼしの疑い: なし")

    out.append("\n-- テープの side の検算 (side=sell は中値より下に寄っているはず)")
    for pair, d in m.data.items():
        s = tape_side_sanity(d.samples, d.times, d.trades)
        ok = "OK" if s["sell_rel_bps"] <= 0 <= s["buy_rel_bps"] else "**宣言と逆**"
        out.append(
            f"{pair:<11} sell {s['sell_rel_bps']:+7.1f}bps (n={int(s['n_sell'])})  "
            f"buy {s['buy_rel_bps']:+7.1f}bps (n={int(s['n_buy'])})  {ok}"
        )
    return "\n".join(out)


# --- ウォークフォワード ---
@dataclass
class WindowRow:
    start: int
    end: int
    window_days: float
    coverage: float
    declared_apy: float | None  # 宣言グリッド (5 銘柄・保守側) = OOS の値
    best_pair: str
    best_apy: float | None  # その窓で後から見た最良の 1 銘柄
    prev_best_pair: str | None
    prev_best_apy: float | None  # 前の窓で選んだ 1 銘柄をこの窓に当てた値 (真の OOS)
    n_fills: int
    n_measured: int
    net_jpy: float

    @property
    def span_ratio(self) -> float:
        """窓が宣言した窓幅のどれだけを占めているか (端数窓は 1 未満)"""
        return (self.end - self.start) / (self.window_days * DAY_MS)

    @property
    def usable(self) -> bool:
        """判定に使える窓か。カバレッジと窓幅の両方が下限以上であること"""
        return (
            self.declared_apy is not None
            and self.coverage >= declared.MIN_WINDOW_COVERAGE
            and self.span_ratio >= declared.MIN_WINDOW_SPAN_RATIO
        )

    @property
    def efficiency(self) -> float | None:
        """宣言設定 / 後から見た最良設定"""
        if self.declared_apy is None or not self.best_apy:
            return None
        return self.declared_apy / self.best_apy


def walkforward(m: Measurement, size: float, days: float, mode: str | None = None) -> list[WindowRow]:
    mode = mode or declared.DECISION_MODE
    rows: list[WindowRow] = []
    prev_best: str | None = None
    for ws, we in window_bounds(m.since, m.until, days):
        per_pair, total = cells_for_window(m, ws, we, size, mode)
        cov = total.measured_sec / ((we - ws) / 1000.0) if we > ws else 0.0
        scored = [(p, c.apy) for p, c in per_pair.items() if c.apy is not None]
        best_pair, best_apy = max(scored, key=lambda x: x[1]) if scored else ("-", None)
        rows.append(
            WindowRow(
                start=ws,
                end=we,
                window_days=days,
                coverage=cov,
                declared_apy=total.apy,
                best_pair=best_pair,
                best_apy=best_apy,
                prev_best_pair=prev_best,
                prev_best_apy=per_pair[prev_best].apy if prev_best in per_pair else None,
                n_fills=total.n_fills,
                n_measured=total.n_measured,
                net_jpy=total.net_jpy,
            )
        )
        if best_apy is not None:
            prev_best = best_pair
    return rows


def section_walkforward(m: Measurement) -> str:
    out = [
        "== 6. ウォークフォワード (主指標)",
        f"   宣言グリッド = {len(m.data)} 銘柄すべてに両側を出す・保守側の約定率推定。",
        f"   床 {declared.FLOOR_APY * 100:.2f}% / 合格 {declared.PASS_APY * 100:.2f}% (床の {declared.PASS_MULTIPLE:.0f} 倍)。",
        f"   年率の分母: 投下資本 (在庫上限 + ヘッジ証拠金) と **実測秒数** (穴はゼロ埋めしない)。",
    ]
    for size in declared.SIZES_JPY:
        for days in declared.WALK_WINDOW_DAYS:
            cap = declared.capital_jpy(size, len(m.data))
            out.append(f"\n-- 1 注文 {size / 10_000:.0f}万円 / {days:.0f} 日窓 (投下資本 ¥{cap:,.0f})")
            out.append(
                "窓                     カバ   約定  測定  純益(円)      宣言(OOS)年率  窓内最良      最良年率  効率   前窓選抜のOOS"
            )
            rows = walkforward(m, size, days)
            for r in rows:
                skip = ""
                if not r.usable:
                    why = "カバレッジ不足" if r.coverage < declared.MIN_WINDOW_COVERAGE else "窓が短い"
                    skip = f"  <- {why} ({r.span_ratio * 100:.0f}% の窓幅) のため判定から除外"
                eff = r.efficiency
                out.append(
                    f"{_ts(r.start)}->{_ts(r.end)}  {r.coverage * 100:4.0f}% {r.n_fills:>6} {r.n_measured:>5} "
                    f"{r.net_jpy:>11,.0f}  {_pct(r.declared_apy)}  {r.best_pair:<11} {_pct(r.best_apy)} "
                    f"{('  n/a' if eff is None else f'{eff:5.2f}')}  "
                    f"{(r.prev_best_pair or '-'):<11} {_pct(r.prev_best_apy)}{skip}"
                )
            usable = [r for r in rows if r.usable]
            if usable:
                apys = [r.declared_apy for r in usable]
                below = sum(1 for a in apys if a < declared.FLOOR_APY)
                out.append(
                    f"  判定に使う窓 {len(usable)} / {len(rows)}  中央値 {_pct(statistics.median(apys))}  "
                    f"最小 {_pct(min(apys))}  最大 {_pct(max(apys))}  "
                    f"床割れ {below}/{len(usable)} ({below / len(usable) * 100:.0f}%)"
                )
    return "\n".join(out)


# --- 合否 ---
def section_verdict(m: Measurement) -> str:
    out = [
        "== 7. 合否の線への当てはめ (着手前に凍結。結果を見て動かしていない)",
        f"   1. 純収益の式: 約定金額 x (半スプレッド + {declared.BITBANK_MAKER_REBATE * 100:.2f}%) "
        f"- 逆選択損({declared.ADVERSE_HORIZON_FOR_NET_SEC}s) - (スリッページ + {declared.HL_TAKER_FEE * 100:.3f}% x {declared.HL_TAKER_LEGS})",
        f"   2. 合格: 保守側で年率 >= {declared.PASS_APY * 100:.2f}% (床 {declared.FLOOR_APY * 100:.2f}% の {declared.PASS_MULTIPLE:.0f} 倍)",
        f"   3. 安定性: 床を割る窓が {declared.MAX_BELOW_FLOOR_RATIO * 100:.0f}% を超えたら不合格",
        "",
    ]
    verdicts = []
    for size in declared.SIZES_JPY:
        for days in declared.WALK_WINDOW_DAYS:
            rows = [r for r in walkforward(m, size, days) if r.usable]
            if not rows:
                out.append(f"{size / 10_000:.0f}万 / {days:.0f}日窓: 判定可能な窓が無い (未測定)")
                verdicts.append(None)
                continue
            apys = [r.declared_apy for r in rows]
            med = statistics.median(apys)
            below = sum(1 for a in apys if a < declared.FLOOR_APY)
            ratio = below / len(rows)
            pass_level = med >= declared.PASS_APY
            pass_stab = ratio <= declared.MAX_BELOW_FLOOR_RATIO
            ok = pass_level and pass_stab
            verdicts.append(ok)
            out.append(
                f"{size / 10_000:.0f}万 / {days:.0f}日窓: 窓中央値 {med * 100:+.2f}%  床割れ {below}/{len(rows)} "
                f"({ratio * 100:.0f}%)  -> 水準 {'満たす' if pass_level else '満たさない'} / "
                f"安定性 {'満たす' if pass_stab else '満たさない'} -> **{'合格' if ok else '不合格'}**"
            )
    decided = [v for v in verdicts if v is not None]
    if not decided:
        out.append("\n**判定不能** (測れた窓が無い)")
    elif all(decided):
        out.append("\n**合格** (宣言した 4 通りの切り方すべてで線を満たした)")
    else:
        out.append(
            f"\n**不合格** ({len(decided) - sum(1 for v in decided if v)} / {len(decided)} の切り方で線を満たさなかった)。"
            "低く出たことを理由に線は動かしていない。"
        )
    return "\n".join(out)


def build_report(store: JpMakerStore, m: Measurement) -> str:
    span_days = (m.until - m.since) / DAY_MS
    head = [
        "# TRA-65 国内の薄い板への両側指値 (マーケットメイク) シャドーモード計測",
        f"記録期間: {_ts(m.since)} -> {_ts(m.until)} UTC ({span_days:.2f} 日)  "
        f"宣言の凍結日 {declared.FROZEN_AT}  目標 {declared.RUN_DAYS} 日",
        f"対象: {', '.join(m.data)}  サイズ: {', '.join(f'{s / 10_000:.0f}万円' for s in declared.SIZES_JPY)}",
        "発注はしていない (読み取り専用の公開 API のみ。鍵・口座なし)。",
        "",
    ]
    return "\n\n".join(
        [
            "\n".join(head),
            section_walkforward(m),
            section_verdict(m),
            section_residency(m),
            section_fills(m),
            section_adverse(m),
            section_hedge(m),
            section_uptime(store, m),
        ]
    )
