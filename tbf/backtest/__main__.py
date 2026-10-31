"""
ローカルバックテスト
  python -m tbf.backtest --bot sma_cross --from 2025-01-01 --to 2025-07-01
  python -m tbf.backtest --bot eth_funding_carry,eth_funding_hedged --days 180     # 同じ期間で並べて比較
  python -m tbf.backtest --bot sma_cross --param fast=20 --param slow=100
  python -m tbf.backtest --bot eth_funding_carry --sweep entry_apr=5,10,20 --sweep leverage=2,3,5
  python -m tbf.backtest --bot sma_cross --csv ./my-data.csv                       # 1 銘柄 bot のみ
  python -m tbf.backtest --bot eth_funding_carry --days 200 --sweep entry_apr=5,10,20 \
      --walk-forward 90:30 --objective sharpe                                     # ウォークフォワード検証
--param / --sweep のキーは戦略の params か、bot の設定 (leverage, capital, rebalance_band,
transfer_fee_usd, rebalance_threshold, cash_buffer) のどちらでもよい。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import math
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tbf.backtest.data import load_bot_data, load_csv
from tbf.backtest.engine import BacktestResult, backtest_async
from tbf.backtest.report import render_html
from tbf.backtest.sweep import OBJECTIVES, configure, parse_grid, parse_value
from tbf.backtest.walkforward import WalkForwardResult, walk_forward
from tbf.registry import load_all


def _parse_date(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _row(label: str, r: BacktestResult) -> str:
    s = r.summary()
    pct = lambda v: f"{v * 100:7.2f}%"  # noqa: E731
    line = (
        f"{label:<36} 年率{pct(s['apr'])} 期間{pct(s['total_return'])} DD{pct(s['max_drawdown'])} "
        f"シャープ{s['sharpe']:6.2f} 決済{s['trades']:>4} 勝率{pct(s['win_rate'])}"
    )
    if s["funding_income"] or s["liquidations"]:
        line += f" FR{s['funding_income']:>9,.0f} 清算{s['liquidations']}"
    line += f" 手数料{s['fees']:>8,.0f} 建玉{pct(s['time_in_market'])}"
    if s["halted"]:
        line += f" 停止: {s['halted']}"
    return line


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.backtest")
    ap.add_argument("--bot", default="all", help="bot id (カンマ区切りで複数 / all)")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--days", type=float, default=180)
    ap.add_argument("--csv", help="1 銘柄 bot 用の足データ")
    ap.add_argument("--param", action="append", default=[], help="key=value で上書き")
    ap.add_argument("--sweep", action="append", default=[], help="key=v1,v2,... (総当たり)")
    ap.add_argument("--walk-forward", metavar="TRAIN:TEST", help="ウォークフォワード検証 (学習日数:検証日数, 例 90:30)")
    ap.add_argument("--objective", choices=OBJECTIVES, default="sharpe", help="学習期間で設定を選ぶ指標")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--cache", default="data")
    a = ap.parse_args(argv)
    wf: tuple[float, float] | None = None
    if a.walk_forward:
        try:
            tr, te = (float(x) for x in a.walk_forward.split(":"))
            wf = (tr, te)
        except ValueError:
            ap.error("--walk-forward は 学習日数:検証日数 で指定してください (例 90:30)")

    end = _parse_date(a.end) if a.end else int(time.time() * 1000)
    start = _parse_date(a.start) if a.start else end - int(a.days * 86_400_000)
    if start >= end:
        ap.error("--from must be before --to")

    bots = load_all()
    wanted = [b.strip() for b in a.bot.split(",") if b.strip()]
    targets = bots if wanted == ["all"] else [b for b in bots if b.id in wanted]
    unknown = set(wanted) - {b.id for b in bots} - {"all"}
    if unknown or not targets:
        print(f"bot {sorted(unknown) or a.bot} not found. available: {', '.join(b.id for b in bots)}", file=sys.stderr)
        return 1

    overrides = {k: parse_value(v) for k, _, v in (kv.partition("=") for kv in a.param)}
    combos = parse_grid(a.sweep)
    if wf and len(combos) < 2:
        ap.error("--walk-forward には --sweep で 2 つ以上の組み合わせを指定してください")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    compared: list[tuple[str, BacktestResult]] = []
    for base in targets:
        if a.csv:
            if len(base.instruments) != 1:
                print(f"{base.id}: --csv は 1 銘柄の bot だけに使えます", file=sys.stderr)
                return 1
            bars, funding = {base.primary: load_csv(a.csv)}, {}
        else:
            try:
                bars, funding = await load_bot_data(base, start, end, Path(a.cache))
            except Exception as e:  # noqa: BLE001  1 つの bot のデータが取れなくても他は続ける
                print(f"\n=== {base.id}: データを取得できませんでした: {type(e).__name__}: {e}", file=sys.stderr)
                continue
        first = min((c[0].time for c in bars.values() if c), default=start)
        last = max((c[-1].time for c in bars.values() if c), default=end)
        insts = ", ".join(f"{k}={v}" for k, v in base.instruments.items())
        print(f"\n=== {base.id}  [{insts}] {base.timeframe}  {_date(first)} .. {_date(last)}")

        if wf:
            base_bot = configure(base, overrides)
            try:
                res = await walk_forward(base_bot, bars, funding, combos, wf[0], wf[1], a.objective)
            except ValueError as e:
                print(f"{base.id}: {e}", file=sys.stderr)
                continue
            print_walk_forward(res, wf)
            write_walk_forward(out, base.id, res, wf)
            compared.append((f"{base.id} (OOS)", res.oos))
            continue

        runs: list[tuple[str, dict, BacktestResult]] = []
        for combo in combos:
            merged = {**overrides, **combo}
            bot = configure(base, merged)
            r = await backtest_async(bot, bars, funding)
            runs.append((" ".join(f"{k}={v}" for k, v in combo.items()) or base.id, merged, r))
        runs.sort(key=lambda x: x[2].metrics.total_return, reverse=True)
        for label, _, r in runs:
            print(_row(label, r))

        label, merged, best = runs[0]
        compared.append((base.id if len(combos) == 1 else f"{base.id} ({label})", best))
        (out / f"{base.id}.json").write_text(
            json.dumps(
                {
                    "bot": base.id,
                    "runs": [{"label": l, "overrides": m, **r.summary()} for l, m, r in runs],
                    "best": {
                        "label": label,
                        "fills": [asdict(f) for f in best.fills],
                        "events": [asdict(e) for e in best.events],
                        "equity": [[p.time, p.equity] for p in best.equity],
                    },
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        (out / f"{base.id}.html").write_text(render_html(f"{base.id} ({_date(first)} .. {_date(last)}) {label}", [(label, best)]))

    if len(compared) > 1:
        print("\n=== 比較")
        for label, r in sorted(compared, key=lambda x: x[1].metrics.total_return, reverse=True):
            print(_row(label, r))
        (out / "compare.html").write_text(render_html("比較", compared))
    print(f"\nreports written to {out}/")
    return 0


def _fmt_score(x: float) -> str:
    return f"{x:8.3f}" if math.isfinite(x) else "       -"


def print_walk_forward(res: WalkForwardResult, wf: tuple[float, float]) -> None:
    pct = lambda v: f"{v * 100:7.2f}%"  # noqa: E731
    print(f"ウォークフォワード: 学習 {wf[0]:g} 日 / 検証 {wf[1]:g} 日 / 指標 {res.objective} / {len(res.folds)} 回")
    print(f"{'検証期間':<24} {'選んだ設定':<32} {'学習の指標':>9}  {'検証の期間':>9} {'検証DD':>8}")
    for f in res.folds:
        r = f.test
        print(
            f"{_date(f.test_start)} .. {_date(f.test_end):<11} {' '.join(f'{k}={v}' for k, v in f.chosen.items()):<32} "
            f"{_fmt_score(f.train_score)}  {pct(r.metrics.total_return)} {pct(r.metrics.max_drawdown)}"
        )
    print(_row("検証期間をつないだ成績 (OOS)", res.oos))
    is_label = " ".join(f"{k}={v}" for k, v in res.in_sample_combo.items())
    print(_row(f"同じ期間・後から見た最良 ({is_label})", res.in_sample_same_span))
    eff = res.efficiency
    print(f"効率 (OOS 年率 / 後から見た最良の年率): {f'{eff:.2f}' if eff is not None else '-'}   ※ 1 に近いほど過去への合わせすぎが小さい")
    print("選ばれた回数: " + ", ".join(f"{k} ×{n}" for k, n in res.choices.most_common()))


def write_walk_forward(out: Path, bot_id: str, res: WalkForwardResult, wf: tuple[float, float]) -> None:
    folds = [
        {
            "train": [f.train_start, f.train_end],
            "test": [f.test_start, f.test_end],
            "chosen": f.chosen,
            "train_score": f.train_score if math.isfinite(f.train_score) else None,
            **f.test.summary(),
        }
        for f in res.folds
    ]
    (out / f"{bot_id}.walkforward.json").write_text(
        json.dumps(
            {
                "bot": bot_id,
                "train_days": wf[0],
                "test_days": wf[1],
                "objective": res.objective,
                "folds": folds,
                "oos": res.oos.summary(),
                "in_sample_combo": res.in_sample_combo,
                "in_sample_same_span": res.in_sample_same_span.summary(),
                "efficiency": res.efficiency,
                "choices": dict(res.choices),
                "oos_equity": [[p.time, p.equity] for p in res.oos.equity],
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    pct = lambda v: f"{v * 100:.2f}%"  # noqa: E731
    rows = "".join(
        f"<tr><td>{_date(f.test_start)} .. {_date(f.test_end)}</td><td>{html.escape(' '.join(f'{k}={v}' for k, v in f.chosen.items()))}</td>"
        f"<td>{_fmt_score(f.train_score).strip()}</td><td>{pct(f.test.metrics.total_return)}</td><td>{pct(f.test.metrics.max_drawdown)}</td></tr>"
        for f in res.folds
    )
    eff = res.efficiency
    extra = (
        f"<h2>ウォークフォワード (学習 {wf[0]:g} 日 / 検証 {wf[1]:g} 日 / 指標 {res.objective})</h2>"
        f"<p>効率 (OOS 年率 / 後から見た最良の年率): <b>{f'{eff:.2f}' if eff is not None else '-'}</b> — 1 に近いほど過去への合わせすぎが小さい</p>"
        "<div class=\"wrap\"><table><tr><th>検証期間</th><th>学習期間で選んだ設定</th><th>学習の指標</th><th>検証の期間リターン</th><th>検証の最大DD</th></tr>"
        f"{rows}</table></div>"
        f"<p>選ばれた回数: {html.escape(', '.join(f'{k} ×{n}' for k, n in res.choices.most_common()))}</p>"
    )
    is_label = " ".join(f"{k}={v}" for k, v in res.in_sample_combo.items())
    (out / f"{bot_id}.walkforward.html").write_text(
        render_html(
            f"{bot_id} ウォークフォワード",
            [("検証期間をつないだ成績 (OOS)", res.oos), (f"同じ期間・後から見た最良 ({is_label})", res.in_sample_same_span)],
            extra,
        )
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
