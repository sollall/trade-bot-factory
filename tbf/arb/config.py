from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ChainConfig:
    name: str
    chain_id: int
    rpc: str
    quoter: str
    fee_tier: int
    tokens: dict[str, str]
    extra_fee_usd: float = 0.0
    # 起動時に check で埋める
    decimals: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ArbConfig:
    chains: list[ChainConfig]
    base: str
    quote: str
    sizes: list[float]
    poll_interval_sec: float = 3.0
    min_profit_usd: float = 1.0
    rebalance_bps: float = 1.0
    min_persist_polls: int = 2


def load_config(path: str | Path) -> ArbConfig:
    raw = tomllib.loads(Path(path).read_text())
    pair = raw["pair"]
    chains = []
    for c in raw["chains"]:
        rpc = os.environ.get(f"ARB_RPC_{c['name'].upper()}", c["rpc"])
        for sym in (pair["base"], pair["quote"]):
            if sym not in c["tokens"]:
                raise ValueError(f"{c['name']}: token {sym} is not configured")
        chains.append(
            ChainConfig(
                name=c["name"],
                chain_id=int(c["chain_id"]),
                rpc=rpc,
                quoter=c["quoter"],
                fee_tier=int(c["fee_tier"]),
                tokens=dict(c["tokens"]),
                extra_fee_usd=float(c.get("extra_fee_usd", 0)),
            )
        )
    if len(chains) < 2:
        raise ValueError("at least 2 chains are required")
    return ArbConfig(
        chains=chains,
        base=pair["base"],
        quote=pair["quote"],
        sizes=[float(s) for s in pair["sizes"]],
        poll_interval_sec=float(raw.get("poll_interval_sec", 3.0)),
        min_profit_usd=float(raw.get("min_profit_usd", 1.0)),
        rebalance_bps=float(raw.get("rebalance_bps", 1.0)),
        min_persist_polls=int(raw.get("min_persist_polls", 2)),
    )
