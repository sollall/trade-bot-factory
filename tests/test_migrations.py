"""
D1 migration はデプロイ中、旧版の Worker が動いている間に適用される。
旧版が壊れないように、列やテーブルを消す・名前を変える・型を変える変更は禁止する
(どうしても必要なら「新しい列を追加 → 新版をデプロイ → 次のリリースで古い列を消す」の 2 段階にし、
 2 段階目のファイルに `-- tbf:allow-breaking` と理由を書く)。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BREAKING = re.compile(r"\b(DROP\s+(TABLE|COLUMN|INDEX)|RENAME\s+(TO|COLUMN)|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b", re.I)


def strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def test_migrations_are_additive():
    bad = []
    for f in sorted((ROOT / "migrations").glob("*.sql")):
        text = f.read_text()
        if "tbf:allow-breaking" in text:
            continue
        for m in BREAKING.finditer(strip_comments(text)):
            bad.append(f"{f.name}: {m.group(0)}")
    assert not bad, "旧版の Worker を壊す migration です:\n" + "\n".join(bad)


def test_new_not_null_columns_have_default():
    for f in sorted((ROOT / "migrations").glob("*.sql")):
        for stmt in strip_comments(f.read_text()).split(";"):
            if re.search(r"ADD\s+COLUMN", stmt, re.I) and re.search(r"NOT\s+NULL", stmt, re.I):
                assert re.search(r"DEFAULT", stmt, re.I), f"{f.name}: NOT NULL の追加列には DEFAULT が必要です: {stmt.strip()}"


def test_migration_numbers_are_unique_and_sequential():
    nums = [int(f.name.split("_")[0]) for f in sorted((ROOT / "migrations").glob("*.sql"))]
    assert nums == list(range(1, len(nums) + 1))
