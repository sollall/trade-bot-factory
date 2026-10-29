from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Sequence

from tbf.neutral.data import FundingPoint
from tbf.neutral.engine import NeutralResult
from tbf.neutral.strategies import HOURS_PER_YEAR


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _path(xs: Sequence[float], ys: Sequence[float], w: int, h: int, lo: float, hi: float) -> str:
    x0, x1 = xs[0], xs[-1]
    return "".join(
        f"{'L' if i else 'M'}{(x - x0) / ((x1 - x0) or 1) * w:.1f},{h - (y - lo) / ((hi - lo) or 1) * h:.1f}"
        for i, (x, y) in enumerate(zip(xs, ys))
    )


def render_html(title: str, result: NeutralResult, baseline: NeutralResult, funding: Sequence[FundingPoint]) -> str:
    w, h = 900, 260
    eq_svg = apr_svg = ""
    if len(result.equity) > 1:
        xs = [p.time for p in result.equity]
        vals = [p.equity for p in result.equity] + [p.equity for p in baseline.equity]
        lo, hi = min(vals), max(vals)
        eq_svg = (
            f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<path d="{_path([p.time for p in baseline.equity], [p.equity for p in baseline.equity], w, h, lo, hi)}" class="base"/>'
            f'<path d="{_path(xs, [p.equity for p in result.equity], w, h, lo, hi)}" class="main"/></svg>'
            f'<div class="axis"><span>{_date(xs[0])}</span><span>min {lo:,.0f} / max {hi:,.0f}</span><span>{_date(xs[-1])}</span></div>'
        )
    if len(funding) > 24:
        # 24 時間移動平均の年率
        aprs, ts = [], []
        for i in range(24, len(funding) + 1):
            win = funding[i - 24 : i]
            aprs.append(sum(f.rate for f in win) / 24 * HOURS_PER_YEAR * 100)
            ts.append(win[-1].time)
        lo, hi = min(min(aprs), 0), max(aprs)
        zero = h - (0 - lo) / ((hi - lo) or 1) * h
        apr_svg = (
            f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none"><line x1="0" x2="{w}" y1="{zero:.1f}" y2="{zero:.1f}" class="zero"/>'
            f'<path d="{_path(ts, aprs, w, h, lo, hi)}" class="main"/></svg>'
            f'<div class="axis"><span>{_date(ts[0])}</span><span>min {lo:.1f}% / max {hi:.1f}%</span><span>{_date(ts[-1])}</span></div>'
        )
    pct = lambda v: f"{v * 100:.2f}%"  # noqa: E731
    rows = []
    for label, r in (("戦略", result), ("持ちっぱなし", baseline)):
        s = r.summary()
        rows.append(
            f"<tr><td>{label}</td><td>{pct(s['apr'])}</td><td>{pct(s['total_return'])}</td><td>{pct(s['max_drawdown'])}</td>"
            f"<td>{s['funding_income']:,.2f}</td><td>{s['fees']:,.2f}</td><td>{s['price_pnl']:,.2f}</td>"
            f"<td>{pct(s['time_in_market'])}</td><td>{s['entries']}</td><td>{s['rebalances']}</td><td>{s['liquidations']}</td></tr>"
        )
    events = "".join(
        f"<tr><td>{datetime.fromtimestamp(e.time / 1000, tz=timezone.utc):%Y-%m-%d %H:%M}</td><td>{e.kind}</td><td>{html.escape(e.detail)}</td></tr>"
        for e in result.events
        if e.kind != "rebalance"
    )
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#222}}
svg{{width:100%;height:260px;background:#f8fafc}}.main{{fill:none;stroke:#2563eb;stroke-width:1.5;vector-effect:non-scaling-stroke}}
.base{{fill:none;stroke:#9ca3af;stroke-width:1;vector-effect:non-scaling-stroke}}.zero{{stroke:#d1d5db;vector-effect:non-scaling-stroke}}
.axis{{display:flex;justify-content:space-between;font-size:12px;color:#666}}.wrap{{overflow-x:auto}}
table{{border-collapse:collapse;margin-top:1rem}}td,th{{padding:4px 10px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap}}td:first-child,th:first-child{{text-align:left}}</style></head>
<body><h1>{html.escape(title)}</h1>
<h2>資産推移 <small>(青: 戦略 / 灰: 持ちっぱなし)</small></h2>{eq_svg}
<h2>ファンディング (24h 平均, 年率)</h2>{apr_svg}
<div class="wrap"><table><tr><th></th><th>年率</th><th>期間リターン</th><th>最大DD</th><th>ファンディング</th><th>手数料</th><th>価格差損益</th><th>建玉時間</th><th>建て回数</th><th>調整回数</th><th>清算</th></tr>{''.join(rows)}</table></div>
<h2>イベント</h2><div class="wrap"><table><tr><th>時刻 (UTC)</th><th>種類</th><th>詳細</th></tr>{events}</table></div>
</body></html>"""
