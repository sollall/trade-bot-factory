"""会場ごとの公開 API の読み方 (気配・約定・銘柄の情報・流量制限の見積もり)。

読み取りだけ。認証つきの API は呼ばない。発注のコードも無い。
会場を足すときは Venue を 1 つ書いて VENUES に登録する。記録・集計・再生・paper は会場を知らず、
ここが返す Book / Trade / Instrument だけを見る。

会場ごとの事実 (2026-11-22〜23 に実物と公式ドキュメントで確かめたもの。docs/mm.md の「会場」に詳しく):

  hyperliquid  無期限先物。気配は片側 20 段、約定は直近 10 件、約定に id (tid) がある。USD 建て
  gmo          GMO コイン。現物 (BTC, SOL, ...) とレバレッジ (BTC_JPY, ...)。気配は全段、約定は 1 回 100 件まで。
               約定に id が無く、仕掛けた側も大半は分からない。気配に時刻が無い (応答の responsetime を使う)。JPY 建て
  bitbank      現物。気配は片側 200 段、約定は直近 60 件、約定に id (transaction_id) がある。JPY 建て

約定の向きは、どの会場も Trade.side を「仕掛けた側」に揃える: "B" = 買い手主導、"A" = 売り手主導。
GMO だけは仕掛けた側が分からない約定が大半で、それは "U" (不明) にする (parse_gmo_trades)。
"""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from tbf.core.instrument import DEFAULT_FEES
from tbf.http import Http
from tbf.market.hyperliquid import INFO_URL, _post
from tbf.mm.book import Book, Trade

#: Hyperliquid のメイカー手数料率 (+0.015%)。無期限・基本ティア。
#: 2026-11-22 に公開 API の {"type":"userFees"} が返す料率表 (feeSchedule.add = 0.00015) で確かめた。
#: 公式ドキュメントの手数料ページでは **未確認** (この環境から届かない)。出来高や
#: ステーキングの割引、紹介の割引は入れていない。
HYPERLIQUID_MAKER_RATE = 0.00015
#: Hyperliquid の 1 注文の最小の代金 (USD)。10 ドルのはずだが **未確認**
HYPERLIQUID_MIN_NOTIONAL = 10.0


#: クォーターの金額のパラメータ (名前が _usd で終わるもの。実際は「建値の通貨での金額」) の既定値を、
#: 建値の通貨に直すための大まかな倍率。為替レートではない (既定値の桁を合わせるだけ。--param で渡した値はそのまま使う)
NOMINAL_PER_USD = {"USD": 1.0, "JPY": 150.0}


def scale_defaults(defaults: dict[str, float], quote: str) -> dict[str, float]:
    k = NOMINAL_PER_USD.get(quote, 1.0)
    return {name: v * k if name.endswith("_usd") else v for name, v in defaults.items()}


class RateLimited(RuntimeError):
    """会場に「投げすぎ」と言われた。記録係はしばらく休む"""


@dataclass(frozen=True)
class Instrument:
    """銘柄の決まりごと。記録を始めるときに会場の API から取り、sqlite に残す (再生はそこから読む)"""

    coin: str
    venue: str = "hyperliquid"
    quote: str = "USD"  # 建値の通貨。損益や代金はこの通貨で出る
    #: 値段の刻み。None なら Hyperliquid の規則 (有効 5 桁、小数は 6 - sz_decimals 桁。book.price_decimals)
    tick: Optional[float] = None
    sz_decimals: Optional[int] = None
    size_step: float = 0.0  # 数量の刻み
    min_size: float = 0.0  # 1 注文の最小の数量
    min_notional: float = 0.0  # 1 注文の最小の代金 (建値の通貨)
    maker_rate: float = HYPERLIQUID_MAKER_RATE  # 負はリベート (受け取り)
    taker_rate: float = 0.0
    #: 在庫 0 から売れるか。現物は売れない (再生はこれをまだ守らせていない。docs/mm.md)
    can_short: bool = True

    @property
    def symbol(self) -> str:
        return {"USD": "$", "JPY": "¥"}.get(self.quote, self.quote + " ")


def hyperliquid_instrument(coin: str, sz_decimals: int) -> Instrument:
    return Instrument(
        coin, "hyperliquid", "USD", None, sz_decimals, 10.0**-sz_decimals, 0.0, HYPERLIQUID_MIN_NOTIONAL,
        HYPERLIQUID_MAKER_RATE, DEFAULT_FEES[("hyperliquid", "perp")].taker_rate, True,
    )


async def _get(http: Http, url: str) -> dict:
    # 取り直さない。流量制限がはっきりしない会場なので、失敗は記録に残して次のポーリングに任せる
    status, text = await http.request("GET", url, {"accept": "application/json"})
    if status == 429:
        raise RateLimited(f"HTTP 429 {text[:200]}")
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError(f"HTTP {status} {text[:200]}") from None
    if status != 200 and not isinstance(data, dict):
        raise RuntimeError(f"HTTP {status} {text[:200]}")
    return data


def _book(time: int, bids: list[tuple[float, float]], asks: list[tuple[float, float]], depth: int) -> Book:
    # 並びは会場の応答に頼らず、ここで揃える (買いは高い順、売りは安い順)
    bids = sorted(bids, key=lambda l: -l[0])[:depth]
    asks = sorted(asks, key=lambda l: l[0])[:depth]
    if not bids or not asks:
        raise ValueError("気配の片側が空")
    return Book(time, tuple(bids), tuple(asks))


class Venue:
    name = ""
    quote = "USD"
    #: 流量制限の単位と、毎分の上限、1 回あたりの重み。confirmed が False の値は公式の裏取りが無い
    unit = "回数"
    limit_per_min = 0.0
    book_cost = 1.0
    trades_cost = 1.0
    limit_note = ""
    trades_per_call = 0
    #: リクエストを投げ始める間隔の下限 (秒)。0 なら銘柄ぶんを同時に投げる
    min_gap_sec = 0.0
    #: 「投げすぎ」と言われたときに休む秒数
    cooldown_sec = 30.0

    def per_min(self, n_coins: int, interval: float, trades_interval: float) -> float:
        """この設定が毎分使う量 (取り直しは含まない)"""
        return n_coins * (self.book_cost * 60 / interval + self.trades_cost * 60 / trades_interval)

    async def instruments(self, http: Http, coins: list[str]) -> dict[str, Instrument]:
        raise NotImplementedError

    async def book(self, http: Http, coin: str, depth: int) -> Book:
        raise NotImplementedError

    async def trades(self, http: Http, coin: str) -> list[Trade]:
        raise NotImplementedError


# ---------------------------------------------------------------- Hyperliquid


def parse_hyperliquid_book(raw: dict, depth: int) -> Book:
    bids, asks = ([(float(l["px"]), float(l["sz"])) for l in side[:depth]] for side in raw["levels"])
    if not bids or not asks:
        raise ValueError("気配の片側が空")
    return Book(int(raw["time"]), tuple(bids), tuple(asks))


def parse_hyperliquid_trades(raw: list) -> list[Trade]:
    return [Trade(int(t["time"]), t["side"], float(t["px"]), float(t["sz"]), int(t["tid"])) for t in raw]


class Hyperliquid(Venue):
    name = "hyperliquid"
    quote = "USD"
    # info は IP ごとに毎分の重みの上限があり、リクエストの種類で重みが違う。
    # 下の値は **未確認** (記憶にある値。公式ドキュメント hyperliquid.gitbook.io の rate limits は
    # この環境から届かず、確かめられていない)。確かめたら直してこのコメントを消す。
    unit = "重み"
    limit_per_min = 1200.0
    book_cost = 2.0  # l2Book
    trades_cost = 20.0  # recentTrades
    limit_note = "重みと上限は未確認の値"
    trades_per_call = 10

    def __init__(self, url: str = INFO_URL) -> None:
        self.url = url

    async def instruments(self, http: Http, coins: list[str]) -> dict[str, Instrument]:
        meta = await _post(http, {"type": "meta"}, self.url)
        decimals = {u["name"]: int(u["szDecimals"]) for u in meta["universe"] if not u.get("isDelisted")}
        missing = [c for c in coins if c not in decimals]
        if missing:
            raise ValueError(f"Hyperliquid の無期限先物に無い銘柄: {', '.join(missing)}")
        return {c: hyperliquid_instrument(c, decimals[c]) for c in coins}

    async def book(self, http: Http, coin: str, depth: int) -> Book:
        return parse_hyperliquid_book(await _post(http, {"type": "l2Book", "coin": coin}, self.url), depth)

    async def trades(self, http: Http, coin: str) -> list[Trade]:
        return parse_hyperliquid_trades(await _post(http, {"type": "recentTrades", "coin": coin}, self.url))


# ---------------------------------------------------------------- GMO コイン


def _iso_ms(s: str) -> int:
    """'2026-11-22T21:54:11.702Z' -> ms"""
    return round(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _gmo_data(raw: dict) -> object:
    if raw.get("status") != 0:
        codes = ", ".join(f"{m.get('message_code')} {m.get('message_string')}" for m in raw.get("messages", []))
        if "ERR-5003" in codes:  # Requests are too many
            raise RateLimited(f"gmo: {codes}")
        raise RuntimeError(f"gmo: status {raw.get('status')} {codes}"[:300])
    return raw["data"]


def parse_gmo_instruments(raw: dict) -> dict[str, Instrument]:
    out = {}
    for s in _gmo_data(raw):  # type: ignore[union-attr]
        out[s["symbol"]] = Instrument(
            s["symbol"], "gmo", "JPY", float(s["tickSize"]), None, float(s["sizeStep"]), float(s["minOrderSize"]), 0.0,
            float(s["makerFee"]), float(s["takerFee"]),
            can_short=s["symbol"].endswith("_JPY"),  # *_JPY はレバレッジ取引 (売りから入れる)。それ以外は現物
        )
    return out


def parse_gmo_book(raw: dict, depth: int) -> Book:
    data = _gmo_data(raw)
    # 気配そのものには時刻が無い。応答に付く responsetime (サーバーが返した時刻) を気配の時刻にする
    side = lambda rows: [(float(l["price"]), float(l["size"])) for l in rows]  # noqa: E731
    return _book(_iso_ms(raw["responsetime"]), side(data["bids"]), side(data["asks"]), depth)  # type: ignore[index]


#: GMO の約定履歴が 1 回に返す件数 (count の最大)
GMO_PAGE = 100


def parse_gmo_trades(raw: dict) -> list[Trade]:
    """GMO の約定履歴を Trade にする。ほかの会場と違う点が 2 つある。

    1. **向きが分からない約定が大半。** 同じ時刻・値段・数量で BUY と SELL の 2 行が返る
       (2026-11-22 の実測で BTC_JPY は全部、ETH_JPY・XRP_JPY もほぼ全部、現物の SOL・DOGE は半分〜8 割)。
       1 つの約定の両側と見るしかなく、どちらが仕掛けたかは API からは分からない。公式ドキュメントの
       side の説明は「売買区分」だけ。対の 2 行は 1 件にまとめ、side を "U" (不明) にする
       (まとめないと出来高を 2 重に数える)。対にならない行だけ BUY -> "B"、SELL -> "A" とする。
    2. **約定に id が無い。** 中身から作る: (向き, 値段, 数量) のハッシュ x 1000 + 同じ時刻・同じ中身の中での通し番号。
       同じミリ秒に同じ中身の約定が複数あっても別のものとして残り、次の応答で同じ約定をもう一度
       受け取れば同じ id になる (重複として落ちる)。

    応答が上限の件数いっぱいのときは、いちばん古い時刻の行を捨てる。その時刻の約定が途中で切れていると、
    対の片方だけが残って別の約定に見えるため (捨てた分は、前のポーリングで取れているはず。
    取れていなければ取りこぼしとして記録に残る)。
    """
    rows = _gmo_data(raw)["list"] or []  # type: ignore[index]
    if len(rows) >= GMO_PAGE:
        oldest = min(t["timestamp"] for t in rows)
        rows = [t for t in rows if t["timestamp"] != oldest]
    groups: dict[tuple, list[int]] = {}  # (時刻, 値段, 数量) -> [BUY の行数, SELL の行数]。古い順に並べる
    for t in reversed(rows):
        groups.setdefault((_iso_ms(t["timestamp"]), t["price"], t["size"]), [0, 0])[0 if t["side"] == "BUY" else 1] += 1
    out = []
    for (time, price, size), (buys, sells) in groups.items():
        both = min(buys, sells)
        for side, count in (("U", both), ("B", buys - both), ("A", sells - both)):
            base = zlib.crc32(f"{side}|{price}|{size}".encode()) * 1000
            out += [Trade(time, side, float(price), float(size), base + n) for n in range(count)]
    return sorted(out, key=lambda t: t.time)


class Gmo(Venue):
    name = "gmo"
    quote = "JPY"
    # Public API (REST) の上限は公式ドキュメント (https://api.coin.z.com/docs/ の「制限」。2026-11-22 に確認) に
    # 記載が無い。書いてあるのは Private API (最低の階層で 20 回/秒) と WebSocket の購読 (1 回/秒) だけ。
    # 下の上限は **仮の値** (毎秒 5 回)。既定ではその半分までしか使わない。
    limit_per_min = 300.0
    limit_note = "Public API の上限は公式に記載なし。仮の値 (毎秒 5 回)"
    trades_per_call = 100
    # 実測 (2026-11-22): 5 銘柄ぶんの気配と約定 (計 10 回) を 1 秒ほどの間に続けて投げると、ときどき
    # ERR-5003 (Requests are too many) が返った (毎分の合計は 130 回)。合計ではなく瞬間の集中に上限がある。
    # 正確な値は分からないので、1 回ずつ 0.25 秒あけて投げる (毎秒 4 回まで)
    min_gap_sec = 0.25
    cooldown_sec = 3.0  # 弾かれるのは約定の取得だけで、気配は通る (実測)。長く休むと気配の記録が粗くなる

    def __init__(self, url: str = "https://api.coin.z.com/public/v1") -> None:
        self.url = url

    async def instruments(self, http: Http, coins: list[str]) -> dict[str, Instrument]:
        known = parse_gmo_instruments(await _get(http, f"{self.url}/symbols"))
        missing = [c for c in coins if c not in known]
        if missing:
            raise ValueError(f"GMO コインに無い銘柄: {', '.join(missing)} (現物は BTC、レバレッジは BTC_JPY の形)")
        return {c: known[c] for c in coins}

    async def book(self, http: Http, coin: str, depth: int) -> Book:
        return parse_gmo_book(await _get(http, f"{self.url}/orderbooks?symbol={coin}"), depth)

    async def trades(self, http: Http, coin: str) -> list[Trade]:
        return parse_gmo_trades(await _get(http, f"{self.url}/trades?symbol={coin}&page=1&count={GMO_PAGE}"))


# ---------------------------------------------------------------- bitbank


def _bitbank_data(raw: dict) -> dict:
    if raw.get("success") != 1:
        code = (raw.get("data") or {}).get("code")
        if code == 10009:  # アクセス頻度が高すぎます
            raise RateLimited(f"bitbank: code {code}")
        raise RuntimeError(f"bitbank: code {code}")
    return raw["data"]


def parse_bitbank_instruments(raw: dict) -> dict[str, Instrument]:
    out = {}
    for p in _bitbank_data(raw)["pairs"]:
        if not p.get("is_enabled", True):
            continue
        quote = p["quote_asset"].upper()
        out[p["name"]] = Instrument(
            p["name"], "bitbank", quote, 10.0 ** -int(p["price_digits"]), None, 10.0 ** -int(p["amount_digits"]),
            float(p["unit_amount"]), 0.0, float(p["maker_fee_rate_quote"]), float(p["taker_fee_rate_quote"]),
            can_short=False,  # 現物として扱う (信用取引は見ていない)
        )
    return out


def parse_bitbank_book(raw: dict, depth: int) -> Book:
    d = _bitbank_data(raw)
    side = lambda rows: [(float(p), float(s)) for p, s in rows]  # noqa: E731
    return _book(int(d["timestamp"]), side(d["bids"]), side(d["asks"]), depth)


def parse_bitbank_trades(raw: dict) -> list[Trade]:
    # side は taker 側の売/買 (公式ドキュメント public-api_JP.md に明記)。buy = 買い手主導
    return [
        Trade(int(t["executed_at"]), "B" if t["side"] == "buy" else "A", float(t["price"]), float(t["amount"]), int(t["transaction_id"]))
        for t in _bitbank_data(raw)["transactions"]
    ]


class Bitbank(Venue):
    name = "bitbank"
    quote = "JPY"
    # 公式ドキュメント (github.com/bitbankinc/bitbank-api-docs の rest-api_JP.md「レートリミット」。2026-11-23 に確認):
    # 「取得系は 10 回/秒」「抵触すると HTTP 429」。ただしこれは認証つきの REST API (ユーザーごと) の記述で、
    # Public API の上限は書かれていない。同じ値を **仮に** 当てはめ、既定ではその半分までしか使わない。
    limit_per_min = 600.0
    limit_note = "Public API の上限は公式に記載なし。認証つき API の値 (取得系 10 回/秒) を仮に流用"
    trades_per_call = 60
    min_gap_sec = 0.1  # 瞬間でも毎秒 10 回を超えないように

    def __init__(self, public_url: str = "https://public.bitbank.cc", api_url: str = "https://api.bitbank.cc/v1") -> None:
        self.public_url, self.api_url = public_url, api_url

    async def instruments(self, http: Http, coins: list[str]) -> dict[str, Instrument]:
        known = parse_bitbank_instruments(await _get(http, f"{self.api_url}/spot/pairs"))
        missing = [c for c in coins if c not in known]
        if missing:
            raise ValueError(f"bitbank に無い (または停止中の) 銘柄: {', '.join(missing)} (xrp_jpy の形)")
        return {c: known[c] for c in coins}

    async def book(self, http: Http, coin: str, depth: int) -> Book:
        return parse_bitbank_book(await _get(http, f"{self.public_url}/{coin}/depth"), depth)

    async def trades(self, http: Http, coin: str) -> list[Trade]:
        return parse_bitbank_trades(await _get(http, f"{self.public_url}/{coin}/transactions"))


VENUES: dict[str, type[Venue]] = {"hyperliquid": Hyperliquid, "gmo": Gmo, "bitbank": Bitbank}


def step_decimals(step: float) -> int:
    """刻みの小数の桁数 (0.001 -> 3、1 -> 0、0.5 -> 1)"""
    return max(0, -Decimal(repr(step)).normalize().as_tuple().exponent)
