"""
ETH/BTC の相対価値 (市場中立)。Hyperliquid の無期限先物 2 本を同額で反対に持つ。

log(ETH/BTC) の z スコア (直近 z_window 本の平均・標準偏差) が entry_z を超えて開いたら、
割高な側をショート・割安な側をロング。|z| が exit_z まで戻ったら手仕舞う。

損切りは 2 つ。比率が一方向に走り続けたときに損を止めるための歯止めで、取引ルールではない。
  - |z| が STOP_Z を超えた (開きがさらに広がった)
  - 建ててから MAX_HOLD_HOURS 時間たっても戻らない
損切りのあとは、|z| がいったん entry_z の内側に戻るまで建て直さない (同じ開きに入り直さない)。

持ち高は片側 leg_weight (資産比)。leverage=1 なので両側合わせて資産の約 1 倍で、
清算は ETH/BTC が建値から数十 % 動かないと起きない。
"""

import math

from tbf import Bot, Context, Decision, Risk, perp
from tbf.core.types import HOUR

# 損切りの固定値。変更には人間の承認が必要。
# 結果を見る前に決めた値で、params に移さない・スイープしない (結果を見てから選べるなら歯止めではなくなる)。
# tests/test_eth_btc_relval.py が固定であることを見張る。
STOP_Z = 4.0  # |z| がこれを超えたら損切り
MAX_HOLD_HOURS = 120  # 建ててからこの時間で戻らなければ手仕舞う (5 日)


def zscore(eth: list, btc: list, window: int):
    """log(ETH/BTC) の直近 window 本での z スコア。足りない・動いていないときは None"""
    by_time = {c.time: c.close for c in btc}
    xs = [math.log(c.close / by_time[c.time]) for c in eth if c.time in by_time and c.close > 0 and by_time[c.time] > 0]
    if len(xs) < window:
        return None
    w = xs[-window:]
    mean = sum(w) / window
    sd = (sum((x - mean) ** 2 for x in w) / window) ** 0.5
    if sd <= 0:
        return None
    return (w[-1] - mean) / sd


def strategy(ctx: Context):
    p = ctx.params
    z = zscore(ctx.series("eth"), ctx.series("btc"), p["z_window"])
    if z is None:
        return None
    st = ctx.state
    held = ctx.holding("eth").qty != 0 or ctx.holding("btc").qty != 0

    if held:
        # side: +1 = ETH ロング / BTC ショート (ETH が割安で入った)
        side = 1 if ctx.holding("eth").qty > 0 or ctx.holding("btc").qty < 0 else -1
        opened = st.get("opened_at", ctx.time)
        hours = (ctx.time - opened) / HOUR
        # 建てた向きから見た開き (プラス = まだ開いている)
        gap = -z * side
        if gap >= STOP_Z:
            st["blocked"] = True
            st["stops_z"] = st.get("stops_z", 0) + 1
            return Decision(weights={}, reason=f"stop: |z| {abs(z):.2f} >= {STOP_Z}")
        if hours >= MAX_HOLD_HOURS:
            st["blocked"] = True
            st["stops_time"] = st.get("stops_time", 0) + 1
            return Decision(weights={}, reason=f"stop: held {hours:.0f}h >= {MAX_HOLD_HOURS}h (z {z:.2f})")
        if gap <= p["exit_z"]:
            return Decision(weights={}, reason=f"exit: z {z:.2f}")
        return None

    st.pop("opened_at", None)
    if st.get("blocked"):
        if abs(z) < p["entry_z"]:
            st["blocked"] = False
        return None
    if abs(z) >= p["entry_z"] and abs(z) < STOP_Z:
        w = p["leg_weight"]
        side = -1 if z > 0 else 1  # z > 0 = ETH が割高 -> ETH ショート / BTC ロング
        st["opened_at"] = ctx.time
        return Decision(weights={"eth": side * w, "btc": -side * w}, reason=f"enter: z {z:.2f}")
    return None


bot = Bot(
    id="eth_btc_relval",
    description="ETH/BTC の相対価値 (先物 2 本を同額で反対に持つ。z スコアの開きで建て、戻りで手仕舞い)",
    instruments={"eth": perp("ETH"), "btc": perp("BTC")},  # どちらも Hyperliquid 無期限先物
    timeframe="1h",
    lookback=340,  # z_window を 336 (14 日) まで試せる長さ
    capital=10_000,
    leverage=1,
    deploy=["paper"],
    risk=Risk(max_drawdown=0.1, max_daily_loss=0.03),
    params={"z_window": 168, "entry_z": 2.0, "exit_z": 0.5, "leg_weight": 0.5},
    strategy=strategy,
)
