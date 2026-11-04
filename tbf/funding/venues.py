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
from datetime import datetime
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
        bids, asks = book["levels"][0][: v.effective_book_depth], book["levels"][1][: v.effective_book_depth]
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
        book = await s.json("GET", f"{v.base_url}/api/v1/orderBookOrders?market_id={d['market_id']}&limit={v.effective_book_depth}")
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
        book = await s.json("GET", f"{v.base_url}/v1/orderbook/{native}?depth={v.effective_book_depth}")
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
                _levels(book["bids"][: v.effective_book_depth], "price", "size", True),
                _levels(book["asks"][: v.effective_book_depth], "price", "size", False),
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


# ------------------------------------------------------------------ 建ての検証
#
# レートの建て (1h / 8h) を取り違えるのがこのスクリーナーで一番効く事故で、
# 間違えると年率が 8 倍ずれる。「レジストリにそう書いてある」では不十分なので、
# check のたびに API 側から建てを確かめる。
#
# 会場ごとに確かめ方が違う:
#   hyperliquid / dydx  履歴の支払い間隔が 1 時間であること
#   lighter             funding-rates の値 / 8 == fundings (1h) の値 であること
#   binance             fundingInfo の fundingIntervalHours (銘柄ごとに 8h / 4h / 1h がある)
#   paradex             markets/summary の funding_interval_minutes (既存の api_interval_field)


@dataclass(frozen=True)
class IntervalCheck:
    venue: str
    # API 側から読めた建て (時間)。読めなければ None
    hours: Optional[float]
    note: str = ""
    error: Optional[str] = None


def _spacing_hours(times_ms: list[int]) -> Optional[float]:
    """支払い時刻の並びから間隔 (時間) を出す。ばらつきは秒に丸めて見る"""
    ts = sorted(times_ms)
    gaps = [round((b - a) / 1000) for a, b in zip(ts, ts[1:])]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    # 記録時刻に数十 ms のばらつきがあるので最頻値を使う
    common = max(set(gaps), key=gaps.count)
    return common / 3600


async def verify_hyperliquid(s: Session, v: VenueConfig, native: str) -> IntervalCheck:
    start = int(time.time() * 1000) - 12 * 3_600_000
    rows = await s.json("POST", v.base_url, json.dumps({"type": "fundingHistory", "coin": native, "startTime": start}))
    hours = _spacing_hours([int(r["time"]) for r in rows]) if rows else None
    return IntervalCheck(v.name, hours, f"fundingHistory {len(rows)} 件の支払い間隔")


async def verify_dydx(s: Session, v: VenueConfig, native: str) -> IntervalCheck:
    rows = (await s.json("GET", f"{v.base_url}/v4/historicalFunding/{native}?limit=12")).get("historicalFunding") or []
    times = []
    for r in rows:
        try:
            times.append(int(datetime.fromisoformat(r["effectiveAt"].replace("Z", "+00:00")).timestamp() * 1000))
        except (KeyError, ValueError):
            pass
    return IntervalCheck(v.name, _spacing_hours(times), f"historicalFunding {len(rows)} 件の支払い間隔")


async def verify_lighter(s: Session, v: VenueConfig, native: str) -> IntervalCheck:
    """funding-rates の rate を /fundings (1 時間あたりの％) で検算する

    fixture に残すだけでは、会場側が建てを変えたときに気づけない。
    """
    details = await s.json("GET", f"{v.base_url}/api/v1/orderBookDetails")
    _check_lighter_code(details)
    mid = next((int(d["market_id"]) for d in details["order_book_details"] if d["symbol"] == native), None)
    if mid is None:
        return IntervalCheck(v.name, None, error=f"{native} is not listed")
    rates = await s.json("GET", f"{v.base_url}/api/v1/funding-rates")
    _check_lighter_code(rates)
    quoted = next(
        (float(r["rate"]) for r in rates["funding_rates"] if r.get("exchange") == "lighter" and r["symbol"] == native),
        None,
    )
    now = int(time.time())
    page = await s.json(
        "GET",
        f"{v.base_url}/api/v1/fundings?market_id={mid}&resolution=1h"
        f"&start_timestamp={now - 12 * 3600}&end_timestamp={now}&count_back=12",
    )
    _check_lighter_code(page)
    rows = page.get("fundings") or []
    if quoted is None or not rows:
        return IntervalCheck(v.name, None, error="funding-rates か fundings が空で検算できません")
    # /fundings は符号を持たず direction で表すので、検算は絶対値で行う
    hourly = [abs(float(r["rate"])) / 100.0 for r in rows]
    avg_hourly = sum(hourly) / len(hourly)
    spacing = _spacing_hours([int(r["timestamp"]) * 1000 for r in rows])
    if spacing is not None and abs(spacing - 1.0) > 1e-6:
        return IntervalCheck(v.name, None, error=f"fundings の間隔が 1h ではありません ({spacing}h)")
    if avg_hourly <= 0:
        return IntervalCheck(v.name, None, note="fundings のレートが 0 で建てを判定できません")
    implied = abs(quoted) / avg_hourly
    # funding-rates が 8 時間ぶんなら implied ≈ 8。レートは時間で動くので幅を持たせる
    note = f"funding-rates {quoted:+.3e} / fundings(1h) 平均 {avg_hourly:.3e} -> 建て {implied:.1f}h 相当"
    if abs(implied - v.funding_interval_hours) > v.funding_interval_hours * 0.5:
        return IntervalCheck(v.name, implied, note, error=f"検算が合いません ({note})")
    return IntervalCheck(v.name, v.funding_interval_hours, note)


async def verify_binance(s: Session, v: VenueConfig, native: str) -> IntervalCheck:
    """銘柄によって 8h / 4h / 1h がある。fundingInfo に出るのは既定と違う銘柄だけ"""
    rows = await s.json("GET", f"{v.base_url}/fapi/v1/fundingInfo")
    hit = next((r for r in rows if r.get("symbol") == native), None)
    if hit is None:
        return IntervalCheck(v.name, 8.0, f"{native} は fundingInfo に無い -> 既定の 8h")
    return IntervalCheck(v.name, float(hit["fundingIntervalHours"]), f"fundingInfo の fundingIntervalHours")


Verifier = Callable[[Session, VenueConfig, str], Awaitable[IntervalCheck]]

VERIFIERS: dict[str, Verifier] = {
    "hyperliquid": verify_hyperliquid,
    "dydx": verify_dydx,
    "lighter": verify_lighter,
    "binance": verify_binance,
}


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

    async def verify_interval(self, v: VenueConfig) -> Optional[IntervalCheck]:
        """API 側からファンディングの建てを確かめる (check のときだけ)"""
        verifier = VERIFIERS.get(v.adapter)
        syms = self.symbols_for(v)
        if verifier is None or not syms:
            return None
        s = Session(self.http)
        return await verifier(s, v, v.symbols[syms[0]])


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


async def check_all(client: VenueClient, verify_intervals: bool = True) -> CheckResult:
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
    verified: dict[str, IntervalCheck] = {}
    if verify_intervals:
        checks = await asyncio.gather(
            *(client.verify_interval(v) for v in cfg.venues), return_exceptions=True
        )
        for v, c in zip(cfg.venues, checks):
            if isinstance(c, BaseException):
                lines.append(f"[建て] {v.name:>12}: 検証できませんでした ({type(c).__name__}: {c})")
                continue
            if c is None:
                continue
            verified[v.name] = c
            if c.error:
                config_errors.append(f"{v.name}: 建ての検証に失敗 — {c.error}")
            elif c.hours is None:
                lines.append(f"[建て] {v.name:>12}: API から建てを読めませんでした ({c.note})")
            else:
                api_intervals[v.name] = c.hours
                mark = "OK" if abs(c.hours - v.funding_interval_hours) < 1e-6 else "NG"
                line = f"[建て] {v.name:>12}: {mark} 設定 {v.funding_interval_hours:g}h / API {c.hours:g}h  ({c.note})"
                lines.append(line)
                if mark == "NG":
                    config_errors.append(
                        f"{v.name}: funding_interval_hours の食い違い "
                        f"(設定 {v.funding_interval_hours}h / API {c.hours}h) — 年率が {c.hours / v.funding_interval_hours:.1f} 倍ずれます"
                    )
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
                # 返ってきた本数が上限に張り付いているなら、板はそこで切れている。
                # 見えている金額が測りたいサイズに足りていれば実害はないが、
                # 銘柄を増やすとこの会場だけ偽の「板不足」が出るので必ず見えるようにする
                cap = v.effective_book_depth
                if cap and max(len(book.bids), len(book.asks)) >= cap:
                    visible = min(_notional(book.bids), _notional(book.asks))
                    need = max(cfg.sizes_usd)
                    depth += f" (上限 {cap} 本で打ち切り"
                    depth += (
                        f" — 見えているのは ${visible:,.0f} までで、測りたい ${need:,.0f} に足りません)"
                        if visible < need
                        else ")"
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
