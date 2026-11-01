"""
PR ごとのお試し paper Worker (.github/workflows/preview.yml から呼ぶ)。標準ライブラリだけで動く。

  bots    : PR で追加・変更された bot id (bots/<id>/ 以下に差分があるもの)。なければ全 bot
            python scripts/preview.py bots --base origin/main
  config  : その PR 専用の Worker / D1 の wrangler 設定を書き出す
            python scripts/preview.py config --pr 12 --db-name trade-bot-pr-12-ab12cd --db-id <uuid>
  report  : Worker の成績を PR コメント用の Markdown にする
            python scripts/preview.py report --url https://... --token ... --meta-from comment.md
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tbf.registry import bot_ids  # noqa: E402

MARKER = "<!-- tbf-preview -->"
META_RE = re.compile(r"<!-- tbf-meta (\{.*?\}) -->")


def worker_name(pr: int) -> str:
    return f"trade-bot-pr-{pr}"


# ---- bots ----


def changed_bots(changed_files: list[str], all_ids: list[str]) -> list[str]:
    ids = set()
    for f in changed_files:
        parts = f.split("/")
        if len(parts) >= 3 and parts[0] == "bots" and parts[1] in all_ids:
            ids.add(parts[1])
    return sorted(ids)


def cmd_bots(base: str) -> None:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD", "--", "bots/"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    all_ids = bot_ids()
    ids = changed_bots(out, all_ids)
    if not ids:
        print("bot の変更がないので全 bot を動かします", file=sys.stderr)
        ids = all_ids
    print(",".join(ids))


# ---- config ----


def render_config(pr: int, db_name: str, db_id: str, base: Optional[dict] = None) -> str:
    base = base if base is not None else tomllib.loads((ROOT / "wrangler.toml").read_text())
    flags = json.dumps(base.get("compatibility_flags", ["python_workers"]))
    return f"""# 自動生成 (scripts/preview.py) — PR #{pr} のお試し paper Worker
name = "{worker_name(pr)}"
main = ".build/entry.py"
compatibility_date = "{base["compatibility_date"]}"
compatibility_flags = {flags}

[observability]
enabled = true

[vars]
MODE = "paper"
PREVIEW = "true"
INSTANCE = "pr-{pr}"
NOTIFY_FILLS = "false"

[triggers]
crons = ["* * * * *", "5 0 * * *"]

[[d1_databases]]
binding = "DB"
database_name = "{db_name}"
database_id = "{db_id}"
migrations_dir = "migrations"
"""


# ---- report ----


def _get(url: str, token: Optional[str]) -> tuple[int, object]:
    headers = {"user-agent": "trade-bot-factory-preview"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, None
    except (OSError, ValueError) as e:
        return 0, {"error": str(e)}


def read_meta(text: str) -> dict:
    m = META_RE.search(text or "")
    return json.loads(m.group(1)) if m else {}


def _pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 100:+.2f}%"


def _exec(ex: Optional[dict]) -> str:
    if not ex:
        return "- | -"
    return f"{ex['delay_avg_sec']:.1f}s / {ex['delay_p95_sec']:.1f}s | {ex['gap_bps']:+.1f}bps ({ex['gap_usd']:+.2f} USD)"


def _rate(m: dict) -> str:
    return f"{m['win_rate'] * 100:.0f}%" if m.get("trades") and m.get("win_rate") is not None else "-"


def render_report(meta: dict, health: tuple[int, object], bots: tuple[int, object], now: float, final: bool = False) -> str:
    started = meta.get("started")
    days = f"{(now - started) / 86400:.1f} 日" if started else "-"
    sha = (meta.get("sha") or "")[:12]
    hs, hb = health
    if final:
        state = "終了 (Worker と D1 は削除しました)"
    elif hs == 200:
        state = "稼働中"
    else:
        reason = hb.get("reason") or hb.get("error") if isinstance(hb, dict) else None
        state = f"**異常** ({hs}: {reason or '応答なし'})"
    lines = [
        MARKER,
        f"<!-- tbf-meta {json.dumps(meta, separators=(',', ':'))} -->",
        "### paper お試し Worker",
        "",
        f"| | |\n|---|---|\n| 状態 | {state} |\n| 経過 | {days} (push するたびにリセット) |\n| build | `{sha}` |"
        + (f"\n| Worker | {meta['url']} (ダッシュボードは `?token=` が必要) |" if meta.get("url") and not final else ""),
        "",
    ]
    bs, bb = bots
    if bs == 200 and isinstance(bb, list) and bb:
        lines += [
            "| bot | 状態 | 資産 | リターン | 最大DD | シャープ | 取引 | 勝率 | 発注まで (平均 / p95) | 対バックテスト |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for b in bb:
            m = b.get("metrics") or {}
            lines.append(
                f"| `{b['id']}` | {b.get('status')} | {b.get('equity', 0):,.2f} | {_pct(m.get('total_return'))} | "
                f"{_pct(-abs(m['max_drawdown'])) if 'max_drawdown' in m else '-'} | {m.get('sharpe', 0):.2f} | "
                f"{m.get('trades', 0)} | {_rate(m)} | {_exec(b.get('execution'))} |"
            )
        if any(b.get("execution") for b in bb):
            lines += [
                "",
                "<sub>発注まで: 足の確定から発注までの秒数。対バックテスト: バックテストが仮定する約定価格"
                " (確定足の終値 + 同じスリッページ) との差、+ は不利。直近 30 日</sub>",
            ]
    elif bs:
        lines.append(f"成績を取得できませんでした (status {bs})")
    lines += ["", f"<sub>更新: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}</sub>"]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bots")
    b.add_argument("--base", required=True)
    c = sub.add_parser("config")
    c.add_argument("--pr", type=int, required=True)
    c.add_argument("--db-name", required=True)
    c.add_argument("--db-id", required=True)
    c.add_argument("--out", default=str(ROOT / "wrangler.preview.toml"))
    r = sub.add_parser("report")
    r.add_argument("--url", default="")
    r.add_argument("--token", default="")
    r.add_argument("--meta-from", default="", help="既存の PR コメント (meta を引き継ぐ)")
    r.add_argument("--sha", default="")
    r.add_argument("--started", type=float, default=None)
    r.add_argument("--final", action="store_true")
    a = ap.parse_args()

    if a.cmd == "bots":
        cmd_bots(a.base)
    elif a.cmd == "config":
        Path(a.out).write_text(render_config(a.pr, a.db_name, a.db_id))
        print(f"wrote {a.out}")
    else:
        meta = read_meta(Path(a.meta_from).read_text()) if a.meta_from and Path(a.meta_from).exists() else {}
        if a.url:
            meta["url"] = a.url
        if a.sha:
            meta["sha"] = a.sha
        if a.started is not None:
            meta["started"] = a.started
        url = meta.get("url", "").rstrip("/")
        health, bots = (0, None), (0, None)
        if url:
            if not a.final:
                health = _get(f"{url}/healthz", None)
            bots = _get(f"{url}/api/bots", a.token or None)  # final は Worker を消す前に呼ぶ
        sys.stdout.write(render_report(meta, health, bots, time.time(), a.final))


if __name__ == "__main__":
    main()
