"""
チェーン間アビトラのシャドーモード (売買はしない。秘密鍵も不要)
  python -m tbf.arb check              設定がチェーン上の実態と合っているか確認
  python -m tbf.arb run                監視を開始 (Ctrl-C で停止)
  python -m tbf.arb report --hours 24  集計を表示
共通オプション: --config arb.toml --db data/arb.sqlite
環境変数: ARB_RPC_<CHAIN> (RPC URL の上書き), WEBHOOK_URL (日次レポートの通知先)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time

from tbf.arb.config import load_config
from tbf.arb.quotes import check_all
from tbf.arb.report import build_report
from tbf.arb.scanner import Scanner
from tbf.arb.store import ArbStore
from tbf.http import UrllibHttp
from tbf.onchain.rpc import Rpc
from tbf.worker.notify import Notifier


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


async def _daily_reports(store: ArbStore, notifier: Notifier, min_persist: int, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=86_400)
        except asyncio.TimeoutError:
            since = int(time.time() * 1000) - 86_400_000
            await notifier.send(":mag: arb shadow daily\n```\n" + build_report(store, since, min_persist) + "\n```")


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.arb")
    ap.add_argument("command", choices=["check", "run", "report"])
    ap.add_argument("--config", default="arb.toml")
    ap.add_argument("--db", default="data/arb.sqlite")
    ap.add_argument("--hours", type=float, default=24)
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    if a.command == "report":
        store = ArbStore(a.db)
        print(build_report(store, int((time.time() - a.hours * 3600) * 1000), cfg.min_persist_polls))
        return 0

    http = UrllibHttp(timeout=10)
    rpcs = {c.name: Rpc(http, c.rpc) for c in cfg.chains}
    try:
        cfg, lines = await check_all(rpcs, cfg)
    except Exception as e:  # noqa: BLE001
        _log(f"check failed: {type(e).__name__}: {e}")
        return 1
    for line in lines:
        _log(line)
    if a.command == "check":
        _log("config OK")
        return 0

    store = ArbStore(a.db)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    notifier = Notifier(http, os.environ.get("WEBHOOK_URL"), "arb-shadow")
    _log(f"shadow mode started: {', '.join(c.name for c in cfg.chains)} every {cfg.poll_interval_sec}s")
    await notifier.send("arb shadow mode started")
    await asyncio.gather(
        Scanner(cfg, rpcs, store, log=_log).run(stop),
        _daily_reports(store, notifier, cfg.min_persist_polls, stop),
    )
    _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
