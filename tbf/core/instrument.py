"""取引する銘柄の定義 (どの取引所の、現物か無期限先物か)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

Kind = Literal["spot", "perp"]


@dataclass(frozen=True)
class Fees:
    # 片道手数料率 (0.001 = 0.1%)
    taker_rate: float = 0.001
    # バックテスト / ペーパーでのスリッページ (bps)
    slippage_bps: float = 5.0

    @property
    def cost_rate(self) -> float:
        """約定代金に上乗せされうるコスト率 (スリッページで価格が上がり、その価格に手数料がかかる)"""
        return (1 + self.slippage_bps / 10_000) * (1 + self.taker_rate) - 1


# 取引所・種類ごとの既定値 (基本ティアの taker 手数料)
DEFAULT_FEES: dict[tuple[str, str], Fees] = {
    ("binance", "spot"): Fees(0.001, 5.0),
    ("hyperliquid", "perp"): Fees(0.00045, 2.0),
    ("hyperliquid", "spot"): Fees(0.0007, 2.0),
}


@dataclass(frozen=True)
class Instrument:
    venue: str
    kind: Kind
    # 取引所での名前 (Binance: BTCUSDT, Hyperliquid: BTC)
    symbol: str
    fees: Optional[Fees] = None
    # 維持証拠金率 (先物のみ)。Hyperliquid は「最大レバレッジの逆数の半分」
    # (例: 最大 25 倍なら 2%、最大 5 倍のアルトなら 10%)
    maintenance_margin: float = 0.02

    def __post_init__(self) -> None:
        if self.kind not in ("spot", "perp"):
            raise ValueError(f"kind must be spot or perp (got {self.kind})")
        if self.fees is None:
            object.__setattr__(self, "fees", DEFAULT_FEES.get((self.venue, self.kind), Fees()))

    @property
    def is_perp(self) -> bool:
        return self.kind == "perp"

    def __str__(self) -> str:
        return f"{self.symbol}:{self.kind}@{self.venue}"


def spot(symbol: str, venue: str = "binance", **kw) -> Instrument:
    return Instrument(venue, "spot", symbol, **kw)


def perp(symbol: str, venue: str = "hyperliquid", **kw) -> Instrument:
    return Instrument(venue, "perp", symbol, **kw)
