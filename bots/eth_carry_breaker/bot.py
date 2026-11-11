"""
ETH 現物ロング + 無期限先物ショートのデルタニュートラルキャリー。
ファンディング回路遮断器つき。bots/btc_carry_breaker と同じ構造・同じ固定値で、銘柄だけが違う。

遮断器は取引ルールではなく歯止めである。閾値は採用基準 §1-4-1 の固定仕様で、
スイープ対象にしてはいけない (結果を見てから閾値を選べるなら歯止めではなくなる)。
既定状態は「閉」= 取引可。ファンディング履歴が足りず平均が計算できない間は
「事故の証拠がない」とみなして通常どおり建てる。

固定値は bot ごとに書き写す。build_worker.py は bots/<id>/ の中だけを Worker に運ぶので、
bot 間で共有モジュールにはできない。書き写しのずれは tests/test_carry_breaker.py が見張る。
"""

from tbf import Bot, Context, Decision, Risk, perp, spot

# 採用基準 §1-4-1 の固定値。変更には人間の承認が必要。
WINDOW_HOURS = 72  # 観測窓: 直近 72 時間の平均ファンディング (年率%)
BREAK_APR = -25.0  # 遮断 (開): これを下回ったら全銘柄を手仕舞う
RESTORE_APR = -10.0  # 復帰 (閉): ここまで回復したら建て直す (ヒステリシス 15pt)


def strategy(ctx: Context):
    apr = ctx.funding_apr("perp", WINDOW_HOURS)
    tripped = bool(ctx.state.get("breaker_open"))
    if apr is not None:
        if not tripped and apr < BREAK_APR:
            tripped = True
            ctx.state["breaker_trips"] = ctx.state.get("breaker_trips", 0) + 1
        elif tripped and apr >= RESTORE_APR:
            tripped = False
            ctx.state["breaker_restores"] = ctx.state.get("breaker_restores", 0) + 1
    ctx.state["breaker_open"] = tripped

    held = ctx.holding("spot").qty != 0 or ctx.holding("perp").qty != 0
    if tripped:
        if not held:
            return None
        return Decision(weights={}, reason=f"breaker open: funding {apr:.1f}%/y")
    if not held:
        w = ctx.neutral_weight()  # 現物代金 + 先物証拠金で資金を使い切る比率
        return Decision(weights={"spot": w, "perp": -w}, reason="carry on")
    return None


bot = Bot(
    id="eth_carry_breaker",
    description="ETH キャリー (現物ロング + 先物ショート) + ファンディング回路遮断器",
    instruments={"spot": spot("ETHUSDT"), "perp": perp("ETH")},
    timeframe="1h",
    lookback=1,
    capital=10_000,
    leverage=3,
    deploy=["paper"],
    risk=Risk(max_drawdown=0.1),
    strategy=strategy,
)
