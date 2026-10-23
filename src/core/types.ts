export type Timeframe = "1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1d";

export const TIMEFRAME_MS: Record<Timeframe, number> = {
  "1m": 60_000,
  "5m": 5 * 60_000,
  "15m": 15 * 60_000,
  "30m": 30 * 60_000,
  "1h": 60 * 60_000,
  "4h": 4 * 60 * 60_000,
  "1d": 24 * 60 * 60_000,
};

/** 確定足。time は足の開始時刻 (UTC ms) */
export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type Mode = "backtest" | "paper" | "live";
export type DeployTarget = "paper" | "live";

export interface Position {
  /** 保有数量 (base 通貨。現物なので >= 0) */
  qty: number;
  /** 平均取得単価 */
  avgPrice: number;
}

export interface Fill {
  time: number;
  side: "buy" | "sell";
  qty: number;
  price: number;
  fee: number;
  /** この約定で確定した損益 (sell 時のみ非 0) */
  realizedPnl: number;
}

export interface EquityPoint {
  time: number;
  equity: number;
}
