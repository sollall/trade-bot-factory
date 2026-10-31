from tbf import Bot, Context, Decision, Risk, perp, spot
from tbf.core import indicators as ind


def strategy(ctx: Context):
    # 1 銘柄: ctx.candles / ctx.closes / ctx.position (主銘柄 = instruments の最初)
    # 複数銘柄: ctx.series("別名") / ctx.closes_of("別名") / ctx.holding("別名") / ctx.weight("別名")
    #           ctx.funding("別名") / ctx.funding_apr("別名", 時間)  ※先物のみ
    # 共通: ctx.time / ctx.equity / ctx.cash / ctx.params / ctx.state (永続化される dict) / ctx.mode
    #
    # 戻り値: 資産に対する目標の持ち高比率 (+ ロング / - ショート)
    #   数値          -> 主銘柄の比率 (例: 1.0 = 全額ロング)
    #   dict          -> {"別名": 比率, ...}。書いていない銘柄は 0 (手仕舞い)
    #   Decision(...) -> 理由つき (target_exposure= または weights=)
    #   None          -> 何もしない (今の持ち高のまま)
    return None


bot = Bot(
    id="__BOT_ID__",
    # 1 銘柄の現物なら symbol="BTCUSDT" だけでもよい
    instruments={"main": spot("BTCUSDT")},  # 先物は perp("BTC") (Hyperliquid)
    timeframe="1h",  # 1m 5m 15m 30m 1h 4h 1d
    lookback=50,
    capital=1000,
    # まずは paper だけ。成績が良ければ "live" を足す
    deploy=["paper"],
    risk=Risk(max_drawdown=0.2, max_daily_loss=0.05),
    params={},
    strategy=strategy,
)
