import { simulateFill } from "../../core/portfolio";
import type { Broker, BrokerFill, OrderRequest } from "./types";

/** 現在価格 + スリッページ + 手数料で即時約定させる (バックテストと同じモデル) */
export class PaperBroker implements Broker {
  async execute(req: OrderRequest): Promise<BrokerFill> {
    const f = simulateFill(req.side, req.qty, req.refPrice, req.now, req.takerRate, req.slippageBps);
    return { ...f, orderId: null };
  }
}
