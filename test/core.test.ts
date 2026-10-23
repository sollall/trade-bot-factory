import { describe, expect, it } from "vitest";
import { defineBot } from "../src/core/bot";
import { applyFill, orderQtyForTarget, simulateFill } from "../src/core/portfolio";
import { checkRisk } from "../src/core/risk";
import { computeMetrics } from "../src/core/metrics";
import { backtest } from "../src/backtest/engine";
import type { Candle } from "../src/core/types";

const H = 3_600_000;
const candles = (closes: number[]): Candle[] =>
  closes.map((c, i) => ({ time: i * H, open: closes[i - 1] ?? c, high: c, low: c, close: c, volume: 1 }));

describe("portfolio", () => {
  it("buys to target exposure without exceeding cash", () => {
    const s = { cash: 1000, position: { qty: 0, avgPrice: 0 } };
    const qty = orderQtyForTarget(s, 100, 1, 0.01, 0.001);
    expect(qty * 100 * 1.001).toBeLessThanOrEqual(1000 + 1e-9);
    const { state } = applyFill(s, simulateFill("buy", qty, 100, 0, 0.001, 0));
    const slipped = applyFill(s, simulateFill("buy", orderQtyForTarget(s, 100, 1, 0.01, 0.0015), 100, 0, 0.001, 5));
    expect(slipped.state.cash).toBeGreaterThanOrEqual(0);
    expect(state.cash).toBeGreaterThanOrEqual(-1e-9);
    expect(state.position.avgPrice).toBe(100);
  });

  it("closes the whole position on target 0 and realizes pnl net of fee", () => {
    const s = { cash: 0, position: { qty: 2, avgPrice: 100 } };
    expect(orderQtyForTarget(s, 110, 0, 0.5, 0)).toBe(-2);
    const { state, fill } = applyFill(s, { time: 0, side: "sell", qty: 2, price: 110, fee: 1 });
    expect(fill.realizedPnl).toBeCloseTo(19);
    expect(state.cash).toBeCloseTo(219);
    expect(state.position.qty).toBe(0);
  });

  it("ignores rebalances below threshold", () => {
    const s = { cash: 5, position: { qty: 9.95, avgPrice: 100 } };
    expect(orderQtyForTarget(s, 100, 1, 0.01, 0)).toBe(0);
  });
});

describe("risk", () => {
  it("detects drawdown, daily loss and losing streaks", () => {
    const base = { equity: 100, peakEquity: 100, dayStartEquity: 100, consecutiveLosses: 0 };
    expect(checkRisk({ maxDrawdown: 0.2 }, base)).toBeNull();
    expect(checkRisk({ maxDrawdown: 0.2 }, { ...base, equity: 79 })).toMatch(/drawdown/);
    expect(checkRisk({ maxDailyLoss: 0.05 }, { ...base, equity: 94, peakEquity: 200 })).toMatch(/daily/);
    expect(checkRisk({ maxConsecutiveLosses: 3 }, { ...base, consecutiveLosses: 3 })).toMatch(/consecutive/);
  });
});

describe("metrics", () => {
  it("computes drawdown and win rate", () => {
    const m = computeMetrics(
      [
        { time: 0, equity: 100 },
        { time: H, equity: 120 },
        { time: 2 * H, equity: 90 },
        { time: 3 * H, equity: 110 },
      ],
      [
        { time: 0, side: "sell", qty: 1, price: 1, fee: 0, realizedPnl: 5 },
        { time: 1, side: "sell", qty: 1, price: 1, fee: 0, realizedPnl: -2 },
      ],
    );
    expect(m.maxDrawdown).toBeCloseTo(0.25);
    expect(m.totalReturn).toBeCloseTo(0.1);
    expect(m.winRate).toBe(0.5);
    expect(m.profitFactor).toBeCloseTo(2.5);
    expect(m.consecutiveLosses).toBe(1);
  });
});

describe("backtest", () => {
  const buyAndHold = defineBot({
    id: "bh",
    symbol: "X",
    timeframe: "1h",
    lookback: 1,
    capital: 1000,
    risk: {},
    fees: { takerRate: 0, slippageBps: 0 },
    params: {},
    strategy: () => ({ targetExposure: 1 }),
  });

  it("executes on the next candle's open (no look-ahead)", () => {
    const r = backtest(buyAndHold, candles([100, 200, 200]));
    expect(r.fills).toHaveLength(1);
    expect(r.fills[0]!.price).toBe(100); // 足 0 確定で判断 -> 足 1 の始値 (=100)
    expect(r.metrics.endEquity).toBeCloseTo(2000);
  });

  it("halts and flattens on drawdown breach", () => {
    const bot = { ...buyAndHold, risk: { maxDrawdown: 0.3, flattenOnHalt: true } };
    const r = backtest(bot, candles([100, 100, 60, 60, 120]));
    expect(r.halted?.reason).toMatch(/drawdown/);
    expect(r.final.position.qty).toBe(0);
    expect(r.metrics.endEquity).toBeCloseTo(600);
  });

  it("persists strategy state across candles", () => {
    const seen: number[] = [];
    const bot = defineBot({
      ...buyAndHold,
      strategy: ({ state }) => {
        state.n = ((state.n as number) ?? 0) + 1;
        seen.push(state.n as number);
      },
    });
    backtest(bot, candles([1, 2, 3]));
    expect(seen).toEqual([1, 2, 3]);
  });
});
