"""TRA-11 monitoring-watchlist の7項目を一次情報で測る (read-only)。

数値だけを返す。閾値の判定・文言付けは daily.py 側で行う。
"""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

from tbf.http import Http
from tbf.market.hyperliquid import INFO_URL, _post, fetch_candles, fetch_funding
from tbf.onchain.abi import decode_words, encode_call
from tbf.onchain.rpc import Call, Rpc, RpcError, eth_call

DAY_MS = 86_400_000
ARCHIVE_RPC = "https://eth.drpc.org"

SUSDE = "0x9D39A5DE30e57443BfF2A8307A4256c8797A3497"
USDE_USDC_POOL = "0x02950460e2b9529d0e00284a5fa2d7bdf3fa4d72"
STETH_ETH_POOL = "0xDC24316b9AE028F1497c275EB9192a3Ea0f67022"
AAVE_POOL = {
    "ethereum": "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
    "arbitrum": "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    "base": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
}
LIQUIDATION_CALL_TOPIC = "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286"


@dataclass
class ItemResult:
    item_id: str
    ok: bool
    value: Optional[float] = None
    unit: str = ""
    note: str = ""
    error: str = ""
    endpoint: str = ""


async def hl_funding_30d_7d(http: Http, coin: str, now_ms: int) -> ItemResult:
    try:
        pts = await fetch_funding(http, coin, now_ms - 30 * DAY_MS, now_ms)
        if not pts:
            return ItemResult(f"1-1-{coin}", False, error="no funding points returned", endpoint=INFO_URL)
        ann_30d = sum(p.rate for p in pts) / len(pts) * 24 * 365
        recent7 = [p for p in pts if p.time >= now_ms - 7 * DAY_MS]
        neg_frac_7d = (sum(1 for p in recent7 if p.rate < 0) / len(recent7)) if recent7 else 0.0
        return ItemResult(
            f"1-1-{coin}",
            True,
            value=ann_30d,
            unit="annualized_rate",
            note=f"n={len(pts)} neg_frac_7d={neg_frac_7d:.4f} n7={len(recent7)}",
            endpoint=f"{INFO_URL} fundingHistory coin={coin}",
        )
    except Exception as e:  # noqa: BLE001
        return ItemResult(f"1-1-{coin}", False, error=f"{type(e).__name__}: {e}", endpoint=INFO_URL)


MIN_CANDLES_30D = 720  # 30日 * 24本/日。満たない場合はゼロ埋め/期間短縮せず取得失敗として報告する


async def eth_realized_vol_30d(http: Http, now_ms: int) -> ItemResult:
    """HL の ETH 1時間足終値から直近30日の年率実現ボラ ( std(log return) * sqrt(8760) ) を出す。

    プール価格の代用（乖離は TRA-14 表E で実測済み: 90日2,160時点で中位-0.045%/σ0.163%）。
    720本 (30日) に満たない場合は代用・短縮せず取得失敗として返す。
    """
    try:
        # fetch_candles は確定足だけ返す (t + tf <= end)。end=now_ms だと直近の未確定分1本が
        # 落ちて 719 本になるため、end を1時間分広げて直近の確定足を含める。
        candles = await fetch_candles(http, "ETH", "1h", now_ms - 30 * DAY_MS, now_ms + 60 * 60_000)
        if len(candles) < MIN_CANDLES_30D:
            return ItemResult(
                "1-4-eth-realized-vol",
                False,
                error=f"取得失敗: 1時間足が{MIN_CANDLES_30D}本に届かない (n={len(candles)})",
                endpoint=f"{INFO_URL} candleSnapshot coin=ETH interval=1h",
            )
        closes = [c.close for c in candles]
        log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(log_returns) < 2:
            return ItemResult(
                "1-4-eth-realized-vol",
                False,
                error=f"取得失敗: 対数収益の点数が不足 (n={len(log_returns)})",
                endpoint=f"{INFO_URL} candleSnapshot coin=ETH interval=1h",
            )
        realized_vol = statistics.stdev(log_returns) * math.sqrt(8760)
        return ItemResult(
            "1-4-eth-realized-vol",
            True,
            value=realized_vol,
            unit="annualized_vol",
            note=f"n_candles={len(candles)} n_returns={len(log_returns)} (HL ETH 1h代用、プール価格ではない)",
            endpoint=f"{INFO_URL} candleSnapshot coin=ETH interval=1h",
        )
    except Exception as e:  # noqa: BLE001
        return ItemResult(
            "1-4-eth-realized-vol", False, error=f"{type(e).__name__}: {e}",
            endpoint=f"{INFO_URL} candleSnapshot coin=ETH interval=1h",
        )


async def block_number(rpc: Rpc) -> int:
    return int(await rpc.call("eth_blockNumber"), 16)


async def susde_convert_to_assets(rpc: Rpc, block_tag: str) -> int:
    data = encode_call("convertToAssets(uint256)", ["uint256"], [10**18])
    res = await rpc.batch([Call("eth_call", [{"to": SUSDE, "data": data}, block_tag])])
    r = res[0]
    if isinstance(r, RpcError) or r is None:
        raise RuntimeError(str(r))
    return decode_words(r)[0]


async def susde_realized_yield_30d(rpc_latest: Rpc, rpc_archive: Rpc, now_ms: int) -> ItemResult:
    try:
        latest_block = await block_number(rpc_latest)
        # Ethereum 平均 ~12s/block
        blocks_30d = int(30 * DAY_MS / 1000 / 12)
        past_block = latest_block - blocks_30d
        now_assets = await susde_convert_to_assets(rpc_latest, "latest")
        past_assets = await susde_convert_to_assets(rpc_archive, hex(past_block))
        ratio = now_assets / past_assets
        apr = ratio ** (365 / 30) - 1
        return ItemResult(
            "1-2-susde-yield",
            True,
            value=apr,
            unit="annualized_rate",
            note=f"block_now={latest_block} block_30d_ago={past_block}",
            endpoint=f"eth_call {SUSDE} convertToAssets",
        )
    except Exception as e:  # noqa: BLE001
        return ItemResult("1-2-susde-yield", False, error=f"{type(e).__name__}: {e}", endpoint=f"eth_call {SUSDE}")


async def usde_peg_dev(rpc: Rpc) -> ItemResult:
    try:
        amt = 10_000 * 10**18  # USDe (index 0) 18 decimals, $10,000 相当
        data = encode_call("get_dy(int128,int128,uint256)", ["uint256", "uint256", "uint256"], [0, 1, amt])
        res = await rpc.batch([Call("eth_call", [{"to": USDE_USDC_POOL, "data": data}, "latest"])])
        r = res[0]
        if isinstance(r, RpcError) or r is None:
            raise RuntimeError(str(r))
        dy_usdc = decode_words(r)[0] / 10**6
        dev = dy_usdc / 10_000 - 1
        return ItemResult(
            "1-2-usde-peg",
            True,
            value=dev,
            unit="deviation",
            note="get_dy(0,1,10000e18) on Curve USDe/USDC pool",
            endpoint=f"eth_call {USDE_USDC_POOL} get_dy",
        )
    except Exception as e:  # noqa: BLE001
        return ItemResult("1-2-usde-peg", False, error=f"{type(e).__name__}: {e}", endpoint=f"eth_call {USDE_USDC_POOL}")


async def gas_median_7d(rpc: Rpc, chain: str, now_ms: int, block_time_sec: float) -> ItemResult:
    try:
        latest = await block_number(rpc)
        span_blocks = int(7 * DAY_MS / 1000 / block_time_sec)
        samples = 20
        step = max(1, span_blocks // samples)
        calls = [Call("eth_getBlockByNumber", [hex(max(0, latest - i * step)), False]) for i in range(samples)]
        res = await rpc.batch(calls)
        base_fees = []
        for r in res:
            if isinstance(r, RpcError) or r is None:
                continue
            bf = r.get("baseFeePerGas")
            if bf is not None:
                base_fees.append(int(bf, 16) / 1e9)
        if not base_fees:
            return ItemResult(f"1-3-gas-{chain}", False, error="no baseFeePerGas samples", endpoint="eth_getBlockByNumber")
        return ItemResult(
            f"1-3-gas-{chain}",
            True,
            value=statistics.median(base_fees),
            unit="gwei",
            note=f"n={len(base_fees)} span_blocks~={span_blocks}",
            endpoint=f"{chain} eth_getBlockByNumber x{len(base_fees)}",
        )
    except Exception as e:  # noqa: BLE001
        return ItemResult(f"1-3-gas-{chain}", False, error=f"{type(e).__name__}: {e}", endpoint="eth_getBlockByNumber")


async def aave_liquidation_window(rpc: Rpc, chain: str, now_ms: int, block_time_sec: float) -> ItemResult:
    try:
        pool = AAVE_POOL[chain]
        latest = await block_number(rpc)
        span_blocks = int(7 * DAY_MS / 1000 / block_time_sec)
        from_block = latest - span_blocks
        logs: list[dict] = []
        cursor = from_block
        while cursor < latest:
            to = min(cursor + 9_990, latest)
            for attempt in range(4):
                try:
                    res = await rpc.call(
                        "eth_getLogs",
                        [{"address": pool, "topics": [LIQUIDATION_CALL_TOPIC], "fromBlock": hex(cursor), "toBlock": hex(to)}],
                    )
                    break
                except RpcError:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1.5 * (attempt + 1))
            logs.extend(res)
            cursor = to + 1
        return ItemResult(
            f"2-1-aave-liq-{chain}",
            True,
            value=float(len(logs)),
            unit="liquidation_count_7d",
            note=f"from_block={from_block} to_block={latest} (7日窓のみ。月次44件サンプルの window≥5ブロック統計は未実施)",
            endpoint=f"{chain} eth_getLogs LiquidationCall {pool}",
        )
    except Exception as e:  # noqa: BLE001
        return ItemResult(f"2-1-aave-liq-{chain}", False, error=f"{type(e).__name__}: {e}", endpoint=f"{chain} eth_getLogs {AAVE_POOL.get(chain,'')}")


def _addr_from_word(word: int) -> str:
    return "0x" + format(word, "040x")


def _addr_from_topic(topic: str) -> str:
    return "0x" + topic[-40:]


async def _eth_call_1(rpc: Rpc, to: str, data: str, block_tag: str = "latest"):
    res = await rpc.batch([Call("eth_call", [{"to": to, "data": data}, block_tag])])
    r = res[0]
    if isinstance(r, RpcError) or r is None:
        raise RuntimeError(str(r))
    return r


_ORACLE_CACHE: dict[str, str] = {}
_DECIMALS_CACHE: dict[str, int] = {}


async def aave_oracle_address(rpc: Rpc, pool: str) -> str:
    if pool in _ORACLE_CACHE:
        return _ORACLE_CACHE[pool]
    provider_word = decode_words(await _eth_call_1(rpc, pool, encode_call("ADDRESSES_PROVIDER()", [], [])))[0]
    provider = _addr_from_word(provider_word)
    oracle_word = decode_words(await _eth_call_1(rpc, provider, encode_call("getPriceOracle()", [], [])))[0]
    oracle = _addr_from_word(oracle_word)
    _ORACLE_CACHE[pool] = oracle
    return oracle


async def asset_decimals(rpc: Rpc, asset: str) -> int:
    if asset in _DECIMALS_CACHE:
        return _DECIMALS_CACHE[asset]
    dec = decode_words(await _eth_call_1(rpc, asset, encode_call("decimals()", [], [])))[0]
    _DECIMALS_CACHE[asset] = dec
    return dec


async def asset_price_usd_raw(rpc: Rpc, oracle: str, asset: str) -> int:
    """Aave Oracle (Chainlink準拠、8桁小数が一般的) の生の戻り値"""
    return decode_words(await _eth_call_1(rpc, oracle, encode_call("getAssetPrice(address)", ["address"], [asset])))[0]


@dataclass
class LiquidationCase:
    chain: str
    user: str
    block: int
    debt_usd: float
    collateral_usd: float
    net_usd: float
    window_blocks: Optional[int]
    window_is_lower_bound: bool  # True なら lookback の範囲内で開始ブロックを特定できず、window_blocks は下限


async def _health_factor_at(rpc: Rpc, pool: str, user: str, block: int) -> Optional[int]:
    data = encode_call("getUserAccountData(address)", ["address"], [user])
    res = await rpc.batch([Call("eth_call", [{"to": pool, "data": data}, hex(block)])])
    r = res[0]
    if isinstance(r, RpcError) or r is None:
        return None
    words = decode_words(r)
    return words[5] if len(words) >= 6 else None


async def find_liquidation_trigger_block(
    rpc: Rpc, pool: str, user: str, liquidation_block: int, lookback_blocks: int = 50_000
) -> tuple[Optional[int], bool]:
    """getUserAccountData().healthFactor が 1e18 を割った最初のブロックを二分探索する。

    lookback 内で見つからない場合は lookback の下限ブロックと True (下限フラグ) を返す。
    直前ブロックでも健全 (推定不能) な場合は (None, False) を返す。
    """
    hi = liquidation_block - 1
    lo = max(0, liquidation_block - lookback_blocks)
    hf_lo = await _health_factor_at(rpc, pool, user, lo)
    if hf_lo is not None and hf_lo < 10**18:
        return lo, True
    hf_hi = await _health_factor_at(rpc, pool, user, hi)
    if hf_hi is None:
        return None, False  # 直前ブロックのHFを取得できず判定不能
    if hf_hi >= 10**18:
        return liquidation_block, False  # 1ブロック前は健全 = window=0 (同一ブロックで不健全化→清算)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        hf_mid = await _health_factor_at(rpc, pool, user, mid)
        if hf_mid is not None and hf_mid < 10**18:
            hi = mid
        else:
            lo = mid
    return hi, False


async def aave_liquidation_cases_30d(
    rpc: Rpc, chain: str, now_ms: int, block_time_sec: float, min_usd: float = 10_000.0
) -> tuple[list[LiquidationCase], ItemResult]:
    """直近30日の $min_usd 超の LiquidationCall について窓幅 (清算可能になったブロック〜実清算ブロック) を測る。

    USD換算は debtToCover (返済された負債) と debtAsset の Aave Oracle 価格による。重い処理なので
    月次 (+ 日次で件数が急増した日) にだけ呼ぶこと。
    """
    try:
        pool = AAVE_POOL[chain]
        latest = await block_number(rpc)
        span_blocks = int(30 * DAY_MS / 1000 / block_time_sec)
        from_block = latest - span_blocks
        logs: list[dict] = []
        cursor = from_block
        while cursor < latest:
            to = min(cursor + 9_990, latest)
            for attempt in range(4):
                try:
                    res = await rpc.call(
                        "eth_getLogs",
                        [{"address": pool, "topics": [LIQUIDATION_CALL_TOPIC], "fromBlock": hex(cursor), "toBlock": hex(to)}],
                    )
                    break
                except RpcError:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1.5 * (attempt + 1))
            logs.extend(res)
            cursor = to + 1

        oracle = await aave_oracle_address(rpc, pool)
        cases: list[LiquidationCase] = []
        for log in logs:
            topics = log["topics"]
            collateral_asset = _addr_from_topic(topics[1])
            debt_asset = _addr_from_topic(topics[2])
            user = _addr_from_topic(topics[3])
            data = bytes.fromhex(log["data"][2:] if log["data"].startswith("0x") else log["data"])
            words = [int.from_bytes(data[i : i + 32], "big") for i in range(0, len(data) - len(data) % 32, 32)]
            debt_to_cover, liquidated_collateral_amount = words[0], words[1]
            block = int(log["blockNumber"], 16)

            debt_dec = await asset_decimals(rpc, debt_asset)
            debt_price = await asset_price_usd_raw(rpc, oracle, debt_asset)
            debt_usd = (debt_to_cover / 10**debt_dec) * (debt_price / 10**8)
            if debt_usd <= min_usd:
                continue

            collateral_dec = await asset_decimals(rpc, collateral_asset)
            collateral_price = await asset_price_usd_raw(rpc, oracle, collateral_asset)
            collateral_usd = (liquidated_collateral_amount / 10**collateral_dec) * (collateral_price / 10**8)
            net_usd = collateral_usd - debt_usd

            trigger_block, is_lower_bound = await find_liquidation_trigger_block(rpc, pool, user, block)
            window = None if trigger_block is None else block - trigger_block
            cases.append(
                LiquidationCase(
                    chain=chain,
                    user=user,
                    block=block,
                    debt_usd=debt_usd,
                    collateral_usd=collateral_usd,
                    net_usd=net_usd,
                    window_blocks=window,
                    window_is_lower_bound=is_lower_bound,
                )
            )

        return cases, ItemResult(
            f"2-1-aave-window-{chain}",
            True,
            value=float(len(cases)),
            unit="qualifying_case_count_30d",
            note=f"from_block={from_block} to_block={latest} min_usd={min_usd} total_logs={len(logs)}",
            endpoint=f"{chain} eth_getLogs LiquidationCall {pool} + getUserAccountData 二分探索",
        )
    except Exception as e:  # noqa: BLE001
        return [], ItemResult(
            f"2-1-aave-window-{chain}", False, error=f"{type(e).__name__}: {e}",
            endpoint=f"{chain} eth_getLogs/eth_call {AAVE_POOL.get(chain, '')}",
        )


async def steth_depeg(rpc: Rpc) -> ItemResult:
    try:
        amt = 10_000 * 10**18
        data = encode_call("get_dy(int128,int128,uint256)", ["uint256", "uint256", "uint256"], [1, 0, amt])
        res = await rpc.batch([Call("eth_call", [{"to": STETH_ETH_POOL, "data": data}, "latest"])])
        r = res[0]
        if isinstance(r, RpcError) or r is None:
            raise RuntimeError(str(r))
        dy_eth = decode_words(r)[0] / 10**18
        dev = dy_eth / 10_000 - 1
        return ItemResult(
            "2-2-steth-depeg",
            True,
            value=dev,
            unit="deviation",
            note="get_dy(1,0,10000e18) stETH->ETH on Curve stETH/ETH pool",
            endpoint=f"eth_call {STETH_ETH_POOL} get_dy",
        )
    except Exception as e:  # noqa: BLE001
        return ItemResult("2-2-steth-depeg", False, error=f"{type(e).__name__}: {e}", endpoint=f"eth_call {STETH_ETH_POOL}")


async def hl_universe_snapshot(http: Http) -> ItemResult:
    try:
        meta = await _post(http, {"type": "meta"})
        names = [a["name"] for a in meta["universe"]]
        return ItemResult(
            "2-3-hl-universe",
            True,
            value=float(len(names)),
            unit="coin_count",
            note="Day-1 baseline: 比較対象の前回スナップショットなし。今回の件数を基準として記録",
            endpoint=f"{INFO_URL} type=meta",
        )
    except Exception as e:  # noqa: BLE001
        return ItemResult("2-3-hl-universe", False, error=f"{type(e).__name__}: {e}", endpoint=INFO_URL)


async def derive_api_status(http: Http) -> ItemResult:
    urls = ["https://api.lyra.finance/public/get_instruments", "https://api.derive.xyz/public/get_instruments"]
    statuses = []
    for u in urls:
        try:
            status, _ = await http.request("GET", u)
        except Exception as e:  # noqa: BLE001
            status = f"ERR:{type(e).__name__}"
        statuses.append(f"{u} -> {status}")
    any_200 = any(s.endswith("-> 200") for s in statuses)
    return ItemResult(
        "3-1-derive-api",
        True,
        value=1.0 if any_200 else 0.0,
        unit="any_200",
        note="; ".join(statuses),
        endpoint="lyra.finance / derive.xyz",
    )
