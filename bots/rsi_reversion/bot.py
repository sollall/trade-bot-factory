from tbf import Bot, Context, Decision, Risk, spot
from tbf.core import indicators as ind


def strategy(ctx: Context):
    p = ctx.params
    r = ind.rsi(ctx.closes, p["period"])
    if r is None:
        return None
    if ctx.position.qty == 0 and r < p["buy_below"]:
        return Decision(1, f"rsi {r:.1f}")
    if ctx.position.qty > 0 and r > p["sell_above"]:
        return Decision(0, f"rsi {r:.1f}")
    return None


bot = Bot(
    id="rsi_reversion",
    description="RSI が売られすぎで買い、買われすぎで手仕舞い",
    instruments={
        # 価格・足は Coinbase から取る。Binance は Cloudflare Workers から安定して取れない
        # (発注先は venue のまま。実測と理由は docs/market-data.md / TRA-81)
        "ETHUSDT": spot("ETHUSDT", price_source=("coinbase", "ETH-USD")),
    },
    timeframe="15m",
    lookback=100,
    capital=1000,
    deploy=["paper"],
    risk=Risk(max_drawdown=0.15, max_daily_loss=0.05, max_consecutive_losses=8),
    params={"period": 14, "buy_below": 30, "sell_above": 55},
    strategy=strategy,
)
