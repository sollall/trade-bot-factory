"""
ウォークフォワード検証。

期間を「学習 train + 検証 test」の窓に分け、test ずつずらしながら:
  1. 学習期間で全組み合わせを試し、指標が最も良いものを選ぶ
  2. 選んだ設定で、続く検証期間を走らせる
検証期間の成績をつないだもの (out-of-sample) は、どの時点でも「その先の答え」を見ずに選んだ結果になる。
全期間で一番良かった設定 (in-sample) と比べた差が、過去への合わせすぎの目安。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from tbf.backtest.engine import BacktestResult, backtest_async
from tbf.backtest.sweep import configure, label, score
from tbf.core.bot import Bot
from tbf.core.metrics import compute_metrics
from tbf.core.portfolio import Portfolio
from tbf.core.types import TIMEFRAME_MS, Candle, EquityPoint, Fill, FundingPoint

DAY = 86_400_000


@dataclass
class Fold:
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    chosen: dict[str, Any]
    train_score: float
    test: BacktestResult


@dataclass
class WalkForwardResult:
    objective: str
    folds: list[Fold]
    # 検証期間をつないだ成績
    oos: BacktestResult
    # 全期間で一番良かった設定とその成績 (比較用)
    in_sample_combo: dict[str, Any]
    in_sample: BacktestResult
    # 検証期間と同じ範囲に切り出した in-sample の成績 (同じ期間で比べるため)
    in_sample_same_span: BacktestResult
    choices: Counter = field(default_factory=Counter)

    @property
    def efficiency(self) -> Optional[float]:
        """検証期間の年率 / 同じ期間の in-sample 最良設定の年率。1 に近いほど過学習が小さい"""
        base = self.in_sample_same_span.apr
        return self.oos.apr / base if base > 0 else None


def make_folds(start: int, end: int, train_ms: int, test_ms: int) -> list[tuple[int, int, int, int]]:
    out = []
    t = start
    while t + train_ms + test_ms <= end:
        out.append((t, t + train_ms, t + train_ms, t + train_ms + test_ms))
        t += test_ms
    return out


def chain(bot: Bot, parts: Sequence[BacktestResult]) -> BacktestResult:
    """検証期間の結果を、資金を引き継いだものとしてつなぐ (各期間は同じ初期資金で走らせている)"""
    equity: list[EquityPoint] = []
    fills: list[Fill] = []
    events = []
    funding = fees = transfers = 0.0
    in_market = total = 0
    capital = bot.capital
    for r in parts:
        if len(r.equity) < 2:
            continue
        k = capital / bot.capital
        pts = [EquityPoint(p.time, p.equity * k) for p in r.equity]
        equity.extend(pts if not equity else pts[1:])
        fills.extend(r.fills)
        events.extend(r.events)
        funding += r.funding_income * k
        fees += r.fees * k
        transfers += r.transfer_costs * k
        in_market += r.bars_in_market
        total += r.bars_total
        capital = pts[-1].equity
    last = parts[-1].final if parts else Portfolio()
    return BacktestResult(
        bot.id, compute_metrics(equity, fills), equity, fills, None, last, events,
        funding, fees, transfers, in_market, total,
    )


async def walk_forward(
    bot: Bot,
    bars: Mapping[str, Sequence[Candle]],
    funding: Mapping[str, Sequence[FundingPoint]],
    combos: list[dict[str, Any]],
    train_days: float,
    test_days: float,
    objective: str = "sharpe",
) -> WalkForwardResult:
    if len(combos) < 2:
        raise ValueError("ウォークフォワードには --sweep で 2 つ以上の組み合わせが必要です")
    tf = TIMEFRAME_MS[bot.timeframe]
    start = max(cs[0].time for cs in bars.values() if cs)
    end = min(cs[-1].time for cs in bars.values() if cs) + tf
    folds_t = make_folds(start, end, int(train_days * DAY), int(test_days * DAY))
    if not folds_t:
        days = (end - start) / DAY
        raise ValueError(f"期間が短すぎます (データ {days:.0f} 日 < 学習 {train_days:g} 日 + 検証 {test_days:g} 日)")

    bots = [(c, configure(bot, c)) for c in combos]
    folds: list[Fold] = []
    choices: Counter = Counter()
    for tr_s, tr_e, te_s, te_e in folds_t:
        best_combo, best_bot, best_score = None, None, None
        for combo, b in bots:
            r = await backtest_async(b, bars, funding, trade_from=tr_s, trade_until=tr_e)
            sc = score(r, objective)
            if best_score is None or sc > best_score:
                best_combo, best_bot, best_score = combo, b, sc
        assert best_bot is not None and best_combo is not None
        test = await backtest_async(best_bot, bars, funding, trade_from=te_s, trade_until=te_e)
        folds.append(Fold(tr_s, tr_e, te_s, te_e, best_combo, best_score, test))  # type: ignore[arg-type]
        choices[label(best_combo)] += 1

    # 比較用: 全期間で一番良かった設定 (= 答えを見て選んだ設定)
    is_combo, is_bot, is_result, is_score = None, None, None, None
    for combo, b in bots:
        r = await backtest_async(b, bars, funding)
        sc = score(r, objective)
        if is_score is None or sc > is_score:
            is_combo, is_bot, is_result, is_score = combo, b, r, sc
    assert is_bot is not None and is_result is not None and is_combo is not None
    oos_from, oos_until = folds[0].test_start, folds[-1].test_end
    same_span = await backtest_async(is_bot, bars, funding, trade_from=oos_from, trade_until=oos_until)

    return WalkForwardResult(
        objective=objective,
        folds=folds,
        oos=chain(bot, [f.test for f in folds]),
        in_sample_combo=is_combo,
        in_sample=is_result,
        in_sample_same_span=same_span,
        choices=choices,
    )
