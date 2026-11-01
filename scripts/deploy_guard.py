"""
デプロイの前後チェック (.github/workflows/deploy-worker.yml から呼ぶ)。標準ライブラリだけで動く。

  preflight: 今の D1 の内容と、これからデプロイする bot 一覧を比べる。
             bot を消した / deploy から外したのに持ち高が残っていたら止める
             (そのままデプロイすると、誰も管理しないポジションが取引所に残る)
      npx wrangler d1 execute trade-bot-live --env live --remote --json \\
          --command "SELECT bot_id, status, portfolio, qty FROM bot_state" > rows.json
      python scripts/deploy_guard.py preflight --mode live --rows rows.json [--allow bot_a,bot_b]

  verify: デプロイ後、新しい版の cron が実際に完了したことを /healthz で確かめる。
          時間内に確認できなければ失敗 (ワークフローが直前の版にロールバックする)
      python scripts/deploy_guard.py verify --url https://trade-bot-live.example.workers.dev --build <sha>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tbf.registry import load_all  # noqa: E402
from tbf.worker.ops import unknown_instruments, unmanaged_positions  # noqa: E402
from tbf.worker.runner import bots_for_mode  # noqa: E402


def d1_rows(text: str) -> list[dict]:
    """wrangler d1 execute --json の出力 ([{"results": [...], ...}]) から行を取り出す"""
    data = json.loads(text) if text.strip() else []
    if isinstance(data, dict):
        data = [data]
    rows: list[dict] = []
    for item in data:
        if isinstance(item, dict) and "results" in item:
            rows += item["results"] or []
        elif isinstance(item, dict):
            rows.append(item)
    return rows


def preflight(mode: str, rows: list[dict], allow: set[str]) -> int:
    bots = bots_for_mode(load_all(), mode)
    ids = [b.id for b in bots]
    left = {b: p for b, p in unmanaged_positions(rows, ids).items() if b not in allow}
    lost = {b: p for b, p in unknown_instruments(rows, bots).items() if b not in allow}
    print(f"[{mode}] deploying {len(ids)} bot(s): {', '.join(ids) or '(none)'}")
    if not left and not lost:
        print(f"[{mode}] ok: 取り残される持ち高はありません")
        return 0
    for b, pos in left.items():
        print(f"::error::[{mode}] {b} はデプロイから外れますが持ち高が残っています: {pos}")
    for b, pos in lost.items():
        print(f"::error::[{mode}] {b} の instruments から消える別名に持ち高が残っています: {pos}")
    print(
        "先に手仕舞ってからデプロイしてください:\n"
        f"  curl -X POST -H 'Authorization: Bearer $ADMIN_TOKEN' '<worker>/api/bots/<id>/halt?flatten=true'\n"
        "  (手仕舞いは今デプロイされている版で行うので、この変更をマージする前に実行する)\n"
        "取引所側で処理済みなら、GitHub の Environment 変数 ALLOW_UNMANAGED に bot id を書くと通せます"
    )
    return 1 if mode == "live" else 0  # paper は警告だけ


def fetch_health(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url.rstrip("/") + "/healthz", headers={"user-agent": "trade-bot-factory-deploy"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}


def verify(url: str, build: str, timeout: float, interval: float) -> int:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        try:
            status, last = fetch_health(url)
        except (OSError, ValueError) as e:
            status, last = 0, {"error": str(e)}
        if status == 200 and last.get("build") == build and last.get("last_tick_build") == build:
            errors = last.get("last_tick_errors", 0)
            if errors:
                print(f"::warning::新しい版で {errors} 個の bot がエラーになっています (/api/events を確認してください)")
            print(f"ok: build {build[:12]} の cron が完了しました {json.dumps(last, ensure_ascii=False)}")
            return 0
        print(f"waiting... status={status} {json.dumps(last, ensure_ascii=False)}")
        time.sleep(interval)
    print(f"::error::{timeout:.0f} 秒以内に build {build[:12]} の cron 完了を確認できませんでした: {json.dumps(last, ensure_ascii=False)}")
    return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("preflight")
    p.add_argument("--mode", required=True, choices=["paper", "live"])
    p.add_argument("--rows", required=True, help="wrangler d1 execute --json の出力ファイル")
    p.add_argument("--allow", default="", help="持ち高が残っていても通す bot id (カンマ区切り)")
    v = sub.add_parser("verify")
    v.add_argument("--url", required=True)
    v.add_argument("--build", required=True)
    v.add_argument("--timeout", type=float, default=300)
    v.add_argument("--interval", type=float, default=15)
    a = ap.parse_args()
    if a.cmd == "preflight":
        rows = d1_rows(Path(a.rows).read_text())
        allow = {x.strip() for x in a.allow.split(",") if x.strip()}
        sys.exit(preflight(a.mode, rows, allow))
    sys.exit(verify(a.url, a.build, a.timeout, a.interval))


if __name__ == "__main__":
    main()
