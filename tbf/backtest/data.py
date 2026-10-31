from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from tbf.core.bot import Bot
from tbf.core.types import Candle, FundingPoint
from tbf.http import Http, UrllibHttp
from tbf.market import VenueMarket, default_markets


async def load_bot_data(
    bot: Bot,
    start: int,
    end: int,
    cache_dir: Optional[Path] = Path("data"),
    markets: Optional[dict[str, VenueMarket]] = None,
    http: Optional[Http] = None,
) -> tuple[dict[str, list[Candle]], dict[str, list[FundingPoint]]]:
    """bot の全銘柄の足とファンディングを取得する (data/ にキャッシュ)"""
    markets = markets or default_markets(http or UrllibHttp())
    bars: dict[str, list[Candle]] = {}
    funding: dict[str, list[FundingPoint]] = {}
    for alias, inst in bot.instruments.items():
        if inst.venue not in markets:
            raise ValueError(f"{bot.id}: no market data for venue {inst.venue}")
        name = f"{inst.venue}_{inst.kind}_{inst.symbol}_{bot.timeframe}_{start}_{end}.json"
        path = cache_dir / name if cache_dir else None
        if path and path.exists():
            raw = json.loads(path.read_text())
            bars[alias] = [Candle(*c) for c in raw["candles"]]
            funding[alias] = [FundingPoint(*f) for f in raw["funding"]]
            continue
        m = markets[inst.venue]
        bars[alias] = await m.history(inst.symbol, bot.timeframe, start, end)
        funding[alias] = await m.funding(inst.symbol, start, end) if inst.is_perp else []
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "candles": [[c.time, c.open, c.high, c.low, c.close, c.volume] for c in bars[alias]],
                        "funding": [[f.time, f.rate, f.premium] for f in funding[alias]],
                    }
                )
            )
    return bars, funding


def load_csv(path: str | Path) -> list[Candle]:
    """time,open,high,low,close,volume のヘッダ付き CSV (time は ms か ISO 文字列)"""
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): v.strip() for k, v in row.items()}
            t = row["time"]
            time = int(t) if t.isdigit() else int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000)
            out.append(
                Candle(time, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row.get("volume") or 0))
            )
    return out
