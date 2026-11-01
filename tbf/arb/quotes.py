"""Uniswap V3 QuoterV2 で各チェーンの売り / 買い戻しの見積もりを取る"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Optional

from tbf.arb.config import ArbConfig, ChainConfig
from tbf.onchain.abi import decode_string, decode_words, encode_call
from tbf.onchain.rpc import Call, Rpc, RpcError, eth_call

_PARAMS = ["address", "address", "uint256", "uint24", "uint160"]
EXACT_IN = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
EXACT_OUT = "quoteExactOutputSingle((address,address,uint256,uint24,uint160))"
# スワップ 1 回のガスに、Quoter の見積もり (プール内部分) 以外に乗る分の概算
SWAP_GAS_OVERHEAD = 60_000


@dataclass(frozen=True)
class SizeQuote:
    size: float
    # base を size 売ったときに受け取る quote 数量 (None は見積もり失敗 = 流動性不足など)
    sell_out: Optional[float]
    # base を size 買うのに必要な quote 数量
    buy_in: Optional[float]
    sell_gas: int
    buy_gas: int


@dataclass(frozen=True)
class ChainQuote:
    chain: str
    block: int
    gas_price_wei: int
    latency_ms: float
    sizes: list[SizeQuote]
    extra_fee_usd: float

    def gas_usd(self, gas: int, eth_usd: float) -> float:
        return (gas + SWAP_GAS_OVERHEAD) * self.gas_price_wei / 1e18 * eth_usd + self.extra_fee_usd


def _calls(c: ChainConfig, cfg: ArbConfig) -> list[Call]:
    base, quote = c.tokens[cfg.base], c.tokens[cfg.quote]
    bd = c.decimals[cfg.base]
    calls = [Call("eth_blockNumber", []), Call("eth_gasPrice", [])]
    for s in cfg.sizes:
        amt = int(round(s * 10**bd))
        calls.append(eth_call(c.quoter, encode_call(EXACT_IN, _PARAMS, [base, quote, amt, c.fee_tier, 0])))
        calls.append(eth_call(c.quoter, encode_call(EXACT_OUT, _PARAMS, [quote, base, amt, c.fee_tier, 0])))
    return calls


async def fetch_chain_quote(rpc: Rpc, c: ChainConfig, cfg: ArbConfig) -> ChainQuote:
    res = await rpc.batch(_calls(c, cfg))
    for r in res[:2]:
        if isinstance(r, RpcError) or r is None:
            raise RpcError(f"{c.name}: {r}")
    qd = c.decimals[cfg.quote]
    sizes = []
    for i, s in enumerate(cfg.sizes):
        sell, buy = res[2 + 2 * i], res[3 + 2 * i]
        sw = decode_words(sell) if isinstance(sell, str) else None
        bw = decode_words(buy) if isinstance(buy, str) else None
        sizes.append(
            SizeQuote(
                size=s,
                sell_out=sw[0] / 10**qd if sw else None,
                buy_in=bw[0] / 10**qd if bw else None,
                sell_gas=sw[3] if sw else 0,
                buy_gas=bw[3] if bw else 0,
            )
        )
    return ChainQuote(c.name, int(res[0], 16), int(res[1], 16), rpc.last_latency_ms, sizes, c.extra_fee_usd)


async def check_chain(rpc: Rpc, c: ChainConfig, cfg: ArbConfig) -> tuple[ChainConfig, list[str]]:
    """設定がチェーン上の実態と合っているか確かめ、decimals を埋めた設定を返す"""
    lines = []
    chain_id = int(await rpc.call("eth_chainId"), 16)
    if chain_id != c.chain_id:
        raise ValueError(f"{c.name}: chain id mismatch (rpc={chain_id}, config={c.chain_id})")
    decimals = {}
    for sym in (cfg.base, cfg.quote):
        addr = c.tokens[sym]
        d_raw, s_raw = await rpc.batch(
            [eth_call(addr, encode_call("decimals()", [], [])), eth_call(addr, encode_call("symbol()", [], []))]
        )
        if not isinstance(d_raw, str) or not isinstance(s_raw, str):
            raise ValueError(f"{c.name}: {sym} at {addr} is not an ERC20 ({d_raw} / {s_raw})")
        decimals[sym] = decode_words(d_raw)[0]
        onchain = decode_string(s_raw)
        if onchain.upper() != sym.upper():
            raise ValueError(f"{c.name}: {addr} symbol is {onchain!r}, expected {sym!r}")
        lines.append(f"{c.name}: {sym} {addr} decimals={decimals[sym]}")
    checked = replace(c, decimals=decimals)
    q = await fetch_chain_quote(rpc, checked, cfg)
    first = q.sizes[0]
    if first.sell_out is None or first.buy_in is None:
        raise ValueError(f"{c.name}: quoter {c.quoter} returned no quote (fee tier {c.fee_tier}?)")
    lines.append(
        f"{c.name}: block {q.block} gas {q.gas_price_wei / 1e9:.4f} gwei rpc {q.latency_ms:.0f}ms "
        f"sell {first.size} -> {first.sell_out:.4f} / buy {first.size} <- {first.buy_in:.4f}"
    )
    return checked, lines


async def check_all(rpcs: dict[str, Rpc], cfg: ArbConfig) -> tuple[ArbConfig, list[str]]:
    results = await asyncio.gather(*(check_chain(rpcs[c.name], c, cfg) for c in cfg.chains))
    return replace(cfg, chains=[r[0] for r in results]), [l for r in results for l in r[1]]
