"""
ローカルバックテスト
  python -m tbf.backtest --bot sma_cross --from 2025-01-01 --to 2025-07-01
  python -m tbf.backtest --bot eth_funding_carry,eth_funding_hedged --days 180     # 同じ期間で並べて比較
  python -m tbf.backtest --bot sma_cross --param fast=20 --param slow=100
  python -m tbf.backtest --bot eth_funding_carry --sweep entry_apr=5,10,20 --sweep leverage=2,3,5
  python -m tbf.backtest --bot sma_cross --csv ./my-data.csv                       # 1 銘柄 bot のみ
--param / --sweep のキーは戦略の params か、bot の設定 (leverage, capital, rebalance_band,
transfer_fee_usd, rebalance_threshold, cash_buffer) のどちらでもよい。
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tbf.backtest.data import load_bot_data, load_csv
from tbf.backtest.engine import BacktestResult, backtest_async
from tbf.backtest.report import render_html
from tbf.core.bot import Bot
from tbf.registry import load_all

BOT_SETTINGS = {"leverage", "capital", "rebalance_band", "transfer_fee_usd", "rebalance_threshold", "cash_buffer"}


def _parse_date(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _value(raw: str) -> Any:
    for conv in (int, float):
        try:
            return conv(raw)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(raw.lower(), raw)


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def configure(bot: Bot, overrides: dict[str, Any]) -> Bot:
    settings = {k: float(v) for k, v in overrides.items() if k in BOT_SETTINGS}
    params = {k: v for k, v in overrides.items() if k not in BOT_SETTINGS}
    out = bot.with_params(**params) if params else bot
    return out.with_settings(**settings) if settings else out


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
    ap.add_argument("--out", default="reports")
    ap.add_argument("--cache", default="data")
    a = ap.parse_args(argv)

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

    overrides = {k: _value(v) for k, _, v in (kv.partition("=") for kv in a.param)}
    keys = [kv.partition("=")[0] for kv in a.sweep]
    vals = [[_value(v) for v in kv.partition("=")[2].split(",")] for kv in a.sweep]
    combos = [dict(zip(keys, vs)) for vs in itertools.product(*vals)] or [{}]

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


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
