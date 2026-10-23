import { bots as registry } from "../generated/bots";
import type { BotDefinition } from "../core/bot";
import { computeMetrics } from "../core/metrics";
import type { DeployTarget } from "../core/types";
import { DEFAULT_MARKET_DATA_URL, fetchPrices, fetchRecent } from "../market/binance";
import { BinanceBroker } from "./brokers/binance";
import { PaperBroker } from "./brokers/paper";
import type { Broker } from "./brokers/types";
import { renderDashboard, type BotSummary } from "./dashboard";
import type { Env } from "./env";
import { botsForMode, dailyReport, halt, tick, toPortfolio, type RunnerDeps } from "./runner";
import { Store } from "./store";
import { equityOf } from "../core/portfolio";

const DAILY_CRON = "5 0 * * *";

function modeOf(env: Env): DeployTarget {
  if (env.MODE !== "paper" && env.MODE !== "live") throw new Error(`MODE must be "paper" or "live" (got "${env.MODE}")`);
  return env.MODE;
}

function makeBroker(env: Env, mode: DeployTarget): Broker {
  if (mode === "paper") return new PaperBroker();
  if (!env.BINANCE_API_KEY || !env.BINANCE_API_SECRET) {
    throw new Error("live mode requires BINANCE_API_KEY / BINANCE_API_SECRET secrets");
  }
  return new BinanceBroker(env.BINANCE_API_KEY, env.BINANCE_API_SECRET, env.BINANCE_API_URL);
}

function makeDeps(env: Env, now: number): RunnerDeps {
  const mode = modeOf(env);
  const base = env.MARKET_DATA_URL ?? DEFAULT_MARKET_DATA_URL;
  return {
    env,
    mode,
    now,
    store: new Store(env.DB),
    broker: makeBroker(env, mode),
    market: {
      prices: (symbols) => fetchPrices(symbols, base),
      recent: (symbol, tf, count, t) => fetchRecent(symbol, tf, count, t, base),
    },
  };
}

function activeBots(env: Env): BotDefinition<any>[] {
  return botsForMode(registry, modeOf(env));
}

async function summarize(store: Store, bot: BotDefinition<any>, now: number): Promise<BotSummary> {
  const row = await store.get(bot.id);
  const curve = await store.equity(bot.id);
  const fills = await store.fills(bot.id);
  const equity = row ? equityOf(toPortfolio(row), row.last_price ?? 0) : bot.capital;
  return {
    bot,
    row,
    equity,
    metrics: computeMetrics(curve, fills),
    curve: curve.filter((p) => p.time >= now - 30 * 86_400_000),
  };
}

function authorized(req: Request, env: Env): boolean {
  if (!env.ADMIN_TOKEN) return true;
  const url = new URL(req.url);
  const header = req.headers.get("authorization");
  const given = header?.startsWith("Bearer ") ? header.slice(7) : url.searchParams.get("token");
  return given === env.ADMIN_TOKEN;
}

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data, null, 2), { status, headers: { "content-type": "application/json" } });

export default {
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    const bots = activeBots(env);
    if (bots.length === 0) return;
    const deps = makeDeps(env, controller.scheduledTime);
    if (controller.cron === DAILY_CRON) {
      await dailyReport(bots, deps);
      return;
    }
    await tick(bots, deps);
  },

  async fetch(req: Request, env: Env): Promise<Response> {
    if (!authorized(req, env)) return new Response("unauthorized", { status: 401 });
    const url = new URL(req.url);
    const now = Date.now();
    const store = new Store(env.DB);
    const bots = activeBots(env);
    const parts = url.pathname.split("/").filter(Boolean);

    if (req.method === "GET" && parts.length === 0) {
      const items = await Promise.all(bots.map((b) => summarize(store, b, now)));
      return new Response(renderDashboard(env.MODE, items), { headers: { "content-type": "text/html; charset=utf-8" } });
    }
    if (parts[0] !== "api") return new Response("not found", { status: 404 });

    // GET /api/bots
    if (req.method === "GET" && parts[1] === "bots" && parts.length === 2) {
      const items = await Promise.all(bots.map((b) => summarize(store, b, now)));
      return json(items.map(({ bot, row, equity, metrics }) => ({ id: bot.id, symbol: bot.symbol, status: row?.status ?? "pending", equity, metrics })));
    }
    // POST /api/tick (手動実行)
    if (req.method === "POST" && parts[1] === "tick") {
      await tick(bots, makeDeps(env, now));
      return json({ ok: true });
    }
    // GET /api/events
    if (req.method === "GET" && parts[1] === "events") return json(await store.events(null, 100));

    if (parts[1] === "bots" && parts[2]) {
      const bot = bots.find((b) => b.id === parts[2]);
      if (!bot) return json({ error: "bot not found" }, 404);
      const action = parts[3];

      if (req.method === "GET" && !action) {
        const s = await summarize(store, bot, now);
        return json({
          id: bot.id,
          description: bot.description,
          symbol: bot.symbol,
          timeframe: bot.timeframe,
          params: bot.params,
          risk: bot.risk,
          state: s.row,
          equity: s.equity,
          metrics: s.metrics,
          recentFills: (await store.fills(bot.id, 50)).reverse(),
          events: await store.events(bot.id, 50),
        });
      }
      // POST /api/bots/:id/halt?flatten=true
      if (req.method === "POST" && action === "halt") {
        const deps = makeDeps(env, now);
        await store.getOrInit(bot.id, bot.capital, now);
        const flatten = url.searchParams.get("flatten") === "true";
        const price = (await deps.market.prices([bot.symbol]))[bot.symbol] ?? 0;
        await halt(bot, "manual", flatten, price, deps);
        return json({ ok: true, flatten });
      }
      // POST /api/bots/:id/resume
      if (req.method === "POST" && action === "resume") {
        const row = await store.get(bot.id);
        if (!row) return json({ error: "bot has not run yet" }, 400);
        const price = (await makeDeps(env, now).market.prices([bot.symbol]))[bot.symbol] ?? row.last_price ?? 0;
        await store.resume(bot.id, equityOf(toPortfolio(row), price), now);
        await store.addEvent(bot.id, "info", "resumed", now);
        return json({ ok: true });
      }
    }
    return json({ error: "not found" }, 404);
  },
} satisfies ExportedHandler<Env>;
