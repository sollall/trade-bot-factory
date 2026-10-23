import { clampExposure, type BotDefinition } from "./bot";
import { equityOf, orderQtyForTarget, type PortfolioState } from "./portfolio";
import type { Candle, Mode } from "./types";

export interface StepResult {
  /** + 買い / - 売り / 0 なし (base 数量) */
  qty: number;
  targetExposure: number | null;
  reason?: string;
}

/**
 * バックテスト・ペーパー・本番で共通の意思決定ロジック。
 * candles は末尾が直近の確定足、price は発注時の参照価格。
 * state は戦略が書き換える可能性がある。
 */
export function runStrategy<P>(
  bot: BotDefinition<P>,
  candles: Candle[],
  portfolio: PortfolioState,
  price: number,
  state: Record<string, unknown>,
  mode: Mode,
): StepResult {
  const window = candles.slice(-bot.lookback);
  const decision = bot.strategy({
    candles: window,
    position: { ...portfolio.position },
    cash: portfolio.cash,
    equity: equityOf(portfolio, window[window.length - 1]?.close ?? price),
    params: bot.params,
    state,
    mode,
  });
  if (!decision) return { qty: 0, targetExposure: null };
  const target = clampExposure(decision.targetExposure);
  const costRate = bot.fees.takerRate + bot.fees.slippageBps / 10_000;
  const qty = orderQtyForTarget(portfolio, price, target, bot.rebalanceThreshold, costRate);
  return { qty, targetExposure: target, reason: decision.reason };
}

export function utcDay(time: number): string {
  return new Date(time).toISOString().slice(0, 10);
}
