/**
 * ローカルバックテスト
 *   npm run backtest -- --bot sma-cross --from 2025-01-01 --to 2025-07-01
 *   npm run backtest -- --bot all --days 90
 *   npm run backtest -- --bot sma-cross --param fast=20 --param slow=100
 *   npm run backtest -- --bot sma-cross --csv ./my-data.csv
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { parseArgs } from "node:util";
import { bots } from "../generated/bots";
import { formatMetrics } from "../core/metrics";
import { backtest } from "./engine";
import { loadCandles, loadCsv } from "./data";
import { renderHtml } from "./report";

const { values } = parseArgs({
  options: {
    bot: { type: "string", default: "all" },
    from: { type: "string" },
    to: { type: "string" },
    days: { type: "string", default: "180" },
    csv: { type: "string" },
    param: { type: "string", multiple: true, default: [] },
    out: { type: "string", default: "reports" },
  },
});

const to = values.to ? Date.parse(values.to) : Date.now();
const from = values.from ? Date.parse(values.from) : to - Number(values.days) * 86_400_000;
if (!Number.isFinite(from) || !Number.isFinite(to) || from >= to) throw new Error("invalid --from/--to");

const targets = values.bot === "all" ? bots : bots.filter((b) => b.id === values.bot);
if (targets.length === 0) {
  console.error(`bot "${values.bot}" not found. available: ${bots.map((b) => b.id).join(", ")}`);
  process.exit(1);
}

const overrides: Record<string, unknown> = {};
for (const kv of values.param) {
  const [k, ...rest] = kv.split("=");
  const raw = rest.join("=");
  overrides[k!] = raw !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw === "true" ? true : raw === "false" ? false : raw;
}

mkdirSync(values.out, { recursive: true });
for (const base of targets) {
  const bot = { ...base, params: { ...base.params, ...overrides } };
  const candles = values.csv ? loadCsv(values.csv) : await loadCandles(bot.symbol, bot.timeframe, from, to);
  const result = backtest(bot, candles);
  const range = candles.length
    ? `${new Date(candles[0]!.time).toISOString().slice(0, 10)} .. ${new Date(candles[candles.length - 1]!.time).toISOString().slice(0, 10)}`
    : "(no data)";
  console.log(`\n=== ${bot.id}  ${bot.symbol} ${bot.timeframe}  ${range}  (${candles.length} candles)`);
  console.log(`params        ${JSON.stringify(bot.params)}`);
  console.log(formatMetrics(result.metrics));
  if (result.halted) console.log(`HALTED        ${new Date(result.halted.time).toISOString()} ${result.halted.reason}`);
  writeFileSync(`${values.out}/${bot.id}.json`, JSON.stringify({ params: bot.params, ...result }, null, 2));
  writeFileSync(`${values.out}/${bot.id}.html`, renderHtml(result, `${bot.id} backtest (${range})`));
}
console.log(`\nreports written to ${values.out}/`);
