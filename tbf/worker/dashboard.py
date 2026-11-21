"""ダッシュボードの HTML。一覧 (`/`) と bot ごとの詳細 (`/bots/<id>`)。

サーバー側で組み立てた静的な HTML を返す。JS は時刻の現地表示とグラフのホバーだけで、無くても読める。
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from tbf.core.bot import Bot
from tbf.core.metrics import Metrics
from tbf.core.portfolio import holding_value
from tbf.core.types import EquityPoint
from tbf.worker.store import BotRow

# グラフに出す点の上限 (1 分足の 30 日ぶんは 4 万点を超えるので間引く)
MAX_CHART_POINTS = 400
RANGES = {"7d": 7, "30d": 30, "all": 0}
RANGE_LABELS = {"7d": "7日", "30d": "30日", "all": "全期間"}


@dataclass
class BotSummary:
    bot: Bot
    row: Optional[BotRow]
    equity: float
    metrics: Metrics
    curve: list[EquityPoint]


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _signed_pct(x: float) -> str:
    return f'<span class="{"pos" if x >= 0 else "neg"}">{x * 100:+.2f}%</span>'


def _money(x: float) -> str:
    return f"{x:,.2f}"


def _signed_money(x: float) -> str:
    return f'<span class="{"pos" if x >= 0 else "neg"}">{x:+,.2f}</span>'


def _num(x: float) -> str:
    """数量・価格。桁の違う銘柄が並ぶので有効数字で出す"""
    return f"{x:,.6g}" if abs(x) < 1000 else f"{x:,.2f}"


def _time(ms: Any) -> str:
    """UTC で出し、JS があれば見ている人の現地時刻に置き換える"""
    try:
        t = int(ms)
    except (TypeError, ValueError):
        return ""
    if t <= 0:
        return "-"
    text = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%m-%d %H:%M")
    return f'<time data-t="{t}">{text} UTC</time>'


def downsample(points: Sequence[EquityPoint], limit: int = MAX_CHART_POINTS) -> list[EquityPoint]:
    """等間隔に間引く。最後の点 (今の値) は必ず残す"""
    if len(points) <= limit:
        return list(points)
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def _sparkline(points: Sequence[EquityPoint]) -> str:
    points = downsample(points, 80)
    if len(points) < 2:
        return ""
    vals = [p.equity for p in points]
    lo, hi = min(vals), max(vals)
    w, h = 160, 32
    d = "".join(
        f"{'L' if i else 'M'}{i / (len(vals) - 1) * w:.1f},{h - 1 - (v - lo) / ((hi - lo) or 1) * (h - 2):.1f}"
        for i, v in enumerate(vals)
    )
    return f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true"><path d="{d}" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>'


def _status(row: Optional[BotRow]) -> str:
    status = row.status if row else "pending"
    label = {"running": "● 稼働中", "halted": "■ 停止", "pending": "○ 未実行"}.get(status, status)
    reason = f'<div class="sub">{_esc(row.halt_reason)}</div>' if row and row.halt_reason else ""
    return f'<span class="st {_esc(status)}">{_esc(label)}</span>{reason}'


def _events_table(events: Sequence[Mapping[str, Any]], with_bot: bool, qs: str) -> str:
    if not events:
        return '<p class="sub">イベントはありません</p>'
    rows = []
    for e in events:
        level = str(e.get("level") or "")
        bot = ""
        if with_bot:
            b = e.get("bot_id")
            bot = f'<td><a href="bots/{_esc(b)}{qs}">{_esc(b)}</a></td>' if b else "<td>-</td>"
        rows.append(
            f'<tr><td>{_time(e.get("time"))}</td>{bot}<td class="lv {_esc(level)}">{_esc(level)}</td>'
            f'<td class="msg">{_esc(e.get("message") or "")}</td></tr>'
        )
    head = "<th>時刻</th>" + ("<th>bot</th>" if with_bot else "") + "<th>種類</th><th>内容</th>"
    return f'<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _health_line(health: Optional[Mapping[str, Any]]) -> str:
    if not health:
        return ""
    if "age_sec" not in health:
        return '<span class="st halted">■ cron はまだ一度も完了していません</span>'
    ok = bool(health.get("ok"))
    errors = int(health.get("last_tick_errors") or 0)
    mark = '<span class="st running">● cron 正常</span>' if ok else '<span class="st halted">■ cron が止まっています</span>'
    err = f' ・ <span class="neg">直近の実行でエラー {errors} 件</span>' if errors else ""
    took = f' ・ 所要 {float(health["duration_sec"]):.1f} 秒' if "duration_sec" in health else ""
    lag = f' ・ 起動の遅れ {float(health.get("last_tick_lag_sec") or 0):.1f} 秒'
    stale = ""
    if health.get("last_tick_build") and health.get("last_tick_build") != health.get("build"):
        stale = ' ・ <span class="neg">cron はまだ前の版で動いています</span>'
    return (
        f'{mark} <span class="sub">最後の完了 {float(health["age_sec"]):.0f} 秒前{took}{lag} ・ '
        f'build {_esc(str(health.get("build", ""))[:12])}</span>{err}{stale}'
    )


def _count(n: int) -> str:
    """0 なら普通の字、1 以上なら赤で目立たせる"""
    return f'<span class="neg">{n}</span>' if n else "0"


def _tile(label: str, value: str, sub: str = "") -> str:
    return f'<div class="tile"><div class="lbl">{label}</div><div class="val">{value}</div><div class="sub">{sub}</div></div>'


_CSS = """
:root{color-scheme:light;--fg:#0b0b0b;--fg2:#52514e;--bg:#fcfcfb;--card:#f4f3f0;--line:#e3e2dd;--pos:#0a6b0a;--neg:#b3261e;--warn:#8a5a00;--acc:#2a78d6;--live:#b3261e}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--fg:#f0efec;--fg2:#c3c2b7;--bg:#1a1a19;--card:#262624;--line:#3a3a37;--pos:#4cc34c;--neg:#f08080;--warn:#e0a83a;--acc:#3987e5;--live:#c9403a}}
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:16px;max-width:1100px;margin-inline:auto;line-height:1.45}
h1{font-size:1.2rem;margin:0 0 4px}h2{font-size:.95rem;margin:28px 0 8px}
a{color:inherit}.back{font-size:.85rem;color:var(--fg2)}
.mode{padding:2px 8px;border-radius:4px;background:var(--acc);color:#fff;font-size:.8rem;vertical-align:middle}.mode.live{background:var(--live)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:16px 0}
.tile{background:var(--card);border-radius:8px;padding:10px 12px}.lbl{font-size:.75rem;color:var(--fg2)}
.val{font-size:1.3rem;font-weight:600;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-size:.75rem;color:var(--fg2);font-weight:600}.num{text-align:right;font-variant-numeric:tabular-nums}
td.msg{white-space:normal;min-width:240px}
.sub{font-size:.75rem;color:var(--fg2)}.pos{color:var(--pos)}.neg{color:var(--neg)}
.st{font-weight:600}.st.running{color:var(--pos)}.st.halted{color:var(--neg)}.st.pending{color:var(--fg2)}
.lv.error{color:var(--neg);font-weight:600}.lv.warn{color:var(--warn);font-weight:600}
.spark{color:var(--acc)}.alert{border:1px solid var(--neg);border-radius:8px;padding:8px 12px;margin:12px 0}
.tabs{display:flex;gap:4px;margin-bottom:8px}.tabs a{padding:3px 10px;border-radius:6px;font-size:.8rem;text-decoration:none;color:var(--fg2);border:1px solid var(--line)}
.tabs a.on{background:var(--card);color:var(--fg);font-weight:600}
.chart{position:relative;height:240px;margin:0 0 36px 0;padding-right:64px}.chart.short{height:140px}
.plot{position:relative;height:100%;touch-action:pan-y;--c:var(--acc)}.plot.dd{--c:var(--neg)}.plot svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.grid{position:absolute;left:0;right:0;border-top:1px solid var(--line)}
.ylab{position:absolute;left:100%;transform:translateY(-50%);padding-left:6px;font-size:.7rem;color:var(--fg2);font-variant-numeric:tabular-nums}
.xlab{position:absolute;top:100%;padding-top:4px;font-size:.7rem;color:var(--fg2)}
.cross{position:absolute;top:0;bottom:0;width:1px;background:var(--fg2);display:none;pointer-events:none}
.dot{position:absolute;width:10px;height:10px;margin:-5px 0 0 -5px;border-radius:50%;background:var(--c);border:2px solid var(--bg);display:none;pointer-events:none}
.tip{position:absolute;top:4px;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 8px;font-size:.75rem;display:none;pointer-events:none;white-space:nowrap;font-variant-numeric:tabular-nums}
details{margin-top:8px}pre{background:var(--card);padding:10px;border-radius:8px;overflow-x:auto;font-size:.8rem}
"""

# 時刻を現地表示にする + グラフのホバー。データは数値だけを JSON で埋め込む
_JS = """
const f=new Intl.DateTimeFormat(undefined,{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
document.querySelectorAll('time[data-t]').forEach(e=>{e.textContent=f.format(new Date(+e.dataset.t))});
document.querySelectorAll('.plot[data-pts]').forEach(p=>{
  const d=JSON.parse(p.dataset.pts),lo=+p.dataset.lo,hi=+p.dataset.hi;if(d.length<2)return;
  const t0=d[0][0],t1=d[d.length-1][0],c=p.querySelector('.cross'),o=p.querySelector('.dot'),tip=p.querySelector('.tip');
  const hide=()=>{c.style.display=o.style.display=tip.style.display='none'};
  p.addEventListener('pointerleave',hide);
  p.addEventListener('pointermove',ev=>{
    const r=p.getBoundingClientRect(),t=t0+(ev.clientX-r.left)/r.width*(t1-t0);
    let b=d[0];for(const q of d)if(Math.abs(q[0]-t)<Math.abs(b[0]-t))b=q;
    const x=(b[0]-t0)/((t1-t0)||1)*100,y=(1-(b[1]-lo)/((hi-lo)||1))*100;
    c.style.left=o.style.left=x+'%';o.style.top=y+'%';c.style.display=o.style.display=tip.style.display='block';
    tip.textContent=f.format(new Date(b[0]))+'  '+b[1].toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})+(p.dataset.unit||'');
    if(x>60){tip.style.left='auto';tip.style.right=(100-x)+'%';tip.style.marginRight='8px'}else{tip.style.right='auto';tip.style.left=x+'%';tip.style.marginLeft='8px'}
  });
});
"""


def _page(title: str, mode: str, body: str) -> str:
    now = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><meta name="robots" content="noindex"><meta name="referrer" content="no-referrer">
<title>{_esc(title)} ({_esc(mode)})</title>
<style>{_CSS}</style></head><body>
{body}
<p class="sub">更新: {_time(now)} ・ 60 秒ごとに自動で読み直します</p>
<script>{_JS}</script>
</body></html>"""


def _mode_badge(mode: str) -> str:
    return f'<span class="mode{" live" if mode == "live" else ""}">{_esc(mode)}</span>'


def render_dashboard(
    mode: str,
    items: Sequence[BotSummary],
    health: Optional[Mapping[str, Any]] = None,
    events: Sequence[Mapping[str, Any]] = (),
    qs: str = "",
    errors_1h: Optional[Mapping[str, int]] = None,
    unmanaged: Optional[Mapping[str, Mapping[str, float]]] = None,
    execution: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> str:
    """一覧。qs は `?token=...` で入ってきたときにリンクへ引き継ぐクエリ。
    errors_1h: bot ごとの直近 1 時間のエラー件数 / unmanaged: デプロイから外れたのに残っている持ち高 /
    execution: 約定の質 (ops.execution_summary)"""
    errors_1h, unmanaged, execution = errors_1h or {}, unmanaged or {}, execution or {}
    rows = []
    for s in items:
        ret = s.equity / s.bot.capital - 1
        day = s.equity / s.row.day_start_equity - 1 if s.row and s.row.day_start_equity > 0 else 0.0
        m = s.metrics
        rows.append(
            f"""<tr>
<td><a href="bots/{_esc(s.bot.id)}{qs}">{_esc(s.bot.id)}</a><div class="sub">{_esc(", ".join(str(i) for i in s.bot.instruments.values()))} {s.bot.timeframe}</div></td>
<td>{_status(s.row)}</td>
<td class="num">{_money(s.equity)}</td>
<td class="num">{_signed_pct(ret)}</td>
<td class="num">{_signed_pct(day)}</td>
<td class="num">{_pct(m.current_drawdown)}<div class="sub">最大 {_pct(m.max_drawdown)}</div></td>
<td class="num">{m.trades}</td>
<td class="num">{_pct(m.win_rate)}</td>
<td class="num">{s.row.consecutive_losses if s.row else 0}</td>
<td class="num">{_count(errors_1h.get(s.bot.id, 0))}</td>
<td class="num">{m.sharpe:.2f}</td>
<td class="spark">{_sparkline(s.curve)}</td>
</tr>"""
        )
    table = "".join(rows) or '<tr><td colspan="12">この環境にデプロイされた bot はありません</td></tr>'
    capital = sum(s.bot.capital for s in items)
    total = sum(s.equity for s in items)
    running = sum(1 for s in items if s.row and s.row.status == "running")
    halted = sum(1 for s in items if s.row and s.row.status == "halted")
    halted_text = f'<span class="neg">停止 {halted}</span>' if halted else "停止 0"
    tiles = "".join(
        [
            _tile("合計資産 (USD)", _money(total), f"元手 {_money(capital)}"),
            _tile("合計損益", _signed_money(total - capital), _signed_pct(total / capital - 1) if capital > 0 else ""),
            _tile("bot", f"{running} <span class=\"sub\">/ {len(items)} 稼働中</span>", halted_text),
            _tile("直近 1 時間のエラー", _count(sum(errors_1h.values())), "bot ごとの件数は下の表"),
            _tile("管理外の持ち高", _count(len(unmanaged)), "デプロイから外れた bot に残っている持ち高"),
        ]
    )
    alert = ""
    if unmanaged:
        items_text = "; ".join(f"{_esc(b)}: " + ", ".join(f"{_esc(a)} {_num(q)}" for a, q in pos.items()) for b, pos in unmanaged.items())
        alert = f'<div class="alert"><span class="neg">■ デプロイされていない bot に持ち高が残っています (手動で処理が必要)</span><div class="sub">{items_text}</div></div>'
    body = f"""<h1>trade-bot-factory {_mode_badge(mode)}</h1>
<div>{_health_line(health)}</div>
<div class="tiles">{tiles}</div>
{alert}
<div class="wrap"><table><thead><tr><th>bot</th><th>状態</th><th class="num">資産</th><th class="num">累計</th><th class="num">今日 (UTC)</th><th class="num">DD</th><th class="num">決済</th><th class="num">勝率</th><th class="num">連敗</th><th class="num">エラー (1h)</th><th class="num">シャープ</th><th>30日</th></tr></thead>
<tbody>{table}</tbody></table></div>
<h2>約定の質 (30日)</h2>
{_execution_table(execution, [s.bot.id for s in items], qs, True)}
<h2>直近のイベント</h2>
{_events_table(events, True, qs)}"""
    return _page("trade-bot-factory", mode, body)


def drawdown_curve(curve: Sequence[EquityPoint], since: int = 0) -> list[EquityPoint]:
    """ピークからの下げ (%, 0 以下)。ピークは全期間で数え、since より前は表示から外す"""
    out, peak = [], 0.0
    for p in curve:
        peak = max(peak, p.equity)
        if p.time >= since:
            out.append(EquityPoint(p.time, -(1 - p.equity / peak) * 100 if peak > 0 else 0.0))
    return out


def downsample_min(points: Sequence[EquityPoint], limit: int = MAX_CHART_POINTS) -> list[EquityPoint]:
    """区間ごとの最小値で間引く (等間隔に拾うと、いちばん深い下げを落とすことがある)"""
    if len(points) <= limit:
        return list(points)
    size = len(points) / limit
    out = [min(points[int(i * size) : max(int((i + 1) * size), int(i * size) + 1)], key=lambda p: p.equity) for i in range(limit)]
    if out[-1] is not points[-1]:
        out.append(points[-1])  # 今の値は必ず残す
    return out


def _chart(points: Sequence[EquityPoint], label: str = "資産推移 (USD)", unit: str = " USD", kind: str = "equity") -> str:
    """kind="equity": 資産 (上下に余白) / kind="drawdown": ピークからの下げ (上端が 0%)"""
    dd = kind == "drawdown"
    points = downsample_min(points) if dd else downsample(points)
    if len(points) < 2:
        return '<p class="sub">この期間の記録はまだありません</p>'
    vals = [p.equity for p in points]
    lo, hi = min(vals), max(vals)
    if dd:
        lo, hi = min(lo * 1.1, -0.5), 0.0
    else:
        pad = (hi - lo) * 0.08 or max(abs(hi) * 0.001, 0.01)
        lo, hi = lo - pad, hi + pad
    fmt = (lambda v: f"{v:.1f}%") if dd else _money
    t0, t1 = points[0].time, points[-1].time
    span = (t1 - t0) or 1
    d = "".join(
        f"{'L' if i else 'M'}{(p.time - t0) / span * 100:.2f},{(1 - (p.equity - lo) / (hi - lo)) * 100:.2f}"
        for i, p in enumerate(points)
    )
    grid = "".join(
        f'<div class="grid" style="top:{(1 - k / 3) * 100:.1f}%"></div>'
        f'<div class="ylab" style="top:{(1 - k / 3) * 100:.1f}%">{fmt(lo + (hi - lo) * k / 3)}</div>'
        for k in range(4)
    )
    pts = json.dumps([[p.time, round(p.equity, 4)] for p in points], separators=(",", ":"))
    cls = "plot dd" if dd else "plot"
    return f"""<div class="chart{' short' if dd else ''}"><div class="{cls}" data-lo="{lo}" data-hi="{hi}" data-unit="{_esc(unit)}" data-pts="{_esc(pts)}" role="img" aria-label="{_esc(label)}">
{grid}
<svg viewBox="0 0 100 100" preserveAspectRatio="none"><path d="{d}" fill="none" stroke="var(--c)" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/></svg>
<div class="xlab" style="left:0">{_time(t0)}</div><div class="xlab" style="right:0">{_time(t1)}</div>
<div class="cross"></div><div class="dot"></div><div class="tip"></div>
</div></div>"""


def _execution_table(execution: Mapping[str, Mapping[str, float]], bot_ids: Sequence[str], qs: str, link: bool) -> str:
    """約定の質 (30 日)。足の確定から発注までの遅れと、バックテストの想定価格との差"""
    rows = []
    for b in bot_ids:
        ex = execution.get(b)
        if not ex:
            continue
        name = f'<td><a href="bots/{_esc(b)}{qs}">{_esc(b)}</a></td>' if link else ""
        gap = float(ex["gap_bps"])
        rows.append(
            f'<tr>{name}<td class="num">{int(ex["fills"])}</td><td class="num">{float(ex["delay_avg_sec"]):.1f}</td>'
            f'<td class="num">{float(ex["delay_p95_sec"]):.1f}</td><td class="num">{float(ex["delay_max_sec"]):.1f}</td>'
            f'<td class="num"><span class="{"neg" if gap > 0 else "pos"}">{gap:+.2f}</span></td>'
            f'<td class="num">{float(ex["gap_usd"]):+,.2f}</td><td class="num">{_money(float(ex["notional_usd"]))}</td></tr>'
        )
    if not rows:
        return '<p class="sub">この 30 日に、遅れと価格差を記録した約定はありません</p>'
    head = ("<th>bot</th>" if link else "") + (
        '<th class="num">約定</th><th class="num">遅れ 平均 (秒)</th><th class="num">95% (秒)</th><th class="num">最大 (秒)</th>'
        '<th class="num">想定との差 (bps)</th><th class="num">差額 (USD)</th><th class="num">約定代金 (USD)</th>'
    )
    return f"""<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>
<p class="sub">遅れ = 足の確定から発注まで。想定との差 = バックテストが仮定した約定価格との差 (+ は不利)</p>"""


def _holdings_table(bot: Bot, row: Optional[BotRow]) -> str:
    if not row:
        return '<p class="sub">まだ実行されていません</p>'
    pf = row.load_portfolio(bot)
    prices = row.prices()
    rows = []
    for alias, inst in bot.instruments.items():
        pos = pf.holdings.get(alias)
        if not pos or not pos.qty:
            rows.append(f'<tr><td>{_esc(alias)}<div class="sub">{_esc(inst)}</div></td><td class="num">0</td><td class="num">-</td><td class="num">-</td><td class="num">-</td></tr>')
            continue
        px = prices.get(alias)
        value = holding_value(inst, pos, px if px else pos.avg_price)
        shown = _signed_money(value) if inst.is_perp else _money(value)
        note = "含み損益" if inst.is_perp else "時価"
        rows.append(
            f'<tr><td>{_esc(alias)}<div class="sub">{_esc(inst)}</div></td><td class="num">{_num(pos.qty)}</td>'
            f'<td class="num">{_num(pos.avg_price)}</td><td class="num">{_num(px) if px else "-"}</td>'
            f'<td class="num">{shown}<div class="sub">{note}</div></td></tr>'
        )
    cash = " ・ ".join(f"{_esc(v)} {_money(c)}" for v, c in pf.cash.items()) or "-"
    return f"""<div class="wrap"><table><thead><tr><th>銘柄</th><th class="num">数量</th><th class="num">建値</th><th class="num">今の価格</th><th class="num">評価 (USD)</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
<p class="sub">現金 (USD): {cash}</p>"""


def _fills_table(fills: Sequence[Mapping[str, Any]]) -> str:
    """fills は新しい順"""
    if not fills:
        return '<p class="sub">約定はまだありません</p>'
    rows = []
    for f in fills:
        pnl = _signed_money(float(f.get("realized_pnl") or 0)) if f.get("reduce") else "-"
        side = str(f.get("side") or "")
        rows.append(
            f'<tr><td>{_time(f.get("time"))}</td><td>{_esc(f.get("instrument") or "")}</td><td>{_esc("買" if side == "buy" else "売" if side == "sell" else side)}</td>'
            f'<td class="num">{_num(float(f.get("qty") or 0))}</td><td class="num">{_num(float(f.get("price") or 0))}</td>'
            f'<td class="num">{float(f.get("fee") or 0):,.4f}</td><td class="num">{pnl}</td><td class="msg">{_esc(f.get("reason") or "")}</td></tr>'
        )
    return f"""<div class="wrap"><table><thead><tr><th>時刻</th><th>銘柄</th><th>売買</th><th class="num">数量</th><th class="num">価格</th><th class="num">手数料</th><th class="num">確定損益</th><th>理由</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>"""


def _limit(limit: Optional[float], fmt) -> str:
    return f"上限 {fmt(limit)}" if limit is not None else "上限なし"


def render_bot_page(
    mode: str,
    s: BotSummary,
    fills: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    range_key: str = "30d",
    qs: str = "",
    drawdown: Sequence[EquityPoint] = (),
    execution: Optional[Mapping[str, float]] = None,
    errors_1h: int = 0,
) -> str:
    """bot ごとの詳細。s.curve は range_key の期間に絞った資産推移、fills は新しい順"""
    bot, row, m = s.bot, s.row, s.metrics
    ret = s.equity / bot.capital - 1
    day = s.equity / row.day_start_equity - 1 if row and row.day_start_equity > 0 else 0.0
    dd = 1 - s.equity / row.peak_equity if row and row.peak_equity > 0 else m.current_drawdown
    streak = row.consecutive_losses if row else 0
    risk = bot.risk
    tiles = "".join(
        [
            _tile("資産 (USD)", _money(s.equity), f"元手 {_money(bot.capital)}"),
            _tile("累計損益", _signed_pct(ret), _signed_money(s.equity - bot.capital)),
            _tile("今日 (UTC)", _signed_pct(day), _limit(risk.max_daily_loss, lambda x: f"-{_pct(x)}")),
            _tile("ピークからの下げ", _pct(max(dd, 0.0)), _limit(risk.max_drawdown, _pct) + f" ・ 過去最大 {_pct(m.max_drawdown)}"),
            _tile("連敗", str(streak), _limit(risk.max_consecutive_losses, lambda x: f"{x} 回")),
            _tile("決済 / 勝率", f"{m.trades} <span class=\"sub\">/ {_pct(m.win_rate)}</span>", f"シャープ {m.sharpe:.2f} ・ 手数料 {_money(m.fees)}"),
        ]
    )
    sep = "&" if qs else "?"
    tabs = "".join(
        f'<a class="{"on" if k == range_key else ""}" href="{_esc(bot.id)}{qs}{sep}range={k}">{RANGE_LABELS[k]}</a>' for k in RANGES
    )
    config = json.dumps(
        {"timeframe": bot.timeframe, "leverage": bot.leverage, "params": bot.params, "risk": vars(risk)},
        indent=2, ensure_ascii=False, default=str,
    )
    desc = f'<p class="sub">{_esc(bot.description)}</p>' if bot.description else ""
    body = f"""<a class="back" href="../{qs}">← 一覧</a>
<h1>{_esc(bot.id)} {_mode_badge(mode)}</h1>
<div>{_status(row)} <span class="sub">{_esc(", ".join(str(i) for i in bot.instruments.values()))} ・ {_esc(bot.timeframe)} ・ 最後に見た足 {_time(row.last_candle) if row else "-"}</span></div>
{desc}
<div class="tiles">{tiles}</div>
<h2>資産推移 (USD)</h2>
<div class="tabs">{tabs}</div>
{_chart(s.curve)}
<h2>ピークからの下げ (%)</h2>
{_chart(drawdown, "ピークからの下げ (%)", "%", "drawdown")}
<h2>持ち高</h2>
{_holdings_table(bot, row)}
<h2>直近の約定</h2>
{_fills_table(fills)}
<h2>約定の質 (30日)</h2>
{_execution_table({bot.id: execution} if execution else {}, [bot.id], qs, False)}
<h2>イベント <span class="sub">直近 1 時間のエラー {_count(errors_1h)} 件</span></h2>
{_events_table(events, False, qs)}
<details><summary class="sub">設定 (パラメータ・リスク上限)</summary><pre>{_esc(config)}</pre></details>
<p class="sub"><a href="../api/bots/{_esc(bot.id)}{qs}">JSON で見る</a></p>"""
    return _page(bot.id, mode, body)
