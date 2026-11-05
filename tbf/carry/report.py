"""計測結果の集計と出力

出すのは依頼票の 4 項目 (i)〜(iv) と、日次 net の系列・シャープ・最大DD・合否基準への当てはめ。

規約:
  - 分母は常に C (= capital_usd)。片脚名目 N で割った利回りは出さない
  - 測れなかったものはゼロ埋めせず「未測定」と理由を書く
  - 全期間を 1 回通した値だけで終わらせず、週ごとの窓を 1 行ずつ出し、
    後から見た最良の窓に対する比 (効率) を併記する
  - 「有望」「良さそう」のような評価語は書かない。基準に当てはめた結果だけを書く
"""

from __future__ import annotations

import csv
import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from tbf.carry.config import CarryConfig
from tbf.carry.position import DAYS_PER_YEAR, annualized, max_drawdown, stats
from tbf.carry.store import CarryStore

DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def _utc(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc)


def _ts(ms: Optional[int]) -> str:
    return "—" if ms is None else _utc(ms).strftime("%Y-%m-%d %H:%M:%S UTC")


def _pct(x: Optional[float], digits: int = 2) -> str:
    return "未測定" if x is None else f"{x * 100:+.{digits}f}%"


def _median(xs: Sequence[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


# ---------------------------------------------------------------- 日次


@dataclass(frozen=True)
class Daily:
    date: str
    samples: int
    spot_mark: float
    perp_mark: float
    basis_bps: float
    funding_cum_usd: float
    funding_usd: float
    mtm_usd: float
    net_pnl_usd: float
    ret: float
    margin_ratio_min: float
    liquidated: bool
    span_days: float


def daily_rows(store: CarryStore, cfg: CarryConfig) -> list[Daily]:
    """UTC 日ごとの終値サンプルから日次 net の系列を作る

    日次リターンの分母は常に C。日 0 の値には建玉コストが一括で入っている。

    `span_days` は「その行が実際に覆っている時間」(前の行の終値から今の行の終値まで。
    最初の行は建玉時刻から)。UTC 日で切るので初日と最終日は部分日になり、
    行数をそのまま日数として年率化すると経過日数を過大に数える。年率化はこの値を使う。
    """
    rows = store.query(
        """SELECT date(time/1000, 'unixepoch') AS d, COUNT(*) AS n,
                  MIN(margin_ratio) AS mr_min, MAX(liquidated) AS liq, MAX(time) AS t
           FROM samples GROUP BY d ORDER BY d"""
    )
    closes = {
        int(r["time"]): r
        for r in store.query(
            """SELECT * FROM samples WHERE time IN
                 (SELECT MAX(time) FROM samples GROUP BY date(time/1000, 'unixepoch'))"""
        )
    }
    entry = store.load_entry()
    out: list[Daily] = []
    prev_net = 0.0
    prev_funding = 0.0
    prev_t = entry.time if entry is not None else None
    for r in rows:
        s = closes[int(r["t"])]
        net = float(s["net_pnl_usd"])
        fund = float(s["funding_cum_usd"])
        t = int(r["t"])
        span = 1.0 if prev_t is None else (t - prev_t) / DAY_MS
        prev_t = t
        out.append(
            Daily(
                date=r["d"],
                samples=int(r["n"]),
                spot_mark=float(s["spot_mark"]),
                perp_mark=float(s["perp_mark"]),
                basis_bps=float(s["basis_bps"]),
                funding_cum_usd=fund,
                funding_usd=fund - prev_funding,
                mtm_usd=float(s["mtm_usd"]),
                net_pnl_usd=net,
                ret=(net - prev_net) / cfg.capital_usd,
                margin_ratio_min=float(r["mr_min"]),
                liquidated=bool(r["liq"]),
                span_days=span,
            )
        )
        prev_net, prev_funding = net, fund
    return out


def write_daily_csv(rows: Sequence[Daily], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([
            "date", "samples", "spot_mark", "perp_mark", "basis_bps",
            "funding_usd_day", "funding_cum_usd", "mtm_usd", "net_pnl_usd",
            "daily_return_on_C", "margin_ratio_min", "liquidated", "span_days",
        ])
        for d in rows:
            w.writerow([
                d.date, d.samples, f"{d.spot_mark:.6f}", f"{d.perp_mark:.6f}", f"{d.basis_bps:.4f}",
                f"{d.funding_usd:.6f}", f"{d.funding_cum_usd:.6f}", f"{d.mtm_usd:.6f}",
                f"{d.net_pnl_usd:.6f}", f"{d.ret:.10f}", f"{d.margin_ratio_min:.6f}", int(d.liquidated),
                f"{d.span_days:.6f}",
            ])
    return p


# ---------------------------------------------------------------- (i) 往復コスト


def cost_series(store: CarryStore, size_usd: float) -> dict[int, dict[str, float]]:
    """時刻 -> {"entry": 入る側の両脚コスト bps, "exit": 出る側の両脚コスト bps}

    歩き切れなかった時刻は欠けたまま返す (ゼロ埋めしない)。
    """
    rows = store.query(
        "SELECT time, leg, side, slip_bps, fee_bps FROM fills WHERE size_usd = ? AND slip_bps IS NOT NULL",
        (size_usd,),
    )
    by_time: dict[int, dict[tuple[str, str], float]] = {}
    for r in rows:
        by_time.setdefault(int(r["time"]), {})[(r["leg"], r["side"])] = float(r["slip_bps"]) + float(r["fee_bps"])
    out: dict[int, dict[str, float]] = {}
    for t, legs in by_time.items():
        entry = legs.get(("spot", "buy")), legs.get(("perp", "sell"))
        exit_ = legs.get(("spot", "sell")), legs.get(("perp", "buy"))
        d: dict[str, float] = {}
        if all(v is not None for v in entry):
            d["entry"] = entry[0] + entry[1]  # type: ignore[operator]
        if all(v is not None for v in exit_):
            d["exit"] = exit_[0] + exit_[1]  # type: ignore[operator]
        if d:
            out[t] = d
    return out


def cost_table(store: CarryStore, cfg: CarryConfig) -> list[str]:
    lines = [
        "| 片脚名目 | 入り(両脚) 中位 | 出口(両脚) 中位 | 往復 中位 | 往復 最小 | 往復 最大 | 歩けた回数 | 歩けなかった回数 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for size in cfg.depth_sizes_usd:
        series = cost_series(store, size)
        entries = [d["entry"] for d in series.values() if "entry" in d]
        exits = [d["exit"] for d in series.values() if "exit" in d]
        rt = [d["entry"] + d["exit"] for d in series.values() if "entry" in d and "exit" in d]
        miss = store.query(
            "SELECT COUNT(*) AS n FROM fills WHERE size_usd = ? AND slip_bps IS NULL", (size,)
        )[0]["n"]
        def f(x: Optional[float]) -> str:
            return "未測定" if x is None else f"{x:.2f}bps ({x / 100:.3f}%)"
        lines.append(
            f"| ${size:,.0f} | {f(_median(entries))} | {f(_median(exits))} | {f(_median(rt))} | "
            f"{f(min(rt) if rt else None)} | {f(max(rt) if rt else None)} | {len(rt)} | {miss} |"
        )
    return lines


# ---------------------------------------------------------------- (iv) 時間帯別の深さ


def depth_by_hour(store: CarryStore, cfg: CarryConfig, size_usd: float) -> list[str]:
    limit_bps = cfg.depth_fill_limit_pct * 100
    lines = [
        f"| UTC時 | 回数 | 買い板(bid)20段 中位 | 売り板(ask)20段 中位 | ${size_usd:,.0f} 買いスリッページ 中位/最大 | "
        f"同 売り 中位/最大 | ${size_usd:,.0f} が歩けず | {cfg.depth_fill_limit_pct:g}% 超 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h in range(24):
        rows = store.query(
            """SELECT side, slip_bps, depth_usd FROM fills
               WHERE leg = 'spot' AND size_usd = ?
                 AND CAST(strftime('%H', time/1000, 'unixepoch') AS INTEGER) = ?""",
            (size_usd, h),
        )
        if not rows:
            continue
        buys = [r for r in rows if r["side"] == "buy"]
        sells = [r for r in rows if r["side"] == "sell"]
        buy_slip = [float(r["slip_bps"]) for r in buys if r["slip_bps"] is not None]
        sell_slip = [float(r["slip_bps"]) for r in sells if r["slip_bps"] is not None]
        ask_depth = [float(r["depth_usd"]) for r in buys]
        bid_depth = [float(r["depth_usd"]) for r in sells]
        unfilled = sum(1 for r in rows if r["slip_bps"] is None)
        over = sum(1 for r in rows if r["slip_bps"] is not None and float(r["slip_bps"]) > limit_bps)
        def d(xs: Sequence[float]) -> str:
            m = _median(xs)
            return "未測定" if m is None else f"${m:,.0f}"
        def s(xs: Sequence[float]) -> str:
            if not xs:
                return "未測定"
            return f"{_median(xs):.2f} / {max(xs):.2f}bps"
        lines.append(
            f"| {h:02d} | {len(buys)} | {d(bid_depth)} | {d(ask_depth)} | {s(buy_slip)} | {s(sell_slip)} | "
            f"{unfilled} | {over} |"
        )
    return lines


# ---------------------------------------------------------------- ベーシスの σ


def _sigma_phases(values: Sequence[float], step: int) -> list[float]:
    """`step` 本おきに間引いた系列の変化 σ を、間引きの位相すべてについて返す

    5 分サンプルから 1 時間刻みを作る位相は 12 通りある。標本が短いと位相で σ が倍近く動くので、
    1 つの位相だけを「実測値」として出さないためにここで全位相を出す。
    """
    out = []
    for off in range(step):
        s = values[off::step]
        ch = [b - a for a, b in zip(s, s[1:])]
        if len(ch) >= 2:
            out.append(statistics.stdev(ch))
    return out


def _autocorr1(changes: Sequence[float]) -> Optional[float]:
    if len(changes) < 3:
        return None
    m = sum(changes) / len(changes)
    den = sum((x - m) ** 2 for x in changes)
    if den == 0:
        return None
    num = sum((changes[i] - m) * (changes[i + 1] - m) for i in range(len(changes) - 1))
    return num / den


def _sigma_scaling_table(values: Sequence[float]) -> list[str]:
    """刻みごとの σ と、5 分 σ を √k で伸ばした値の比

    事前予想のシャープ 2.18 は 1 時間 σ を時間方向に伸ばして作られている。
    実測の σ が √t で伸びるかどうかはその前提の成否に直接効くので、比をそのまま出す。
    """
    steps = ((1, "5分"), (3, "15分"), (6, "30分"), (12, "1時間"), (24, "2時間"),
             (48, "4時間"), (72, "6時間"), (144, "12時間"), (288, "1日"))
    lines = [
        "",
        "ベーシス変化の σ — 刻み別（σ が √t で伸びるかどうかが事前予想シャープの前提）:",
        "",
        "| 刻み | 変化のサンプル n | σ(bps) 中位(全位相) | σ の SE | 5分σ×√k (ランダムウォーク前提) | 実測/RW |",
        "|---|---|---|---|---|---|",
    ]
    base: Optional[float] = None
    for k, label in steps:
        phases = _sigma_phases(values, k)
        n = len(values) // k - 1
        if not phases:
            lines.append(f"| {label} | {max(n, 0)} | 未測定(n<2) | — | — | — |")
            continue
        s = statistics.median(phases)
        if base is None:
            base = s
        rw = base * (k ** 0.5)
        se = s / (2 * max(n - 1, 1)) ** 0.5
        ratio = "—" if rw == 0 else f"{s / rw:.2f}"
        lines.append(f"| {label} | {n} | {s:.2f} | ±{se:.2f} | {rw:.2f} | {ratio} |")
    return lines


# ---------------------------------------------------------------- 窓別 (週)


def window_rows(dailies: Sequence[Daily], cfg: CarryConfig, window_days: int = 7) -> list[str]:
    """窓ごとの実現 net 年率とシャープを 1 行ずつ。後から見た最良窓に対する比 (効率) を併記する

    A① は事前宣言グリッドが 1 通りしかなく、設定を選び直す余地がないので「前の窓で最良の設定を
    次の窓で使う」形のウォークフォワードにはならない。窓別に出す意味は、全期間 1 本の数字が
    どの窓にどれだけ依存しているかを隠さないことにある。
    """
    if not dailies:
        return ["（日次サンプルがまだありません）"]
    chunks = [dailies[i : i + window_days] for i in range(0, len(dailies), window_days)]
    aprs: list[Optional[float]] = []
    rows = []
    for i, c in enumerate(chunks, 1):
        pnl = sum(d.ret for d in c) * cfg.capital_usd
        # 行数ではなく実際に覆っている時間で年率化する (UTC 日で切るため初日・最終日は部分日)
        span = sum(d.span_days for d in c)
        apr = annualized(pnl, cfg.capital_usd, span)
        st = stats([d.ret for d in c])
        aprs.append(apr)
        rows.append((i, c[0].date, c[-1].date, len(c), span, pnl, apr, st))
    best = max((a for a in aprs if a is not None), default=None)
    lines = [
        "| 窓 | 開始(UTC日) | 終了 | 行数 | 実日数 | net損益 | net年率(OOS) | シャープ | σ(日次) | 効率(最良窓比) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for (i, d0, d1, n, span, pnl, apr, st) in rows:
        eff = "—" if (best is None or apr is None or best == 0) else f"{apr / best:.2f}"
        sh = "未測定(n<2)" if st is None else f"{st.sharpe:+.2f}"
        sd = "未測定(n<2)" if st is None else f"{st.std * 100:.4f}%"
        lines.append(
            f"| {i} | {d0} | {d1} | {n} | {span:.2f} | ${pnl:+,.2f} | {_pct(apr)} | {sh} | {sd} | {eff} |"
        )
    return lines


# ---------------------------------------------------------------- 本体


def build_report(store: CarryStore, cfg: CarryConfig, now: int, days: Optional[float] = None) -> str:
    """days は「何日経ったら判定してよいか」の宣言 (既定は carry.toml の measure_days)"""
    measure_days = cfg.measure_days if days is None else days
    L: list[str] = []
    entry = store.load_entry()
    first_run = store.get_meta("first_run_ms")
    last_run = store.get_meta("last_run_ms")
    start_ms = int(first_run) if first_run else None

    L.append("# A① HL HYPE 現物ロング × perp ショート — シャドーモード計測 (売買なし・読み取りのみ)")
    L.append("")
    L.append(f"- DB: `{store.path}`")
    L.append(f"- 計測開始 (run 起動): {_ts(start_ms)}")
    L.append(f"- 直近の記録: {_ts(int(last_run) if last_run else None)}")
    L.append(f"- 集計時刻: {_ts(now)}")
    L.append(
        f"- 資本規約: C = ${cfg.capital_usd:,.0f} "
        f"(現物ロング N = ${cfg.spot_notional_usd:,.0f} + perp 証拠金 ${cfg.perp_margin_usd:,.0f}, "
        f"C/N = {cfg.capital_ratio:.4f})。**年率・シャープ・最大DD の分母は常に C**"
    )
    L.append(
        "- HL 現物残高が perp 証拠金に算入されるかは未確定 (TRA-14 §8)。"
        "保守側 = 算入されない前提で証拠金率を評価している"
    )

    if entry is None:
        L.append("")
        L.append("**建玉がまだ確定していません** (板を歩き切れた最初のポーリングで確定します)。")
        return "\n".join(L)

    elapsed_ms = now - entry.time
    elapsed_days = elapsed_ms / DAY_MS
    dailies = daily_rows(store, cfg)
    last = store.query("SELECT * FROM samples ORDER BY time DESC LIMIT 1")
    latest = last[0] if last else None

    # 稼働率
    polls = store.query("SELECT COUNT(*) AS n, SUM(ok) AS ok, SUM(rate_limited) AS rl FROM polls")[0]
    n_samples = store.query("SELECT COUNT(*) AS n FROM samples")[0]["n"]
    expected = max(1, int(elapsed_ms / (cfg.poll_interval_sec * 1000)))
    L.append(
        f"- カバー率: サンプル {n_samples} / 期待 {expected} = {n_samples / expected * 100:.1f}% "
        f"(ポーリング {polls['n']} 回中 成功 {polls['ok'] or 0}, レート制限 {polls['rl'] or 0})"
    )
    L.append(f"- 経過: {elapsed_days:.2f} 日 / 宣言 {measure_days:g} 日")
    L.append("")

    # ---- 建玉
    L.append("## 建玉 (シミュレーション。注文は出していない)")
    L.append("")
    L.append(f"- 建玉時刻: {_ts(entry.time)}")
    L.append(f"- サイズ: {entry.size} HYPE (両脚同枚数)")
    L.append(
        f"- 現物 `{cfg.spot_pair}` 買い: 平均約定 {entry.spot_px:,.4f} "
        f"(板 mid {entry.spot_mid:,.4f}, スリッページ {entry.spot_slip_bps:+.2f}bps, "
        f"手数料 {entry.spot_fee_bps:g}bps) 名目 ${entry.spot_notional_usd:,.2f}"
    )
    L.append(
        f"- perp `{cfg.perp_coin}` 売り: 平均約定 {entry.perp_px:,.4f} "
        f"(板 mid {entry.perp_mid:,.4f}, スリッページ {entry.perp_slip_bps:+.2f}bps, "
        f"手数料 {entry.perp_fee_bps:g}bps) 名目 ${entry.perp_notional_usd:,.2f}"
    )
    L.append(f"- 建玉時のベーシス: {(entry.perp_px - entry.spot_px) / entry.spot_px * 10_000:+.2f}bps")
    L.append(
        f"- 建玉コスト (片道・両脚): {entry.entry_cost_bps:.2f}bps = ${entry.entry_cost_usd:,.2f} "
        "— 日次 net では建玉時に一括計上している"
    )
    L.append("")

    # ---- (i)
    L.append("## (i) 実効往復コスト — `l2Book` を実際に歩いた値 (表示価格は使っていない)")
    L.append("")
    L.append(f"手数料の出典: {cfg.fee_source}")
    L.append("")
    L.extend(cost_table(store, cfg))
    L.append("")
    L.append(
        "「歩けなかった回数」は `l2Book` の上位 20 段でそのサイズに届かなかった回数。"
        "届かなかった時刻はゼロ埋めせず集計から除いている (= 未測定)。"
    )
    L.append("")

    # ---- (ii)
    L.append("## (ii) 実現ファンディング累計 — `fundingHistory`(HYPE) の 1 時間値を実際に積んだ値")
    L.append("")
    f = store.query(
        """SELECT COUNT(*) AS n, SUM(rate) AS srate, SUM(amount_usd) AS amt, SUM(nominal_amount_usd) AS nom,
                  SUM(CASE WHEN rate < 0 THEN 1 ELSE 0 END) AS neg,
                  MIN(rate) AS lo, MAX(rate) AS hi, MIN(time) AS t0, MAX(time) AS t1
           FROM funding"""
    )[0]
    if not f["n"]:
        L.append("**未測定**: 建玉後のファンディング確定時刻がまだ来ていません。")
    else:
        hours = int(f["n"])
        amt = float(f["amt"])
        nom = float(f["nom"])
        mean_rate = float(f["srate"]) / hours
        L.append(f"- 対象: {hours} 時間ぶん ({_ts(int(f['t0']))} 〜 {_ts(int(f['t1']))})")
        L.append(f"- 1 時間値の平均: {mean_rate:.8f} -> 年率 {mean_rate * 24 * 365 * 100:+.2f}%")
        L.append(f"- 負だった時間: {int(f['neg'])} / {hours} = {int(f['neg']) / hours * 100:.1f}%")
        L.append(
            f"- 1 時間値の範囲: {float(f['lo']):.8f} 〜 {float(f['hi']):.8f} "
            f"(年率 {float(f['lo']) * 24 * 365 * 100:+.2f}% 〜 {float(f['hi']) * 24 * 365 * 100:+.2f}%)"
        )
        L.append(f"- 受取額 (建玉の時価名目に対して): ${amt:,.4f}")
        L.append(f"- 受取額 (名目 ${cfg.spot_notional_usd:,.0f} 固定で計算した場合): ${nom:,.4f}")
        apr_f = annualized(amt, cfg.capital_usd, hours / 24)
        L.append(f"- **C = ${cfg.capital_usd:,.0f} に対する年率換算 (コスト控除前): {_pct(apr_f)}**")
    L.append("")

    # ---- (iii)
    L.append("## (iii) ベーシスの最大逸脱と証拠金")
    L.append("")
    b = store.query(
        """SELECT COUNT(*) AS n, AVG(basis_bps) AS avg, MIN(basis_bps) AS lo, MAX(basis_bps) AS hi,
                  MIN(margin_ratio) AS mr_lo, AVG(margin_ratio) AS mr_avg,
                  SUM(margin_warn) AS warn, SUM(liquidated) AS liq
           FROM samples"""
    )[0]
    basis_vals = [float(r["basis_bps"]) for r in store.query("SELECT basis_bps FROM samples ORDER BY time")]
    changes = [b2 - b1 for b1, b2 in zip(basis_vals, basis_vals[1:])]
    if b["n"]:
        L.append(f"- サンプル {int(b['n'])} 本 (5 分間隔)")
        L.append(
            f"- ベーシス (perp markPx − 現物 `{cfg.spot_pair}` markPx) / 現物: "
            f"平均 {float(b['avg']):+.2f}bps, 最小 {float(b['lo']):+.2f}bps, 最大 {float(b['hi']):+.2f}bps"
        )
        L.append(f"- 建玉時からの最大逸脱: {max(abs(v - basis_vals[0]) for v in basis_vals):.2f}bps")
        if len(changes) >= 2:
            sd = statistics.stdev(changes)
            L.append(
                f"- 5 分変化の σ: {sd:.2f}bps (n={len(changes)}) -> √12 倍で 1 時間換算 {sd * 12**0.5:.2f}bps"
            )
        else:
            L.append("- 5 分変化の σ: **未測定** (サンプルが 3 本未満)")
        # 事前予想 (4.14bps) は 1 時間変化の σ なので、1 時間間隔の実測で直接比べる。
        # ただし 5 分サンプルから 1 時間刻みを作る位相は 12 通りあり、標本が短いと位相で値が大きく動く。
        # 1 つの位相だけを出すと恣意的な数字になるので、12 位相すべてを出して中位と範囲で比べる。
        phases = _sigma_phases(basis_vals, 12)
        if phases:
            med = statistics.median(phases)
            L.append(
                f"- **1 時間変化の σ (直接計測, 5 分グリッドの 12 位相すべて): 中位 {med:.2f}bps "
                f"(範囲 {min(phases):.2f}〜{max(phases):.2f}bps, 位相 {len(phases)} 通り × 各 "
                f"n={len(basis_vals) // 12 - 1}) / 事前予想 {cfg.expected_basis_sigma_bps:.2f}bps**"
            )
            if min(phases) <= cfg.expected_basis_sigma_bps <= max(phases):
                L.append(
                    f"  - 事前予想 {cfg.expected_basis_sigma_bps:.2f}bps は位相による振れ幅の内側にある"
                    "（この標本数では一致・不一致を言えない）"
                )
        else:
            L.append(
                "- 1 時間変化の σ (直接計測): **未測定** (1 時間刻みを作るには 5 分サンプルが "
                f"26 本以上必要。現在 {len(basis_vals)} 本) / 事前予想 "
                f"{cfg.expected_basis_sigma_bps:.2f}bps"
            )
        L.extend(_sigma_scaling_table(basis_vals))
        ac = _autocorr1(changes)
        if ac is not None:
            L.append(
                f"- 5 分変化の lag-1 自己相関: {ac:+.3f} (n={len(changes)}, ±1SE≈"
                f"{1 / len(changes) ** 0.5:.3f})"
                + (
                    " — 負なので平均回帰しており、σ を √t で伸ばすと長い刻みの揺れを過大に見積もる"
                    if ac < -2 / len(changes) ** 0.5
                    else ""
                )
            )
        L.append(
            f"- 証拠金率 (perp 口座のみ): 現在 "
            f"{float(latest['margin_ratio']) * 100:.2f}%, 平均 {float(b['mr_avg']) * 100:.2f}%, "
            f"**最悪 {float(b['mr_lo']) * 100:.2f}%**"
        )
        L.append(
            f"- **証拠金率が {cfg.margin_warn_frac * 100:g}% を割ったサンプル: {int(b['warn'])} / {int(b['n'])}**"
        )
        L.append(
            f"- **維持証拠金 {cfg.maintenance_margin_frac * 100:g}% に到達 (= シミュレーション上の清算): "
            f"{int(b['liq'])} 回**"
        )
        # 清算までの価格余裕 (保守側モデルの含意を数字で出す)
        ratio = entry.perp_notional_usd / entry.margin_usd
        for frac, label in (
            (cfg.margin_warn_frac, f"証拠金率 {cfg.margin_warn_frac * 100:g}% 割れ"),
            (cfg.maintenance_margin_frac, f"維持証拠金 {cfg.maintenance_margin_frac * 100:g}% = 清算"),
        ):
            trig = (1 + ratio) / (ratio * (1 + frac))
            L.append(
                f"- 保守側モデルでの {label} 水準: HYPE が建玉時より約 {(trig - 1) * 100:+.1f}% "
                f"({entry.perp_px * trig:,.2f}) まで上がると到達 "
                "(現物側の含み益を証拠金に算入しない前提のため)"
            )
        L.append(
            f"- 証拠金の追加は行わない前提 (C = ${cfg.capital_usd:,.0f} は現物 N と証拠金 N/3 で使い切っており、"
            "追加に回せる現金が規約上存在しない)。したがって追加回数は 0 回で、"
            f"{cfg.margin_warn_frac * 100:g}% 割れの回数が「追加が必要になった回数」に相当する"
        )
    else:
        L.append("**未測定**: サンプルがありません。")
    L.append("")

    # ---- (iv)
    L.append("## (iv) 現物板上位 20 段の深さ — UTC 時刻別")
    L.append("")
    L.extend(depth_by_hour(store, cfg, max(cfg.depth_sizes_usd)))
    L.append("")
    L.append(
        "`l2Book` は上位 20 段しか返さないので、ここでの深さは「取得できた 20 段の範囲で」の値。"
        "20 段で届かなかった回数は「歩けず」列に出している。"
    )
    L.append("")

    # ---- 日次・シャープ
    L.append("## 日次 net と窓別の成績")
    L.append("")
    rets = [d.ret for d in dailies]
    st = stats(rets)
    net_total = dailies[-1].net_pnl_usd if dailies else 0.0
    apr = annualized(net_total, cfg.capital_usd, elapsed_days)
    dd = max_drawdown([0.0] + [d.net_pnl_usd for d in dailies])
    L.append(f"- 日次サンプル: {len(dailies)} 日")
    L.append(f"- 実現 net 損益: ${net_total:+,.2f} (ファンディング ${dailies[-1].funding_cum_usd:+,.2f}, "
             f"ベーシス評価損益 ${dailies[-1].mtm_usd:+,.2f}, 建玉コスト ${-entry.entry_cost_usd:,.2f})")
    L.append(f"- **実現 net 年率 (分母 C): {_pct(apr)}** / 事前予想 {_pct(cfg.expected_net_apr)}")
    if st is None:
        L.append("- **シャープ: 未測定** (日次が 2 日未満)")
    else:
        L.append(
            f"- **シャープ (rf=0, 日次 × √365): {st.sharpe:+.2f}** / 事前予想 {cfg.expected_sharpe:+.2f}"
        )
        L.append(
            f"- σ (日次, 分母 C): {st.std * 100:.4f}% ± {st.std_se * 100:.4f}% (σ の標準誤差, n={st.n}) "
            f"-> 年率 σ {st.std * (DAYS_PER_YEAR ** 0.5) * 100:.2f}%"
        )
        L.append(
            f"- シャープの標準誤差: ±{st.sharpe_se:.2f} "
            f"(±1SE の幅 {st.sharpe_low:+.2f} 〜 {st.sharpe_high:+.2f})"
        )
        for th, name in ((cfg.sharpe_pass, "合格境界"), (cfg.sharpe_reject, "却下境界")):
            inside = st.sharpe_low <= th <= st.sharpe_high
            L.append(
                f"  - {name} {th:.1f} は ±1SE の内側か: **{'はい' if inside else 'いいえ'}**"
                + ("（この標本数では判定が標本誤差の内側に入る）" if inside else "")
            )
        # 建玉日は (a) 部分日で (b) 往復コストの片道ぶんが一括で乗るので、σ をそこだけで膨らませる。
        # 判定に使うのは上の宣言どおりの値。これは内訳として併記するだけ。
        st2 = stats([d.ret for d in dailies[1:]])
        if st2 is not None:
            L.append(
                f"- 参考 (判定には使わない): 建玉日 ({dailies[0].date}, 部分日・建玉コスト一括計上) を除くと "
                f"シャープ {st2.sharpe:+.2f} ± {st2.sharpe_se:.2f}, σ {st2.std * 100:.4f}% (n={st2.n})"
            )
    L.append(f"- 最大DD (日次終値ベース): ${dd:,.2f} = C の {dd / cfg.capital_usd * 100:.2f}%")
    L.append("")
    L.extend(window_rows(dailies, cfg))
    L.append("")
    L.append("| UTC日 | サンプル | 覆う日数 | 現物mark | perp mark | ベーシスbps | 当日ファンディング | 累計ファンディング | "
             "ベーシス評価損益 | net損益 | 日次リターン(C) | 証拠金率 最悪 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for d in dailies:
        L.append(
            f"| {d.date} | {d.samples} | {d.span_days:.2f} | {d.spot_mark:,.4f} | {d.perp_mark:,.4f} | {d.basis_bps:+.2f} | "
            f"${d.funding_usd:+,.4f} | ${d.funding_cum_usd:+,.4f} | ${d.mtm_usd:+,.2f} | "
            f"${d.net_pnl_usd:+,.2f} | {d.ret * 100:+.4f}% | {d.margin_ratio_min * 100:.2f}% |"
        )
    L.append("")

    # ---- 合否基準の当てはめ
    L.append("## 合否基準への当てはめ (機械的に当てはめただけ。採否は決めない)")
    L.append("")
    liq = int(b["liq"] or 0) if b["n"] else 0
    L.append(f"- 基準: 合格 = net年率 ≥ {cfg.hurdle_net_apr * 100:.2f}% かつ シャープ ≥ {cfg.sharpe_pass:.1f}")
    L.append(
        f"- 基準: 却下 = net年率 < {cfg.hurdle_net_apr * 100:.2f}% / シャープ < {cfg.sharpe_reject:.1f} / 清算 ≥ 1 回"
    )
    L.append("")
    if elapsed_days < measure_days:
        L.append(
            f"**判定しない**: 宣言した計測期間 {measure_days:g} 日に対して {elapsed_days:.2f} 日しか"
            "経過していない。上の数値は中間値である。"
        )
    elif st is None:
        L.append("**判定しない**: シャープが未測定 (日次が 2 日未満)。")
    elif liq >= 1:
        L.append(f"**却下**: シミュレーション上の清算が {liq} 回発生 (却下条件に直接該当)。")
    elif apr is None:
        L.append("**判定しない**: net 年率が未測定。")
    elif apr < cfg.hurdle_net_apr:
        L.append(f"**却下**: net年率 {_pct(apr)} < 床 {cfg.hurdle_net_apr * 100:.2f}%。")
    elif st.sharpe < cfg.sharpe_reject:
        L.append(f"**却下**: シャープ {st.sharpe:+.2f} < {cfg.sharpe_reject:.1f}。")
    elif apr >= cfg.hurdle_net_apr and st.sharpe >= cfg.sharpe_pass:
        L.append(f"**合格**: net年率 {_pct(apr)} ≥ {cfg.hurdle_net_apr * 100:.2f}% かつ "
                 f"シャープ {st.sharpe:+.2f} ≥ {cfg.sharpe_pass:.1f}。")
    else:
        L.append(
            f"**中間帯 — 判定せず**: net年率 {_pct(apr)} は床 {cfg.hurdle_net_apr * 100:.2f}% を超えているが、"
            f"シャープ {st.sharpe:+.2f} が {cfg.sharpe_reject:.1f} 以上 {cfg.sharpe_pass:.1f} 未満。"
            f" 最大DD ${dd:,.2f} (C の {dd / cfg.capital_usd * 100:.2f}%)、"
            f"{cfg.margin_warn_frac * 100:g}% 割れ {int(b['warn'] or 0)} 回、証拠金追加 0 回 (規約上追加しない)。"
        )
    return "\n".join(L)
