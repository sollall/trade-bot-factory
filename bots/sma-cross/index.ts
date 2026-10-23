import { defineBot } from "../../src/core/bot";
import { sma } from "../../src/core/indicators";

export default defineBot({
  id: "sma-cross",
  description: "短期 SMA が長期 SMA を上回っている間ロング",
  symbol: "BTCUSDT",
  timeframe: "1h",
  lookback: 60,
  capital: 1000,
  deploy: ["paper"],
  risk: { maxDrawdown: 0.2, maxDailyLoss: 0.05, flattenOnHalt: true },
  params: { fast: 10, slow: 50 },
  strategy: ({ candles, params }) => {
    const closes = candles.map((c) => c.close);
    const fast = sma(closes, params.fast);
    const slow = sma(closes, params.slow);
    if (fast == null || slow == null) return;
    return fast > slow
      ? { targetExposure: 1, reason: `fast ${fast.toFixed(2)} > slow ${slow.toFixed(2)}` }
      : { targetExposure: 0, reason: `fast ${fast.toFixed(2)} <= slow ${slow.toFixed(2)}` };
  },
});
