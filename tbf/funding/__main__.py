"""
DEX 間ファンディング・スクリーナー (シャドーモード。売買しない。秘密鍵も API キーも使わない)
  python -m tbf.funding check              設定の会場につながるか・記号が存在するか・建てが合っているか
  python -m tbf.funding backfill --days 90 確定済みファンディングの履歴を取り込む (主指標の素)
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
from tbf.funding.history import fetch_history, has_history
from tbf.funding.report import build_report
from tbf.funding.scanner import Scanner
from tbf.funding.store import FundingStore
from tbf.funding.venues import Session, VenueClient, VenueError, check_all
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


async def backfill(cfg, http, db: str, days: float) -> int:
    """確定済みファンディングの履歴を取り込む

    7 日のライブ計測では 14 日保有の損益は検証できない (今の予測レートの外挿になる)。
    各会場に確定済みの履歴 API があるので、そこから数か月ぶんを埋めて
    「実際に確定したファンディングの合計」を主指標にする。読み取りのみ。
    """
    store = FundingStore(db, cfg.spread_bucket_sec)
    end = int(time.time() * 1000)
    start = end - int(days * 86_400_000)
    total = 0
    failed = 0
    for v in cfg.venues:
        if not has_history(v):
            _log(f"[{v.name}] 履歴アダプタがないので飛ばします")
            continue
        for sym in cfg.symbols:
            if not v.covers(sym):
                continue
            s = Session(http)
            try:
                points = await fetch_history(s, v, sym, start, end)
            except Exception as e:  # noqa: BLE001  1 会場で落ちても他を続ける
                failed += 1
                _log(f"[{v.name}] {sym}: 失敗 {type(e).__name__}: {e}")
                continue
            n = store.add_history(v.name, sym, points)
            total += n
            span = (points[-1].time - points[0].time) / 86_400_000 if points else 0
            aprs = sorted(p.apr for p in points)
            mid = aprs[len(aprs) // 2] if aprs else 0.0
            _log(
                f"[{v.name}] {sym}: {n} 区間 / {span:.1f}日 ({s.calls} calls)  "
                f"年率 中央 {mid * 100:+.2f}% / min {aprs[0] * 100:+.2f}% / max {aprs[-1] * 100:+.2f}%"
                if aprs
                else f"[{v.name}] {sym}: 0 区間 ({s.calls} calls)"
            )
    _log(f"合計 {total} 区間を取り込みました (失敗 {failed} 件)")
    for r in store.history_coverage():
        _log(
            f"  {r['venue']:<14}{r['symbol']:<5}{r['n']:>6} 区間  "
            f"{time.strftime('%m-%d %H:%M', time.gmtime(r['lo'] / 1000))} 〜 "
            f"{time.strftime('%m-%d %H:%M', time.gmtime(r['hi'] / 1000))}  mark {r['marks']}/{r['n']}"
        )
    return 0 if total else 1


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.funding", description=__doc__)
    ap.add_argument("command", choices=["check", "backfill", "run", "report"])
    ap.add_argument("--config", default="funding.toml")
    ap.add_argument("--venues", default=None, help="venues.toml の場所 (既定は funding.toml の venues_file)")
    ap.add_argument("--db", default="data/funding.sqlite")
    ap.add_argument("--days", type=float, default=None, help="report の集計日数 / backfill の遡る日数")
    a = ap.parse_args(argv)

    cfg = load_config(a.config, a.venues)

    if a.command == "report":
        store = FundingStore(a.db, cfg.spread_bucket_sec)
        days = a.days if a.days is not None else 7
        print(build_report(store, cfg, int((time.time() - days * 86_400) * 1000)))
        return 0

    http = UrllibHttp(timeout=15)

    if a.command == "backfill":
        return await backfill(cfg, http, a.db, a.days if a.days is not None else cfg.backfill_days)

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
