"""
DEX 間ファンディング・スクリーナー (シャドーモード。売買しない。秘密鍵も API キーも使わない)
  python -m tbf.funding check              設定の会場につながるか・記号が存在するか・建てが合っているか
  python -m tbf.funding run                常駐して記録 (Ctrl-C で停止)
  python -m tbf.funding report --days 7    会場ペアのランキングを表示
共通オプション: --config funding.toml --venues data/venues.toml --db data/funding.sqlite
環境変数: FUNDING_URL_<VENUE> (base_url の上書き), WEBHOOK_URL (日次レポートの通知先)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time

from tbf.funding.config import load_config
from tbf.funding.report import build_report
from tbf.funding.scanner import Scanner
from tbf.funding.store import FundingStore
from tbf.funding.venues import VenueClient, VenueError, check_all
from tbf.http import UrllibHttp
from tbf.worker.notify import Notifier


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


async def _daily_reports(store: FundingStore, cfg, notifier: Notifier, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=86_400)
        except asyncio.TimeoutError:
            since = int(time.time() * 1000) - 86_400_000
            await notifier.send(
                ":chart_with_upwards_trend: funding shadow daily\n```\n" + build_report(store, cfg, since) + "\n```"
            )


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.funding", description=__doc__)
    ap.add_argument("command", choices=["check", "run", "report"])
    ap.add_argument("--config", default="funding.toml")
    ap.add_argument("--venues", default=None, help="venues.toml の場所 (既定は funding.toml の venues_file)")
    ap.add_argument("--db", default="data/funding.sqlite")
    ap.add_argument("--days", type=float, default=7)
    a = ap.parse_args(argv)

    cfg = load_config(a.config, a.venues)

    if a.command == "report":
        store = FundingStore(a.db, cfg.spread_bucket_sec)
        print(build_report(store, cfg, int((time.time() - a.days * 86_400) * 1000)))
        return 0

    http = UrllibHttp(timeout=15)
    client = VenueClient(http, cfg)
    try:
        res = await check_all(client)
    except Exception as e:  # noqa: BLE001
        _log(f"check failed: {type(e).__name__}: {e}")
        return 1
    for line in res.lines:
        _log(line)
    _log(
        f"対象 {len(cfg.tradable)} 会場 / 参照 {len(cfg.venues) - len(cfg.tradable)} 会場, "
        f"銘柄 {', '.join(cfg.symbols)}, サイズ {', '.join(f'${s:,.0f}' for s in cfg.sizes_usd)}"
    )
    if res.api_intervals:
        _log("API が建てを返した会場: " + ", ".join(f"{k}={v:g}h" for k, v in sorted(res.api_intervals.items())))
    try:
        # check は設定の点検なので厳しく、run は計測を止めないよう緩く判定する
        res.raise_for_check(cfg) if a.command == "check" else res.raise_for_run(cfg)
    except VenueError as e:
        _log(f"check failed: {e}")
        return 1
    for name, err in res.errors.items():
        _log(f"警告: {name} は今回つながりませんでした ({err}) — この会場を除いて続けます")
    if a.command == "check":
        _log("config OK")
        return 0

    store = FundingStore(a.db, cfg.spread_bucket_sec)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    notifier = Notifier(http, os.environ.get("WEBHOOK_URL"), "funding-shadow")
    _log(
        "shadow mode started: "
        + ", ".join(f"{v.name}({v.poll_interval_sec:g}s)" for v in cfg.venues)
        + f" -> {a.db}"
    )
    await notifier.send("funding shadow mode started (読み取りのみ・売買なし)")
    await asyncio.gather(
        Scanner(cfg, client, store, log=_log).run(stop),
        _daily_reports(store, cfg, notifier, stop),
    )
    _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
