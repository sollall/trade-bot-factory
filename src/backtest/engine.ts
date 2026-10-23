import type { BotDefinition } from "../core/bot";
import { computeMetrics, type Metrics } from "../core/metrics";
import { applyFill, equityOf, simulateFill, type PortfolioState } from "../core/portfolio";
import { checkRisk, nextConsecutiveLosses } from "../core/risk";
import { runStrategy, utcDay } from "../core/step";
import type { Candle, EquityPoint, Fill } from "../core/types";

export interface BacktestResult {
  botId: string;
  metrics: Metrics;
  equity: EquityPoint[];
  fills: Fill[];
  halted: { time: number; reason: string } | null;
  final: PortfolioState;
}

/**
 * 足 i の確定時に戦略を評価し、足 i+1 の始値で約定させる (先読みなし)。
 * 本番と同じリスク判定を適用し、違反したら以降の売買を止める。
 */
export function backtest<P>(bot: BotDefinition<P>, candles: Candle[]): BacktestResult {
  let ps: PortfolioState = { cash: bot.capital, position: { qty: 0, avgPrice: 0 } };
  const state: Record<string, unknown> = {};
  const equity: EquityPoint[] = [];
  const fills: Fill[] = [];
  let pending = 0;
  let halted: BacktestResult["halted"] = null;
  let day = "";
  let dayStartEquity = bot.capital;
  let peakEquity = bot.capital;
  let consecutiveLosses = 0;

  for (let i = 0; i < candles.length; i++) {
    const c = candles[i]!;

    if (pending !== 0) {
      const side = pending > 0 ? "buy" : "sell";
      const sim = simulateFill(side, Math.abs(pending), c.open, c.time, bot.fees.takerRate, bot.fees.slippageBps);
      const r = applyFill(ps, sim);
      ps = r.state;
      fills.push(r.fill);
      consecutiveLosses = nextConsecutiveLosses(consecutiveLosses, r.fill);
      pending = 0;
    }

    const eq = equityOf(ps, c.close);
    const d = utcDay(c.time);
    if (d !== day) {
      day = d;
      dayStartEquity = equity.length > 0 ? equity[equity.length - 1]!.equity : eq;
    }
    equity.push({ time: c.time, equity: eq });
    peakEquity = Math.max(peakEquity, eq);

    if (halted) continue;

    const breach = checkRisk(bot.risk, { equity: eq, peakEquity, dayStartEquity, consecutiveLosses });
    if (breach) {
      halted = { time: c.time, reason: breach };
      if (bot.risk.flattenOnHalt !== false && ps.position.qty > 0) pending = -ps.position.qty;
      continue;
    }

    if (i + 1 < bot.lookback) continue;
    const { qty } = runStrategy(bot, candles.slice(Math.max(0, i + 1 - bot.lookback), i + 1), ps, c.close, state, "backtest");
    pending = qty;
  }

  return { botId: bot.id, metrics: computeMetrics(equity, fills), equity, fills, halted, final: ps };
}
