import type { Fill, Position } from "./types";

export interface PortfolioState {
  cash: number;
  position: Position;
}

export function equityOf(s: PortfolioState, price: number): number {
  return s.cash + s.position.qty * price;
}

/**
 * 目標比率に合わせるための base 数量の差分を返す (+ は買い, - は売り)。
 * 閾値未満の差は 0 を返す。
 */
export function orderQtyForTarget(
  s: PortfolioState,
  price: number,
  targetExposure: number,
  threshold: number,
  /** 手数料 + スリッページ等、約定価格に上乗せされうるコスト率 */
  costRate: number,
): number {
  const equity = equityOf(s, price);
  if (equity <= 0 || price <= 0) return 0;
  const targetQty = (equity * targetExposure) / price;
  let delta = targetQty - s.position.qty;
  // 全決済は端数を残さない
  if (targetExposure === 0) delta = -s.position.qty;
  if (Math.abs(delta * price) < equity * threshold && !(targetExposure === 0 && s.position.qty > 0)) return 0;
  if (delta > 0) {
    // 手数料・スリッページ込みで現金を超えないようにする
    const maxQty = s.cash / (price * (1 + costRate) ** 2);
    delta = Math.min(delta, maxQty);
    if (delta <= 0) return 0;
  }
  return delta;
}

/** 約定を反映した新しい状態と、realizedPnl を埋めた Fill を返す */
export function applyFill(
  s: PortfolioState,
  fill: Omit<Fill, "realizedPnl">,
): { state: PortfolioState; fill: Fill } {
  const { qty, price, fee } = fill;
  if (fill.side === "buy") {
    const newQty = s.position.qty + qty;
    const avgPrice = newQty > 0 ? (s.position.qty * s.position.avgPrice + qty * price) / newQty : 0;
    return {
      state: { cash: s.cash - qty * price - fee, position: { qty: newQty, avgPrice } },
      fill: { ...fill, realizedPnl: 0 },
    };
  }
  const sellQty = Math.min(qty, s.position.qty);
  const realizedPnl = (price - s.position.avgPrice) * sellQty - fee;
  const remaining = s.position.qty - sellQty;
  return {
    state: {
      cash: s.cash + sellQty * price - fee,
      position: remaining > 1e-12 ? { qty: remaining, avgPrice: s.position.avgPrice } : { qty: 0, avgPrice: 0 },
    },
    fill: { ...fill, qty: sellQty, realizedPnl },
  };
}

/** スリッページ込みのシミュレーション約定 */
export function simulateFill(
  side: "buy" | "sell",
  qty: number,
  refPrice: number,
  time: number,
  takerRate: number,
  slippageBps: number,
): Omit<Fill, "realizedPnl"> {
  const slip = (slippageBps / 10_000) * refPrice;
  const price = side === "buy" ? refPrice + slip : refPrice - slip;
  return { time, side, qty, price, fee: qty * price * takerRate };
}
