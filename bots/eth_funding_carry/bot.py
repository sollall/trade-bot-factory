"""
デルタニュートラルのファンディング裁定:
ETH 現物ロング + ETH 無期限先物ショートで値動きを打ち消し、先物のファンディングを受け取る。
直近の平均ファンディング (年率) が entry_apr% 以上で建て、exit_apr% 以下で解消する。
"""

from tbf import Bot, Context, Decision, Risk, perp, spot


def strategy(ctx: Context):
    p = ctx.params
    apr = ctx.funding_apr("perp", p["lookback_hours"])
    if apr is None:
        return None
    in_position = ctx.holding("perp").qty != 0
    if not in_position and apr >= p["entry_apr"]:
        ctx.state["entered_at"] = ctx.time
        w = ctx.neutral_weight()  # 現物代金 + 先物の証拠金で資金を使い切る比率
        return Decision(weights={"spot": w, "perp": -w}, reason=f"funding {apr:.1f}%/y")
    held_hours = (ctx.time - ctx.state.get("entered_at", ctx.time)) / 3_600_000
    if in_position and apr <= p["exit_apr"] and held_hours >= p["min_hold_hours"]:
        return Decision(weights={}, reason=f"funding {apr:.1f}%/y")
    return None


bot = Bot(
    id="eth_funding_carry",
    description="ETH 現物ロング + 先物ショートでファンディングを受け取る (閾値で出入り)",
    instruments={"spot": spot("ETHUSDT"), "perp": perp("ETH")},
    timeframe="1h",
    lookback=24,
    capital=10_000,
    leverage=3,
    deploy=["paper"],
    risk=Risk(max_drawdown=0.1),
    params={"lookback_hours": 24, "entry_apr": 10.0, "exit_apr": 2.0, "min_hold_hours": 24},
    strategy=strategy,
)
