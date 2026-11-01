"""比較用: ETH 現物ロング + 先物ショートを最初から最後まで持ち続ける"""

from tbf import Bot, Context, Risk, perp, spot


def strategy(ctx: Context):
    if ctx.holding("perp").qty == 0:
        w = ctx.neutral_weight()
        return {"spot": w, "perp": -w}
    return None


bot = Bot(
    id="eth_funding_hedged",
    description="比較用: ヘッジしたまま持ちっぱなし",
    instruments={"spot": spot("ETHUSDT"), "perp": perp("ETH")},
    timeframe="1h",
    lookback=1,
    capital=10_000,
    leverage=3,
    deploy=["paper"],
    risk=Risk(max_drawdown=0.1),
    strategy=strategy,
)
