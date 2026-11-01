import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tbf.worker.app import App, Settings

from test_worker import NOW, FakeHttp, FakeMarket, carry_bot, make_db, single_bot

ROOT = Path(__file__).resolve().parent.parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


preview = load_script("preview")


def test_changed_bots():
    files = ["bots/sma_cross/bot.py", "bots/_template/bot.py", "tbf/core/bot.py", "bots/deleted/bot.py", "bots/README.md"]
    assert preview.changed_bots(files, ["rsi_reversion", "sma_cross"]) == ["sma_cross"]
    assert preview.changed_bots(["tbf/core/bot.py"], ["sma_cross"]) == []


def test_config_is_isolated_paper_worker():
    cfg = tomllib.loads(preview.render_config(12, "trade-bot-pr-12-abc", "uuid-1"))
    assert cfg["name"] == "trade-bot-pr-12" and cfg["main"] == ".build/entry.py"
    assert cfg["vars"] == {"MODE": "paper", "PREVIEW": "true", "INSTANCE": "pr-12", "NOTIFY_FILLS": "false"}
    assert cfg["d1_databases"] == [
        {"binding": "DB", "database_name": "trade-bot-pr-12-abc", "database_id": "uuid-1", "migrations_dir": "migrations"}
    ]
    base = tomllib.loads((ROOT / "wrangler.toml").read_text())
    assert cfg["compatibility_date"] == base["compatibility_date"]
    # paper / live の D1 は使わない
    assert "trade-bot-paper" not in str(cfg["d1_databases"]) and "trade-bot-live" not in str(cfg["d1_databases"])


def test_report_roundtrip_meta():
    meta = {"url": "https://trade-bot-pr-3.x.workers.dev", "sha": "abcdef1234567890", "started": 1_000_000.0}
    bots = [{"id": "sma_cross", "status": "running", "equity": 1050.5,
             "metrics": {"total_return": 0.0505, "max_drawdown": 0.02, "sharpe": 1.234, "trades": 4, "win_rate": 0.5}}]
    body = preview.render_report(meta, (200, {"ok": True}), (200, bots), 1_000_000 + 2.5 * 86400)
    assert body.startswith(preview.MARKER)
    assert preview.read_meta(body) == meta
    assert "2.5 日" in body and "`abcdef123456`" in body and "稼働中" in body
    assert "| `sma_cross` | running | 1,050.50 | +5.05% | -2.00% | 1.23 | 4 | 50% |" in body
    bad = preview.render_report(meta, (503, {"reason": "last tick 400s ago"}), (200, []), 1_000_000)
    assert "異常" in bad and "last tick 400s ago" in bad
    final = preview.render_report(meta, (0, None), (200, bots), 1_000_000, final=True)
    assert "削除しました" in final and meta["url"] not in final.split("-->", 2)[2]


def test_build_only(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_worker.py"), "--only", "sma_cross"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    reg = (ROOT / ".build" / "bot_registry.py").read_text()
    assert "bots.sma_cross.bot" in reg and "rsi_reversion" not in reg
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_worker.py"), "--only", "nope"], capture_output=True, text=True)
    assert r.returncode != 0 and "nope" in r.stderr
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_worker.py")], check=True, capture_output=True)


async def test_preview_runs_bots_regardless_of_deploy():
    db, http = make_db(), FakeHttp()
    bots = [single_bot(deploy=["live"]), carry_bot()]
    s = Settings.from_env({"MODE": "paper", "PREVIEW": "true", "INSTANCE": "pr-7", "WEBHOOK_URL": "https://hook"}.get)
    app = App(bots, s, db, http, {"binance": FakeMarket(150), "hyperliquid": FakeMarket(3000)}, build="b")
    assert [b.id for b in app.bots] == ["up", "carry"]
    await app.scheduled("* * * * *", NOW)
    assert {r["bot_id"] for r in await db.query("SELECT bot_id FROM bot_state")} == {"up", "carry"}
    assert all(p.startswith("[pr-7]") for p in http.posts)
    h = json.loads((await app.handle("GET", "https://w/healthz", {}, now=NOW)).body)
    assert h["instance"] == "pr-7" and h["mode"] == "paper"


def test_preview_cannot_be_live():
    with pytest.raises(ValueError):
        Settings.from_env({"MODE": "live", "PREVIEW": "true"}.get)
