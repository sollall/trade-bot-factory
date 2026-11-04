"""シャドーモードの常駐計測: 読んで記録するだけ。売買はしない

1 回のポーリングで投げる info リクエスト (すべて読み取り):
  spotMetaAndAssetCtxs / metaAndAssetCtxs / l2Book(@107) / l2Book(HYPE)
  + funding_refresh_sec ごとに fundingHistory(HYPE)

重み (HL の公開ドキュメントの info weight: 既定 20、l2Book は 2) で 5 分あたり 64。
1 分 1200 の上限に対して十分に余裕があるが、レート制限に当たったら間隔を伸ばす。
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

from tbf.carry.config import CarryConfig
from tbf.carry.hl import Book, HlError, InfoClient, round_hour
from tbf.carry.position import Entry, Fill, mark, walk
from tbf.carry.store import CarryStore
from tbf.http import Http

LEGS = ("spot", "perp")


@dataclass(frozen=True)
class Snapshot:
    time: int
    spot_ctx: dict
    perp_def: dict
    perp_ctx: dict
    spot_book: Book
    perp_book: Book

    @property
    def spot_mark(self) -> float:
        return float(self.spot_ctx["markPx"])

    @property
    def perp_mark(self) -> float:
        return float(self.perp_ctx["markPx"])

    @property
    def funding_rate_1h(self) -> float:
        return float(self.perp_ctx["funding"])


async def fetch_snapshot(client: InfoClient, cfg: CarryConfig, now: int) -> Snapshot:
    spot_ctx = await client.spot_ctx(cfg.spot_pair)
    perp_def, perp_ctx = await client.perp_ctx(cfg.perp_coin)
    spot_book = await client.book(cfg.spot_pair, cfg.book_depth)
    perp_book = await client.book(cfg.perp_coin, cfg.book_depth)
    return Snapshot(now, spot_ctx, perp_def, perp_ctx, spot_book, perp_book)


def verify_identity(spot_meta: dict, cfg: CarryConfig) -> list[str]:
    """`@107` が本当に HYPE/USDC か。ティッカー一致で別資産を指す例 (HL 現物 @20 PUMP) があるので毎回見る"""
    pairs = [u for u in spot_meta["universe"] if u.get("name") == cfg.spot_pair]
    if not pairs:
        raise HlError(f"spotMeta の universe に {cfg.spot_pair} がありません")
    pair = pairs[0]
    tokens = list(pair.get("tokens") or [])
    want = [cfg.spot_base_token_index, cfg.spot_quote_token_index]
    if tokens != want:
        raise HlError(f"{cfg.spot_pair} の tokens が {tokens} で、宣言した {want} と違います")
    base = spot_meta["tokens"][cfg.spot_base_token_index]
    quote = spot_meta["tokens"][cfg.spot_quote_token_index]
    if base.get("name") != cfg.spot_base_token_name or base.get("fullName") != cfg.spot_base_token_full_name:
        raise HlError(
            f"token {cfg.spot_base_token_index} が {base.get('name')!r} / {base.get('fullName')!r} で、"
            f"宣言した {cfg.spot_base_token_name!r} / {cfg.spot_base_token_full_name!r} と違います"
        )
    if quote.get("name") != cfg.spot_quote_token_name:
        raise HlError(f"token {cfg.spot_quote_token_index} が {quote.get('name')!r} で USDC ではありません")
    return [
        f"{cfg.spot_pair} = {base['name']}/{quote['name']} "
        f"(token {base['index']} fullName={base.get('fullName')!r} tokenId={base.get('tokenId')}, "
        f"szDecimals={base.get('szDecimals')}) — spotMeta で確認",
    ]


def floor_to(value: float, decimals: int) -> float:
    f = 10**decimals
    return math.floor(value * f) / f


def build_entry(snap: Snapshot, cfg: CarryConfig, sz_decimals: int) -> tuple[Optional[Entry], list[str]]:
    """建玉を板を歩いて決める。歩き切れなければ建てない (表示価格で代用しない)"""
    notes: list[str] = []
    spot_mid, perp_mid = snap.spot_book.mid, snap.perp_book.mid
    if spot_mid is None or perp_mid is None:
        return None, ["板の片側が空なので建玉を作りません"]
    # 入る: 現物は売り板を食べて買う / perp は買い板を食べて売る
    spot_buy = walk(snap.spot_book.asks, cfg.spot_notional_usd, spot_mid, "buy", cfg.spot_taker_bps)
    perp_sell = walk(snap.perp_book.bids, cfg.spot_notional_usd, perp_mid, "sell", cfg.perp_taker_bps)
    if not spot_buy.ok:
        notes.append(
            f"現物の売り板 {spot_buy.levels_seen} 段 (${spot_buy.depth_usd:,.0f}) では "
            f"${cfg.spot_notional_usd:,.0f} を歩き切れません"
        )
    if not perp_sell.ok:
        notes.append(
            f"perp の買い板 {perp_sell.levels_seen} 段 (${perp_sell.depth_usd:,.0f}) では "
            f"${cfg.spot_notional_usd:,.0f} を歩き切れません"
        )
    if notes:
        return None, notes
    assert spot_buy.avg_px is not None and perp_sell.avg_px is not None
    size = floor_to(cfg.spot_notional_usd / spot_buy.avg_px, sz_decimals)
    if size <= 0:
        return None, ["サイズが 0 に丸められました"]
    entry = Entry(
        time=snap.time,
        size=size,
        spot_px=spot_buy.avg_px,
        perp_px=perp_sell.avg_px,
        spot_notional_usd=size * spot_buy.avg_px,
        perp_notional_usd=size * perp_sell.avg_px,
        margin_usd=cfg.perp_margin_usd,
        capital_usd=cfg.capital_usd,
        spot_fee_bps=cfg.spot_taker_bps,
        perp_fee_bps=cfg.perp_taker_bps,
        spot_slip_bps=spot_buy.slip_bps or 0.0,
        perp_slip_bps=perp_sell.slip_bps or 0.0,
        spot_mid=spot_mid,
        perp_mid=perp_mid,
    )
    return entry, notes


def depth_fills(snap: Snapshot, cfg: CarryConfig) -> list[tuple[str, Fill]]:
    """(iv) 板の深さ: サイズ × 脚 × 方向 を全部歩く。歩き切れなければ avg_px は None = 未測定"""
    out: list[tuple[str, Fill]] = []
    books = {"spot": (snap.spot_book, cfg.spot_taker_bps), "perp": (snap.perp_book, cfg.perp_taker_bps)}
    for leg, (book, fee) in books.items():
        mid = book.mid
        if mid is None:
            continue
        for size in cfg.depth_sizes_usd:
            out.append((leg, walk(book.asks, size, mid, "buy", fee)))
            out.append((leg, walk(book.bids, size, mid, "sell", fee)))
    return out


class Recorder:
    def __init__(
        self,
        cfg: CarryConfig,
        http: Http,
        store: CarryStore,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        log: Callable[[str], None] = print,
        sz_decimals: int = 2,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.store = store
        self.clock = clock
        self.log = log
        self.sz_decimals = sz_decimals
        self.entry: Optional[Entry] = store.load_entry()
        self.next_funding_ms = 0
        self.liquidation_logged = False
        self.warn_logged = False

    # ---- ファンディング (ii)

    async def update_funding(self, client: InfoClient, now: int) -> int:
        """fundingHistory の 1 時間値を建玉後のぶんだけ積む。新しく積んだ件数を返す"""
        assert self.entry is not None
        last = self.store.last_funding_time()
        start = (last + 1) if last is not None else round_hour(self.entry.time)
        points = await client.funding_history(self.cfg.perp_coin, start, now)
        added = 0
        for p in points:
            if p.time <= self.entry.time:
                continue  # 建玉前の時間ぶんは受け取れない
            mark_px = self._perp_mark_at(p.time)
            amount = p.rate * self.entry.size * mark_px if mark_px else None
            self.store.add_funding(
                p.time, p.rate, p.premium, mark_px or 0.0,
                amount if amount is not None else 0.0,
                p.rate * self.cfg.spot_notional_usd,
            )
            added += 1
        return added

    def _perp_mark_at(self, t: int) -> Optional[float]:
        """その時刻に最も近い記録済みサンプルの perp マーク価格 (±15 分以内)"""
        row = self.store.query(
            "SELECT perp_mark, ABS(time - ?) AS d FROM samples ORDER BY d ASC LIMIT 1", (t,)
        )
        if row and row[0]["d"] <= 900_000:
            return float(row[0]["perp_mark"])
        return None

    def _record_sample(self, snap: Snapshot, now: int, funding_cum: float):
        assert self.entry is not None
        m = mark(
            self.entry, now, snap.spot_mark, snap.perp_mark, funding_cum,
            self.cfg.maintenance_margin_frac, self.cfg.margin_warn_frac,
        )
        self.store.add_sample(m, {
            "spot_mid": snap.spot_book.mid,
            "perp_mid": snap.perp_book.mid,
            "perp_oracle": float(snap.perp_ctx.get("oraclePx") or 0) or None,
            "funding_rate_1h": snap.funding_rate_1h,
            "premium": float(snap.perp_ctx.get("premium") or 0),
            "spot_day_ntl_vlm": float(snap.spot_ctx.get("dayNtlVlm") or 0) or None,
            "perp_day_ntl_vlm": float(snap.perp_ctx.get("dayNtlVlm") or 0) or None,
            "open_interest": float(snap.perp_ctx.get("openInterest") or 0) or None,
        })
        return m

    # ---- 1 回のポーリング

    async def poll_once(self) -> None:
        now = self.clock()
        client = InfoClient(self.http, self.cfg.info_url)
        try:
            snap = await fetch_snapshot(client, self.cfg, now)
        except Exception as e:  # noqa: BLE001  計測は止めない
            self.store.add_poll(now, False, client.calls, client.latency_ms, client.rate_limited,
                                f"{type(e).__name__}: {e}")
            self.store.commit()
            self.log(f"poll error: {type(e).__name__}: {e}")
            if client.rate_limited:
                raise
            return

        if self.entry is None:
            entry, notes = build_entry(snap, self.cfg, self.sz_decimals)
            for n in notes:
                self.log(f"建玉せず: {n}")
            if entry is None:
                self.store.add_poll(now, False, client.calls, client.latency_ms, False,
                                    "entry not established: " + "; ".join(notes))
                self.store.commit()
                return
            self.entry = entry
            self.store.save_entry(entry)
            self.store.set_meta("entry_ms", str(entry.time))
            self.log(
                f"建玉 (シミュレーション): size {entry.size} HYPE  "
                f"現物 buy @{entry.spot_px:,.4f} (mid {entry.spot_mid:,.4f}, slip {entry.spot_slip_bps:+.2f}bps)  "
                f"perp sell @{entry.perp_px:,.4f} (mid {entry.perp_mid:,.4f}, slip {entry.perp_slip_bps:+.2f}bps)  "
                f"片道コスト {entry.entry_cost_bps:.2f}bps = ${entry.entry_cost_usd:,.2f}"
            )

        # 板の深さ (i)(iv) は毎回歩く
        for leg, f in depth_fills(snap, self.cfg):
            self.store.add_fill(now, leg, f)

        # サンプルを先に入れてからファンディングを積む (_perp_mark_at がサンプルを引くため)
        m = self._record_sample(snap, now, self.store.funding_cum_usd())
        self.store.commit()

        if now >= self.next_funding_ms:
            try:
                added = await self.update_funding(client, now)
            except Exception as e:  # noqa: BLE001
                self.log(f"fundingHistory error: {type(e).__name__}: {e}")
            else:
                self.next_funding_ms = now + int(self.cfg.funding_refresh_sec * 1000)
                if added:
                    cum = self.store.funding_cum_usd()
                    self.log(f"ファンディング {added} 時間ぶん追加 -> 累計 ${cum:,.4f}")
                    m = self._record_sample(snap, now, cum)  # 累計が動いたので評価を入れ直す

        self.store.add_poll(now, True, client.calls, client.latency_ms, client.rate_limited, None)
        self.store.set_meta("last_run_ms", str(now))
        self.store.commit()

        if m.liquidated and not self.liquidation_logged:
            self.liquidation_logged = True
            self.log(
                f"★ 清算 (シミュレーション): 証拠金率 {m.margin_ratio * 100:.2f}% <= "
                f"{self.cfg.maintenance_margin_frac * 100:g}%  perp 自己資本 ${m.perp_equity_usd:,.2f} / "
                f"名目 ${m.perp_notional_usd:,.2f}  ベーシス {m.basis_bps:+.2f}bps"
            )
        if m.margin_warn and not self.warn_logged:
            self.warn_logged = True
            self.log(f"証拠金率が {self.cfg.margin_warn_frac * 100:g}% を割りました: {m.margin_ratio * 100:.2f}%")
        if not m.margin_warn:
            self.warn_logged = False

    async def run(self, stop: Optional[asyncio.Event] = None, max_polls: Optional[int] = None) -> None:
        stop = stop or asyncio.Event()
        self.store.note_start(self.clock())
        interval = self.cfg.poll_interval_sec
        n = 0
        while not stop.is_set() and (max_polls is None or n < max_polls):
            started = time.monotonic()
            backoff = 1.0
            try:
                await self.poll_once()
            except HlError as e:
                if e.rate_limited:
                    backoff = 4.0
                    self.log(f"rate limited -> 次回まで {interval * backoff:g}s 待ちます")
                else:
                    self.log(f"poll failed: {type(e).__name__}: {e}")
            except Exception as e:  # noqa: BLE001  監視は止めない
                self.log(f"poll failed: {type(e).__name__}: {e}")
            n += 1
            delay = max(0.0, interval * backoff - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        self.store.set_meta("last_run_ms", str(self.clock()))
        self.store.commit()
