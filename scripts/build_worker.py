"""
Cloudflare にアップロードする Python モジュールを .build/ に集める。
  - worker/entry.py   -> .build/entry.py
  - tbf/ (backtest / arb / onchain 以外) -> .build/tbf/
  - bots/<id>/*.py    -> .build/bots/<id>/
  - bot_registry.py を生成 (Worker はファイル走査せずこの一覧から bot を読む)
bot の import / 定義チェックもここで行うので、壊れた bot はデプロイ前に落ちる。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / ".build"
sys.path.insert(0, str(ROOT))

from tbf.registry import bot_ids, load_bot  # noqa: E402
from tbf.worker.brokers import LIVE_SUPPORTED  # noqa: E402


def copy_py(src: Path, dst: Path, skip: tuple[str, ...] = ()) -> None:
    for f in src.rglob("*.py"):
        rel = f.relative_to(src)
        if any(part in skip or part == "__pycache__" for part in rel.parts):
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst / rel)


def live_errors(bot) -> list[str]:
    """本番発注に対応していない銘柄を live に出さない"""
    if "live" not in bot.deploy:
        return []
    return [
        f"bots/{bot.id}: {alias} ({inst}) は本番発注に未対応のため live にできません"
        for alias, inst in bot.instruments.items()
        if (inst.venue, inst.kind) not in LIVE_SUPPORTED
    ]


def build_id() -> str:
    """デプロイしたコミット。/healthz に出し、デプロイ後に新しい版が動いているかの確認に使う"""
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "dev"


def main() -> None:
    ids = bot_ids()
    errors = []
    for i in ids:
        try:
            bot = load_bot(i)
        except Exception as e:  # noqa: BLE001
            errors.append(f"bots/{i}: {type(e).__name__}: {e}")
            continue
        errors += live_errors(bot)
    if errors:
        sys.exit("\n".join(errors))

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    shutil.copy2(ROOT / "worker" / "entry.py", OUT / "entry.py")
    copy_py(ROOT / "tbf", OUT / "tbf", skip=("backtest", "arb", "onchain"))
    (OUT / "bots").mkdir()
    (OUT / "bots" / "__init__.py").write_text("")
    for i in ids:
        copy_py(ROOT / "bots" / i, OUT / "bots" / i)
        init = OUT / "bots" / i / "__init__.py"
        if not init.exists():
            init.write_text("")

    lines = ["# 自動生成ファイル (scripts/build_worker.py)"]
    lines += [f"from bots.{i}.bot import bot as bot_{n}" for n, i in enumerate(ids)]
    lines += ["", f"BOTS = [{', '.join(f'bot_{n}' for n in range(len(ids)))}]", ""]
    (OUT / "bot_registry.py").write_text("\n".join(lines))
    (OUT / "build_info.py").write_text(f"# 自動生成ファイル (scripts/build_worker.py)\nBUILD = {build_id()!r}\n")
    print(f"built .build/ with {len(ids)} bot(s): {', '.join(ids) or '(none)'}")


if __name__ == "__main__":
    main()
