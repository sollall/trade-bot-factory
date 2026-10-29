"""
デルタニュートラル (現物ロング + 無期限先物ショート) の 1 時間刻みバックテスト。

口座は 2 つに分けて持つ:
  現物口座:  cash, spot_qty
  先物口座:  margin (証拠金 + 実現損益 + 受け取ったファンディング), short_qty, entry
建玉比率 e のとき、資産 E を「現物代金 N」と「証拠金 N / leverage」に配分する (N = e*E*L/(L+1))。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from typing import Any, Optional, Sequence

from tbf.core.metrics import compute_metrics
from tbf.core.types import EquityPoint
from tbf.neutral.data import HOUR, FundingPoint, HourBar
from tbf.neutral.strategies import NeutralContext, NeutralStrategy

YEAR_MS = 365 * 24 * HOUR


@dataclass(frozen=True)
class NeutralParams:
    capital: float = 10_000.0
    # 先物ショートの目標レバレッジ
    leverage: float = 3.0
    # 手数料 (taker, 片道)。既定は Hyperliquid の基本ティア相当
    spot_fee: float = 0.0007
    perp_fee: float = 0.00045
    slippage_bps: float = 2.0
    # 現物と先物の口座間で資金を移すコスト (同じ取引所内なら 0)
    transfer_fee_usd: float = 1.0
    # 実効レバレッジが 目標×band を超える / 目標÷band を下回ったら配分し直す
    rebalance_band: float = 1.5
    # 維持証拠金率 (先物の評価額に対して)。これを割ったら清算
    maintenance_margin: float = 0.02
    # 配分時に手数料ぶん残しておく現金の比率
    cash_buffer: float = 0.005

    @classmethod
    def split(cls, values: dict[str, Any]) -> tuple["NeutralParams", dict[str, Any]]:
        """engine 用と戦略用のパラメータに振り分ける"""
        names = {f.name for f in fields(cls)}
        mine = {k: float(v) for k, v in values.items() if k in names}
        return cls(**mine), {k: v for k, v in values.items() if k not in names}


@dataclass
class Event:
    time: int
    kind: str  # enter | exit | rebalance | liquidation
    detail: str = ""


@dataclass
class NeutralResult:
    equity: list[EquityPoint]
    events: list[Event]
    funding_income: float
    fees: float
    transfer_costs: float
    hours_in_market: int
    hours_total: int

    @property
    def start_equity(self) -> float:
        return self.equity[0].equity if self.equity else 0.0

    @property
    def end_equity(self) -> float:
        return self.equity[-1].equity if self.equity else 0.0

    @property
    def total_return(self) -> float:
        return self.end_equity / self.start_equity - 1 if self.start_equity else 0.0

    @property
    def apr(self) -> float:
        """年率換算リターン (複利)"""
        if len(self.equity) < 2 or self.start_equity <= 0 or self.end_equity <= 0:
            return 0.0
        years = (self.equity[-1].time - self.equity[0].time) / YEAR_MS
        return (self.end_equity / self.start_equity) ** (1 / years) - 1 if years > 0 else 0.0

    @property
    def price_pnl(self) -> float:
        """ファンディング・手数料・移動コスト以外の損益 (先物と現物の価格差の変化など)"""
        return self.end_equity - self.start_equity - self.funding_income + self.fees + self.transfer_costs

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e.kind == kind)

    def summary(self) -> dict[str, Any]:
        m = compute_metrics(self.equity, [])
        return {
            "end_equity": self.end_equity,
            "total_return": self.total_return,
            "apr": self.apr,
            "max_drawdown": m.max_drawdown,
            "sharpe": m.sharpe,
            "funding_income": self.funding_income,
            "fees": self.fees,
            "transfer_costs": self.transfer_costs,
            "price_pnl": self.price_pnl,
            "time_in_market": self.hours_in_market / self.hours_total if self.hours_total else 0.0,
            "entries": self.count("enter"),
            "rebalances": self.count("rebalance"),
            "liquidations": self.count("liquidation"),
        }


@dataclass
class _Book:
    cash: float
    spot_qty: float = 0.0
    margin: float = 0.0
    short_qty: float = 0.0
    entry: float = 0.0

    def perp_value(self, perp_px: float) -> float:
        return self.margin + self.short_qty * (self.entry - perp_px)

    def equity(self, spot_px: float, perp_px: float) -> float:
        return self.cash + self.spot_qty * spot_px + self.perp_value(perp_px)


@dataclass
class _Sim:
    p: NeutralParams
    book: _Book
    fees: float = 0.0
    transfer_costs: float = 0.0
    events: list[Event] = field(default_factory=list)

    def set_target(self, exposure: float, spot_px: float, perp_px: float) -> None:
        p, b = self.p, self.book
        slip = p.slippage_bps / 10_000
        equity = b.equity(spot_px, perp_px)
        notional = max(0.0, exposure * equity * p.leverage / (p.leverage + 1) * (1 - p.cash_buffer))
        qty = notional / spot_px

        # 現物
        dq = qty - b.spot_qty
        if abs(dq) * spot_px > 1e-9:
            px = spot_px * (1 + slip if dq > 0 else 1 - slip)
            fee = abs(dq) * px * p.spot_fee
            b.cash -= dq * px + fee
            b.spot_qty = qty
            self.fees += fee

        # 先物ショート (現物と同数量)
        dp = qty - b.short_qty
        if abs(dp) * perp_px > 1e-9:
            if dp > 0:  # ショートを増やす = 売り
                px = perp_px * (1 - slip)
                b.entry = (b.short_qty * b.entry + dp * px) / qty
            else:  # 買い戻し
                px = perp_px * (1 + slip)
                b.margin += -dp * (b.entry - px)
            fee = abs(dp) * px * p.perp_fee
            b.margin -= fee
            self.fees += fee
            b.short_qty = qty
            if qty == 0:
                b.entry = 0.0

        # 先物口座の評価額を「証拠金 = 想定元本 / レバレッジ」に合わせる
        want = notional / p.leverage if qty > 0 else 0.0
        move = want - b.perp_value(perp_px)
        if abs(move) > 1e-6:
            b.margin += move
            b.cash -= move
            if abs(move) >= 1.0:
                b.cash -= p.transfer_fee_usd
                self.transfer_costs += p.transfer_fee_usd

    def effective_leverage(self, perp_px: float) -> float:
        v = self.book.perp_value(perp_px)
        return self.book.short_qty * perp_px / v if v > 0 else math.inf


def backtest_neutral(
    bars: Sequence[HourBar],
    funding: Sequence[FundingPoint],
    strategy: NeutralStrategy,
    params: NeutralParams = NeutralParams(),
    strategy_params: Optional[dict[str, Any]] = None,
) -> NeutralResult:
    """
    足 h の間 (h .. h+1h) は直前に決めた建玉を持ち、h+1h の時点で
      1. 足 h の高値で清算を判定  2. h+1h のファンディングを受け払い
      3. 終値で評価  4. 戦略を評価して建玉を調整 (終値 + スリッページで約定)
    """
    sim = _Sim(params, _Book(cash=params.capital))
    b = sim.book
    by_time = {f.time: f for f in funding}
    hist: list[FundingPoint] = []
    closes: list[float] = []
    equity: list[EquityPoint] = []
    state: dict[str, Any] = {}
    exposure = 0.0
    entered_at: Optional[int] = None
    premium = 0.0
    funding_income = 0.0
    in_market = 0

    if bars:
        equity.append(EquityPoint(bars[0].time, params.capital))

    for bar in bars:
        t = bar.time + HOUR
        f = by_time.get(t)
        if b.short_qty > 0:
            in_market += 1
            # 1. 清算 (足の高値で先物口座が維持証拠金を割ったか)
            perp_high = bar.high * (1 + premium)
            if b.perp_value(perp_high) <= params.maintenance_margin * b.short_qty * perp_high:
                sim.events.append(Event(t, "liquidation", f"perp high {perp_high:.2f}"))
                b.margin, b.short_qty, b.entry = 0.0, 0.0, 0.0  # 証拠金は全損とみなす
            # 2. ファンディング (オラクル価格 ≒ 現物価格 × 数量 × レート。+ ならショートが受け取る)
            if f and b.short_qty > 0:
                pay = b.short_qty * bar.close * f.rate
                b.margin += pay
                funding_income += pay
        if f:
            premium = f.premium
            hist.append(f)
        closes.append(bar.close)
        spot_px, perp_px = bar.close, bar.close * (1 + premium)

        # 清算でヘッジが外れた現物は手仕舞う
        if b.short_qty == 0 and b.spot_qty > 0:
            sim.set_target(0.0, spot_px, perp_px)
            exposure, entered_at = 0.0, None

        # 3. 評価
        equity.append(EquityPoint(t, b.equity(spot_px, perp_px)))

        # 4. 戦略
        target = strategy(
            NeutralContext(
                time=t,
                funding=hist,
                closes=closes,
                exposure=exposure,
                hours_in_position=(t - entered_at) // HOUR if entered_at is not None else 0,
                params=strategy_params or {},
                state=state,
            )
        )
        if target is not None:
            target = min(1.0, max(0.0, float(target)))
        if target is not None and abs(target - exposure) > 1e-9:
            sim.set_target(target, spot_px, perp_px)
            if exposure == 0 and target > 0:
                sim.events.append(Event(t, "enter", f"exposure {target:.2f}"))
                entered_at = t
            elif target == 0:
                sim.events.append(Event(t, "exit"))
                entered_at = None
            exposure = target
        elif exposure > 0:
            lev = sim.effective_leverage(perp_px)
            if lev > params.leverage * params.rebalance_band or lev < params.leverage / params.rebalance_band:
                sim.set_target(exposure, spot_px, perp_px)
                sim.events.append(Event(t, "rebalance", f"leverage {lev:.2f} -> {params.leverage:.2f}"))
        if equity:
            equity[-1] = EquityPoint(t, b.equity(spot_px, perp_px))

    return NeutralResult(
        equity=equity,
        events=sim.events,
        funding_income=funding_income,
        fees=sim.fees,
        transfer_costs=sim.transfer_costs,
        hours_in_market=in_market,
        hours_total=len(bars),
    )
