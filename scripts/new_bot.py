"""python scripts/new_bot.py <id> : テンプレートから bot を作る"""

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if len(sys.argv) != 2 or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", sys.argv[1]):
    sys.exit("usage: python scripts/new_bot.py <id>   (a-z, 0-9, _ / 先頭は英字)")
bot_id = sys.argv[1]
dest = ROOT / "bots" / bot_id
if dest.exists():
    sys.exit(f"bots/{bot_id} already exists")
shutil.copytree(ROOT / "bots" / "_template", dest, ignore=shutil.ignore_patterns("__pycache__"))
f = dest / "bot.py"
f.write_text(f.read_text().replace("__BOT_ID__", bot_id))
print(f"created bots/{bot_id}/bot.py")
