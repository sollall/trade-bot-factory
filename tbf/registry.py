"""bots/<id>/bot.py を探して Bot を集める"""

from __future__ import annotations

import importlib
from pathlib import Path

from tbf.core.bot import Bot

ROOT = Path(__file__).resolve().parent.parent
BOTS_DIR = ROOT / "bots"


def bot_ids(bots_dir: Path = BOTS_DIR) -> list[str]:
    return sorted(
        d.name for d in bots_dir.iterdir() if d.is_dir() and not d.name.startswith("_") and (d / "bot.py").exists()
    )


def load_bot(bot_id: str) -> Bot:
    mod = importlib.import_module(f"bots.{bot_id}.bot")
    bot = getattr(mod, "bot", None)
    if not isinstance(bot, Bot):
        raise TypeError(f"bots/{bot_id}/bot.py: `bot = Bot(...)` を定義してください")
    if bot.id != bot_id:
        raise ValueError(f'bots/{bot_id}: id "{bot.id}" はディレクトリ名と一致させてください')
    return bot


def load_all() -> list[Bot]:
    return [load_bot(i) for i in bot_ids()]
