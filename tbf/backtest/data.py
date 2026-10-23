from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from tbf.core.types import Candle
from tbf.http import UrllibHttp
from tbf.market.binance import fetch_range


async def load_candles(symbol: str, timeframe: str, start: int, end: int, cache_dir: Path = Path("data")) -> list[Candle]:
    """data/ にキャッシュしつつ過去足を取得する"""
    path = cache_dir / f"{symbol}_{timeframe}_{start}_{end}.json"
    if path.exists():
        return [Candle(*row) for row in json.loads(path.read_text())]
    candles = await fetch_range(UrllibHttp(), symbol, timeframe, start, end)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([[c.time, c.open, c.high, c.low, c.close, c.volume] for c in candles]))
    return candles


def load_csv(path: str | Path) -> list[Candle]:
    """time,open,high,low,close,volume のヘッダ付き CSV (time は ms か ISO 文字列)"""
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): v.strip() for k, v in row.items()}
            t = row["time"]
            time = int(t) if t.isdigit() else int(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000)
            out.append(
                Candle(time, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"]))
            )
    return out
