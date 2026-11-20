"""
BTC 現物のブレイクアウト (ロング / ノーポジ) + ボラティリティ目標。日足。

- 入る: 終値が「直前 entry_n 本の高値」を上抜けた (Donchian)
- 出る: 終値が「直前 exit_n 本の安値」を下抜けた (exit_n < entry_n)
- 大きさ: 目標ボラ ÷ 実現ボラ (直近 vol_window 本の日次対数リターンの標準偏差を年率にしたもの) を 0〜1 に丸める。
  持っている間も、今の比率と resize_band 以上ずれたら合わせ直す (毎日の細かい売買はしない)

足は日足だけ。Coinbase に 4h 足が無い (tbf/market/coinbase.py の GRANULARITY_SEC)。
"""

import math

from tbf import Bot, Context, Decision, Risk, spot

DAYS_PER_YEAR = 365  # 暗号資産は毎日動く


def realized_vol(closes: list, window: int):
    """直近 window 本の日次対数リターンの標準偏差 (年率)。足りなければ None"""
    if len(closes) < window + 1:
        return None
    w = closes[-(window + 1) :]
    rets = [math.log(b / a) for a, b in zip(w, w[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var * DAYS_PER_YEAR)


def target_size(closes: list, target_vol: float, window: int):
    vol = realized_vol(closes, window)
    if vol is None:
        return None
    if vol <= 0:
        return 1.0
    return max(0.0, min(1.0, target_vol / vol))


def strategy(ctx: Context):
    p = ctx.params
    cs = ctx.candles
    entry_n, exit_n = p["entry_n"], p["exit_n"]
    if len(cs) < max(entry_n, exit_n) + 1:
        return None
    last = cs[-1]
    size = target_size(ctx.closes, p["target_vol"], p["vol_window"])
    if size is None:
        return None

    if ctx.position.qty > 0:
        floor = min(c.low for c in cs[-(exit_n + 1) : -1])
        if last.close < floor:
            return Decision(0, f"exit: close {last.close:.0f} < {exit_n}-bar low {floor:.0f}")
        if abs(size - ctx.weight(ctx.primary)) >= p["resize_band"]:
            return Decision(size, f"resize to {size:.2f}")
        return None

    ceiling = max(c.high for c in cs[-(entry_n + 1) : -1])
    if last.close > ceiling:
        return Decision(size, f"enter: close {last.close:.0f} > {entry_n}-bar high {ceiling:.0f}, size {size:.2f}")
    return None


bot = Bot(
    id="btc_vol_breakout",
    description="BTC 現物の高値抜け (Donchian) ロング。持ち高は 目標ボラ ÷ 実現ボラ で 0〜1",
    instruments={
        # 価格・足は Coinbase から取る (Binance は Cloudflare Workers から取れない。docs/market-data.md)
        "BTCUSDT": spot("BTCUSDT", price_source=("coinbase", "BTC-USD")),
    },
    timeframe="1d",
    lookback=64,  # entry_n を 55 まで試せる長さ
    capital=1000,
    deploy=["paper"],
    risk=Risk(max_drawdown=0.25, max_daily_loss=0.1),
    params={"entry_n": 20, "exit_n": 10, "target_vol": 0.4, "vol_window": 30, "resize_band": 0.1},
    strategy=strategy,
)
