"""
マーケットメイクの準備: シャドーモードの記録とオフライン再生 (売買はしない。鍵も不要)
  python -m tbf.mm record --coins INJ,MON,ZRO   Hyperliquid の気配と約定を記録 (Ctrl-C で停止)
  python -m tbf.mm report [--hours N]           記録した市場の様子を集計
  python -m tbf.mm replay --coin INJ [--param half_spread_bps=4 ...]
                                                記録の上で指値の出し方を試す (API は叩かない)
共通オプション: --db data/mm.sqlite   詳細は docs/mm.md
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time

from tbf.core.instrument import DEFAULT_FEES
from tbf.http import UrllibHttp
from tbf.mm.quoter import QUOTERS, parse_params
from tbf.mm.recorder import DEFAULT_BUDGET, WEIGHT_LIMIT_PER_MIN, Recorder, check_budget
from tbf.mm.replay import DEFAULT_LATENCY_MS, DEFAULT_MAKER_RATE, format_result, simulate
from tbf.mm.report import build_report
from tbf.mm.store import MmStore


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


async def _record(a: argparse.Namespace) -> int:
    coins = [c.strip() for c in a.coins.split(",") if c.strip()]
    if not coins:
        _log("--coins に銘柄を指定してください (例: --coins INJ,MON,ZRO)")
        return 1
    try:
        used = check_budget(len(coins), a.interval, a.trades_interval, a.budget)
    except ValueError as e:
        _log(str(e))
        return 1
    store = MmStore(a.db)
    recorder = Recorder(UrllibHttp(timeout=10), store, coins, a.interval, a.trades_interval, a.depth, log=_log)
    try:
        await recorder.setup()
    except Exception as e:  # noqa: BLE001
        _log(f"setup failed: {type(e).__name__}: {e}")
        return 1
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    _log(
        f"recording {', '.join(coins)}: 気配 {a.interval:g} 秒ごと / 約定 {a.trades_interval:g} 秒ごと  "
        f"毎分の重み {used:.0f} / 上限 {WEIGHT_LIMIT_PER_MIN} (重みと上限は未確認の値。docs/mm.md)"
    )
    await recorder.run(stop, a.minutes * 60 if a.minutes else None)
    _log(f"stopped (約定の取りこぼしの可能性 {recorder.gaps} 回)")
    return 0


def _since(store: MmStore, hours: float | None, coin: str | None = None) -> int:
    """--hours は「今から」ではなく記録の末尾から遡る (記録を後から読み返す使い方が主なので)"""
    return 0 if hours is None else store.last_time(coin) - int(hours * 3_600_000)


def _replay(a: argparse.Namespace) -> int:
    if not a.coin:
        _log("--coin を指定してください")
        return 1
    quoter, defaults = QUOTERS[a.quoter]
    try:
        params = parse_params(defaults, a.param)
    except ValueError as e:
        _log(str(e))
        return 1
    store = MmStore(a.db)
    since = _since(store, a.hours, a.coin)
    books = store.books(a.coin, since)
    sz_decimals = store.sz_decimals(a.coin)
    if len(books) < 2 or sz_decimals is None:
        _log(f"{a.coin} の記録がありません ({a.db})。記録のある銘柄: {', '.join(store.coins()) or '(なし)'}")
        return 1
    maker_rate = a.maker_bps / 10_000
    res = simulate(
        books, store.trades(a.coin, since), quoter, params, sz_decimals=sz_decimals, maker_rate=maker_rate,
        latency_ms=a.latency_ms, horizons=tuple(int(h) for h in a.horizons.split(",")), cross_fills=not a.no_cross_fill,
    )
    print(format_result(
        a.coin, res, params, maker_rate=maker_rate, taker_rate=DEFAULT_FEES[("hyperliquid", "perp")].taker_rate,
        latency_ms=a.latency_ms, trade_gaps=store.gap_count(a.coin, books[0].time, books[-1].time), quoter=a.quoter,
        cross_fills=not a.no_cross_fill,
    ))
    return 0


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.mm")
    ap.add_argument("command", choices=["record", "report", "replay"])
    ap.add_argument("--db", default="data/mm.sqlite")
    ap.add_argument("--hours", type=float, default=None, help="記録の末尾から遡る時間 (省略すると全部)")
    ap.add_argument("--maker-bps", type=float, default=DEFAULT_MAKER_RATE * 10_000, help="メイカー手数料 (bps。払う向きが正)")
    # record
    ap.add_argument("--coins", default="", help="カンマ区切り (例: INJ,MON,ZRO)")
    ap.add_argument("--interval", type=float, default=2.0, help="気配を取る間隔 (秒)")
    ap.add_argument("--trades-interval", type=float, default=10.0, help="約定を取る間隔 (秒)")
    ap.add_argument("--depth", type=int, default=10, help="残す気配の段数 (片側。API が返すのは 20 段まで)")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET, help="流量制限の上限のうち使ってよい割合")
    ap.add_argument("--minutes", type=float, default=None, help="この分数で止める (省略すると止めるまで)")
    # replay
    ap.add_argument("--coin", default="")
    ap.add_argument("--quoter", default="baseline", choices=sorted(QUOTERS))
    ap.add_argument("--param", action="append", default=[], metavar="k=v", help="クォーターのパラメータを上書き")
    ap.add_argument("--latency-ms", type=int, default=DEFAULT_LATENCY_MS, help="指値が板に載るまでの遅れ (仮定)")
    ap.add_argument("--horizons", default="5,30,60", help="約定の何秒後のミッドで測り直すか")
    ap.add_argument("--no-cross-fill", action="store_true", help="約定の仮定 (c) を外す (甘い側。幅を見るため)")
    a = ap.parse_args(argv)

    if a.command == "record":
        return await _record(a)
    if a.command == "replay":
        return _replay(a)
    store = MmStore(a.db)
    print(build_report(store, _since(store, a.hours), a.maker_bps / 10_000))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
