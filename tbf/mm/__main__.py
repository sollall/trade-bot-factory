"""
マーケットメイクの準備: シャドーモードの記録とオフライン再生 (売買はしない。鍵も不要)
  python -m tbf.mm record --coins INJ,MON,ZRO   気配と約定を記録 (Ctrl-C で停止)。既定の会場は Hyperliquid
  python -m tbf.mm record --venue bitbank --coins xrp_jpy,eth_jpy --db data/mm_bitbank.sqlite
                                                会場は hyperliquid / gmo / bitbank。db は会場ごとに分ける
  python -m tbf.mm report [--hours N]           記録した市場の様子を集計
  python -m tbf.mm replay --coin INJ [--param half_spread_bps=4 ...]
                                                記録の上で指値の出し方を試す (API は叩かない)
  python -m tbf.mm paper --coins MON [--param k=v ...]
                                                記録しながら、その場で疑似的に約定を付ける (record を兼ねる)
  python -m tbf.mm paper-report [--run ID] [--hours N]
                                                paper の成績 (run ごと、日ごと、時間帯ごと)
  python -m tbf.mm replay --run ID              paper の run と同じ設定・同じ区間を再生して突き合わせる
環境変数: WEBHOOK_URL (paper の日次のまとめの通知先。未設定なら何も送らない)
共通オプション: --db data/mm.sqlite   詳細は docs/mm.md
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time

from tbf.core.instrument import DEFAULT_FEES
from tbf.http import UrllibHttp
from tbf.mm.paper import Paper, build_paper_report, load_run, run_result, run_span
from tbf.mm.quoter import QUOTERS, parse_params
from tbf.mm.recorder import DEFAULT_BUDGET, Recorder, check_budget
from tbf.mm.replay import DEFAULT_LATENCY_MS, add_markouts, format_result, simulate
from tbf.mm.report import build_report
from tbf.mm.store import MmStore
from tbf.mm.streams import DEFAULT_WS_DEPTH, STREAMS, StreamRecorder
from tbf.mm.venues import VENUES, scale_defaults
from tbf.worker.notify import Notifier

TAKER_RATE = DEFAULT_FEES[("hyperliquid", "perp")].taker_rate


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


async def _daily_reports(store: MmStore, run: int, notifier: Notifier, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=86_400)
        except asyncio.TimeoutError:
            text = build_paper_report(store, run, 24, taker_rate=TAKER_RATE, brief=True)
            await notifier.send(":bar_chart: mm paper daily\n```\n" + text + "\n```")


def _maker(a: argparse.Namespace):
    """--maker-bps (--fee-bps) を渡したときだけその値。省くと None = 銘柄ごとに会場の値を使う"""
    return None if a.maker_bps is None else a.maker_bps / 10_000


async def _record(a: argparse.Namespace) -> int:
    paper_mode = a.command == "paper"
    coins = [c.strip() for c in a.coins.split(",") if c.strip()]
    if not coins:
        _log("--coins に銘柄を指定してください (例: --coins INJ,MON,ZRO)")
        return 1
    venue = VENUES[a.venue]()
    ws_mode = a.source == "ws"
    if ws_mode and a.venue not in STREAMS:
        _log(f"{a.venue} の配信での記録は未対応です (対応: {', '.join(STREAMS)})")
        return 1
    depth = a.depth if a.depth is not None else (DEFAULT_WS_DEPTH if ws_mode else 10)
    try:
        used = 0.0 if ws_mode else check_budget(len(coins), a.interval, a.trades_interval, a.budget, venue)
    except ValueError as e:
        _log(str(e))
        return 1
    if paper_mode:
        try:
            params = parse_params(scale_defaults(QUOTERS[a.quoter][1], venue.quote), a.param)
        except ValueError as e:
            _log(str(e))
            return 1
    store = MmStore(a.db)
    http = UrllibHttp(timeout=10)
    if ws_mode:
        recorder = StreamRecorder(store, venue, coins, depth, log=_log, min_book_ms=a.book_ms)
    else:
        recorder = Recorder(http, store, coins, a.interval, a.trades_interval, depth, log=_log, venue=venue)
    try:
        await (recorder.setup(http) if ws_mode else recorder.setup())
    except Exception as e:  # noqa: BLE001
        _log(f"setup failed: {type(e).__name__}: {e}")
        return 1
    paper = None
    if paper_mode:
        paper = Paper(
            store, coins, a.quoter, params, maker_rate=_maker(a), latency_ms=a.latency_ms,
            cross_fills=not a.no_cross_fill, interval=0 if ws_mode else a.interval,
            trades_interval=0 if ws_mode else a.trades_interval, depth=depth, log=_log,
        )
        recorder.on_book, recorder.on_trades = paper.on_book, paper.on_trades
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if ws_mode:
        _log(f"recording {', '.join(coins)}: 配信 (WebSocket) で記録。気配は上位 {depth} 段が変わったときだけ残す。切れたら自動でつなぎ直す")
    else:
        _log(
            f"recording {', '.join(coins)}: 気配 {a.interval:g} 秒ごと / 約定 {a.trades_interval:g} 秒ごと  "
            f"毎分の{venue.unit} {used:.0f} / 上限 {venue.limit_per_min:.0f} ({venue.limit_note}。docs/mm.md)"
        )
    if paper is None:
        await recorder.run(stop, a.minutes * 60 if a.minutes else None)
    else:
        _log(
            f"paper run {paper.run} を開始: 在庫 0 から (前の run の在庫は引き継ぎません)。発注はしません。"
            f"クォーター {a.quoter} " + " ".join(f"{k}={v:g}" for k, v in params.items())
        )
        notifier = Notifier(http, os.environ.get("WEBHOOK_URL"), "mm-paper")
        await notifier.send(f"mm paper run {paper.run} started: {', '.join(coins)}")
        reports = asyncio.create_task(_daily_reports(store, paper.run, notifier, stop))
        try:
            await recorder.run(stop, a.minutes * 60 if a.minutes else None)
        finally:
            stop.set()
            await reports
            paper.stop()
        _log(f"paper run {paper.run} を停止。成績: python -m tbf.mm paper-report --db {a.db} --run {paper.run}")
    if ws_mode:
        _log(
            f"stopped (受け取った気配 {recorder.books_seen} 件のうち残したのは {recorder.books_kept} 件、約定 {recorder.trades_kept} 件、"
            f"つなぎ直し {recorder.reconnects} 回)"
        )
    else:
        _log(f"stopped (約定の取りこぼしの可能性 {recorder.gaps} 回)")
    return 0


def _since(store: MmStore, hours: float | None, coin: str | None = None) -> int:
    """--hours は「今から」ではなく記録の末尾から遡る (記録を後から読み返す使い方が主なので)"""
    return 0 if hours is None else store.last_time(coin) - int(hours * 3_600_000)


def _replay_run(a: argparse.Namespace) -> int:
    """paper の run と同じ設定・同じ区間を記録から流し直し、paper 自身の成績と並べる"""
    store = MmStore(a.db)
    cfg = load_run(store, a.run)
    if cfg is None:
        _log(f"run {a.run} がありません ({a.db})")
        return 1
    horizons = tuple(int(h) for h in a.horizons.split(","))
    for coin in [a.coin] if a.coin else cfg.coins:
        span = run_span(store, cfg.id, coin)
        if span is None:
            print(f"== {coin}: run {cfg.id} には確定した気配がありません\n")
            continue
        inst = store.instrument(coin)
        books = store.books(coin, span[0], span[1])
        res = simulate(
            books, store.trades(coin, span[0], span[1]), QUOTERS[cfg.quoter][0], cfg.params,
            instrument=inst, maker_rate=cfg.rate(coin), latency_ms=cfg.latency_ms,
            horizons=horizons, cross_fills=cfg.cross_fills, queue_from_book=store.source() == "ws",
        )
        add_markouts(res, store.books(coin, span[0]), horizons)  # paper-report と同じく、区間より後の気配も使って測る
        print(format_result(
            coin, res, cfg.params, maker_rate=cfg.rate(coin), taker_rate=inst.taker_rate, latency_ms=cfg.latency_ms, instrument=inst,
            trade_gaps=store.gap_count(coin, span[0], span[1]), quoter=cfg.quoter, cross_fills=cfg.cross_fills,
            queue_from_book=store.source() == "ws",
        ))
        p = run_result(store, cfg.id, coin, horizons=horizons)
        same = (
            [(f.time, f.side, f.px, f.size, f.kind) for f in p.fills] == [(f.time, f.side, f.px, f.size, f.kind) for f in res.fills]
            and abs(p.total_pnl - res.total_pnl) < 1e-6
        )
        print(
            f"\npaper run {cfg.id} との突き合わせ: {'一致' if same else '不一致'}  "
            f"約定 paper {len(p.fills)} 件 / 再生 {len(res.fills)} 件  合計 paper {p.total_pnl:+.6f} / 再生 {res.total_pnl:+.6f}\n"
        )
    return 0


def _replay(a: argparse.Namespace) -> int:
    if a.run is not None:
        return _replay_run(a)
    if not a.coin:
        _log("--coin を指定してください")
        return 1
    quoter, defaults = QUOTERS[a.quoter]
    store = MmStore(a.db)
    since = _since(store, a.hours, a.coin)
    books = store.books(a.coin, since)
    inst = store.instrument(a.coin)
    if len(books) < 2 or inst is None:
        _log(f"{a.coin} の記録がありません ({a.db})。記録のある銘柄: {', '.join(store.coins()) or '(なし)'}")
        return 1
    try:
        params = parse_params(scale_defaults(defaults, inst.quote), a.param)
    except ValueError as e:
        _log(str(e))
        return 1
    maker_rate = inst.maker_rate if a.maker_bps is None else a.maker_bps / 10_000
    res = simulate(
        books, store.trades(a.coin, since), quoter, params, instrument=inst, maker_rate=maker_rate,
        latency_ms=a.latency_ms, horizons=tuple(int(h) for h in a.horizons.split(",")), cross_fills=not a.no_cross_fill,
        queue_from_book=store.source() == "ws",
    )
    print(format_result(
        a.coin, res, params, maker_rate=maker_rate, taker_rate=inst.taker_rate, instrument=inst,
        latency_ms=a.latency_ms, trade_gaps=store.gap_count(a.coin, books[0].time, books[-1].time), quoter=a.quoter,
        cross_fills=not a.no_cross_fill, queue_from_book=store.source() == "ws",
    ))
    return 0


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.mm")
    ap.add_argument("command", choices=["record", "report", "replay", "paper", "paper-report"])
    ap.add_argument("--db", default="data/mm.sqlite")
    ap.add_argument("--hours", type=float, default=None, help="記録の末尾から遡る時間 (省略すると全部)")
    ap.add_argument("--maker-bps", "--fee-bps", dest="maker_bps", type=float, default=None,
                    help="メイカー手数料 (bps。払う向きが正、リベートは負)。省くと会場の値 (銘柄ごと)")
    ap.add_argument("--venue", default="hyperliquid", choices=sorted(VENUES), help="record / paper の会場 (db ごとに 1 つ)")
    # record
    ap.add_argument("--coins", default="", help="カンマ区切り (例: INJ,MON,ZRO)")
    ap.add_argument("--interval", type=float, default=2.0, help="気配を取る間隔 (秒)")
    ap.add_argument("--trades-interval", type=float, default=10.0, help="約定を取る間隔 (秒)")
    ap.add_argument("--depth", type=int, default=None, help="残す気配の段数 (片側)。既定はポーリング 10、配信 5")
    ap.add_argument("--source", default="rest", choices=["rest", "ws"],
                    help="record / paper の取り方: rest = ポーリング (既定)、ws = 配信 (WebSocket。gmo と bitbank)")
    ap.add_argument("--book-ms", type=int, default=0, help="配信の記録で、気配を残す間隔の下限 (ms)。0 なら変わるたびに残す")
    ap.add_argument("--budget", type=float, default=DEFAULT_BUDGET, help="流量制限の上限のうち使ってよい割合")
    ap.add_argument("--minutes", type=float, default=None, help="この分数で止める (省略すると止めるまで)")
    # replay
    ap.add_argument("--coin", default="")
    ap.add_argument("--quoter", default="baseline", choices=sorted(QUOTERS))
    ap.add_argument("--param", action="append", default=[], metavar="k=v", help="クォーターのパラメータを上書き")
    ap.add_argument("--latency-ms", type=int, default=DEFAULT_LATENCY_MS, help="指値が板に載るまでの遅れ (仮定)")
    ap.add_argument("--horizons", default="5,30,60", help="約定の何秒後のミッドで測り直すか")
    ap.add_argument("--no-cross-fill", action="store_true", help="約定の仮定 (c) を外す (甘い側。幅を見るため)")
    ap.add_argument("--run", type=int, default=None, help="paper の run の id (paper-report は省くと最後の run)")
    a = ap.parse_args(argv)

    if a.command == "paper-report":
        horizons = tuple(int(h) for h in a.horizons.split(","))
        print(build_paper_report(MmStore(a.db), a.run, a.hours, taker_rate=TAKER_RATE, horizons=horizons))
        return 0
    if a.command in ("record", "paper"):
        return await _record(a)
    if a.command == "replay":
        return _replay(a)
    store = MmStore(a.db)
    print(build_report(store, _since(store, a.hours), _maker(a)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
