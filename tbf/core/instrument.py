"""取引する銘柄の定義 (どの取引所の、現物か無期限先物か、成行か指値か)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

Kind = Literal["spot", "perp"]
# 成行 (テイカー) か指値 (メイカー) か
OrderType = Literal["market", "limit"]
# 指値が時間内に約定しなかったときの扱い
#   market: 成行へ落とす (板が走ってもヘッジの片脚だけ残さない)
#   skip:   その回は見送る (次の足で targets を評価し直す)
Unfilled = Literal["market", "skip"]


@dataclass(frozen=True)
class Fees:
    # 片道のテイカー手数料率 (0.001 = 0.1%)
    taker_rate: float = 0.001
    # バックテスト / ペーパーでのスリッページ (bps)
    slippage_bps: float = 5.0
    # 片道のメイカー手数料率。マイナスはリベート (受け取り)。None なら指値でも taker_rate を使う
    maker_rate: Optional[float] = None

    def rate(self, order_type: OrderType = "market") -> float:
        if order_type == "limit" and self.maker_rate is not None:
            return self.maker_rate
        return self.taker_rate

    def cost_rate_of(self, order_type: OrderType = "market") -> float:
        """約定代金に上乗せされうるコスト率 (スリッページで価格が上がり、その価格に手数料がかかる)。

        メイカーのリベートがスリッページより大きいとマイナスになる (= 建てるほど現金が増える)。
        呼び出し側はこの値が負でも成り立つようにしてある。
        """
        return (1 + self.slippage_bps / 10_000) * (1 + self.rate(order_type)) - 1

    @property
    def cost_rate(self) -> float:
        """成行のコスト率。資金の手当ては成行を前提に (= 多めに) 見積もる"""
        return self.cost_rate_of("market")


# 取引所・種類ごとの既定値 (基本ティアの taker 手数料)
#
# 国内2社 (gmo / bitbank) は TRA-50 §1-2 (各社の公開API・公式手数料ページ) と
# §1-4 (板スナップショット2回の片道平均) の実測。銘柄で値が違うものは SYMBOL_FEES で上書きする。
# ここに入れる (venue, kind) の値は、その会場の2銘柄のうち高くつく方 = 保守側。
DEFAULT_FEES: dict[tuple[str, str], Fees] = {
    ("binance", "spot"): Fees(0.001, 5.0),
    ("hyperliquid", "perp"): Fees(0.00045, 2.0),
    ("hyperliquid", "spot"): Fees(0.0007, 2.0),
    # GMOコイン 取引所: テイカー +0.05% / メイカー -0.01% (BTC・ETH 共通)。スリッページは BTC 側
    ("gmo", "spot"): Fees(0.0005, 4.2, maker_rate=-0.0001),
    # bitbank: 料率が銘柄で違う。ここは高い方の ETH (テイカー +0.12% / メイカー -0.02%)
    ("bitbank", "spot"): Fees(0.0012, 2.4, maker_rate=-0.0002),
}

# (取引所, 種類, 銘柄) 単位の実測値。DEFAULT_FEES より優先する。
# 銘柄名は Binance の表記のまま (国内業者の板は JPY 建てだが、価格データは Binance を使う。
# Instrument.price_source のコメントを参照)。
SYMBOL_FEES: dict[tuple[str, str, str], Fees] = {
    # TRA-50 §1-2 / §1-4 実測
    ("gmo", "spot", "BTCUSDT"): Fees(0.0005, 4.2, maker_rate=-0.0001),
    ("gmo", "spot", "ETHUSDT"): Fees(0.0005, 2.6, maker_rate=-0.0001),
    # bitbank BTC/JPY は 2026-02-02 改定でメイカー 0% / テイカー 0.10% (マイナス手数料は終了)
    ("bitbank", "spot", "BTCUSDT"): Fees(0.0010, 0.4, maker_rate=0.0),
    ("bitbank", "spot", "ETHUSDT"): Fees(0.0012, 2.4, maker_rate=-0.0002),
}


def default_fees(venue: str, kind: str, symbol: str) -> Fees:
    return SYMBOL_FEES.get((venue, kind, symbol)) or DEFAULT_FEES.get((venue, kind)) or Fees()


@dataclass(frozen=True)
class Instrument:
    venue: str
    kind: Kind
    # 取引所での名前 (Binance: BTCUSDT, Hyperliquid: BTC)
    symbol: str
    fees: Optional[Fees] = None
    # 維持証拠金率 (先物のみ)。Hyperliquid は「最大レバレッジの逆数の半分」
    # (例: 最大 25 倍なら 2%、最大 5 倍のアルトなら 10%)
    maintenance_margin: float = 0.02
    # 発注の既定の種類。"limit" にすると指値 (メイカー料率) で出し、
    # 時間内に約定しなければ unfilled の方針に従う
    order_type: OrderType = "market"
    # 指値が約定しなかったときの方針
    unfilled: Unfilled = "market"
    # 価格データの取得元 (取引所, 銘柄)。省略すると (venue, symbol) をそのまま使う。
    #
    # 【近似】国内業者 (gmo / bitbank) の板は JPY 建てで、tbf/market/ に JPY 足の取り込みがない。
    # そのため国内の現物レッグでも価格・足は Binance の USDT 建て (BTCUSDT / ETHUSDT) で代用する。
    # TRA-50 §1-5 の実測では、為替の共通成分を落とした「銘柄固有の内外価格差」は
    # 日次 約4.95bps・年率σ 0.94%、水準の平均は -0.50bps。向きの偏りがないので年率リターンには効かず、
    # ぶれ (シャープ) だけが増える。手数料・スリッページは会場ごとの実測値を使うので近似ではない。
    price_source: Optional[tuple[str, str]] = None

    def __post_init__(self) -> None:
        if self.kind not in ("spot", "perp"):
            raise ValueError(f"kind must be spot or perp (got {self.kind})")
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be market or limit (got {self.order_type})")
        if self.unfilled not in ("market", "skip"):
            raise ValueError(f"unfilled must be market or skip (got {self.unfilled})")
        if self.fees is None:
            object.__setattr__(self, "fees", default_fees(self.venue, self.kind, self.symbol))
        if self.price_source is not None:
            object.__setattr__(self, "price_source", tuple(self.price_source))

    @property
    def is_perp(self) -> bool:
        return self.kind == "perp"

    @property
    def price_venue(self) -> str:
        return self.price_source[0] if self.price_source else self.venue

    @property
    def price_symbol(self) -> str:
        return self.price_source[1] if self.price_source else self.symbol

    def __str__(self) -> str:
        s = f"{self.symbol}:{self.kind}@{self.venue}"
        return s + "(limit)" if self.order_type == "limit" else s


def spot(symbol: str, venue: str = "binance", **kw) -> Instrument:
    return Instrument(venue, "spot", symbol, **kw)


def perp(symbol: str, venue: str = "hyperliquid", **kw) -> Instrument:
    return Instrument(venue, "perp", symbol, **kw)
