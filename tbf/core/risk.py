from __future__ import annotations

from typing import Optional

from tbf.core.bot import Risk
from tbf.core.types import Fill


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def check_risk(
    risk: Risk, *, equity: float, peak_equity: float, day_start_equity: float, consecutive_losses: int
) -> Optional[str]:
    """上限違反があれば理由を返す"""
    dd = 1 - equity / peak_equity if peak_equity > 0 else 0.0
    if risk.max_drawdown is not None and dd >= risk.max_drawdown:
        return f"drawdown {_pct(dd)} >= limit {_pct(risk.max_drawdown)}"
    if risk.max_daily_loss is not None and day_start_equity > 0:
        loss = 1 - equity / day_start_equity
        if loss >= risk.max_daily_loss:
            return f"daily loss {_pct(loss)} >= limit {_pct(risk.max_daily_loss)}"
    if risk.max_consecutive_losses is not None and consecutive_losses >= risk.max_consecutive_losses:
        return f"consecutive losses {consecutive_losses} >= limit {risk.max_consecutive_losses}"
    return None


def next_consecutive_losses(current: int, fill: Fill) -> int:
    if fill.side != "sell":
        return current
    return 0 if fill.realized_pnl > 0 else current + 1
