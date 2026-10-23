from __future__ import annotations

import html
import math
from datetime import datetime, timezone

from tbf.backtest.engine import BacktestResult


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def render_html(r: BacktestResult, title: str) -> str:
    """外部依存なしの HTML レポート (インライン SVG の資産曲線)"""
    pts = r.equity
    w, h = 900, 300
    svg = ""
    if len(pts) > 1:
        t0, t1 = pts[0].time, pts[-1].time
        vals = [p.equity for p in pts]
        lo, hi = min(vals), max(vals)
        x = lambda t: (t - t0) / ((t1 - t0) or 1) * w  # noqa: E731
        y = lambda v: h - (v - lo) / ((hi - lo) or 1) * h  # noqa: E731
        d = "".join(f"{'L' if i else 'M'}{x(p.time):.1f},{y(p.equity):.1f}" for i, p in enumerate(pts))
        svg = (
            f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none"><path d="{d}" fill="none" stroke="currentColor" '
            f'stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>'
            f'<div class="axis"><span>{_date(t0)}</span><span>min {lo:.2f} / max {hi:.2f}</span><span>{_date(t1)}</span></div>'
        )
    m = r.metrics
    pct = lambda v: f"{v * 100:.2f}%"  # noqa: E731
    rows = [
        ("Total return", pct(m.total_return)),
        ("Max drawdown", pct(m.max_drawdown)),
        ("Sharpe", f"{m.sharpe:.2f}"),
        ("Trades", str(m.trades)),
        ("Win rate", pct(m.win_rate)),
        ("Profit factor", "∞" if math.isinf(m.profit_factor) else f"{m.profit_factor:.2f}"),
        ("Fees", f"{m.fees:.2f}"),
        ("Halted", f"{_date(r.halted.time)} — {r.halted.reason}" if r.halted else "no"),
    ]
    table = "".join(f"<tr><td>{k}</td><td>{html.escape(v)}</td></tr>" for k, v in rows)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#222}}
svg{{width:100%;height:300px;color:#2563eb;background:#f8fafc}}.axis{{display:flex;justify-content:space-between;font-size:12px;color:#666}}
table{{border-collapse:collapse;margin-top:1rem}}td{{padding:4px 12px;border-bottom:1px solid #eee}}</style></head>
<body><h1>{html.escape(title)}</h1>{svg}<table>{table}</table></body></html>"""
