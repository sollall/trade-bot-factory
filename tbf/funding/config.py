"""funding.toml / venues.toml の読み込み"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class VenueConfig:
    name: str
    adapter: str
    base_url: str
    # API が返すファンディングレートが何時間ぶんか (1h / 8h)。年率化に使う
    funding_interval_hours: float
    taker_bps: float
    maker_bps: float
    liquidation_bps: float
    poll_interval_sec: float
    book_depth: int
    # 共通シンボル -> 会場固有シンボル
    symbols: dict[str, str]
    # 売買しない会場 (CEX など)。ファンディングの水準を読むだけで、ペアには入れない
    reference_only: bool = False
    fee_source: str = ""
    region_restricted: tuple[str, ...] = ()
    points_program: str = ""
    source: str = ""
    # API が建てを返すならそのフィールド名 (設定値との食い違いを check で出す)
    api_interval_field: str = ""

    def covers(self, symbol: str) -> bool:
        return symbol in self.symbols


@dataclass(frozen=True)
class FundingConfig:
    venues: list[VenueConfig]
    symbols: list[str]
    sizes_usd: list[float]
    hold_days: list[float]
    rank_hold_days: float = 14.0
    min_net_apr: float = 0.05
    poll_interval_sec: float = 15.0
    max_stale_sec: float = 180.0
    spread_bucket_sec: float = 300.0
    retention_days: float = 14.0
    walkforward_windows: int = 7
    ref_size_usd: float = 5_000.0
    venues_file: str = ""
    # check で埋める: 会場 -> API が返した建て (時間)
    api_intervals: dict[str, float] = field(default_factory=dict)

    @property
    def tradable(self) -> list[VenueConfig]:
        return [v for v in self.venues if not v.reference_only]

    def venue(self, name: str) -> VenueConfig:
        return next(v for v in self.venues if v.name == name)


def _venue(raw: dict, defaults: dict) -> VenueConfig:
    name = raw["name"]
    base_url = os.environ.get(f"FUNDING_URL_{name.upper()}", raw["base_url"])
    if not raw.get("symbols"):
        raise ValueError(f"{name}: symbols が空です")
    return VenueConfig(
        name=name,
        adapter=raw.get("adapter", name),
        base_url=base_url.rstrip("/"),
        funding_interval_hours=float(raw["funding_interval_hours"]),
        taker_bps=float(raw.get("taker_bps", 0.0)),
        maker_bps=float(raw.get("maker_bps", 0.0)),
        liquidation_bps=float(raw.get("liquidation_bps", 0.0)),
        poll_interval_sec=float(raw.get("poll_interval_sec", defaults["poll_interval_sec"])),
        book_depth=int(raw.get("book_depth", defaults["book_depth"])),
        symbols=dict(raw["symbols"]),
        reference_only=bool(raw.get("reference_only", False)),
        fee_source=str(raw.get("fee_source", "")),
        region_restricted=tuple(raw.get("region_restricted", ())),
        points_program=str(raw.get("points_program", "")),
        source=str(raw.get("source", "")),
        api_interval_field=str(raw.get("api_interval_field", "")),
    )


def load_config(path: str | Path, venues_path: str | Path | None = None) -> FundingConfig:
    p = Path(path)
    raw = tomllib.loads(p.read_text())
    vpath = Path(venues_path) if venues_path else (p.parent / raw.get("venues_file", "data/venues.toml"))
    vraw = tomllib.loads(Path(vpath).read_text())
    if int(vraw.get("schema_version", 1)) != 1:
        raise ValueError(f"{vpath}: schema_version {vraw.get('schema_version')} は未対応")

    defaults = {
        "poll_interval_sec": float(raw.get("poll_interval_sec", 15.0)),
        "book_depth": int(raw.get("book_depth", 100)),
    }
    symbols = [str(s) for s in raw["symbols"]]
    venues = [_venue(v, defaults) for v in vraw.get("venues", []) if v.get("enabled", True)]
    if len({v.name for v in venues}) != len(venues):
        raise ValueError(f"{vpath}: 会場名が重複しています")

    for v in venues:
        if not any(v.covers(s) for s in symbols):
            raise ValueError(f"{v.name}: {symbols} のどれも symbols に無いので対象にできません")
    tradable = [v for v in venues if not v.reference_only]
    if len(tradable) < 2:
        raise ValueError("ペアを作るには売買対象の会場が 2 つ以上必要です")
    for s in symbols:
        if sum(1 for v in tradable if v.covers(s)) < 2:
            raise ValueError(f"{s}: 対応している売買対象の会場が 2 つ未満です")

    sizes = [float(s) for s in raw["sizes_usd"]]
    ref_size = float(raw.get("ref_size_usd", sizes[0]))
    if ref_size not in sizes:
        raise ValueError(f"ref_size_usd {ref_size} は sizes_usd {sizes} に含まれていません")
    holds = [float(h) for h in raw.get("hold_days", [7, 14, 30])]
    rank = float(raw.get("rank_hold_days", 14.0))
    if rank not in holds:
        holds = sorted({*holds, rank})
    return FundingConfig(
        venues=venues,
        symbols=symbols,
        sizes_usd=sizes,
        hold_days=holds,
        rank_hold_days=rank,
        min_net_apr=float(raw.get("min_net_apr", 0.05)),
        poll_interval_sec=defaults["poll_interval_sec"],
        max_stale_sec=float(raw.get("max_stale_sec", 180.0)),
        spread_bucket_sec=float(raw.get("spread_bucket_sec", 300.0)),
        retention_days=float(raw.get("retention_days", 14.0)),
        walkforward_windows=int(raw.get("walkforward_windows", 7)),
        ref_size_usd=ref_size,
        venues_file=str(vpath),
    )
