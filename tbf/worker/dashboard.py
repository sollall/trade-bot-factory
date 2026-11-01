from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from tbf.core.bot import Bot
from tbf.core.metrics import Metrics
from tbf.core.types import EquityPoint
from tbf.worker.store import BotRow


@dataclass
class BotSummary:
    bot: Bot
    row: Optional[BotRow]
    equity: float
    metrics: Metrics
    curve: list[EquityPoint]


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _sparkline(points: Sequence[EquityPoint]) -> str:
    if len(points) < 2:
        return ""
    vals = [p.equity for p in points]
    lo, hi = min(vals), max(vals)
    w, h = 160, 32
    d = "".join(
        f"{'L' if i else 'M'}{i / (len(vals) - 1) * w:.1f},{h - (v - lo) / ((hi - lo) or 1) * h:.1f}"
        for i, v in enumerate(vals)
    )
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"><path d="{d}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'


def render_dashboard(mode: str, items: Sequence[BotSummary]) -> str:
    rows = []
    for s in items:
        ret = s.equity / s.bot.capital - 1
        status = s.row.status if s.row else "pending"
        reason = f'<div class="sub">{_esc(s.row.halt_reason)}</div>' if s.row and s.row.halt_reason else ""
        m = s.metrics
        rows.append(
            f"""<tr>
<td><a href="api/bots/{_esc(s.bot.id)}">{_esc(s.bot.id)}</a><div class="sub">{_esc(", ".join(str(i) for i in s.bot.instruments.values()))} {s.bot.timeframe}</div></td>
<td class="st {status}">{status}{reason}</td>
<td class="num">{s.equity:.2f}</td>
<td class="num {'pos' if ret >= 0 else 'neg'}">{_pct(ret)}</td>
<td class="num">{_pct(m.max_drawdown)}</td>
<td class="num">{m.trades}</td>
<td class="num">{_pct(m.win_rate)}</td>
<td class="num">{m.sharpe:.2f}</td>
<td class="spark">{_sparkline(s.curve)}</td>
</tr>"""
        )
    body = "".join(rows) or '<tr><td colspan="9">この環境にデプロイされた bot はありません</td></tr>'
    badge = "var(--neg)" if mode == "live" else "var(--acc)"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>trade-bot-factory ({_esc(mode)})</title>
<style>
:root{{--fg:#1f2937;--bg:#fff;--mut:#6b7280;--line:#e5e7eb;--pos:#15803d;--neg:#b91c1c;--acc:#2563eb}}
@media (prefers-color-scheme:dark){{:root{{--fg:#e5e7eb;--bg:#111827;--mut:#9ca3af;--line:#374151;--pos:#4ade80;--neg:#f87171;--acc:#60a5fa}}}}
body{{font-family:system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:16px}}
h1{{font-size:1.2rem}}.mode{{padding:2px 8px;border-radius:4px;background:{badge};color:#fff;font-size:.8rem}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%}}th,td{{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
th{{font-size:.75rem;color:var(--mut);font-weight:600}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
.sub{{font-size:.75rem;color:var(--mut)}}.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.halted{{color:var(--neg);font-weight:600}}
.spark{{color:var(--acc)}}a{{color:inherit}}
</style></head><body>
<h1>trade-bot-factory <span class="mode">{_esc(mode)}</span></h1>
<div class="wrap"><table><thead><tr><th>bot</th><th>status</th><th class="num">equity</th><th class="num">return</th><th class="num">max DD</th><th class="num">trades</th><th class="num">win</th><th class="num">sharpe</th><th>30d</th></tr></thead>
<tbody>{body}</tbody></table></div>
<p class="sub">更新: {now}</p>
</body></html>"""
