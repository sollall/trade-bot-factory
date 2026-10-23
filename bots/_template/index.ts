import { defineBot } from "../../src/core/bot";

export default defineBot({
  id: "__BOT_ID__",
  description: "",
  symbol: "BTCUSDT",
  timeframe: "1h",
  lookback: 50,
  capital: 1000,
  // まずは paper だけ。成績が良ければ "live" を足す
  deploy: ["paper"],
  risk: { maxDrawdown: 0.2, maxDailyLoss: 0.05, flattenOnHalt: true },
  params: {},
  strategy: ({ candles }) => {
    const last = candles[candles.length - 1];
    if (!last) return;
    // targetExposure: 資産に対する保有比率 (0 = ノーポジ, 1 = 全力ロング)
    return { targetExposure: 0 };
  },
});
