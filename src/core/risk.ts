import type { RiskLimits } from "./bot";

export interface RiskInput {
  equity: number;
  /** これまでの equity のピーク */
  peakEquity: number;
  /** 当日 (UTC) 開始時点の equity */
  dayStartEquity: number;
  consecutiveLosses: number;
}

/** 違反があれば理由を返す。なければ null */
export function checkRisk(limits: RiskLimits, input: RiskInput): string | null {
  const { equity, peakEquity, dayStartEquity, consecutiveLosses } = input;
  const pct = (x: number) => `${(x * 100).toFixed(2)}%`;
  const dd = peakEquity > 0 ? 1 - equity / peakEquity : 0;
  if (limits.maxDrawdown != null && dd >= limits.maxDrawdown) {
    return `drawdown ${pct(dd)} >= limit ${pct(limits.maxDrawdown)}`;
  }
  if (limits.maxDailyLoss != null && dayStartEquity > 0) {
    const loss = 1 - equity / dayStartEquity;
    if (loss >= limits.maxDailyLoss) return `daily loss ${pct(loss)} >= limit ${pct(limits.maxDailyLoss)}`;
  }
  if (limits.maxConsecutiveLosses != null && consecutiveLosses >= limits.maxConsecutiveLosses) {
    return `consecutive losses ${consecutiveLosses} >= limit ${limits.maxConsecutiveLosses}`;
  }
  return null;
}

/** sell 約定の realizedPnl を見て連敗数を更新する */
export function nextConsecutiveLosses(current: number, fill: { side: "buy" | "sell"; realizedPnl: number }): number {
  if (fill.side !== "sell") return current;
  return fill.realizedPnl > 0 ? 0 : current + 1;
}
