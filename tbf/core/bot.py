from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from tbf.core.types import TIMEFRAME_MS, Candle, Mode, Position


@dataclass
class Context:
    """戦略に渡される情報"""

    # 古い順。末尾が直近の確定足。長さは最大 lookback
    candles: list[Candle]
    position: Position
    cash: float
    equity: float
    params: dict[str, Any]
    # 実行間で永続化される自由領域 (JSON 化可能な値のみ)
    state: dict[str, Any]
    mode: Mode

    @property
    def closes(self) -> list[float]:
        return [c.close for c in self.candles]


@dataclass(frozen=True)
class Decision:
    """target_exposure は資産 (equity) に対する保有比率 0..1"""

    target_exposure: float
    reason: Optional[str] = None


# 戦略は Decision / float (目標比率) / None (何もしない) を返す
StrategyResult = Union[Decision, float, int, None]
Strategy = Callable[[Context], StrategyResult]


@dataclass(frozen=True)
class Risk:
    # ピークからの最大ドローダウン (0.2 = 20%)。超えたら停止
    max_drawdown: Optional[float] = 0.25
    # UTC 日次の最大損失率 (日初 equity 比)
    max_daily_loss: Optional[float] = None
    # 連敗数の上限
    max_consecutive_losses: Optional[int] = None
    # 停止時にポジションを解消するか
    flatten_on_halt: bool = True


@dataclass(frozen=True)
class Fees:
    # 片道手数料率 (0.001 = 0.1%)
    taker_rate: float = 0.001
    # バックテスト / ペーパーでのスリッページ (bps)
    slippage_bps: float = 5.0

    @property
    def cost_rate(self) -> float:
        return self.taker_rate + self.slippage_bps / 10_000


_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class Bot:
    # bots/<id>/ のディレクトリ名と同じにする (a-z, 0-9, _)
    id: str
    # 取引所のシンボル (例: BTCUSDT)
    symbol: str
    timeframe: str
    # 戦略に渡す過去足の本数
    lookback: int
    # バックテスト / ペーパーの初期資金、本番では bot に割り当てる資金 (quote 通貨)
    capital: float
    strategy: Strategy
    params: dict[str, Any] = field(default_factory=dict)
    # どの環境にデプロイするか。paper で検証してから "live" を足す
    deploy: tuple[str, ...] = ("paper",)
    risk: Risk = field(default_factory=Risk)
    fees: Fees = field(default_factory=Fees)
    # 目標比率との差がこの比率 (対 equity) 未満なら発注しない
    rebalance_threshold: float = 0.01
    description: str = ""

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f'invalid bot id "{self.id}" (use a-z, 0-9, _)')
        if self.timeframe not in TIMEFRAME_MS:
            raise ValueError(f"{self.id}: timeframe must be one of {list(TIMEFRAME_MS)}")
        if self.lookback < 1:
            raise ValueError(f"{self.id}: lookback must be >= 1")
        if self.capital <= 0:
            raise ValueError(f"{self.id}: capital must be > 0")
        object.__setattr__(self, "deploy", tuple(self.deploy))
        for d in self.deploy:
            if d not in ("paper", "live"):
                raise ValueError(f'{self.id}: deploy must contain only "paper" / "live"')

    def with_params(self, **overrides: Any) -> "Bot":
        from dataclasses import replace

        return replace(self, params={**self.params, **overrides})


def normalize(result: StrategyResult) -> Optional[Decision]:
    if result is None:
        return None
    if isinstance(result, Decision):
        d = result
    else:
        d = Decision(float(result))
    x = d.target_exposure
    x = 0.0 if not math.isfinite(x) else min(1.0, max(0.0, x))
    return Decision(x, d.reason)
