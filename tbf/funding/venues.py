"""会場ごとの公開 API アダプタ (読み取り専用)

注文系のエンドポイントは一切呼ばない。認証もしない。

各アダプタの役割は 2 つだけ:
  1. 現在のファンディングレートを「その会場の建て」で取り、年率に正規化する
  2. 板を (価格, 数量) の配列で返す -> スリッページはここでは計算しない (spread.py の仕事)
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from tbf.funding.config import FundingConfig, VenueConfig
from tbf.http import Http

HOURS_PER_YEAR = 24 * 365


class VenueError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, rate_limited: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.rate_limited = rate_limited


@dataclass(frozen=True)
class Book:
    """買い板は価格の降順、売り板は昇順"""

    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]

    @property
    def mid(self) -> Optional[float]:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    # API が返した生のレートと、その建て (時間)
    raw_rate: float
    interval_hours: float
    mark: float
    book: Optional[Book] = None
    # API 自身が建てを返した場合の値 (設定との食い違いを見るため)
    api_interval_hours: Optional[float] = None

    @property
    def funding_apr(self) -> float:
        return annualize(self.raw_rate, self.interval_hours)

    @property
    def ref_price(self) -> float:
        """スリッページの基準価格。板があれば mid、無ければ mark"""
        m = self.book.mid if self.book else None
        return m if m else self.mark


@dataclass(frozen=True)
class VenueSnapshot:
    venue: str
    time: int
    latency_ms: float
    calls: int
    symbols: dict[str, SymbolSnapshot]


def annualize(rate: float, interval_hours: float) -> float:
    """「interval_hours 時間あたり rate」を年率 (小数) にする"""
    if interval_hours <= 0:
        raise ValueError(f"funding_interval_hours must be > 0 (got {interval_hours})")
    return rate / interval_hours * HOURS_PER_YEAR


@dataclass
class Session:
    """1 回のポーリングぶんの HTTP。遅延とレート制限到達を数える"""

    http: Http
    latency_ms: float = 0.0
    calls: int = 0
    rate_limited: bool = False
    clock: Callable[[], float] = time.monotonic

    async def json(self, method: str, url: str, body: Optional[str] = None):
        started = self.clock()
        headers = {"content-type": "application/json"} if body is not None else None
        status, text = await self.http.request(method, url, headers, body)
        self.latency_ms += (self.clock() - started) * 1000
        self.calls += 1
        if status in (418, 429):
            self.rate_limited = True
            raise VenueError(f"HTTP {status} (rate limited)", status, True)
        if status != 200:
            raise VenueError(f"HTTP {status} {text[:120]}", status)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise VenueError(f"not JSON: {e} ({text[:80]})") from e


def _levels(raw, price_key, size_key, descending: bool) -> tuple[tuple[float, float], ...]:
    out = []
    for r in raw:
        if isinstance(r, dict):
            px, sz = float(r[price_key]), float(r[size_key])
        else:
            px, sz = float(r[0]), float(r[1])
        if sz > 0:
            out.append((px, sz))
    # 会場が並び順を変えても壊れないように、こちらで並べ直す
    out.sort(key=lambda l: l[0], reverse=descending)
    return tuple(out)


# ------------------------------------------------------------------ アダプタ


async def fetch_hyperliquid(s: Session, v: VenueConfig, symbols: list[str]) -> dict[str, SymbolSnapshot]:
    meta, ctxs = await s.json("POST", v.base_url, json.dumps({"type": "metaAndAssetCtxs"}))
    idx = {a["name"]: i for i, a in enumerate(meta["universe"])}
    out = {}
    for sym in symbols:
        native = v.symbols[sym]
        if native not in idx:
            raise VenueError(f"{native} is not in the universe")
        c = ctxs[idx[native]]
        book = await s.json("POST", v.base_url, json.dumps({"type": "l2Book", "coin": native}))
        bids, asks = book["levels"][0][: v.book_depth], book["levels"][1][: v.book_depth]
        out[sym] = SymbolSnapshot(
            symbol=sym,
            raw_rate=float(c["funding"]),
            interval_hours=v.funding_interval_hours,
            mark=float(c["markPx"]),
            book=Book(_levels(bids, "px", "sz", True), _levels(asks, "px", "sz", False)),
        )
    return out


async def fetch_lighter(s: Session, v: VenueConfig, symbols: list[str]) -> dict[str, SymbolSnapshot]:
    details = await s.json("GET", f"{v.base_url}/api/v1/orderBookDetails")
    _check_lighter_code(details)
    by_symbol = {d["symbol"]: d for d in details["order_book_details"]}
    rates = await s.json("GET", f"{v.base_url}/api/v1/funding-rates")
    _check_lighter_code(rates)
    # この endpoint は他会場 (binance / bybit / hyperliquid) の値も返す。自分の会場の行だけ使う
    own = {r["symbol"]: r for r in rates["funding_rates"] if r.get("exchange") == "lighter"}
    out = {}
    for sym in symbols:
        native = v.symbols[sym]
        d, r = by_symbol.get(native), own.get(native)
        if d is None:
            raise VenueError(f"{native} is not listed")
        if d.get("status") != "active":
            raise VenueError(f"{native} status={d.get('status')}")
        if r is None:
            raise VenueError(f"{native} has no lighter funding rate")
        book = await s.json("GET", f"{v.base_url}/api/v1/orderBookOrders?market_id={d['market_id']}&limit={v.book_depth}")
        _check_lighter_code(book)
        out[sym] = SymbolSnapshot(
            symbol=sym,
            raw_rate=float(r["rate"]),
            interval_hours=v.funding_interval_hours,
            mark=float(d["mark_price"]),
            book=Book(
                _levels(book.get("bids", []), "price", "remaining_base_amount", True),
                _levels(book.get("asks", []), "price", "remaining_base_amount", False),
            ),
        )
    return out


def _check_lighter_code(payload: dict) -> None:
    # Lighter は HTTP 200 でも body の code でエラーを返すことがある
    if int(payload.get("code", 200)) != 200:
        raise VenueError(f"lighter code={payload.get('code')} {payload.get('message')}")


async def fetch_paradex(s: Session, v: VenueConfig, symbols: list[str]) -> dict[str, SymbolSnapshot]:
    out = {}
    for sym in symbols:
        native = v.symbols[sym]
        res = (await s.json("GET", f"{v.base_url}/v1/markets/summary?market={native}")).get("results") or []
        if not res:
            raise VenueError(f"{native} has no summary")
        m = res[0]
        book = await s.json("GET", f"{v.base_url}/v1/orderbook/{native}?depth={v.book_depth}")
        minutes = m.get("funding_interval_minutes")
        out[sym] = SymbolSnapshot(
            symbol=sym,
            raw_rate=float(m["funding_rate"]),
            interval_hours=v.funding_interval_hours,
            mark=float(m["mark_price"]),
            book=Book(_levels(book["bids"], 0, 1, True), _levels(book["asks"], 0, 1, False)),
            api_interval_hours=float(minutes) / 60 if minutes else None,
        )
    return out


async def fetch_dydx(s: Session, v: VenueConfig, symbols: list[str]) -> dict[str, SymbolSnapshot]:
    out = {}
    for sym in symbols:
        native = v.symbols[sym]
        markets = (await s.json("GET", f"{v.base_url}/v4/perpetualMarkets?ticker={native}")).get("markets") or {}
        m = markets.get(native)
        if m is None:
            raise VenueError(f"{native} is not listed")
        if m.get("status") != "ACTIVE":
            raise VenueError(f"{native} status={m.get('status')}")
        book = await s.json("GET", f"{v.base_url}/v4/orderbooks/perpetualMarket/{native}")
        out[sym] = SymbolSnapshot(
            symbol=sym,
            raw_rate=float(m["nextFundingRate"]),
            interval_hours=v.funding_interval_hours,
            mark=float(m["oraclePrice"]),
            book=Book(
                _levels(book["bids"][: v.book_depth], "price", "size", True),
                _levels(book["asks"][: v.book_depth], "price", "size", False),
            ),
        )
    return out


async def fetch_binance(s: Session, v: VenueConfig, symbols: list[str]) -> dict[str, SymbolSnapshot]:
    """参照のみ (売買しない)。板は取らない"""
    out = {}
    for sym in symbols:
        native = v.symbols[sym]
        m = await s.json("GET", f"{v.base_url}/fapi/v1/premiumIndex?symbol={native}")
        out[sym] = SymbolSnapshot(
            symbol=sym,
            raw_rate=float(m["lastFundingRate"]),
            interval_hours=v.funding_interval_hours,
            mark=float(m["markPrice"]),
        )
    return out


Adapter = Callable[[Session, VenueConfig, list[str]], Awaitable[dict[str, SymbolSnapshot]]]

ADAPTERS: dict[str, Adapter] = {
    "hyperliquid": fetch_hyperliquid,
    "lighter": fetch_lighter,
    "paradex": fetch_paradex,
    "dydx": fetch_dydx,
    "binance": fetch_binance,
}


@dataclass
class VenueClient:
    http: Http
    cfg: FundingConfig
    clock: Callable[[], int] = lambda: int(time.time() * 1000)
    # 直近のポーリングの計測値 (scanner が polls テーブルに書く)
    last: dict[str, tuple[float, int, bool]] = field(default_factory=dict)

    def symbols_for(self, v: VenueConfig) -> list[str]:
        return [s for s in self.cfg.symbols if v.covers(s)]

    async def fetch(self, v: VenueConfig) -> VenueSnapshot:
        adapter = ADAPTERS.get(v.adapter)
        if adapter is None:
            raise VenueError(f"unknown adapter {v.adapter!r} (known: {', '.join(sorted(ADAPTERS))})")
        s = Session(self.http)
        try:
            symbols = await adapter(s, v, self.symbols_for(v))
        finally:
            self.last[v.name] = (s.latency_ms, s.calls, s.rate_limited)
        return VenueSnapshot(v.name, self.clock(), s.latency_ms, s.calls, symbols)


@dataclass
class CheckResult:
    # 会場 -> API 自身が返した建て (時間)
    api_intervals: dict[str, float]
    lines: list[str]
    # 取得できた会場
    ok: set[str]
    # 会場 -> 失敗の理由
    errors: dict[str, str]
    # 設定の誤り (建ての食い違いなど)。接続できないのとは別扱い
    config_errors: list[str]

    def ok_tradable(self, cfg: FundingConfig) -> list[str]:
        return [v.name for v in cfg.tradable if v.name in self.ok]

    def raise_for_check(self, cfg: FundingConfig) -> None:
        """check サブコマンド用: 設定の誤りと、売買対象の会場の接続失敗は許さない

        参照のみの会場 (CEX など) が地域制限などでつながらないのは、計測を止める理由にならない
        ので警告だけにする。
        """
        bad = list(self.config_errors)
        bad += [f"{n}: {e}" for n, e in self.errors.items() if not cfg.venue(n).reference_only]
        if bad:
            raise VenueError("check failed:\n  " + "\n  ".join(bad))

    def raise_for_run(self, cfg: FundingConfig) -> None:
        """run 用: ペアが作れる数の会場が生きていればよい

        7 日回す計測で 1 会場の一時的な障害に起動を止められると、再起動ループで
        計測自体が進まない。落ちている会場は scanner が実行中もスキップする。
        """
        if self.config_errors:
            raise VenueError("check failed:\n  " + "\n  ".join(self.config_errors))
        alive = self.ok_tradable(cfg)
        if len(alive) < 2:
            raise VenueError(
                "売買対象の会場が 2 つ以上つながりません (生きている: "
                + (", ".join(alive) or "なし")
                + ")\n  "
                + "\n  ".join(f"{n}: {e}" for n, e in self.errors.items())
            )


async def check_all(client: VenueClient) -> CheckResult:
    """設定した会場に実際につながるか・記号が存在するか・建てが設定と合っているかを確かめる

    ここでは投げない。呼び出し側が raise_for_check / raise_for_run で厳しさを選ぶ。
    """
    cfg = client.cfg
    results = await asyncio.gather(*(client.fetch(v) for v in cfg.venues), return_exceptions=True)
    lines: list[str] = []
    errors: dict[str, str] = {}
    config_errors: list[str] = []
    ok: set[str] = set()
    api_intervals: dict[str, float] = {}
    for v, r in zip(cfg.venues, results):
        tag = "参照" if v.reference_only else "対象"
        if isinstance(r, BaseException):
            errors[v.name] = f"{type(r).__name__}: {r}"
            note = " (参照のみなので計測は続行)" if v.reference_only else ""
            lines.append(f"[{tag}] {v.name:>12}: NG  {errors[v.name]}{note}")
            continue
        ok.add(v.name)
        for sym, snap in sorted(r.symbols.items()):
            book = snap.book
            depth = ""
            if book:
                depth = (
                    f" 板 bid {len(book.bids)}本/${_notional(book.bids):,.0f}"
                    f" ask {len(book.asks)}本/${_notional(book.asks):,.0f}"
                )
            lines.append(
                f"[{tag}] {v.name:>12} {sym:<4} funding {snap.raw_rate:+.8f}/{snap.interval_hours:g}h"
                f" -> 年率 {snap.funding_apr * 100:+7.2f}%  mark {snap.mark:,.2f}{depth}"
            )
            if snap.api_interval_hours is not None:
                api_intervals[v.name] = snap.api_interval_hours
                if abs(snap.api_interval_hours - v.funding_interval_hours) > 1e-9:
                    config_errors.append(
                        f"{v.name}: funding_interval_hours の食い違い "
                        f"(設定 {v.funding_interval_hours}h / API {snap.api_interval_hours}h)"
                    )
        lines.append(
            f"[{tag}] {v.name:>12}      {r.calls} calls {r.latency_ms:.0f}ms"
            f"  taker {v.taker_bps:g}bps maker {v.maker_bps:g}bps  ({v.fee_source or 'fee_source 未記入'})"
        )
    return CheckResult(api_intervals, lines, ok, errors, config_errors)


def _notional(levels: tuple[tuple[float, float], ...]) -> float:
    return sum(px * sz for px, sz in levels)
