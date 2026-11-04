"""carry.toml の読み込み

依頼票の事前宣言グリッドをそのまま持つ。読み込み時に資本規約の整合だけ検算する
(C = N + N/3 が崩れていたら年率の分母が変わってしまうので、黙って通さない)。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CAPITAL_RATIO = 4 / 3  # C = N + N/3


@dataclass(frozen=True)
class CarryConfig:
    info_url: str
    spot_pair: str
    spot_base_token_index: int
    spot_base_token_name: str
    spot_base_token_full_name: str
    spot_quote_token_index: int
    spot_quote_token_name: str
    perp_coin: str
    spot_notional_usd: float
    perp_margin_usd: float
    capital_usd: float
    spot_counts_as_perp_margin: bool
    perp_taker_bps: float
    spot_taker_bps: float
    fee_source: str
    maintenance_margin_frac: float
    margin_warn_frac: float
    perp_margin_table_id: int
    depth_sizes_usd: tuple[float, ...]
    depth_fill_limit_pct: float
    book_depth: int
    poll_interval_sec: float
    measure_days: float
    funding_refresh_sec: float
    max_stale_sec: float
    hurdle_net_apr: float
    sharpe_pass: float
    sharpe_reject: float
    expected_net_apr: float
    expected_sharpe: float
    expected_funding_apr: float
    expected_basis_sigma_bps: float

    @property
    def capital_ratio(self) -> float:
        """C / N。依頼票の 1.33 (厳密には 4/3)"""
        return self.capital_usd / self.spot_notional_usd


def load_config(path: str | Path) -> CarryConfig:
    raw = tomllib.loads(Path(path).read_text())
    cfg = CarryConfig(
        info_url=str(raw.get("info_url", "https://api.hyperliquid.xyz/info")).rstrip("/"),
        spot_pair=str(raw["spot_pair"]),
        spot_base_token_index=int(raw["spot_base_token_index"]),
        spot_base_token_name=str(raw["spot_base_token_name"]),
        spot_base_token_full_name=str(raw["spot_base_token_full_name"]),
        spot_quote_token_index=int(raw["spot_quote_token_index"]),
        spot_quote_token_name=str(raw["spot_quote_token_name"]),
        perp_coin=str(raw["perp_coin"]),
        spot_notional_usd=float(raw["spot_notional_usd"]),
        perp_margin_usd=float(raw["perp_margin_usd"]),
        capital_usd=float(raw["capital_usd"]),
        spot_counts_as_perp_margin=bool(raw.get("spot_counts_as_perp_margin", False)),
        perp_taker_bps=float(raw["perp_taker_bps"]),
        spot_taker_bps=float(raw["spot_taker_bps"]),
        fee_source=str(raw.get("fee_source", "")),
        maintenance_margin_frac=float(raw["maintenance_margin_frac"]),
        margin_warn_frac=float(raw["margin_warn_frac"]),
        perp_margin_table_id=int(raw.get("perp_margin_table_id", 0)),
        depth_sizes_usd=tuple(float(s) for s in raw["depth_sizes_usd"]),
        depth_fill_limit_pct=float(raw.get("depth_fill_limit_pct", 1.0)),
        book_depth=int(raw.get("book_depth", 20)),
        poll_interval_sec=float(raw.get("poll_interval_sec", 300)),
        measure_days=float(raw.get("measure_days", 30)),
        funding_refresh_sec=float(raw.get("funding_refresh_sec", 900)),
        max_stale_sec=float(raw.get("max_stale_sec", 1800)),
        hurdle_net_apr=float(raw["hurdle_net_apr"]),
        sharpe_pass=float(raw["sharpe_pass"]),
        sharpe_reject=float(raw["sharpe_reject"]),
        expected_net_apr=float(raw.get("expected_net_apr", 0.0)),
        expected_sharpe=float(raw.get("expected_sharpe", 0.0)),
        expected_funding_apr=float(raw.get("expected_funding_apr", 0.0)),
        expected_basis_sigma_bps=float(raw.get("expected_basis_sigma_bps", 0.0)),
    )
    _validate(cfg)
    return cfg


def _validate(cfg: CarryConfig) -> None:
    if abs(cfg.spot_notional_usd + cfg.perp_margin_usd - cfg.capital_usd) > 1e-6:
        raise ValueError(
            "資本規約が崩れています: spot_notional_usd + perp_margin_usd != capital_usd "
            f"({cfg.spot_notional_usd} + {cfg.perp_margin_usd} != {cfg.capital_usd})"
        )
    if abs(cfg.capital_ratio - CAPITAL_RATIO) > 0.01:
        raise ValueError(
            f"資本規約が崩れています: C/N = {cfg.capital_ratio:.4f} (規約は 4/3 = 1.3333)"
        )
    if cfg.spot_counts_as_perp_margin:
        raise ValueError(
            "spot_counts_as_perp_margin = true は未確定の前提です。"
            "オーナー判断により保守側 (false) で進めると決まっています (TRA-14 §8)"
        )
    if cfg.spot_notional_usd not in cfg.depth_sizes_usd:
        raise ValueError(f"depth_sizes_usd に建玉サイズ {cfg.spot_notional_usd} が含まれていません")
    if not 0 < cfg.maintenance_margin_frac < cfg.margin_warn_frac:
        raise ValueError("maintenance_margin_frac < margin_warn_frac である必要があります")
