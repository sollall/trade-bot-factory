from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Sequence

from tbf.backtest.engine import BacktestResult


def _date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _path(pts, w: int, h: int, t0: int, t1: int, lo: float, hi: float) -> str:
    return "".join(
        f"{'L' if i else 'M'}{(p.time - t0) / ((t1 - t0) or 1) * w:.1f},{h - (p.equity - lo) / ((hi - lo) or 1) * h:.1f}"
        for i, p in enumerate(pts)
    )


COLORS = ["#2563eb", "#9ca3af", "#d97706", "#059669", "#db2777", "#7c3aed"]


def render_html(title: str, results: Sequence[tuple[str, BacktestResult]]) -> str:
    """外部依存なしの HTML レポート。results は (ラベル, 結果) のリスト (先頭が主役)"""
    w, h = 900, 300
    svg = ""
    curves = [r.equity for _, r in results if len(r.equity) > 1]
    if curves:
        t0 = min(c[0].time for c in curves)
        t1 = max(c[-1].time for c in curves)
        vals = [p.equity for c in curves for p in c]
        lo, hi = min(vals), max(vals)
        paths = "".join(
            f'<path d="{_path(r.equity, w, h, t0, t1, lo, hi)}" stroke="{COLORS[i % len(COLORS)]}"/>'
            for i, (_, r) in reversed(list(enumerate(results)))
            if len(r.equity) > 1
        )
        legend = " ".join(
            f'<span style="color:{COLORS[i % len(COLORS)]}">■ {html.escape(label)}</span>' for i, (label, _) in enumerate(results)
        )
        svg = (
            f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">{paths}</svg>'
            f'<div class="axis"><span>{_date(t0)}</span><span>min {lo:,.2f} / max {hi:,.2f}</span><span>{_date(t1)}</span></div>'
            f"<p>{legend}</p>"
        )
    pct = lambda v: f"{v * 100:.2f}%"  # noqa: E731
    head = "<tr><th></th><th>年率</th><th>期間</th><th>最大DD</th><th>シャープ</th><th>決済回数</th><th>勝率</th><th>ファンディング</th><th>手数料</th><th>値動き損益</th><th>建玉時間</th><th>清算</th><th>停止</th></tr>"
    rows = []
    for label, r in results:
        s = r.summary()
        rows.append(
            f"<tr><td>{html.escape(label)}</td><td>{pct(s['apr'])}</td><td>{pct(s['total_return'])}</td><td>{pct(s['max_drawdown'])}</td>"
            f"<td>{s['sharpe']:.2f}</td><td>{s['trades']}</td><td>{pct(s['win_rate'])}</td><td>{s['funding_income']:,.2f}</td>"
            f"<td>{s['fees']:,.2f}</td><td>{s['price_pnl']:,.2f}</td><td>{pct(s['time_in_market'])}</td><td>{s['liquidations']}</td>"
            f"<td>{html.escape(s['halted'] or '-')}</td></tr>"
        )
    main = results[0][1] if results else None
    events = ""
    if main:
        events = "".join(
            f"<tr><td>{datetime.fromtimestamp(e.time / 1000, tz=timezone.utc):%Y-%m-%d %H:%M}</td><td>{e.kind}</td><td>{html.escape(e.detail)}</td></tr>"
            for e in main.events
            if e.kind != "rebalance"
        )
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#222}}
svg{{width:100%;height:300px;background:#f8fafc}}svg path{{fill:none;stroke-width:1.5;vector-effect:non-scaling-stroke}}
.axis{{display:flex;justify-content:space-between;font-size:12px;color:#666}}.wrap{{overflow-x:auto}}
table{{border-collapse:collapse;margin-top:1rem}}td,th{{padding:4px 10px;border-bottom:1px solid #eee;text-align:right;white-space:nowrap}}
td:first-child,th:first-child{{text-align:left}}</style></head>
<body><h1>{html.escape(title)}</h1>{svg}
<div class="wrap"><table>{head}{''.join(rows)}</table></div>
<h2>イベント ({html.escape(results[0][0]) if results else ''})</h2><div class="wrap"><table><tr><th>時刻 (UTC)</th><th>種類</th><th>詳細</th></tr>{events}</table></div>
</body></html>"""

