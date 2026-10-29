"""デルタニュートラル戦略。ctx を受け取り、目標の建玉比率 0..1 (None なら現状維持) を返す"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from tbf.neutral.data import FundingPoint

HOURS_PER_YEAR = 24 * 365


@dataclass
class NeutralContext:
    time: int
    # 古い順。末尾が直近に確定したファンディング
    funding: list[FundingPoint]
    # 現物価格 (1 時間足の終値) の履歴
    closes: list[float]
    # 現在の建玉比率 (0 = ノーポジ)
    exposure: float
    # 今の建玉を持ってからの経過時間
    hours_in_position: int
    params: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)

    def funding_apr(self, hours: int) -> Optional[float]:
        """直近 hours 時間の平均ファンディングを年率 (%) で"""
        if len(self.funding) < hours:
            return None
        w = self.funding[-hours:]
        return sum(f.rate for f in w) / len(w) * HOURS_PER_YEAR * 100


NeutralStrategy = Callable[[NeutralContext], Optional[float]]


def funding_threshold(ctx: NeutralContext) -> Optional[float]:
    """
    直近 lookback_hours の平均ファンディング (年率) が entry_apr 以上で建て、exit_apr 以下で解消。
    建ててから min_hold_hours は解消しない (手数料負けの往復を防ぐ)。
    """
    p = {"lookback_hours": 24, "entry_apr": 10.0, "exit_apr": 2.0, "min_hold_hours": 24, **ctx.params}
    apr = ctx.funding_apr(int(p["lookback_hours"]))
    if apr is None:
        return None
    if ctx.exposure == 0 and apr >= p["entry_apr"]:
        return 1.0
    if ctx.exposure > 0 and apr <= p["exit_apr"] and ctx.hours_in_position >= p["min_hold_hours"]:
        return 0.0
    return None


def always_in(ctx: NeutralContext) -> Optional[float]:
    """比較用: 最初から最後まで建てっぱなし"""
    return 1.0
