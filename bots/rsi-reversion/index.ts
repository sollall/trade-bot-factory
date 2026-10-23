import { defineBot } from "../../src/core/bot";
import { rsi } from "../../src/core/indicators";

export default defineBot({
  id: "rsi-reversion",
  description: "RSI が売られすぎで買い、買われすぎで手仕舞い",
  symbol: "ETHUSDT",
  timeframe: "15m",
  lookback: 100,
  capital: 1000,
  deploy: ["paper"],
  risk: { maxDrawdown: 0.15, maxDailyLoss: 0.05, maxConsecutiveLosses: 8, flattenOnHalt: true },
  params: { period: 14, buyBelow: 30, sellAbove: 55 },
  strategy: ({ candles, params, position }) => {
    const r = rsi(candles.map((c) => c.close), params.period);
    if (r == null) return;
    if (position.qty === 0 && r < params.buyBelow) return { targetExposure: 1, reason: `rsi ${r.toFixed(1)}` };
    if (position.qty > 0 && r > params.sellAbove) return { targetExposure: 0, reason: `rsi ${r.toFixed(1)}` };
  },
});
