import type { BotDefinition } from "../core/bot";
import { computeMetrics } from "../core/metrics";
import { applyFill, equityOf, type PortfolioState } from "../core/portfolio";
import { checkRisk, nextConsecutiveLosses } from "../core/risk";
import { runStrategy, utcDay } from "../core/step";
import { TIMEFRAME_MS, type Candle, type DeployTarget, type Timeframe } from "../core/types";
import type { Broker } from "./brokers/types";
import type { Env } from "./env";
import { notify } from "./notify";
import type { BotRow, Store } from "./store";

export interface MarketData {
  prices(symbols: string[]): Promise<Record<string, number>>;
  recent(symbol: string, timeframe: Timeframe, count: number, now: number): Promise<{ closed: Candle[] }>;
}

export interface RunnerDeps {
  env: Env;
  mode: DeployTarget;
  store: Store;
  market: MarketData;
  broker: Broker;
  now: number;
}

export function botsForMode(all: BotDefinition<any>[], mode: DeployTarget): BotDefinition<any>[] {
  return all.filter((b) => b.deploy.includes(mode));
}

/** cron 1 回分。全 bot を監視し、新しい確定足があれば戦略を実行する */
export async function tick(bots: BotDefinition<any>[], deps: RunnerDeps): Promise<void> {
  if (bots.length === 0) return;
  const prices = await deps.market.prices(bots.map((b) => b.symbol));
  for (const bot of bots) {
    try {
      const price = prices[bot.symbol];
      if (!(price && price > 0)) throw new Error(`no price for ${bot.symbol}`);
      await runBot(bot, price, deps);
    } catch (e) {
      const msg = `${bot.id}: ${e instanceof Error ? e.message : String(e)}`;
      console.error(msg);
      await deps.store.addEvent(bot.id, "error", msg, deps.now);
      await notify(deps.env, `:warning: error ${msg}`);
    }
  }
}

async function runBot(bot: BotDefinition<any>, price: number, deps: RunnerDeps): Promise<void> {
  const { store, now } = deps;
  let row = await store.getOrInit(bot.id, bot.capital, now);

  // --- 監視 (毎 tick) ---
  const eq = equityOf(toPortfolio(row), price);
  const day = utcDay(now);
  if (row.day !== day) {
    row.day = day;
    row.day_start_equity = eq;
  }
  row.peak_equity = Math.max(row.peak_equity, eq);
  row.last_price = price;
  row.updated_at = now;
  await store.saveMonitor(row);

  if (row.status === "running") {
    const breach = checkRisk(bot.risk, {
      equity: eq,
      peakEquity: row.peak_equity,
      dayStartEquity: row.day_start_equity,
      consecutiveLosses: row.consecutive_losses,
    });
    if (breach) {
      await halt(bot, `risk: ${breach}`, bot.risk.flattenOnHalt !== false, price, deps);
      return;
    }
  }
  if (row.status !== "running") return;

  // --- 戦略 (新しい確定足ごと) ---
  const tf = TIMEFRAME_MS[bot.timeframe];
  const latestClosed = Math.floor(now / tf) * tf - tf;
  if (latestClosed <= row.last_candle) return;
  if (!(await store.claimCandle(bot.id, latestClosed))) return;

  const { closed } = await deps.market.recent(bot.symbol, bot.timeframe, bot.lookback, now);
  row = (await store.get(bot.id))!;
  if (closed.length < bot.lookback) {
    await store.addEvent(bot.id, "warn", `only ${closed.length}/${bot.lookback} candles available`, now);
    return;
  }

  const state = JSON.parse(row.strategy_state) as Record<string, unknown>;
  const step = runStrategy(bot, closed, toPortfolio(row), price, state, deps.mode);
  row.strategy_state = JSON.stringify(state);

  if (step.qty !== 0) {
    row = await trade(bot, row, step.qty, price, clientOrderId(bot.id, latestClosed), step.reason ?? null, deps);
  }
  row.updated_at = now;
  await store.saveAccount(row);
  await store.addEquity(bot.id, { time: latestClosed, equity: equityOf(toPortfolio(row), price) });
}

async function trade(
  bot: BotDefinition<any>,
  row: BotRow,
  qty: number,
  price: number,
  orderId: string,
  reason: string | null,
  deps: RunnerDeps,
): Promise<BotRow> {
  const side = qty > 0 ? "buy" : "sell";
  const res = await deps.broker.execute({
    botId: bot.id,
    symbol: bot.symbol,
    side,
    qty: Math.abs(qty),
    refPrice: price,
    clientOrderId: orderId,
    takerRate: bot.fees.takerRate,
    slippageBps: bot.fees.slippageBps,
    now: deps.now,
  });
  if (!res) {
    await deps.store.addEvent(bot.id, "info", `${side} ${Math.abs(qty)} skipped (below exchange minimum)`, deps.now);
    return row;
  }
  const { orderId: exchangeOrderId, ...fill } = res;
  const applied = applyFill(toPortfolio(row), fill);
  const next: BotRow = {
    ...row,
    cash: applied.state.cash,
    qty: applied.state.position.qty,
    avg_price: applied.state.position.avgPrice,
    consecutive_losses: nextConsecutiveLosses(row.consecutive_losses, applied.fill),
  };
  await deps.store.addFill(bot.id, applied.fill, exchangeOrderId, reason);
  if (deps.env.NOTIFY_FILLS === "true") {
    const f = applied.fill;
    const pnl = f.side === "sell" ? ` pnl ${f.realizedPnl.toFixed(2)}` : "";
    await notify(deps.env, `${bot.id} ${f.side} ${f.qty} ${bot.symbol} @ ${f.price.toFixed(4)}${pnl}${reason ? ` (${reason})` : ""}`);
  }
  return next;
}

/** bot を停止する。flatten なら保有を成行で手仕舞う */
export async function halt(
  bot: BotDefinition<any>,
  reason: string,
  flatten: boolean,
  price: number,
  deps: RunnerDeps,
): Promise<void> {
  const { store, now } = deps;
  await store.setStatus(bot.id, "halted", reason);
  await store.addEvent(bot.id, "warn", `halted: ${reason}`, now);
  let msg = `:octagonal_sign: ${bot.id} halted — ${reason}`;
  const row = await store.get(bot.id);
  if (flatten && row && row.qty > 0 && !(price > 0)) {
    msg += " — FLATTEN SKIPPED (no price), manual action required";
  } else if (flatten && row && row.qty > 0) {
    try {
      const next = await trade(bot, row, -row.qty, price, clientOrderId(bot.id, now, "h"), "halt", deps);
      next.updated_at = now;
      await store.saveAccount(next);
      msg += next.qty > 0 ? ` (position partially closed, ${next.qty} left)` : " (position closed)";
    } catch (e) {
      const err = e instanceof Error ? e.message : String(e);
      await store.addEvent(bot.id, "error", `flatten failed: ${err}`, now);
      msg += ` — FLATTEN FAILED, manual action required: ${err}`;
    }
  }
  await notify(deps.env, msg);
}

/** 1 日 1 回の成績レポート */
export async function dailyReport(bots: BotDefinition<any>[], deps: RunnerDeps): Promise<void> {
  if (bots.length === 0) return;
  const lines: string[] = [`:bar_chart: daily report (${utcDay(deps.now)})`];
  for (const bot of bots) {
    const row = await deps.store.get(bot.id);
    if (!row) continue;
    const m = computeMetrics(await deps.store.equity(bot.id), await deps.store.fills(bot.id));
    const eq = equityOf(toPortfolio(row), row.last_price ?? 0);
    const pct = (x: number) => `${(x * 100).toFixed(2)}%`;
    lines.push(
      `• ${bot.id} [${row.status}] equity ${eq.toFixed(2)} (${pct(eq / bot.capital - 1)}) ` +
        `maxDD ${pct(m.maxDrawdown)} trades ${m.trades} win ${pct(m.winRate)} sharpe ${m.sharpe.toFixed(2)}`,
    );
  }
  await notify(deps.env, lines.join("\n"));
}

export function toPortfolio(row: BotRow): PortfolioState {
  return { cash: row.cash, position: { qty: row.qty, avgPrice: row.avg_price } };
}

/** Binance の newClientOrderId は 36 文字まで */
function clientOrderId(botId: string, time: number, prefix = ""): string {
  return `${botId.slice(0, 24)}-${prefix}${Math.floor(time / 1000).toString(36)}`;
}
