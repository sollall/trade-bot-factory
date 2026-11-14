"""`jp_maker.toml` の読み込み。

**会場・銘柄・サイズ・ポーリング間隔は設定ファイル駆動** (依頼票 §実装の方針)。
ただし「合否の線」と「費用」は設定ファイルから動かせないようにしてある
(`tbf/jpmaker/declared.py` にハードコードし、git 履歴に残す)。
設定ファイルで銘柄やサイズを宣言グリッドから外した場合は `load_config` が弾く。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from tbf.jpmaker import declared


@dataclass(frozen=True)
class PairConfig:
    pair: str  # bitbank の銘柄 (例 render_jpy)
    hedge_coin: str  # Hyperliquid perp の銘柄 (例 RENDER)


@dataclass(frozen=True)
class JpMakerConfig:
    pairs: list[PairConfig]
    sizes_jpy: list[float]
    book_poll_sec: float
    tape_poll_sec: float
    hl_poll_sec: float
    hl_depth_levels: int
    bitbank_public: str
    hyperliquid_info: str
    http_timeout_sec: float

    @property
    def pair_names(self) -> list[str]:
        return [p.pair for p in self.pairs]


DEFAULTS = {
    "book_poll_sec": declared.BOOK_POLL_SEC,
    "tape_poll_sec": declared.TAPE_POLL_SEC,
    "hl_poll_sec": declared.HL_POLL_SEC,
    "hl_depth_levels": declared.HL_DEPTH_LEVELS,
    "bitbank_public": "https://public.bitbank.cc",
    "hyperliquid_info": "https://api.hyperliquid.xyz/info",
    "http_timeout_sec": 10.0,
}


def load_config(path: str | Path) -> JpMakerConfig:
    raw = tomllib.loads(Path(path).read_text())
    pairs = [PairConfig(pair=p["pair"], hedge_coin=p["hedge_coin"]) for p in raw.get("pairs", [])]
    if not pairs:
        raise ValueError("pairs is empty")

    # 宣言グリッドからの逸脱を弾く。良い数字が出る銘柄だけ残す操作を設定ファイルで
    # できてしまうと、事前宣言の意味が無くなる。
    declared_pairs = set(declared.PAIRS)
    got = {p.pair for p in pairs}
    if got != declared_pairs:
        raise ValueError(
            f"pairs は宣言グリッドと一致していなければならない: "
            f"不足 {sorted(declared_pairs - got)} / 余り {sorted(got - declared_pairs)}"
        )
    for p in pairs:
        if declared.PAIRS[p.pair] != p.hedge_coin:
            raise ValueError(f"{p.pair}: ヘッジ先は {declared.PAIRS[p.pair]} で宣言済み (設定は {p.hedge_coin})")

    sizes = [float(s) for s in raw.get("sizes_jpy", declared.SIZES_JPY)]
    if sorted(sizes) != sorted(declared.SIZES_JPY):
        raise ValueError(f"sizes_jpy は宣言グリッド {list(declared.SIZES_JPY)} と一致していなければならない")

    book_poll = float(raw.get("book_poll_sec", DEFAULTS["book_poll_sec"]))
    if book_poll < declared.BOOK_POLL_SEC:
        # 上流が max-age=1 のキャッシュなので速くしても中身は変わらず、BAN の危険だけ増える
        raise ValueError(f"book_poll_sec は {declared.BOOK_POLL_SEC} 秒より短くできない")

    return JpMakerConfig(
        pairs=pairs,
        sizes_jpy=sizes,
        book_poll_sec=book_poll,
        tape_poll_sec=float(raw.get("tape_poll_sec", DEFAULTS["tape_poll_sec"])),
        hl_poll_sec=float(raw.get("hl_poll_sec", DEFAULTS["hl_poll_sec"])),
        hl_depth_levels=int(raw.get("hl_depth_levels", DEFAULTS["hl_depth_levels"])),
        bitbank_public=str(raw.get("bitbank_public", DEFAULTS["bitbank_public"])).rstrip("/"),
        hyperliquid_info=str(raw.get("hyperliquid_info", DEFAULTS["hyperliquid_info"])),
        http_timeout_sec=float(raw.get("http_timeout_sec", DEFAULTS["http_timeout_sec"])),
    )
