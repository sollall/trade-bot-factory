import type { Fill } from "../../core/types";

export interface OrderRequest {
  botId: string;
  symbol: string;
  side: "buy" | "sell";
  /** base 数量 */
  qty: number;
  /** 判断時の参照価格 */
  refPrice: number;
  /** 冪等キー (同じキーで二重発注しない) */
  clientOrderId: string;
  takerRate: number;
  slippageBps: number;
  now: number;
}

export type BrokerFill = Omit<Fill, "realizedPnl"> & { orderId: string | null };

export interface Broker {
  /** 約定しなかった (最小数量未満など) 場合は null */
  execute(req: OrderRequest): Promise<BrokerFill | null>;
}
