"""
国内の薄い板への両側指値 (マーケットメイク) のシャドーモード計測 — TRA-65
**発注はしない。注文系 API に触らない。鍵も口座も要らない。**

  python -m tbf.jpmaker check                   設定と公開 API の疎通を確認
  python -m tbf.jpmaker run                     記録を開始 (Ctrl-C で停止。7 日回す)
  python -m tbf.jpmaker report --days 7         集計を表示
  python -m tbf.jpmaker report --out report.md  ファイルに書く

共通オプション: --config jp_maker.toml --db data/jp_maker.sqlite
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import statistics
import sys
import time

from tbf.http import UrllibHttp
from tbf.jpmaker import declared
from tbf.jpmaker.collect import Collector
from tbf.jpmaker.config import load_config
from tbf.jpmaker.measure import DAY_MS, load, run_all
from tbf.jpmaker.report import build_report
from tbf.jpmaker.store import JpMakerStore


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def hedge_delay_ms(store: JpMakerStore, since: int, until: int) -> float:
    """ヘッジが入るまでの時間差の **下限**。約定検知の下限 + l2Book 往復の実測 p50"""
    lat = [
        r[0]
        for r in store.query(
            "SELECT latency_ms FROM polls WHERE venue='hyperliquid' AND time>=? AND time<? AND latency_ms IS NOT NULL",
            (since, until),
        )
    ]
    rtt = statistics.median(lat) if lat else 300.0
    return declared.FILL_DETECTION_FLOOR_MS + rtt


def period_since(period: str, first: int, until: int, days: float) -> int:
    """集計期間の先頭。

    `record` = 記録の先頭から全部 (既定)。`trailing` = 集計時刻から遡って `days` 日。

    既定を `record` にしてあるのは、`trailing` が **記録が目標日数に達した瞬間から
    記録の先頭を削り落とす**ためである。この計測では記録が 11-14 11:30:27Z 始まりで、
    11-21 12:00:00Z より後に集計すると先頭の 1 日窓が 12 時間を割り、
    `MIN_WINDOW_SPAN_RATIO` (50%) を 1 秒差で下回って**判定から外れる**。
    その窓はこの計測で最も悪い窓なので、落とすと中央値が良い側に動く。
    「期間の区切り方を変えると結論が反転する」失敗 (TRA-50 §7・§8) そのものなので、
    判定は記録の先頭を固定した `record` で出し、`trailing` は感度として並べるだけにする。
    """
    if period == "trailing":
        return max(first, until - int(days * DAY_MS))
    return first


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.jpmaker")
    ap.add_argument("command", choices=["check", "run", "report"])
    ap.add_argument("--config", default="jp_maker.toml")
    ap.add_argument("--db", default="data/jp_maker.sqlite")
    ap.add_argument("--days", type=float, default=float(declared.RUN_DAYS))
    ap.add_argument("--period", choices=["record", "trailing"], default="record")
    ap.add_argument("--out")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    store = JpMakerStore(a.db)

    if a.command == "report":
        until = int(time.time() * 1000)
        row = store.query("SELECT MIN(time) t FROM book")
        first = row[0]["t"] if row and row[0]["t"] is not None else until
        since = period_since(a.period, first, until, a.days)
        data = load(store, declared.PAIRS, since, until)
        m = run_all(
            data,
            since,
            until,
            cfg.book_poll_sec,
            hedge_delay_ms(store, since, until),
            hl_poll_sec=cfg.hl_poll_sec,
        )
        text = build_report(store, m, period=a.period)
        if a.out:
            open(a.out, "w").write(text + "\n")
            _log(f"wrote {a.out}")
        else:
            print(text)
        return 0

    http = UrllibHttp(timeout=cfg.http_timeout_sec)
    col = Collector(cfg, http, store, log=_log)

    if a.command == "check":
        ok = True
        for p in cfg.pairs:
            b = await col.poll_book(p.pair)
            t = await col.poll_tape(p.pair)
            h = await col.poll_hl(p.hedge_coin)
            _log(f"{p.pair:<11} depth {'OK' if b else 'NG'}  transactions {'OK' if t else 'NG'}  "
                 f"hl {p.hedge_coin} {'OK' if h else 'NG'}")
            ok = ok and b and t and h
        store.commit()
        _log("config OK" if ok else "一部の取得に失敗した (polls を見る)")
        return 0 if ok else 1

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    _log(
        f"shadow mode started (発注しない): {', '.join(cfg.pair_names)}  "
        f"depth {cfg.book_poll_sec}s / tape {cfg.tape_poll_sec}s / hl {cfg.hl_poll_sec}s"
    )
    await col.run(stop)
    _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
