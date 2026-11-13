"""
TRA-57 の測定スクリプト。コミット済みの bot 定義を使って、現物レッグの会場・執行の
宣言グリッドを並べ、次の4つを出す。

  1. TRA-50 §1-6 の再現 (会場 × 成行/指値 の年率・手数料)
  2. 窓の取り方に対する感度 (180日窓の終端を1日ずつずらしたときの年率の散らばり)
  3. 指値の約定率 (bot の実際の発注ぶん / 全時間足での無条件の約定率)
  4. ウォークフォワード (学習期間で会場・執行を選び、続く検証期間で走らせる)

  python scripts/execution_study.py [--end 2026-11-13T09:00] [--days 180]

データは data/ のキャッシュを使う (無ければ取得する)。公開マーケットデータの read-only 照会のみ。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tbf.backtest.data import load_bot_data  # noqa: E402
from tbf.backtest.engine import backtest_async  # noqa: E402
from tbf.backtest.sweep import configure  # noqa: E402
from tbf.backtest.walkforward import walk_forward  # noqa: E402
from tbf.registry import load_bot  # noqa: E402

DAY = 86_400_000
BOTS = ["btc_carry_breaker", "eth_carry_breaker"]
# TRA-57 で宣言されている会場・執行のグリッド。binance は現行基準の参照用
VENUES = ["gmo", "bitbank", "binance"]
ORDER_TYPES = ["limit", "market"]
# TRA-50 §3 の床 (sUSDe 30日平均)
FLOOR = 5.12
# ウォークフォワードの (学習日数, 検証日数)。Hyperliquid の足が直近 5000 本 (約208日) までなので窓数は限られる
SPLITS = [(90.0, 30.0), (60.0, 20.0), (40.0, 14.0)]


def combos() -> list[dict[str, Any]]:
    return [
        {"instruments.spot.venue": v, "instruments.spot.order_type": t} for v in VENUES for t in ORDER_TYPES
    ]


def ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def day(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def label(c: dict[str, Any]) -> str:
    return f"{c['instruments.spot.venue']} {c['instruments.spot.order_type']}"


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", default="2026-11-13T09:00", help="評価の終端 (UTC)")
    ap.add_argument("--days", type=float, default=180, help="主たる窓の長さ")
    ap.add_argument("--history-days", type=float, default=400, help="取得する足の長さ (キャッシュの鍵)")
    ap.add_argument("--out", default="reports/tra57")
    a = ap.parse_args(argv)

    end = ms(a.end)
    start = end - int(a.history_days * DAY)
    win_start = end - int(a.days * DAY)
    out: dict[str, Any] = {"end": a.end, "window_days": a.days, "floor_apr": FLOOR, "bots": {}}

    for bot_id in BOTS:
        base = load_bot(bot_id)
        bars, funding = await load_bot_data(base, start, end, Path("data"))
        first = max(c[0].time for c in bars.values() if c)
        last = min(c[-1].time for c in bars.values() if c)
        print(f"\n########## {bot_id}  データ {day(first)} .. {day(last)} ({(last - first) / DAY:.0f} 日)")
        rec: dict[str, Any] = {"data_from": day(first), "data_to": day(last)}

        # --- 1. §1-6 の再現 ---
        print(f"\n--- 1. §1-6 再現  {day(win_start)} .. {day(end)} ({a.days:g} 日)")
        print(f"{'会場 執行':<18}{'年率':>8}{'床との差':>9}{'シャープ':>9}{'手数料':>8}{'指値':>6}{'成行落ち':>9}")
        rows = []
        for c in combos():
            r = await backtest_async(configure(base, c), bars, funding, trade_from=win_start, trade_until=end)
            s = r.summary()
            apr = s["apr"] * 100
            fb = f"{r.limit_fallbacks}/{r.limit_orders}" if r.limit_orders else "-"
            print(
                f"{label(c):<18}{apr:>7.2f}%{apr - FLOOR:>+9.2f}{s['sharpe']:>9.2f}"
                f"{s['fees']:>8,.0f}{r.limit_orders:>6}{fb:>9}"
            )
            rows.append({"combo": label(c), **s})
        rec["reproduction"] = rows

        # --- 2. 窓の感度 (終端を1日ずつずらす) ---
        print(f"\n--- 2. {a.days:g}日窓の終端を1日ずつずらしたときの年率")
        print(f"{'会場 執行':<18}{'最小':>8}{'中央':>8}{'最大':>8}{'幅':>8}{'床超え':>9}  窓数")
        sens = []
        for c in combos():
            bot = configure(base, c)
            series = []
            cut = end
            while cut - int(a.days * DAY) >= first:
                r = await backtest_async(bot, bars, funding, trade_from=cut - int(a.days * DAY), trade_until=cut)
                if len(r.equity) >= 2:
                    series.append((day(cut), r.apr * 100))
                cut -= DAY
            if not series:
                continue
            aprs = sorted(x for _, x in series)
            med = aprs[len(aprs) // 2]
            over = sum(1 for x in aprs if x > FLOOR) / len(aprs)
            print(
                f"{label(c):<18}{aprs[0]:>7.2f}%{med:>7.2f}%{aprs[-1]:>7.2f}%"
                f"{aprs[-1] - aprs[0]:>7.2f}{over * 100:>8.0f}%{len(aprs):>6}"
            )
            sens.append({"combo": label(c), "min": aprs[0], "median": med, "max": aprs[-1],
                         "over_floor_share": over, "windows": len(aprs),
                         "by_end": [{"end": d, "apr": x} for d, x in series]})
        rec["window_sensitivity"] = sens

        # --- 3. 指値の約定率 (無条件) ---
        # 「前の足の終値に指値を置き、次の足 (1時間) のあいだ待つ」が約定するか
        spot = bars["spot"]
        buy = sum(1 for p, c2 in zip(spot, spot[1:]) if c2.low <= p.close)
        sell = sum(1 for p, c2 in zip(spot, spot[1:]) if c2.high >= p.close)
        n = len(spot) - 1
        print(f"\n--- 3. 指値の約定率 (全 {n:,} 本の時間足, 無条件)")
        print(f"  買い指値 (前足終値) が 1 時間以内に約定: {buy:>6,}/{n:,}  {buy / n * 100:.2f}%  未約定 {(1 - buy / n) * 100:.2f}%")
        print(f"  売り指値 (前足終値) が 1 時間以内に約定: {sell:>6,}/{n:,}  {sell / n * 100:.2f}%  未約定 {(1 - sell / n) * 100:.2f}%")
        rec["unconditional_fill"] = {"bars": n, "buy_filled": buy, "sell_filled": sell}

        # --- 3b. 成行へ落ちた 1 回あたりのコスト (実測) ---
        # 未約定だった足について、置いた値段 (前足終値) から見て足の終値がどれだけ不利かを測る。
        # 指値が約定しない足は「値段が逃げた足」なので、ここは必ず不利側に出る (逆選択)。
        def adverse(side: str) -> list[float]:
            out = []
            for p, c2 in zip(spot, spot[1:]):
                filled = c2.low <= p.close if side == "buy" else c2.high >= p.close
                if filled:
                    continue
                gap = (c2.close / p.close - 1) * (1 if side == "buy" else -1)
                out.append(gap * 10_000)
            return sorted(out)

        fees = base.instruments["spot"].fees
        gaps = {s: adverse(s) for s in ("buy", "sell")}
        # 1 回の成行落ちで余計に払う分 = 値段の逃げ + テイカーの板食い + (テイカー料率 - メイカー料率)
        rate_gap = (fees.taker_rate - fees.rate("limit")) * 10_000
        costs = {}
        for s, g in gaps.items():
            avg = sum(g) / len(g) if g else 0.0
            costs[s] = {"n": len(g), "gap_avg_bps": avg, "gap_p95_bps": g[int(len(g) * 0.95)] if g else 0.0,
                        "total_bps": avg + fees.slippage_bps + rate_gap}
        print(f"\n--- 3b. 成行へ落ちた 1 回あたりの追加コスト (未約定だった足で実測, {base.instruments['spot'].venue})")
        for s, c in costs.items():
            print(f"  {s:<5} 未約定 {c['n']:>4} 本  値段の逃げ 平均 {c['gap_avg_bps']:>6.1f}bps / p95 {c['gap_p95_bps']:>6.1f}bps"
                  f"  + 板食い {fees.slippage_bps:.1f} + 料率差 {rate_gap:.1f} = {c['total_bps']:>6.1f}bps")
        rec["fallback_cost"] = costs

        # --- 3c. 床を割るのに必要な成行落ち割合 ---
        lim = next(c for c in combos() if label(c) == f"{base.instruments['spot'].venue} limit")
        rlim = await backtest_async(configure(base, lim), bars, funding, trade_from=win_start, trade_until=end)
        notional = sum(f.qty * f.price for f in rlim.fills if f.instrument == "spot")
        margin_pt = rlim.apr * 100 - FLOOR
        worst = max(c["total_bps"] for c in costs.values())
        # 全部が成行に落ちたときの年率の下げ幅 (pt)
        drag_all = notional * worst / 10_000 / rlim.metrics.start_equity * (365 / a.days) * 100
        need = margin_pt / drag_all if drag_all > 0 else float("inf")
        print(f"\n--- 3c. 床割れに必要な成行落ち割合 ({label(lim)})")
        print(f"  指値の年率 {rlim.apr * 100:.2f}% / 床 {FLOOR:.2f}% / マージン {margin_pt:+.2f}pt")
        print(f"  現物の指値 約定代金 合計 ${notional:,.0f} ({rlim.limit_orders} 回) / 初期資金 ${rlim.metrics.start_equity:,.0f}")
        print(f"  全部が成行に落ちた場合の年率の下げ幅 {drag_all:.2f}pt (1回あたり {worst:.1f}bps で換算)")
        print(f"  => 床を割るのに必要な成行落ち割合 {need * 100:.0f}%  (実測 {max(1 - buy / n, 1 - sell / n) * 100:.2f}%)")
        rec["floor_break"] = {"limit_apr": rlim.apr, "margin_pt": margin_pt, "spot_notional": notional,
                              "per_event_bps": worst, "drag_if_all_fallback_pt": drag_all,
                              "fallback_share_to_break_floor": need,
                              "measured_fallback_share": max(1 - buy / n, 1 - sell / n)}

        # --- 4. ウォークフォワード ---
        rec["walk_forward"] = []
        for train, test in SPLITS:
            print(f"\n--- 4. ウォークフォワード (学習 {train:g} 日 / 検証 {test:g} 日 / 指標 sharpe)")
            try:
                wf = await walk_forward(base, bars, funding, combos(), train, test, "sharpe")
            except ValueError as e:
                print(f"  {e}")
                continue
            print(f"{'検証期間':<25}{'学習で選んだ設定':<18}{'検証 年率':>10}{'床との差':>9}{'検証DD':>8}{'成行落ち':>9}")
            folds = []
            for f in wf.folds:
                fb = f"{f.test.limit_fallbacks}/{f.test.limit_orders}" if f.test.limit_orders else "-"
                apr = f.test.apr * 100
                print(
                    f"{day(f.test_start)} .. {day(f.test_end):<11}{label(f.chosen):<18}"
                    f"{apr:>9.2f}%{apr - FLOOR:>+9.2f}{f.test.metrics.max_drawdown * 100:>7.2f}%{fb:>9}"
                )
                folds.append({"test": [day(f.test_start), day(f.test_end)], "chosen": label(f.chosen),
                              "apr": f.test.apr, "max_drawdown": f.test.metrics.max_drawdown,
                              "limit_orders": f.test.limit_orders, "limit_fallbacks": f.test.limit_fallbacks})
            eff = wf.efficiency
            oos = wf.oos.apr * 100
            print(f"{'検証をつないだ成績 (OOS)':<40}{oos:>9.2f}%{oos - FLOOR:>+9.2f}{wf.oos.metrics.max_drawdown * 100:>7.2f}%")
            print(f"{'同じ期間・後から見た最良 (' + label(wf.in_sample_combo) + ')':<40}{wf.in_sample_same_span.apr * 100:>9.2f}%")
            print(f"効率 (OOS 年率 / 後から見た最良の年率): {f'{eff:.3f}' if eff is not None else '-'}")
            print("選ばれた回数: " + ", ".join(f"{k} x{v}" for k, v in wf.choices.most_common()))
            rec["walk_forward"].append({
                "train_days": train, "test_days": test, "folds": folds,
                "oos_apr": wf.oos.apr, "oos_max_drawdown": wf.oos.metrics.max_drawdown,
                "in_sample_combo": label(wf.in_sample_combo), "in_sample_apr": wf.in_sample_same_span.apr,
                "efficiency": eff, "choices": dict(wf.choices),
            })

        out["bots"][bot_id] = rec

    d = Path(a.out)
    d.mkdir(parents=True, exist_ok=True)
    (d / "execution_study.json").write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    print(f"\nwrote {d}/execution_study.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
