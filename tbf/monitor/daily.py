"""日次定点観測ランナー。

TRA-11 monitoring-watchlist の8項目 (2026-11-04改訂: 1-4追加、2-1は日次/月次分割) を
一次情報で測り、閾値比較の結果を stdout に、全実測値を reports/monitoring/daily.csv に残す。
候補の採否・閾値変更はしない（判定基準はこのファイルに書かず monitoring-watchlist を単純に転記する）。
"""

from __future__ import annotations

import asyncio
import csv
import datetime as dt
import os
import sys
import time

from tbf.http import UrllibHttp
from tbf.monitor import items as it
from tbf.onchain.rpc import Rpc

MAINNET_RPC = "https://eth.drpc.org"  # 過去ブロックへの eth_call 用 (無料アーカイブ)
MAINNET_LOGS_RPC = "https://rpc.mevblocker.io"  # batch / eth_getLogs 用
ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"
BASE_RPC = "https://base-rpc.publicnode.com"

CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "reports", "monitoring", "daily.csv")

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

    # 2-1 日次: 3チェーンの直近7日 LiquidationCall 件数
    daily_counts: dict[str, it.ItemResult] = {}
    spike_chains: list[str] = []
    for chain in ("ethereum", "arbitrum", "base"):
        r = await it.aave_liquidation_window(liq_rpc[chain], chain, now_ms, BLOCK_TIME_SEC[chain])
        results.append(r)
        daily_counts[chain] = r
        if r.ok and r.value is not None:
            baseline = AAVE_BASELINE_7D[chain]
            threshold = baseline * AAVE_SPIKE_MULTIPLIER
            if r.value > threshold:
                spike_chains.append(chain)
                fired.append(
                    f"2-1 {chain} 清算件数(直近7日): {int(r.value)}件 が Day-1基準({baseline}件)の2倍超"
                    f" ({threshold:.0f}件) を超えた。窓幅算出を実施"
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
    results.append(await it.hl_universe_snapshot(http))
    results.append(await it.derive_api_status(http))

    return results, fired


def append_csv(results: list[it.ItemResult], date_utc: str) -> None:
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["date_utc", "item_id", "value", "unit", "source", "note"])
        for r in results:
            if r.ok:
                w.writerow([date_utc, r.item_id, r.value, r.unit, r.endpoint, r.note])


def main() -> None:
    fake_date = None
    if "--fake-date" in sys.argv:
        fake_date = sys.argv[sys.argv.index("--fake-date") + 1]

    results, fired = asyncio.run(run(fake_date))
    date_utc = fake_date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if fake_date is None:
        append_csv(results, date_utc)

    n_items = len({r.item_id.split("-monthly-")[0] for r in results})
    print(f"=== {date_utc} 定点観測{' (ドライラン: CSV書き込みなし)' if fake_date else ''} ===")
    if fired:
        for line in fired:
            print(line)
    else:
        print(f"{date_utc} 定点観測: 全{n_items}項目 閾値内")

    print("--- 実測値の内訳 ---")
    for r in results:
        status = "OK" if r.ok else "FAIL"
        print(f"[{status}] {r.item_id} value={r.value} unit={r.unit} note={r.note} error={r.error} endpoint={r.endpoint}")


if __name__ == "__main__":
    main()
