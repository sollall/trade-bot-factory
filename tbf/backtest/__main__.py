"""
ローカルバックテスト
  python -m tbf.backtest --bot sma_cross --from 2025-01-01 --to 2025-07-01
  python -m tbf.backtest --bot all --days 90
  python -m tbf.backtest --bot sma_cross --param fast=20 --param slow=100
  python -m tbf.backtest --bot sma_cross --csv ./my-data.csv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from tbf.backtest.data import load_candles, load_csv
from tbf.backtest.engine import backtest
from tbf.backtest.report import render_html
from tbf.core.metrics import format_metrics
from tbf.registry import load_all


def _parse_date(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _parse_value(raw: str):
    for conv in (int, float):
        try:
            return conv(raw)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(raw.lower(), raw)


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.backtest")
    ap.add_argument("--bot", default="all")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--days", type=float, default=180)
    ap.add_argument("--csv")
    ap.add_argument("--param", action="append", default=[], help="key=value でパラメータを上書き")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args(argv)

    end = _parse_date(a.end) if a.end else int(time.time() * 1000)
    start = _parse_date(a.start) if a.start else end - int(a.days * 86_400_000)
    if start >= end:
        ap.error("--from must be before --to")

    bots = load_all()
    targets = bots if a.bot == "all" else [b for b in bots if b.id == a.bot]
    if not targets:
        print(f'bot "{a.bot}" not found. available: {", ".join(b.id for b in bots)}', file=sys.stderr)
        return 1
    overrides = {}
    for kv in a.param:
        k, _, v = kv.partition("=")
        overrides[k] = _parse_value(v)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for base in targets:
        bot = base.with_params(**overrides)
        candles = load_csv(a.csv) if a.csv else await load_candles(bot.symbol, bot.timeframe, start, end)
        r = backtest(bot, candles)
        span = f"{_date(candles[0].time)} .. {_date(candles[-1].time)}" if candles else "(no data)"
        print(f"\n=== {bot.id}  {bot.symbol} {bot.timeframe}  {span}  ({len(candles)} candles)")
        print(f"params        {json.dumps(bot.params)}")
        print(format_metrics(r.metrics))
        if r.halted:
            print(f"HALTED        {_date(r.halted.time)} {r.halted.reason}")
        report = {
            "bot": bot.id,
            "params": bot.params,
            "metrics": r.metrics.to_dict(),
            "halted": asdict(r.halted) if r.halted else None,
            "fills": [asdict(f) for f in r.fills],
            "equity": [[p.time, p.equity] for p in r.equity],
        }
        (out / f"{bot.id}.json").write_text(json.dumps(report, indent=2))
        (out / f"{bot.id}.html").write_text(render_html(r, f"{bot.id} backtest ({span})"))
    print(f"\nreports written to {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
