from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Sequence

from tbf.core.types import EquityPoint, Fill

YEAR_MS = 365 * 24 * 60 * 60_000


@dataclass(frozen=True)
class Metrics:
    start_equity: float
    end_equity: float
    total_return: float
    max_drawdown: float
    current_drawdown: float
    # 年率換算シャープレシオ (リスクフリー 0)
    sharpe: float
    trades: int
    win_rate: float
    profit_factor: float
    consecutive_losses: int
    fees: float

    def to_dict(self) -> dict:
        d = asdict(self)
        if math.isinf(d["profit_factor"]):
            d["profit_factor"] = None
        return d


def compute_metrics(equity: Sequence[EquityPoint], fills: Sequence[Fill]) -> Metrics:
    start = equity[0].equity if equity else 0.0
    end = equity[-1].equity if equity else start

    peak = -math.inf
    max_dd = cur_dd = 0.0
    for p in equity:
        peak = max(peak, p.equity)
        cur_dd = (peak - p.equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, cur_dd)

    rets = [b.equity / a.equity - 1 for a, b in zip(equity, equity[1:]) if a.equity > 0]
    sharpe = 0.0
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        span = equity[-1].time - equity[0].time
        per_year = len(rets) * YEAR_MS / span if span > 0 else 0.0
        if sd > 0:
            sharpe = mean / sd * math.sqrt(per_year)

    closes = [f for f in fills if f.side == "sell"]
    wins = [f for f in closes if f.realized_pnl > 0]
    gross_win = sum(f.realized_pnl for f in wins)
    gross_loss = -sum(f.realized_pnl for f in closes if f.realized_pnl <= 0)
    streak = 0
    for f in reversed(closes):
        if f.realized_pnl > 0:
            break
        streak += 1

    return Metrics(
        start_equity=start,
        end_equity=end,
        total_return=end / start - 1 if start > 0 else 0.0,
        max_drawdown=max_dd,
        current_drawdown=cur_dd,
        sharpe=sharpe,
        trades=len(closes),
        win_rate=len(wins) / len(closes) if closes else 0.0,
        profit_factor=gross_win / gross_loss if gross_loss > 0 else (math.inf if gross_win > 0 else 0.0),
        consecutive_losses=streak,
        fees=sum(f.fee for f in fills),
    )


def format_metrics(m: Metrics) -> str:
    pct = lambda x: f"{x * 100:.2f}%"  # noqa: E731
    pf = "inf" if math.isinf(m.profit_factor) else f"{m.profit_factor:.2f}"
    return "\n".join(
        [
            f"equity        {m.start_equity:.2f} -> {m.end_equity:.2f}",
            f"total return  {pct(m.total_return)}",
            f"max drawdown  {pct(m.max_drawdown)}",
            f"sharpe        {m.sharpe:.2f}",
            f"trades        {m.trades}",
            f"win rate      {pct(m.win_rate)}",
            f"profit factor {pf}",
            f"fees          {m.fees:.2f}",
        ]
    )
