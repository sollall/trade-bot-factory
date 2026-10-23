import type { Candle, DeployTarget, Mode, Position, Timeframe } from "./types";

export interface StrategyContext<P> {
  /** 古い順。末尾が直近の確定足。長さは最大 lookback */
  candles: Candle[];
  position: Position;
  cash: number;
  equity: number;
  params: P;
  /** 実行間で永続化される自由領域 (JSON 化可能な値のみ) */
  state: Record<string, unknown>;
  mode: Mode;
}

/**
 * 戦略の出力。targetExposure は資産 (equity) に対する保有比率 0..1。
 * null / undefined を返すと「何もしない」。
 */
export type Decision = { targetExposure: number; reason?: string } | null | undefined | void;

export type Strategy<P> = (ctx: StrategyContext<P>) => Decision;

export interface RiskLimits {
  /** ピークからの最大ドローダウン (0.2 = 20%)。超えたら停止 */
  maxDrawdown?: number;
  /** UTC 日次の最大損失率 (日初 equity 比)。超えたら停止 */
  maxDailyLoss?: number;
  /** 連敗数の上限。超えたら停止 */
  maxConsecutiveLosses?: number;
  /** 停止時にポジションを解消するか (default: true) */
  flattenOnHalt?: boolean;
}

export interface FeeModel {
  /** 片道手数料率 (0.001 = 0.1%) */
  takerRate: number;
  /** バックテスト / ペーパーでのスリッページ (bps) */
  slippageBps: number;
}

export interface BotDefinition<P = Record<string, unknown>> {
  id: string;
  description?: string;
  /** 取引所のシンボル (例: BTCUSDT) */
  symbol: string;
  timeframe: Timeframe;
  /** 戦略に渡す過去足の本数 */
  lookback: number;
  /** バックテスト / ペーパーの初期資金、本番では bot に割り当てる資金 (quote 通貨) */
  capital: number;
  /** どの環境にデプロイするか。paper で検証してから live を足す */
  deploy: DeployTarget[];
  params: P;
  risk: RiskLimits;
  fees: FeeModel;
  /** 目標比率との差がこの比率 (対 equity) 未満なら発注しない */
  rebalanceThreshold: number;
  strategy: Strategy<P>;
}

export type BotInput<P> = Omit<BotDefinition<P>, "fees" | "rebalanceThreshold" | "risk" | "deploy"> &
  Partial<Pick<BotDefinition<P>, "fees" | "rebalanceThreshold" | "risk" | "deploy">>;

const ID_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

export function defineBot<P>(input: BotInput<P>): BotDefinition<P> {
  if (!ID_RE.test(input.id)) throw new Error(`invalid bot id "${input.id}" (use a-z, 0-9, -)`);
  if (input.lookback < 1) throw new Error(`${input.id}: lookback must be >= 1`);
  if (input.capital <= 0) throw new Error(`${input.id}: capital must be > 0`);
  return {
    deploy: ["paper"],
    risk: { maxDrawdown: 0.25, flattenOnHalt: true },
    fees: { takerRate: 0.001, slippageBps: 5 },
    rebalanceThreshold: 0.01,
    ...input,
  };
}

export function clampExposure(x: number): number {
  if (!Number.isFinite(x)) return 0;
  return Math.min(1, Math.max(0, x));
}
