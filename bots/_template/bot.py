from tbf import Bot, Context, Decision, Risk
from tbf.core import indicators as ind


def strategy(ctx: Context):
    # ctx.candles: 確定足 (古い順) / ctx.closes: 終値リスト / ctx.position / ctx.params / ctx.state
    # 戻り値: Decision(target_exposure=0..1) か 0..1 の数値。None なら何もしない
    return None


bot = Bot(
    id="__BOT_ID__",
    symbol="BTCUSDT",
    timeframe="1h",  # 1m 5m 15m 30m 1h 4h 1d
    lookback=50,
    capital=1000,
    # まずは paper だけ。成績が良ければ "live" を足す
    deploy=["paper"],
    risk=Risk(max_drawdown=0.2, max_daily_loss=0.05),
    params={},
    strategy=strategy,
)
