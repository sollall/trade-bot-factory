import type { EquityPoint, Fill } from "./types";

export interface Metrics {
  startEquity: number;
  endEquity: number;
  totalReturn: number;
  maxDrawdown: number;
  /** 現在のピークからのドローダウン */
  currentDrawdown: number;
  /** 年率換算シャープレシオ (リスクフリー 0) */
  sharpe: number;
  trades: number;
  winRate: number;
  profitFactor: number;
  consecutiveLosses: number;
  fees: number;
}

const YEAR_MS = 365 * 24 * 60 * 60_000;

export function computeMetrics(equity: EquityPoint[], fills: Fill[]): Metrics {
  const start = equity[0]?.equity ?? 0;
  const end = equity[equity.length - 1]?.equity ?? start;

  let peak = -Infinity;
  let maxDd = 0;
  let curDd = 0;
  for (const p of equity) {
    peak = Math.max(peak, p.equity);
    curDd = peak > 0 ? (peak - p.equity) / peak : 0;
    maxDd = Math.max(maxDd, curDd);
  }

  const rets: number[] = [];
  for (let i = 1; i < equity.length; i++) {
    const prev = equity[i - 1]!.equity;
    if (prev > 0) rets.push(equity[i]!.equity / prev - 1);
  }
  let sharpe = 0;
  if (rets.length > 1) {
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1));
    const span = equity[equity.length - 1]!.time - equity[0]!.time;
    const periodsPerYear = span > 0 ? (rets.length * YEAR_MS) / span : 0;
    if (sd > 0) sharpe = (mean / sd) * Math.sqrt(periodsPerYear);
  }

  const closes = fills.filter((f) => f.side === "sell");
  const wins = closes.filter((f) => f.realizedPnl > 0);
  const grossWin = wins.reduce((a, f) => a + f.realizedPnl, 0);
  const grossLoss = -closes.filter((f) => f.realizedPnl <= 0).reduce((a, f) => a + f.realizedPnl, 0);
  let consecutiveLosses = 0;
  for (let i = closes.length - 1; i >= 0 && closes[i]!.realizedPnl <= 0; i--) consecutiveLosses++;

  return {
    startEquity: start,
    endEquity: end,
    totalReturn: start > 0 ? end / start - 1 : 0,
    maxDrawdown: maxDd,
    currentDrawdown: curDd,
    sharpe,
    trades: closes.length,
    winRate: closes.length ? wins.length / closes.length : 0,
    profitFactor: grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0,
    consecutiveLosses,
    fees: fills.reduce((a, f) => a + f.fee, 0),
  };
}

export function formatMetrics(m: Metrics): string {
  const pct = (x: number) => `${(x * 100).toFixed(2)}%`;
  return [
    `equity        ${m.startEquity.toFixed(2)} -> ${m.endEquity.toFixed(2)}`,
    `total return  ${pct(m.totalReturn)}`,
    `max drawdown  ${pct(m.maxDrawdown)}`,
    `sharpe        ${m.sharpe.toFixed(2)}`,
    `trades        ${m.trades}`,
    `win rate      ${pct(m.winRate)}`,
    `profit factor ${Number.isFinite(m.profitFactor) ? m.profitFactor.toFixed(2) : "inf"}`,
    `fees          ${m.fees.toFixed(2)}`,
  ].join("\n");
}
