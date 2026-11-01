from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Optional, Union

from tbf.core.instrument import Fees, Instrument, spot
from tbf.core.types import HOUR, TIMEFRAME_MS, Candle, FundingPoint, Mode, Position

# 別名 -> 資産に対する目標の持ち高比率 (+ ロング / - ショート)
Targets = dict[str, float]


@dataclass
class Context:
    """戦略に渡される情報"""

    time: int
    # 別名 -> 確定足 (古い順。末尾が直近の確定足。長さは最大 lookback)
    bars: dict[str, list[Candle]]
    # 別名 -> ファンディング履歴 (先物のみ, 古い順)
    funding_history: dict[str, list[FundingPoint]]
    # 別名 -> 持ち高
    holdings: dict[str, Position]
    # 別名 -> 現在の持ち高比率 (資産比, 符号つき)
    weights: dict[str, float]
    cash: float
    equity: float
    params: dict[str, Any]
    # 実行間で永続化される自由領域 (JSON 化可能な値のみ)
    state: dict[str, Any]
    mode: Mode
    instruments: dict[str, Instrument]
    primary: str
    # bot の先物レバレッジ設定 (現物 + 先物ショートで資金を使い切る比率は leverage / (leverage + 1))
    leverage: float = 1.0

    def neutral_weight(self) -> float:
        """現物ロング + 先物ショートで資金を使い切るときの片側の比率"""
        return self.leverage / (self.leverage + 1)

    # ---- 複数銘柄用 ----
    def series(self, alias: str) -> list[Candle]:
        return self.bars.get(alias, [])

    def closes_of(self, alias: str) -> list[float]:
        return [c.close for c in self.series(alias)]

    def holding(self, alias: str) -> Position:
        return self.holdings.get(alias, Position())

    def weight(self, alias: str) -> float:
        return self.weights.get(alias, 0.0)

    def funding(self, alias: str) -> list[FundingPoint]:
        return self.funding_history.get(alias, [])

    def funding_apr(self, alias: str, hours: int) -> Optional[float]:
        """直近 hours 時間の平均ファンディングを年率 (%) で。データ不足なら None"""
        fs = [f for f in self.funding(alias) if f.time > self.time - hours * HOUR]
        if not fs or fs[0].time > self.time - hours * HOUR + 2 * HOUR:
            return None
        return sum(f.rate for f in fs) / len(fs) * 24 * 365 * 100

    # ---- 1 銘柄 bot 用 (主銘柄) ----
    @property
    def candles(self) -> list[Candle]:
        return self.series(self.primary)

    @property
    def closes(self) -> list[float]:
        return self.closes_of(self.primary)

    @property
    def position(self) -> Position:
        return self.holding(self.primary)


@dataclass(frozen=True)
class Decision:
    """
    1 銘柄なら target_exposure (主銘柄の資産比率)、複数銘柄なら weights を使う。
    weights に書いていない銘柄は 0 (手仕舞い) とみなす。
    """

    target_exposure: Optional[float] = None
    reason: Optional[str] = None
    weights: Optional[Mapping[str, float]] = None


# 戦略は Decision / 数値 (主銘柄の比率) / dict (別名 -> 比率) / None (何もしない) を返す
StrategyResult = Union[Decision, Mapping[str, float], float, int, None]
Strategy = Callable[[Context], StrategyResult]


@dataclass(frozen=True)
class Risk:
    # ピークからの最大ドローダウン (0.2 = 20%)。超えたら停止
    max_drawdown: Optional[float] = 0.25
    # UTC 日次の最大損失率 (日初 equity 比)
    max_daily_loss: Optional[float] = None
    # 連敗数の上限
    max_consecutive_losses: Optional[int] = None
    # 停止時に全ポジションを解消するか
    flatten_on_halt: bool = True


_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class Bot:
    # bots/<id>/ のディレクトリ名と同じにする (a-z, 0-9, _)
    id: str
    timeframe: str
    # 戦略に渡す過去足の本数
    lookback: int
    # バックテスト / ペーパーの初期資金、本番では bot に割り当てる資金 (USD)
    capital: float
    strategy: Strategy
    # 別名 -> 銘柄。1 銘柄の現物 bot なら symbol だけ指定してもよい
    instruments: Mapping[str, Instrument] = field(default_factory=dict)
    symbol: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)
    # どの環境にデプロイするか。paper で検証してから "live" を足す
    deploy: tuple[str, ...] = ("paper",)
    risk: Risk = field(default_factory=Risk)
    # 指定すると全銘柄の手数料をこれで上書き (省略時は取引所ごとの既定値)
    fees: Optional[Fees] = None
    # 目標比率との差がこの比率 (対 equity) 未満なら発注しない
    rebalance_threshold: float = 0.01
    # 先物の証拠金に対する目標レバレッジ (= 先物の想定元本 / 証拠金)
    leverage: float = 3.0
    # 先物口座の実効レバレッジが 目標×band を超える / 目標÷band を下回ったら配分し直す
    rebalance_band: float = 1.5
    # 取引所間で資金を移すコスト (1 回あたり)
    transfer_fee_usd: float = 1.0
    # 配分時に手数料ぶん残しておく現金の比率
    cash_buffer: float = 0.005
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
        if self.leverage <= 0:
            raise ValueError(f"{self.id}: leverage must be > 0")
        insts = dict(self.instruments)
        if self.symbol:
            if insts:
                raise ValueError(f"{self.id}: symbol と instruments はどちらか一方だけ指定してください")
            insts = {self.symbol: spot(self.symbol)}
        if not insts:
            raise ValueError(f"{self.id}: instruments (または symbol) を指定してください")
        if self.fees is not None:
            insts = {a: replace(i, fees=self.fees) for a, i in insts.items()}
        object.__setattr__(self, "instruments", insts)
        object.__setattr__(self, "deploy", tuple(self.deploy))
        for d in self.deploy:
            if d not in ("paper", "live"):
                raise ValueError(f'{self.id}: deploy must contain only "paper" / "live"')

    @property
    def primary(self) -> str:
        return next(iter(self.instruments))

    @property
    def venues(self) -> list[str]:
        return list(dict.fromkeys(i.venue for i in self.instruments.values()))

    def with_params(self, **overrides: Any) -> "Bot":
        return replace(self, params={**self.params, **overrides}, symbol=None)

    def with_settings(self, **fields_: Any) -> "Bot":
        return replace(self, symbol=None, **fields_)


def normalize_targets(bot: Bot, result: StrategyResult) -> tuple[Optional[Targets], Optional[str]]:
    """戦略の戻り値を {別名: 比率} にそろえる。None は「何もしない」"""
    if result is None:
        return None, None
    reason = None
    if isinstance(result, Decision):
        reason = result.reason
        if result.weights is not None:
            raw: Mapping[str, float] = result.weights
        elif result.target_exposure is not None:
            raw = {bot.primary: result.target_exposure}
        else:
            return None, reason
    elif isinstance(result, Mapping):
        raw = result
    else:
        raw = {bot.primary: float(result)}

    unknown = set(raw) - set(bot.instruments)
    if unknown:
        raise ValueError(f"{bot.id}: unknown instrument alias {sorted(unknown)}")
    out: Targets = {}
    for alias, inst in bot.instruments.items():
        w = float(raw.get(alias, 0.0))
        if not math.isfinite(w):
            w = 0.0
        # 現物はロングのみ・レバレッジなし
        out[alias] = min(1.0, max(0.0, w)) if inst.kind == "spot" else max(-bot.leverage, min(bot.leverage, w))
    return out, reason
