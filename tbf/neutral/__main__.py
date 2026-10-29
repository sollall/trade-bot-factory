"""
デルタニュートラル (現物ロング + 無期限先物ショート) のバックテスト。データは Hyperliquid。
  python -m tbf.neutral --coin ETH --days 180
  python -m tbf.neutral --coin BTC,ETH,SOL --days 180 --param entry_apr=15 --param leverage=2
  python -m tbf.neutral --coin ETH --sweep entry_apr=5,10,20 --sweep leverage=2,3,5
  python -m tbf.neutral --coin ETH --strategy bots.my_neutral.strategy:decide
--param / --sweep のキーは engine 用 (leverage, spot_fee, perp_fee, slippage_bps, transfer_fee_usd,
rebalance_band, maintenance_margin, capital) と戦略用 (既定戦略なら lookback_hours, entry_apr,
exit_apr, min_hold_hours) のどちらも指定できる。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import itertools
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from tbf.http import UrllibHttp
from tbf.neutral.data import HOUR, load_hyperliquid
from tbf.neutral.engine import NeutralParams, backtest_neutral
from tbf.neutral.report import render_html
from tbf.neutral.strategies import always_in, funding_threshold


def _parse_date(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _value(raw: str):
    for conv in (int, float):
        try:
            return conv(raw)
        except ValueError:
            pass
    return raw


def _load_strategy(spec: str):
    mod, _, fn = spec.partition(":")
    return getattr(importlib.import_module(mod), fn)


def _row(label: str, s: dict) -> str:
    pct = lambda v: f"{v * 100:7.2f}%"  # noqa: E731
    return (
        f"{label:<40} 年率{pct(s['apr'])} 期間{pct(s['total_return'])} DD{pct(s['max_drawdown'])} "
        f"FR {s['funding_income']:>9,.0f} 手数料 {s['fees']:>7,.0f} 価格差 {s['price_pnl']:>8,.0f} "
        f"建玉{pct(s['time_in_market'])} 建{s['entries']:>3} 調整{s['rebalances']:>3} 清算{s['liquidations']:>2}"
    )


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.neutral")
    ap.add_argument("--coin", default="ETH", help="カンマ区切りで複数可 (例: BTC,ETH,SOL)")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--days", type=float, default=180, help="1 時間足は直近 5000 本 (約 208 日) まで")
    ap.add_argument("--strategy", default="tbf.neutral.strategies:funding_threshold")
    ap.add_argument("--param", action="append", default=[], help="key=value")
    ap.add_argument("--sweep", action="append", default=[], help="key=v1,v2,... (総当たり)")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--cache", default="data")
    a = ap.parse_args(argv)

    end = _parse_date(a.end) if a.end else int(time.time() * 1000) // HOUR * HOUR
    start = _parse_date(a.start) if a.start else end - int(a.days * 24) * HOUR
    strategy = _load_strategy(a.strategy) if a.strategy != "tbf.neutral.strategies:funding_threshold" else funding_threshold
    base_params = {k: _value(v) for k, _, v in (kv.partition("=") for kv in a.param)}
    grid_keys = [kv.partition("=")[0] for kv in a.sweep]
    grid_vals = [[_value(v) for v in kv.partition("=")[2].split(",")] for kv in a.sweep]
    combos = [dict(zip(grid_keys, vs)) for vs in itertools.product(*grid_vals)] or [{}]

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    http = UrllibHttp()
    for coin in [c.strip().upper() for c in a.coin.split(",") if c.strip()]:
        bars, funding = await load_hyperliquid(http, coin, start, end, Path(a.cache))
        if not bars or not funding:
            print(f"{coin}: データがありません (bars={len(bars)}, funding={len(funding)})", file=sys.stderr)
            continue
        span = f"{datetime.fromtimestamp(bars[0].time / 1000, tz=timezone.utc):%Y-%m-%d} .. " \
               f"{datetime.fromtimestamp((bars[-1].time + HOUR) / 1000, tz=timezone.utc):%Y-%m-%d}"
        avg_apr = sum(f.rate for f in funding) / len(funding) * 24 * 365 * 100
        pos = sum(f.rate > 0 for f in funding) / len(funding)
        print(f"\n=== {coin}  {span}  ({len(bars)} 時間, ファンディング平均 年率 {avg_apr:.2f}%, プラスの時間 {pos * 100:.0f}%)")

        engine_base, _ = NeutralParams.split(base_params)
        baseline = backtest_neutral(bars, funding, always_in, engine_base)
        print(_row("持ちっぱなし (比較用)", baseline.summary()))

        results = []
        for combo in combos:
            merged = {**base_params, **combo}
            engine_params, strat_params = NeutralParams.split(merged)
            r = backtest_neutral(bars, funding, strategy, engine_params, strat_params)
            label = " ".join(f"{k}={v}" for k, v in combo.items()) or "戦略"
            results.append((label, merged, r))
        results.sort(key=lambda x: x[2].total_return, reverse=True)
        for label, _, r in results:
            print(_row(label, r.summary()))

        best_label, best_params, best = results[0]
        (out / f"neutral_{coin}.json").write_text(
            json.dumps(
                {
                    "coin": coin,
                    "span": span,
                    "baseline": baseline.summary(),
                    "runs": [{"params": p, **r.summary()} for _, p, r in results],
                    "best_events": [asdict(e) for e in best.events],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        title = f"{coin} delta neutral ({span}) {best_label}"
        (out / f"neutral_{coin}.html").write_text(render_html(title, best, baseline, funding))
    print(f"\nreports written to {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
