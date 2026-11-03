#!/usr/bin/env python3
"""常設 paper Worker の初回起動で wrangler.toml を安全に書き換える。

.github/workflows/agent-paper-bootstrap.yml から呼ばれる。

歯止めをここに閉じ込めてある:
  - 書き換えるのは [env.paper] の d1_databases の database_id だけ
  - 置換が 1 件でなければ異常終了 (0 件でも 2 件以上でも)
  - [env.live] の database_id がプレースホルダでなければ、読むだけでも異常終了
  - 書き込み後にもう一度全体を検査する
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOML = ROOT / "wrangler.toml"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def sections(text: str) -> dict[str, str]:
    """[env.paper] / [env.live] ごとの本文に切り分ける ([[env.x.d1_databases]] も含めて)"""
    out: dict[str, list[str]] = {"": []}
    cur = ""
    for line in text.splitlines(keepends=True):
        m = re.match(r"\s*\[+\s*env\.([a-z0-9_]+)", line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
        elif re.match(r"\s*\[", line) and not re.match(r"\s*\[+\s*env\.", line):
            cur = ""
        out.setdefault(cur, []).append(line)
    return {k: "".join(v) for k, v in out.items()}


def live_ids(text: str, placeholder: str) -> list[str]:
    live = sections(text).get("live", "")
    return re.findall(r'database_id\s*=\s*"([^"]*)"', live)


def assert_live(placeholder: str) -> None:
    text = TOML.read_text()
    ids = live_ids(text, placeholder)
    if not ids:
        sys.exit("::error::wrangler.toml の [env.live] に database_id が見つかりません。中止します")
    bad = [i for i in ids if i != placeholder]
    if bad:
        sys.exit(
            "::error::[env.live] の database_id がプレースホルダではありません。"
            "live が実際にデプロイ可能な状態になっているため、この workflow は中止します"
        )
    print(f"ok: [env.live] の database_id は {len(ids)} 件すべてプレースホルダのままです")


def patch(db_id: str, placeholder: str) -> None:
    if not UUID_RE.match(db_id):
        sys.exit("::error::--db-id が UUID の形式ではありません")
    if db_id == placeholder:
        sys.exit("::error::--db-id がプレースホルダそのものです")
    assert_live(placeholder)

    text = TOML.read_text()
    secs = sections(text)
    paper = secs.get("paper")
    if not paper:
        sys.exit("::error::wrangler.toml に [env.paper] がありません")
    pattern = f'database_id = "{placeholder}"'
    n = paper.count(pattern)
    if n != 1:
        sys.exit(f"::error::[env.paper] のプレースホルダが {n} 件です (1 件であるべき)。中止します")

    new_paper = paper.replace(pattern, f'database_id = "{db_id}"')
    new_text = text.replace(paper, new_paper, 1)
    if new_text == text or new_text.count(f'database_id = "{db_id}"') != 1:
        sys.exit("::error::置換結果が想定と違います。中止します")
    TOML.write_text(new_text)

    # 書き込み後の再検査
    assert_live(placeholder)
    after = sections(TOML.read_text())
    if after.get("paper", "").count(f'database_id = "{db_id}"') != 1:
        sys.exit("::error::書き込み後の検査に失敗しました")
    print("ok: [env.paper] の database_id だけを書き換えました ([env.live] はプレースホルダのまま)")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assert-live")
    a.add_argument("--placeholder", required=True)
    p = sub.add_parser("patch")
    p.add_argument("--db-id", required=True)
    p.add_argument("--placeholder", required=True)
    args = ap.parse_args()
    if args.cmd == "assert-live":
        assert_live(args.placeholder)
    else:
        patch(args.db_id, args.placeholder)


if __name__ == "__main__":
    main()
