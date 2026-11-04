"""日次定点観測ランナー。

TRA-11 monitoring-watchlist の8項目 (2026-11-04改訂: 1-4追加、2-1は日次/月次分割) を
一次情報で測り、閾値比較の結果を stdout に、全実測値を reports/monitoring/daily.csv に残す。
候補の採否・閾値変更はしない（判定基準はこのファイルに書かず monitoring-watchlist を単純に転記する）。
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time

from tbf.http import UrllibHttp
from tbf.monitor import items as it
from tbf.onchain.rpc import Rpc, RpcError

MAINNET_RPC = "https://eth.drpc.org"  # 過去ブロックへの eth_call 用 (無料アーカイブ)
MAINNET_LOGS_RPC = "https://rpc.mevblocker.io"  # batch / eth_getLogs 用
ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"
BASE_RPC = "https://base-rpc.publicnode.com"

# TRA-19: eth_getLogs がレート制限 (403/408/429) で落ちた先に備えて、チェーンごとに複数のRPCをローテーションする。
# TRA-13 の申し送りで「10,000ブロック幅の eth_getLogs が通る」と確認済みの先。
LOGS_RPC_CANDIDATES: dict[str, list[str]] = {
    "ethereum": [
        "https://ethereum-rpc.publicnode.com",
        "https://rpc.flashbots.net",
        "https://rpc.mevblocker.io",
        "https://0xrpc.io/eth",
    ],
    "arbitrum": [
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum.publicnode.com",
        "https://1rpc.io/arb",
    ],
    "base": [
        "https://mainnet.base.org",
        "https://base-rpc.publicnode.com",
        "https://1rpc.io/base",
    ],
}

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "reports", "monitoring", "daily.csv")
UNIVERSE_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "reports", "monitoring", "hl_perp_universe.json"
)
RUN_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "reports", "monitoring", "run_log.json")
REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
STATE_FILES = [
    "reports/monitoring/hl_perp_universe.json",
    "reports/monitoring/run_log.json",
    "reports/monitoring/daily.csv",
]
COMMIT_STATE_ITEM_ID = "_state-commit"
# この自動コミットを実行するのは定点観測係。チェックアウト共有の .git/config は
# 別エージェント名で固定されているため、ここで著者を明示する。
COMMIT_AUTHOR_NAME = "定点観測係 (Paperclip agent)"
COMMIT_AUTHOR_EMAIL = "agent+monitor@users.noreply.github.com"

# TRA-11 monitoring-watchlist のトリガー値 (このファイルでは変更しない。変更は文書のオーナーの仕事)
AAVE_BASELINE_7D = {"ethereum": 67, "arbitrum": 76, "base": 88}  # Day-1 (2026-11-03) 実測
AAVE_SPIKE_MULTIPLIER = 2.0
AAVE_WINDOW_FRAC_THRESHOLD = 0.20  # window >= 5 blocks の案件が全体の20%以上
AAVE_WINDOW_NET_USD_THRESHOLD = 500.0  # 該当案件 (window>=5) の純額合計が月$500以上
ETH_REALIZED_VOL_THRESHOLD = 0.40  # 30日実現ボラ < 40.0% で発火


def _chain_rpcs(http: UrllibHttp) -> dict[str, Rpc]:
    return {
        "ethereum": Rpc(http, MAINNET_LOGS_RPC),
        "arbitrum": Rpc(http, ARBITRUM_RPC),
        "base": Rpc(http, BASE_RPC),
    }


BLOCK_TIME_SEC = {"ethereum": 12.0, "arbitrum": 0.25, "base": 2.0}


def _is_first_of_month(date_utc: str) -> bool:
    return date_utc.endswith("-01")


def load_universe_snapshot() -> dict | None:
    if not os.path.exists(UNIVERSE_SNAPSHOT_PATH):
        return None
    with open(UNIVERSE_SNAPSHOT_PATH) as f:
        return json.load(f)


def save_universe_snapshot(as_of_iso: str, coins: set[str]) -> None:
    os.makedirs(os.path.dirname(UNIVERSE_SNAPSHOT_PATH), exist_ok=True)
    with open(UNIVERSE_SNAPSHOT_PATH, "w") as f:
        json.dump({"asOf": as_of_iso, "coins": sorted(coins)}, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_run_log() -> dict:
    if not os.path.exists(RUN_LOG_PATH):
        return {}
    with open(RUN_LOG_PATH) as f:
        return json.load(f)


def save_run_log(log: dict) -> None:
    os.makedirs(os.path.dirname(RUN_LOG_PATH), exist_ok=True)
    with open(RUN_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
        f.write("\n")


def record_item_outcome(run_log: dict, item_id: str, date_utc: str, ok: bool) -> bool:
    """run_log を更新し、この項目が「2日連続で未測定」になったかどうかを返す。"""
    entry = run_log.get(item_id, {"consecutive_fails": 0, "last_fail_date": None})
    if ok:
        entry["consecutive_fails"] = 0
        entry["last_fail_date"] = None
        run_log[item_id] = entry
        return False
    prev_fail_date = entry.get("last_fail_date")
    is_consecutive = prev_fail_date is not None and _prev_day(date_utc) == prev_fail_date
    entry["consecutive_fails"] = (entry.get("consecutive_fails", 0) + 1) if is_consecutive else 1
    entry["last_fail_date"] = date_utc
    run_log[item_id] = entry
    return entry["consecutive_fails"] >= 2


def _prev_day(date_utc: str) -> str:
    d = dt.date.fromisoformat(date_utc)
    return (d - dt.timedelta(days=1)).isoformat()


def commit_monitoring_state(date_utc: str) -> tuple[bool, str]:
    """reports/monitoring の状態ファイル3本を1runあたり1コミットでローカルに記録する。pushはしない。

    失敗しても例外を投げない（監視結果の出力を中断させないため）。
    """
    try:
        subprocess.run(
            ["git", "add", *STATE_FILES],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        )
        # pathspec を付けて index を迂回する。同じチェックアウトで別の作業が
        # ステージされていても、それを監視コミットに巻き込まないため。
        # 著者は -c で run ごとに上書きする。チェックアウトの .git/config は
        # 全エージェント共有なので、そちらは書き換えない。
        res = subprocess.run(
            [
                "git",
                "-c", f"user.name={COMMIT_AUTHOR_NAME}",
                "-c", f"user.email={COMMIT_AUTHOR_EMAIL}",
                "commit", "-m", f"monitor: {date_utc} 定点観測ログ", "--", *STATE_FILES,
            ],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if res.returncode != 0:
            combined = (res.stdout + res.stderr)
            if "nothing to commit" in combined:
                return True, "変更なし（コミット不要）"
            # 上の git add でステージした分を共有 index に残さない。
            # 残すと、別エージェントの pathspec なし commit に紛れ込む。
            subprocess.run(
                ["git", "reset", "-q", "--", *STATE_FILES],
                cwd=REPO_ROOT, capture_output=True, text=True,
            )
            # git が無言で失敗することがある（例: フックによる拒否）ので終了コードを残す。
            return False, f"git commit exit={res.returncode} {combined.strip()[:500]}".strip()
        return True, res.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


async def aave_liquidation_window_with_rotation(
    http: UrllibHttp, chain: str, now_ms: int, block_time_sec: float
) -> it.ItemResult:
    """チェーンごとに複数RPCをローテーションし、429/408 ではブロック幅を縮めて指数バックオフで再試行する。

    全RPC/全ブロック幅で失敗した場合は ok=False (未測定) を返す。ゼロ埋めはしない。
    """
    item_id = f"2-1-aave-liq-{chain}"
    pool = it.AAVE_POOL[chain]
    candidates = LOGS_RPC_CANDIDATES[chain]
    attempts_log: list[str] = []

    for rpc_url in candidates:
        rpc = Rpc(http, rpc_url)
        try:
            latest = int(await rpc.call("eth_blockNumber"), 16)
        except Exception as e:  # noqa: BLE001
            attempts_log.append(f"{rpc_url}: eth_blockNumber失敗 {type(e).__name__}: {e}")
            continue

        span_blocks = int(7 * 86_400 / block_time_sec)
        from_block = latest - span_blocks

        for width in (9_990, 2_000):
            logs: list[dict] = []
            ok = True
            cursor = from_block
            while cursor < latest:
                to = min(cursor + width - 1, latest)
                backoff = 1.0
                for attempt in range(3):
                    try:
                        res = await rpc.call(
                            "eth_getLogs",
                            [{
                                "address": pool,
                                "topics": [it.LIQUIDATION_CALL_TOPIC],
                                "fromBlock": hex(cursor),
                                "toBlock": hex(to),
                            }],
                        )
                        break
                    except RpcError as e:
                        msg = str(e)
                        is_rate_limited = any(code in msg for code in ("403", "408", "429"))
                        attempts_log.append(f"{rpc_url} width={width} [{cursor}:{to}] attempt={attempt}: {msg[:160]}")
                        if attempt == 2 or not is_rate_limited:
                            ok = False
                            res = None
                            break
                        await asyncio.sleep(backoff)
                        backoff *= 2
                if not ok or res is None:
                    break
                logs.extend(res)
                cursor = to + 1
            if ok:
                return it.ItemResult(
                    item_id,
                    True,
                    value=float(len(logs)),
                    unit="liquidation_count_7d",
                    note=(
                        f"from_block={from_block} to_block={latest} width={width} rpc={rpc_url} "
                        "(7日窓のみ。月次44件サンプルの window≥5ブロック統計は未実施)"
                    ),
                    endpoint=f"{chain} eth_getLogs LiquidationCall {pool}",
                )
            # この幅で失敗したら次の幅を試す (同じRPC)

    return it.ItemResult(
        item_id,
        False,
        error=(
            f"未測定: 全RPC候補 ({', '.join(candidates)}) で eth_getLogs が失敗。詳細: "
            + " | ".join(attempts_log[-6:])
        ),
        endpoint=f"{chain} eth_getLogs LiquidationCall {pool}",
    )


async def run(fake_date_utc: str | None = None) -> tuple[list[it.ItemResult], list[str]]:
    """実測値のリストと、発火した項目の説明行のリストを返す。"""
    http = UrllibHttp()
    now_ms = int(time.time() * 1000)
    date_utc = fake_date_utc or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    rpc_eth = Rpc(http, MAINNET_RPC)
    rpc_eth_logs = Rpc(http, MAINNET_LOGS_RPC)
    rpc_arb = Rpc(http, ARBITRUM_RPC)
    rpc_base = Rpc(http, BASE_RPC)
    liq_rpc = _chain_rpcs(http)

    results: list[it.ItemResult] = []
    fired: list[str] = []
    escalations: list[str] = []
    run_log = load_run_log()

    for coin in ("BTC", "ETH", "HYPE"):
        results.append(await it.hl_funding_30d_7d(http, coin, now_ms))

    results.append(await it.susde_realized_yield_30d(rpc_eth, rpc_eth, now_ms))
    results.append(await it.usde_peg_dev(rpc_eth))

    results.append(await it.gas_median_7d(rpc_eth_logs, "ethereum", now_ms, 12.0))
    results.append(await it.gas_median_7d(rpc_base, "base", now_ms, 2.0))
    results.append(await it.gas_median_7d(rpc_arb, "arbitrum", now_ms, 0.25))

    # 1-4 ETH実現ボラ (2026-11-04 追加)
    vol_result = await it.eth_realized_vol_30d(http, now_ms)
    results.append(vol_result)
    if not vol_result.ok:
        fired.append(f"1-4 ETH実現ボラ: 取得失敗 ({vol_result.error})。発火判定なし。再測定はこのrunでは行わない")
    elif vol_result.value is not None and vol_result.value < ETH_REALIZED_VOL_THRESHOLD:
        fired.append(
            f"1-4 ETH実現ボラ: 閾値{ETH_REALIZED_VOL_THRESHOLD:.1%}を下回った。測定値 {vol_result.value:.4f} "
            f"({vol_result.note})。A/|B|の再測定はこのrunでは行わない"
        )

    # 2-1 日次: 3チェーンの直近7日 LiquidationCall 件数 (RPCローテーション + 幅縮小 + 指数バックオフ)
    daily_counts: dict[str, it.ItemResult] = {}
    spike_chains: list[str] = []
    for chain in ("ethereum", "arbitrum", "base"):
        r = await aave_liquidation_window_with_rotation(http, chain, now_ms, BLOCK_TIME_SEC[chain])
        results.append(r)
        daily_counts[chain] = r
        if not r.ok:
            two_day_fail = record_item_outcome(run_log, r.item_id, date_utc, ok=False)
            fired.append(f"2-1 {chain} 清算件数(直近7日): {r.error}")
            if two_day_fail:
                escalations.append(
                    f"監視項目: 2-1 {chain} 清算件数(直近7日) / 実測値: 未測定（2日連続） / "
                    f"取得元: {r.endpoint} / 覆る決定: 当該チェーンの清算件数監視が常時未測定化している可能性。"
                    "RPC候補の見直しが必要かリーダーが判断する"
                )
            continue
        record_item_outcome(run_log, r.item_id, date_utc, ok=True)
        if r.value is not None:
            baseline = AAVE_BASELINE_7D[chain]
            threshold = baseline * AAVE_SPIKE_MULTIPLIER
            if r.value > threshold:
                spike_chains.append(chain)
                fired.append(
                    f"2-1 {chain} 清算件数(直近7日): {int(r.value)}件 が Day-1基準({baseline}件)の2倍超"
                    f" ({threshold:.0f}件) を超えた。窓幅算出を実施"
                )
                escalations.append(
                    f"監視項目: 2-1 {chain} 清算件数(直近7日) / 実測値: {int(r.value)}件 / "
                    f"取得元: {r.endpoint} ({r.note}) / 覆る決定: B SL-1 (Aave清算ボット) の優先度見直し"
                )

    # 月次 (毎月1日): 全3チェーンで $10,000超の案件の窓幅・純額を算出
    # 日次スパイクでも同じ重い計算を同日に1回走らせる (重複を避けるため first_of_month なら全チェーン、
    # それ以外は spike_chains のみ)
    is_monthly = _is_first_of_month(date_utc)
    chains_to_measure = set(spike_chains)
    if is_monthly:
        chains_to_measure |= {"ethereum", "arbitrum", "base"}

    if chains_to_measure:
        all_cases: list[it.LiquidationCase] = []
        for chain in sorted(chains_to_measure):
            cases, window_result = await it.aave_liquidation_cases_30d(liq_rpc[chain], chain, now_ms, BLOCK_TIME_SEC[chain])
            results.append(window_result)
            if window_result.ok:
                all_cases.extend(cases)
            else:
                fired.append(f"2-1 {chain} 窓幅算出: 取得失敗 ({window_result.error})")

        if is_monthly:
            qualifying = [c for c in all_cases if c.window_blocks is not None]
            unresolved = [c for c in all_cases if c.window_blocks is None]
            total = len(all_cases)
            if total == 0:
                fired.append("2-1 月次窓幅: 対象期間に$10,000超のLiquidationCallなし（全チェーン合算）")
            else:
                ge5 = [c for c in qualifying if c.window_blocks >= 5 or c.window_is_lower_bound]
                frac_ge5 = len(ge5) / total
                net_total_ge5 = sum(c.net_usd for c in ge5)
                monthly_note = (
                    f"total_cases_30d={total} window>=5blocks={len(ge5)} frac={frac_ge5:.4f} "
                    f"net_total_usd(window>=5)={net_total_ge5:.2f} window_unresolved={len(unresolved)}"
                )
                results.append(
                    it.ItemResult(
                        "2-1-monthly-window-aggregate",
                        True,
                        value=frac_ge5,
                        unit="frac_window_ge5_blocks",
                        note=monthly_note,
                        endpoint="aave_liquidation_cases_30d (3チェーン合算)",
                    )
                )
                if frac_ge5 >= AAVE_WINDOW_FRAC_THRESHOLD and net_total_ge5 >= AAVE_WINDOW_NET_USD_THRESHOLD:
                    fired.append(
                        f"2-1 月次窓幅: window>=5ブロックが{frac_ge5:.1%}（閾値{AAVE_WINDOW_FRAC_THRESHOLD:.0%}以上）"
                        f" かつ 純額合計${net_total_ge5:,.2f}（閾値${AAVE_WINDOW_NET_USD_THRESHOLD:,.0f}以上）。"
                        f"B SL-1を差し戻して再検討すべき水準。{monthly_note}"
                    )
                else:
                    fired.append(
                        f"2-1 月次窓幅: 閾値内。{monthly_note}"
                    )

    results.append(await it.steth_depeg(rpc_eth))

    # 2-3 新規perp上場: coin名の**集合**で前回スナップショットと比較する (位置/indexでの比較は禁止)
    try:
        current_names = await it.hl_perp_universe_names(http)
        snapshot = load_universe_snapshot()
        prev_names = set(snapshot["coins"]) if snapshot else None
        if prev_names is None:
            results.append(
                it.ItemResult(
                    "2-3-hl-universe", True, value=float(len(current_names)), unit="coin_count",
                    note="前回スナップショットなし。今回の取得を基準として保存する",
                    endpoint=f"{it.INFO_URL} type=meta",
                )
            )
        else:
            new_coins = current_names - prev_names
            delisted_coins = prev_names - current_names
            results.append(
                it.ItemResult(
                    "2-3-hl-universe", True, value=float(len(current_names)), unit="coin_count",
                    note=(
                        f"prev_asOf={snapshot['asOf']} prev_count={len(prev_names)} "
                        f"new={sorted(new_coins)} delisted={sorted(delisted_coins)}"
                    ),
                    endpoint=f"{it.INFO_URL} type=meta",
                )
            )
            for coin in sorted(delisted_coins):
                results.append(
                    it.ItemResult(
                        f"2-3-delisted-{coin}", True, value=0.0, unit="delisted",
                        note="universeから消滅（上場廃止）。判定不要、記録のみ", endpoint=f"{it.INFO_URL} type=meta",
                    )
                )
            for coin in sorted(new_coins):
                fired.append(f"2-3 新規perp上場: {coin} を検知。現物ヘッジ実在と初回ファンディング率を測定")
                hedge_r = await it.hl_spot_hedge_check(http, coin)
                results.append(hedge_r)
                funding_r = await it.hl_first_funding_rate(http, coin, now_ms)
                results.append(funding_r)
                escalations.append(
                    f"監視項目: 2-3 新規perp上場 {coin} / 実測値: hedge_contract_identified={hedge_r.value} "
                    f"({hedge_r.note}); first_funding_rate={funding_r.value if funding_r.ok else funding_r.error} / "
                    f"取得元: {hedge_r.endpoint}; {funding_r.endpoint} / "
                    "覆る決定: 新規coinでのファンディング受け (B SL-2) 着手の採否はリーダーが判断する"
                )
        save_universe_snapshot(dt.datetime.now(dt.timezone.utc).isoformat(), current_names)
    except Exception as e:  # noqa: BLE001
        results.append(it.ItemResult("2-3-hl-universe", False, error=f"{type(e).__name__}: {e}", endpoint=f"{it.INFO_URL} type=meta"))

    results.append(await it.derive_api_status(http))

    save_run_log(run_log)
    return results, fired, escalations


def append_csv(results: list[it.ItemResult], date_utc: str, run_ts_utc: str) -> None:
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["date_utc", "item_id", "value", "unit", "source", "note", "run_ts_utc"])
        for r in results:
            if r.ok:
                w.writerow([date_utc, r.item_id, r.value, r.unit, r.endpoint, r.note, run_ts_utc])


def main() -> None:
    fake_date = None
    if "--fake-date" in sys.argv:
        fake_date = sys.argv[sys.argv.index("--fake-date") + 1]

    run_ts_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    results, fired, escalations = asyncio.run(run(fake_date))
    date_utc = fake_date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if fake_date is None:
        append_csv(results, date_utc, run_ts_utc)

    n_items = len({r.item_id.split("-monthly-")[0] for r in results})
    print(f"=== {date_utc} 定点観測{' (ドライラン: CSV書き込みなし)' if fake_date else ''} ===")
    if fired:
        for line in fired:
            print(line)
    else:
        print(f"{date_utc} 定点観測: 全{n_items}項目 閾値内")

    if escalations:
        print("--- 【上申候補】リーダーへのissue起票が必要 (このrunでは最大1本にまとめる) ---")
        for line in escalations:
            print(line)

    print("--- 実測値の内訳 ---")
    for r in results:
        status = "OK" if r.ok else "FAIL"
        print(f"[{status}] {r.item_id} value={r.value} unit={r.unit} note={r.note} error={r.error} endpoint={r.endpoint}")

    if fake_date is None:
        commit_ok, commit_msg = commit_monitoring_state(date_utc)
        run_log = load_run_log()
        two_run_fail = record_item_outcome(run_log, COMMIT_STATE_ITEM_ID, date_utc, ok=commit_ok)
        save_run_log(run_log)
        if not commit_ok:
            print(f"--- 記録未保全: reports/monitoring の状態ファイルのコミットに失敗 ({commit_msg}) ---")
            if two_run_fail:
                print("--- 【上申候補】記録未保全が2run連続。リーダー宛てissue1本で上げる対象 ---")
                print(
                    "監視項目: 定点観測ログの保全 / 実測値: 記録未保全が2run連続 / "
                    "取得元: git commit (ローカル、未push) / "
                    "覆る決定: 保全ルーチンの見直しが必要かリーダーが判断する"
                )


if __name__ == "__main__":
    main()
