import type { BacktestResult } from "./engine";

/** 外部依存なしの HTML レポート (インライン SVG の資産曲線) */
export function renderHtml(r: BacktestResult, title: string): string {
  const pts = r.equity;
  const w = 900;
  const h = 300;
  let svg = "";
  if (pts.length > 1) {
    const t0 = pts[0]!.time;
    const t1 = pts[pts.length - 1]!.time;
    const vals = pts.map((p) => p.equity);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * w;
    const y = (v: number) => h - ((v - lo) / (hi - lo || 1)) * h;
    const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.time).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
    svg = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>
<div class="axis"><span>${new Date(t0).toISOString().slice(0, 10)}</span><span>min ${lo.toFixed(2)} / max ${hi.toFixed(2)}</span><span>${new Date(t1).toISOString().slice(0, 10)}</span></div>`;
  }
  const m = r.metrics;
  const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
  const rows: [string, string][] = [
    ["Total return", pct(m.totalReturn)],
    ["Max drawdown", pct(m.maxDrawdown)],
    ["Sharpe", m.sharpe.toFixed(2)],
    ["Trades", String(m.trades)],
    ["Win rate", pct(m.winRate)],
    ["Profit factor", Number.isFinite(m.profitFactor) ? m.profitFactor.toFixed(2) : "∞"],
    ["Fees", m.fees.toFixed(2)],
    ["Halted", r.halted ? `${new Date(r.halted.time).toISOString()} — ${r.halted.reason}` : "no"],
  ];
  const esc = (s: string) => s.replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);
  return `<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title>
<style>body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#222}
svg{width:100%;height:300px;color:#2563eb;background:#f8fafc}.axis{display:flex;justify-content:space-between;font-size:12px;color:#666}
table{border-collapse:collapse;margin-top:1rem}td{padding:4px 12px;border-bottom:1px solid #eee}</style></head>
<body><h1>${esc(title)}</h1>${svg}<table>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(v)}</td></tr>`).join("")}</table></body></html>`;
}
