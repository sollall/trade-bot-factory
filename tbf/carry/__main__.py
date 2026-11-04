"""
A① HL HYPE 現物ロング × HYPE perp ショート のシャドーモード計測 (TRA-22)
売買しない。注文系 API を呼ばない。秘密鍵も API キーも使わない。公開 info API の読み取りだけ。

  python -m tbf.carry check                 @107 と HYPE perp に接続でき、板と履歴が取れるか確認
  python -m tbf.carry run                   常駐して記録 (5分間隔。Ctrl-C で停止)
  python -m tbf.carry report --days 30      実現net年率・シャープ・最大DD・証拠金の推移
  python -m tbf.carry csv                   日次 net の系列を CSV に書き出す

共通オプション: --config carry.toml --db data/carry_a1.sqlite
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time

from tbf.carry.config import load_config
from tbf.carry.hl import InfoClient
from tbf.carry.position import walk
from tbf.carry.recorder import Recorder, fetch_snapshot, verify_identity
from tbf.carry.report import build_report, daily_rows, write_daily_csv
from tbf.carry.store import CarryStore
from tbf.http import UrllibHttp

# 手数料表の照会だけに使うアドレス (ゼロアドレス)。残高も署名も関係しない読み取り
FEE_PROBE_ADDRESS = "0x0000000000000000000000000000000000000000"
DEFAULT_CSV = "reports/carry/a1_daily.csv"


def _log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z", msg, flush=True)


async def check(cfg, http) -> int:
    """接続・識別・板・履歴・手数料を一次情報で確かめる。1 つでも食い違えば非 0 を返す"""
    client = InfoClient(http, cfg.info_url)
    bad: list[str] = []

    # 1. @107 が本当に HYPE/USDC か (ティッカー一致で別資産を指す実例があるので毎回見る)
    meta = await client.spot_meta()
    for line in verify_identity(meta, cfg):
        _log(line)

    # 2. spotMetaAndAssetCtxs は coin で結合する。位置 zip がズレることをここで実演しておく
    raw_meta, raw_ctxs = await client.post({"type": "spotMetaAndAssetCtxs"})
    idx = next(i for i, u in enumerate(raw_meta["universe"]) if u["name"] == cfg.spot_pair)
    at_idx = raw_ctxs[idx].get("coin") if idx < len(raw_ctxs) else None
    _log(
        f"spotMetaAndAssetCtxs: universe {len(raw_meta['universe'])} 件 / assetCtxs {len(raw_ctxs)} 件 "
        f"(長さが一致しない) — universe[{idx}].name={cfg.spot_pair} に対し assetCtxs[{idx}].coin={at_idx} "
        "なので位置 zip は使わず coin で結合する"
    )
    if at_idx == cfg.spot_pair:
        _log("注意: 今回は位置でも一致した。実装は coin 結合のままにする (一致は保証されない)")

    snap = await fetch_snapshot(client, cfg, int(time.time() * 1000))
    _log(
        f"現物 {cfg.spot_pair}: markPx {snap.spot_mark:,.4f} midPx {snap.spot_ctx.get('midPx')} "
        f"24h出来高 ${float(snap.spot_ctx['dayNtlVlm']):,.0f}"
    )
    _log(
        f"perp {cfg.perp_coin}: markPx {snap.perp_mark:,.4f} oraclePx {snap.perp_ctx.get('oraclePx')} "
        f"funding(1h) {snap.funding_rate_1h:+.8f} -> 年率 {snap.funding_rate_1h * 24 * 365 * 100:+.2f}% "
        f"premium {snap.perp_ctx.get('premium')} OI {float(snap.perp_ctx['openInterest']):,.0f} "
        f"maxLeverage {snap.perp_def.get('maxLeverage')} marginTableId {snap.perp_def.get('marginTableId')}"
    )
    _log(
        f"ベーシス (perp mark − 現物 mark)/現物: "
        f"{(snap.perp_mark - snap.spot_mark) / snap.spot_mark * 10_000:+.2f}bps"
    )

    # 3. 建て: HL の fundingHistory は 1 時間刻み。metaAndAssetCtxs の funding も 1 時間ぶん
    mt_id = snap.perp_def.get("marginTableId")
    if mt_id is not None:
        mt = await client.margin_table(int(mt_id))
        tiers = ", ".join(f"≥${float(t['lowerBound']):,.0f}: {t['maxLeverage']}x" for t in mt["marginTiers"])
        _log(f"marginTable {mt_id} ({mt.get('description')}): {tiers}")
        top = mt["marginTiers"][0]["maxLeverage"]
        implied_mm = 1 / float(top) / 2
        _log(
            f"  -> 当初証拠金 {100 / float(top):.1f}% / 維持証拠金はその半分 = {implied_mm * 100:.1f}% "
            f"(設定 {cfg.maintenance_margin_frac * 100:g}%)"
        )
        if abs(implied_mm - cfg.maintenance_margin_frac) > 1e-9:
            bad.append(
                f"維持証拠金の食い違い: 設定 {cfg.maintenance_margin_frac} / marginTable から {implied_mm}"
            )

    # 4. 板を歩く (表示価格は使わない)
    for leg, book, fee in (
        ("現物", snap.spot_book, cfg.spot_taker_bps),
        ("perp", snap.perp_book, cfg.perp_taker_bps),
    ):
        mid = book.mid
        if mid is None:
            bad.append(f"{leg}: 板の片側が空")
            continue
        _log(
            f"{leg} 板: bid {len(book.bids.levels)}段/${book.bids.notional_usd:,.0f} "
            f"ask {len(book.asks.levels)}段/${book.asks.notional_usd:,.0f} mid {mid:,.4f} "
            f"(l2Book は上位 20 段しか返さない)"
        )
        for size in cfg.depth_sizes_usd:
            b = walk(book.asks, size, mid, "buy", fee)
            s = walk(book.bids, size, mid, "sell", fee)
            def f(x):
                return "届かず(未測定)" if x.slip_bps is None else f"{x.slip_bps:+.2f}bps"
            _log(f"  ${size:>7,.0f}: 買い {f(b)} / 売り {f(s)}  (手数料 {fee:g}bps は別)")

    # 5. fundingHistory (500 件上限のページングが効いているか)
    now = int(time.time() * 1000)
    pts = await client.funding_history(cfg.perp_coin, now - 7 * 86_400_000, now)
    if len(pts) < 24:
        bad.append(f"fundingHistory が 7 日で {len(pts)} 件しか返りません")
    else:
        rates = [p.rate for p in pts]
        mean = sum(rates) / len(rates)
        neg = sum(1 for r in rates if r < 0)
        _log(
            f"fundingHistory 7日: {len(pts)} 時間 (期待 ~168) 平均 {mean:.8f} -> 年率 "
            f"{mean * 24 * 365 * 100:+.2f}%, 負だった時間 {neg}/{len(pts)} = {neg / len(pts) * 100:.1f}%"
        )

    # 6. 手数料を API で確認 (設定と食い違えば check は失敗させる)
    fees = await client.user_fees(FEE_PROBE_ADDRESS)
    sched = fees["feeSchedule"]
    api_perp = float(sched["cross"]) * 10_000
    api_spot = float(sched["spotCross"]) * 10_000
    _log(
        f"userFees feeSchedule (最下位 tier): perp taker {api_perp:g}bps / 現物 taker {api_spot:g}bps "
        f"(設定 {cfg.perp_taker_bps:g} / {cfg.spot_taker_bps:g})"
    )
    if abs(api_perp - cfg.perp_taker_bps) > 1e-9 or abs(api_spot - cfg.spot_taker_bps) > 1e-9:
        bad.append(
            f"手数料の食い違い: API perp {api_perp}bps 現物 {api_spot}bps / "
            f"設定 perp {cfg.perp_taker_bps}bps 現物 {cfg.spot_taker_bps}bps"
        )

    _log(
        f"資本規約: C = ${cfg.capital_usd:,.0f} (現物 N ${cfg.spot_notional_usd:,.0f} + "
        f"perp 証拠金 ${cfg.perp_margin_usd:,.0f}, C/N = {cfg.capital_ratio:.4f})。"
        f"計 {client.calls} calls {client.latency_ms:.0f}ms"
    )
    if bad:
        for b in bad:
            _log(f"check failed: {b}")
        return 1
    _log("check OK — 読み取りのみ。注文系 API は呼んでいない")
    return 0


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m tbf.carry", description=__doc__)
    ap.add_argument("command", choices=["check", "run", "report", "csv"])
    ap.add_argument("--config", default="carry.toml")
    ap.add_argument("--db", default="data/carry_a1.sqlite")
    ap.add_argument("--days", type=float, default=None, help="report: 判定してよい経過日数の宣言")
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--max-polls", type=int, default=None, help="run: 指定回数で止める (動作確認用)")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    http = UrllibHttp(timeout=20)

    if a.command == "check":
        return await check(cfg, http)

    store = CarryStore(a.db)

    if a.command == "report":
        print(build_report(store, cfg, int(time.time() * 1000), a.days))
        return 0

    if a.command == "csv":
        rows = daily_rows(store, cfg)
        if not rows:
            _log("日次サンプルがまだありません (CSV は書きません)")
            return 1
        p = write_daily_csv(rows, a.csv)
        _log(f"{len(rows)} 行を {p} に書きました")
        return 0

    # run: 起動前に check を通す (識別の食い違いに気づかないまま 30 日回すのを防ぐ)
    rc = await check(cfg, http)
    if rc != 0:
        return rc

    store.set_meta_json("config", {
        "spot_pair": cfg.spot_pair, "perp_coin": cfg.perp_coin,
        "capital_usd": cfg.capital_usd, "spot_notional_usd": cfg.spot_notional_usd,
        "perp_margin_usd": cfg.perp_margin_usd, "poll_interval_sec": cfg.poll_interval_sec,
        "spot_taker_bps": cfg.spot_taker_bps, "perp_taker_bps": cfg.perp_taker_bps,
        "maintenance_margin_frac": cfg.maintenance_margin_frac,
        "margin_warn_frac": cfg.margin_warn_frac, "measure_days": cfg.measure_days,
        "hurdle_net_apr": cfg.hurdle_net_apr, "sharpe_pass": cfg.sharpe_pass,
        "sharpe_reject": cfg.sharpe_reject,
    })
    store.commit()

    recorder = Recorder(cfg, http, store, log=_log)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    _log(
        f"shadow mode started: {cfg.spot_pair} 現物ロング × {cfg.perp_coin} perp ショート, "
        f"{cfg.poll_interval_sec:g}s 間隔, {cfg.measure_days:g} 日 -> {a.db}"
    )
    await recorder.run(stop, a.max_polls)
    _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
