from tbf import Bot, Context, Decision, Risk
from tbf.core import indicators as ind


def strategy(ctx: Context):
    fast = ind.sma(ctx.closes, ctx.params["fast"])
    slow = ind.sma(ctx.closes, ctx.params["slow"])
    if fast is None or slow is None:
        return None
    if fast > slow:
        return Decision(1, f"fast {fast:.2f} > slow {slow:.2f}")
    return Decision(0, f"fast {fast:.2f} <= slow {slow:.2f}")


bot = Bot(
    id="sma_cross",
    description="短期 SMA が長期 SMA を上回っている間ロング",
    symbol="BTCUSDT",
    timeframe="1h",
    lookback=60,
    capital=1000,
    deploy=["paper"],
    risk=Risk(max_drawdown=0.2, max_daily_loss=0.05),
    params={"fast": 10, "slow": 50},
    strategy=strategy,
)
