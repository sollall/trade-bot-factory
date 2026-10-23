import type { BotDefinition } from "../core/bot";
import type { Metrics } from "../core/metrics";
import type { EquityPoint } from "../core/types";
import type { BotRow } from "./store";

export interface BotSummary {
  bot: BotDefinition<any>;
  row: BotRow | null;
  equity: number;
  metrics: Metrics;
  curve: EquityPoint[];
}

const esc = (s: string) => s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const pct = (x: number) => `${(x * 100).toFixed(2)}%`;

function sparkline(points: EquityPoint[]): string {
  if (points.length < 2) return "";
  const vals = points.map((p) => p.equity);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  const w = 160;
  const h = 32;
  const d = vals
    .map((v, i) => `${i ? "L" : "M"}${((i / (vals.length - 1)) * w).toFixed(1)},${(h - ((v - lo) / (hi - lo || 1)) * h).toFixed(1)}`)
    .join("");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;
}

export function renderDashboard(mode: string, items: BotSummary[]): string {
  const rows = items
    .map(({ bot, row, equity, metrics: m, curve }) => {
      const ret = equity / bot.capital - 1;
      const status = row?.status ?? "pending";
      return `<tr>
<td><a href="api/bots/${esc(bot.id)}">${esc(bot.id)}</a><div class="sub">${esc(bot.symbol)} ${bot.timeframe}</div></td>
<td class="st ${status}">${status}${row?.halt_reason ? `<div class="sub">${esc(row.halt_reason)}</div>` : ""}</td>
<td class="num">${equity.toFixed(2)}</td>
<td class="num ${ret >= 0 ? "pos" : "neg"}">${pct(ret)}</td>
<td class="num">${pct(m.maxDrawdown)}</td>
<td class="num">${m.trades}</td>
<td class="num">${pct(m.winRate)}</td>
<td class="num">${m.sharpe.toFixed(2)}</td>
<td class="spark">${sparkline(curve)}</td>
</tr>`;
    })
    .join("");
  return `<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>trade-bot-factory (${esc(mode)})</title>
<style>
:root{--fg:#1f2937;--bg:#fff;--mut:#6b7280;--line:#e5e7eb;--pos:#15803d;--neg:#b91c1c;--acc:#2563eb}
@media (prefers-color-scheme:dark){:root{--fg:#e5e7eb;--bg:#111827;--mut:#9ca3af;--line:#374151;--pos:#4ade80;--neg:#f87171;--acc:#60a5fa}}
body{font-family:system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:16px}
h1{font-size:1.2rem}.mode{padding:2px 8px;border-radius:4px;background:${mode === "live" ? "var(--neg)" : "var(--acc)"};color:#fff;font-size:.8rem}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-size:.75rem;color:var(--mut);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}
.sub{font-size:.75rem;color:var(--mut)}.pos{color:var(--pos)}.neg{color:var(--neg)}.halted{color:var(--neg);font-weight:600}
.spark{color:var(--acc)}a{color:inherit}
</style></head><body>
<h1>trade-bot-factory <span class="mode">${esc(mode)}</span></h1>
<div class="wrap"><table><thead><tr><th>bot</th><th>status</th><th class="num">equity</th><th class="num">return</th><th class="num">max DD</th><th class="num">trades</th><th class="num">win</th><th class="num">sharpe</th><th>30d</th></tr></thead>
<tbody>${rows || `<tr><td colspan="9">この環境にデプロイされた bot はありません</td></tr>`}</tbody></table></div>
<p class="sub">更新: ${new Date().toISOString()}</p>
</body></html>`;
}
