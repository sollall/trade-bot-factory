"""会場ごとの公開の配信 (WebSocket) の読み方と、それを記録に残す係。

読み取りだけ。認証つきの配信には触らない。発注のコードも無い。

配信の読み方 (GmoStream / BitbankStream) は、受け取った文を 1 通ずつ食わせると出来事の列を返すだけの
部品で、回線を持たない (テストは固定の例を食わせる)。回線・再接続・sqlite への保存は StreamRecorder がやる。

会場ごとの事実 (2026-11-23 に実物と公式ドキュメントで確かめたもの。docs/mm.md の「配信で記録する」に詳しく):

  gmo      wss://api.coin.z.com/ws/public/v1。購読は {"command":"subscribe","channel":...,"symbol":...}。
           購読の命令は同じ IP から 1 秒に 1 回まで (公式)。サーバーが 1 分に 1 回 ping を送り、3 回続けて
           pong が無いと切られる (公式)。orderbooks は配信のたびに板の全体 (片側 30 段)。
           trades は option を付けないと 1 つの約定が BUY と SELL の 2 通で来る (REST と同じ)。
           "option":"TAKER_ONLY" を付けると仕掛けた側の 1 通だけになり、**約定の向きが分かる** (公式)。
  bitbank  wss://stream.bitbank.cc/socket.io/?EIO=4&transport=websocket (socket.io 4 / Engine.IO 4)。
           開くと 0{...} が来るので 40 を返し、42["join-room","<部屋>"] で部屋に入る。サーバーの ping "2" には "3"。
           部屋は transactions_{pair} (約定。side は taker 側)、depth_whole_{pair} (板の全体、片側 200 段)、
           depth_diff_{pair} (板の差分)。板は「全体で置き換え、それより新しい差分を当て直す」(公式の手順)。
"""

from __future__ import annotations

import asyncio
import heapq
import json
import time
import zlib
from typing import Awaitable, Callable, Optional

from tbf.http import Http
from tbf.mm.book import Book, Trade
from tbf.mm.store import MmStore
from tbf.mm.venues import Venue, _iso_ms
from tbf.mm.ws import WebSocket

#: 配信の記録で残す気配の段数の既定 (片側)。配信は 1 秒に何度も来るので、ポーリングの既定 (10 段) より浅くして量を抑える
DEFAULT_WS_DEPTH = 5
#: paper で、気配を確定するまで待つ時間。気配より前の時刻の約定が遅れて届くことがあるので、この分だけ待ってから処理する
SETTLE_MS = 2000

# 出来事: ("book", 銘柄, Book) / ("trades", 銘柄, [Trade]) / ("send", 文) / ("error", 説明)
Event = tuple


class StreamError(RuntimeError):
    """配信の側から「だめ」と言われた。つなぎ直す"""


class GmoStream:
    name = "gmo"
    url = "wss://api.coin.z.com/ws/public/v1"
    #: これだけ何も来なければ切れたとみなす (気配は 1 秒に何通も来る。サーバーの ping も 1 分に 1 回ある)
    idle_timeout = 90.0
    #: 購読の命令の間隔 (秒)。公式の上限は 1 秒に 1 回。少し余裕を持たせる
    subscribe_gap = 1.2

    def __init__(self, coins: list[str], depth: int) -> None:
        self.coins, self.depth = coins, depth
        self._last: dict[str, tuple[int, dict]] = {}  # 銘柄 -> (直前の約定の時刻, その時刻の中での (向き, 値段, 数量) ごとの件数)

    def start(self) -> list[tuple[str, float]]:
        """つないだ直後に送る文と、その前に待つ秒数"""
        out = []
        for c in self.coins:
            out.append((json.dumps({"command": "subscribe", "channel": "orderbooks", "symbol": c}), self.subscribe_gap))
            # TAKER_ONLY: 仕掛けた側の約定だけ。付けないと 1 つの約定が BUY と SELL の 2 通で来て、向きが分からない
            out.append((json.dumps({"command": "subscribe", "channel": "trades", "symbol": c, "option": "TAKER_ONLY"}), self.subscribe_gap))
        return out

    def feed(self, text: str, recv: int) -> list[Event]:
        d = json.loads(text)
        if "error" in d:
            raise StreamError(f"gmo: {d['error']}")
        coin, channel = d.get("symbol"), d.get("channel")
        if coin not in self.coins:
            return []
        if channel == "orderbooks":
            bids = [(float(l["price"]), float(l["size"])) for l in d["bids"]]
            asks = [(float(l["price"]), float(l["size"])) for l in d["asks"]]
            bids, asks = heapq.nlargest(self.depth, bids), heapq.nsmallest(self.depth, asks)
            if not bids or not asks:
                return []
            return [("book", coin, Book(_iso_ms(d["timestamp"]), tuple(bids), tuple(asks)))]
        if channel == "trades":
            # 約定に id が無いので中身から作る (REST の parse_gmo_trades と同じ考え方): 同じ時刻・同じ中身の中での通し番号を付ける
            t = _iso_ms(d["timestamp"])
            last_time, counts = self._last.get(coin, (0, {}))
            if t != last_time:
                counts = {}
            key = (d["side"], d["price"], d["size"])
            n = counts.get(key, 0)
            counts[key] = n + 1
            self._last[coin] = (t, counts)
            tid = zlib.crc32(f"{d['side']}|{d['price']}|{d['size']}".encode()) * 1000 + n
            return [("trades", coin, [Trade(t, "B" if d["side"] == "BUY" else "A", float(d["price"]), float(d["size"]), tid)])]
        return []


class BitbankStream:
    name = "bitbank"
    url = "wss://stream.bitbank.cc/socket.io/?EIO=4&transport=websocket"
    idle_timeout = 90.0  # サーバーの ping が 25 秒ごとに来る
    #: 差分をためておく上限 (板の全体が来るたびに古いものは捨てるので、ふつうは数十件)
    max_buffer = 20_000

    def __init__(self, coins: list[str], depth: int) -> None:
        self.coins, self.depth = coins, depth
        self.sides: dict[str, tuple[dict, dict]] = {}  # 銘柄 -> (買いの 値段 -> 数量, 売りの 値段 -> 数量)。板の全体が来てから
        self.seq: dict[str, int] = {}  # 最後に当てた順番 (sequenceId / s)
        self.diffs: dict[str, list[tuple[int, list, list, int]]] = {c: [] for c in coins}  # (s, 買いの差分, 売りの差分, 時刻)
        self.time: dict[str, int] = {}

    def start(self) -> list[tuple[str, float]]:
        return []  # サーバーの 0{...} を待ってから 40 を返す (feed の中)

    def _apply(self, coin: str, bids: list, asks: list) -> None:
        for book, rows in zip(self.sides[coin], (bids, asks)):
            for px, sz in rows:
                if float(sz) == 0:
                    book.pop(float(px), None)
                else:
                    book[float(px)] = float(sz)

    def _book(self, coin: str) -> list[Event]:
        bids, asks = self.sides[coin]
        top_b, top_a = heapq.nlargest(self.depth, bids.items()), heapq.nsmallest(self.depth, asks.items())
        if not top_b or not top_a or top_b[0][0] >= top_a[0][0]:
            return []  # 片側が空、または買いと売りが逆転している間 (差分の途中) は残さない
        return [("book", coin, Book(self.time[coin], tuple(top_b), tuple(top_a)))]

    def feed(self, text: str, recv: int) -> list[Event]:
        if text.startswith("0"):  # Engine.IO: 開いた
            return [("send", "40")]
        if text == "2":  # Engine.IO: ping
            return [("send", "3")]
        if text.startswith("40"):  # socket.io: つながった。部屋に入る
            rooms = [f"{kind}_{c}" for c in self.coins for kind in ("transactions", "depth_whole", "depth_diff")]
            return [("send", "42" + json.dumps(["join-room", r])) for r in rooms]
        if text.startswith(("41", "44")):
            raise StreamError(f"bitbank: {text[:200]}")
        if not text.startswith("42"):
            return []
        name, body = json.loads(text[2:])
        if name != "message":
            return []
        room, data = body["room_name"], body["message"]["data"]
        if room.startswith("transactions_"):
            coin = room[len("transactions_"):]
            if coin not in self.coins:
                return []
            # side は taker 側の売/買 (公式ドキュメント)。buy = 買い手主導
            return [("trades", coin, [
                Trade(int(t["executed_at"]), "B" if t["side"] == "buy" else "A", float(t["price"]), float(t["amount"]), int(t["transaction_id"]))
                for t in data["transactions"]
            ])]
        if room.startswith("depth_diff_"):
            coin = room[len("depth_diff_"):]
            if coin not in self.coins:
                return []
            s = int(data["s"])
            buf = self.diffs[coin]
            buf.append((s, data.get("b", []), data.get("a", []), int(data["t"])))
            if len(buf) > self.max_buffer:
                del buf[: len(buf) // 2]
            if coin not in self.sides or s <= self.seq[coin]:
                return []  # 板の全体がまだ無い、または古い差分
            self._apply(coin, data.get("b", []), data.get("a", []))
            self.seq[coin] = s
            self.time[coin] = max(self.time[coin], int(data["t"]))
            return self._book(coin)
        if room.startswith("depth_whole_"):
            coin = room[len("depth_whole_"):]
            if coin not in self.coins:
                return []
            # 公式の手順: 全体で置き換え、ためておいた差分のうち全体より新しいものだけを順に当て直す。古いものは捨てる
            seq = int(data["sequenceId"])
            self.sides[coin] = ({float(p): float(s) for p, s in data["bids"]}, {float(p): float(s) for p, s in data["asks"]})
            self.seq[coin] = seq
            self.time[coin] = max(self.time.get(coin, 0), int(data["timestamp"]))
            self.diffs[coin] = newer = sorted(d for d in self.diffs[coin] if d[0] > seq)
            for s, b, a, t in newer:
                self._apply(coin, b, a)
                self.seq[coin] = s
                self.time[coin] = max(self.time[coin], t)
            return self._book(coin)
        return []


STREAMS = {"gmo": GmoStream, "bitbank": BitbankStream}


class StreamRecorder:
    """配信を受けて、ポーリングの記録と同じテーブルに同じ形で残す。切れたら待ち時間を伸ばしながらつなぎ直す。

    気配は「残す段数ぶんの中身が前に残したものから変わったとき」だけ残す (配信のたびに残すと量が多すぎる)。
    同じ時刻 (ms) の気配が続いたら最初の 1 つだけ残す。約定は全件残す。
    切断の間の約定は取れないので、つなぎ直したときに gaps に残す (抜けた件数は分からない)。
    """

    def __init__(
        self,
        store: MmStore,
        venue: Venue,
        coins: list[str],
        depth: int = DEFAULT_WS_DEPTH,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
        log: Callable[[str], None] = print,
        connect: Callable[[str], Awaitable] = WebSocket.connect,
        on_book: Optional[Callable[[str, Book, int], None]] = None,
        on_trades: Optional[Callable[[str, list[Trade], int], None]] = None,
        min_book_ms: int = 0,
        backoff: tuple[float, float] = (1.0, 60.0),
    ) -> None:
        if venue.name not in STREAMS:
            raise ValueError(f"{venue.name} の配信での記録は未対応です (対応: {', '.join(STREAMS)})")
        self.store, self.venue, self.coins, self.depth = store, venue, coins, depth
        self.clock, self.log, self.connect = clock, log, connect
        # on_book(銘柄, 残した気配, 受け取った時刻) / on_trades(銘柄, 残した約定, この時刻より前に受け取った気配は確定してよい)
        self.on_book, self.on_trades = on_book, on_trades
        self.min_book_ms, self.backoff = min_book_ms, backoff
        self.last_top: dict[str, tuple] = {}
        self.last_book_time: dict[str, int] = {}
        self.last_seen = 0  # 最後に何かを受け取った時刻 (切断の間を gaps に残すのに使う)
        self.gaps = self.reconnects = self.messages = 0
        self.books_seen = self.books_kept = self.trades_kept = 0

    async def setup(self, http: Http) -> None:
        """違う会場・違う取り方の db でないこと、銘柄が実在することを確かめ、銘柄の決まりごとを残す (ここだけ REST)"""
        self.store.claim_venue(self.venue.name, "ws")
        for inst in (await self.venue.instruments(http, self.coins)).values():
            self.store.set_instrument(inst, self.clock())
        self.store.commit()

    def handle(self, events: list[Event], recv: int) -> list[str]:
        """配信の出来事を記録に残す。相手に送り返す文を返す"""
        out = []
        for ev in events:
            if ev[0] == "send":
                out.append(ev[1])
            elif ev[0] == "book":
                _, coin, book = ev
                self.books_seen += 1
                top = (book.bids, book.asks)
                last = self.last_book_time.get(coin, 0)
                if top == self.last_top.get(coin) or book.time <= last or book.time - last < self.min_book_ms:
                    continue  # 中身が同じ、時刻が進んでいない、または間引き
                if self.store.add_book(coin, book, recv):
                    self.last_top[coin], self.last_book_time[coin] = top, book.time
                    self.books_kept += 1
                    if self.on_book:
                        self.on_book(coin, book, recv)
            elif ev[0] == "trades":
                _, coin, trades = ev
                new = self.store.add_trades(coin, trades, recv)
                self.trades_kept += len(new)
                if self.on_trades and new:
                    self.on_trades(coin, new, recv - SETTLE_MS)
        return out

    def settle(self, recv: int) -> None:
        """paper 用: 約定が来ない銘柄でも、十分前に受け取った気配は確定させる"""
        if self.on_trades:
            for coin in self.coins:
                self.on_trades(coin, [], recv - SETTLE_MS)

    async def _session(self, stop: asyncio.Event, deadline: Optional[float]) -> None:
        stream = STREAMS[self.venue.name](self.coins, self.depth)
        started = time.monotonic()
        ws = await self.connect(stream.url)
        stopping = asyncio.ensure_future(stop.wait())
        receiving = None
        try:
            self.store.add_poll(self.clock(), "*", "ws", (time.monotonic() - started) * 1000, None, None)
            if self.last_seen:
                now = self.clock()
                for c in self.coins:  # 切れていた間の約定は取れていない
                    self.store.add_gap(c, now, self.last_seen, now)
                    self.gaps += 1
            self.store.commit()
            pending = list(stream.start())
            next_send = time.monotonic() + (pending[0][1] if pending else 0)
            last_commit = last_settle = last_message = time.monotonic()
            # 受信は 1 つの作業として持ち続ける (読みかけで取り消すと、フレームの途中のバイトを失って以後が読めなくなる)
            receiving = asyncio.ensure_future(ws.recv())
            while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
                now = time.monotonic()
                if pending and now >= next_send:
                    await ws.send(pending.pop(0)[0])
                    next_send = now + (pending[0][1] if pending else 0)
                    continue
                waits = [last_message + stream.idle_timeout - now]
                if pending:
                    waits.append(next_send - now)
                if deadline is not None:
                    waits.append(deadline - now)
                done, _ = await asyncio.wait({receiving, stopping}, timeout=max(0.0, min(waits)), return_when=asyncio.FIRST_COMPLETED)
                if receiving not in done:
                    if time.monotonic() - last_message >= stream.idle_timeout:
                        raise StreamError(f"{stream.idle_timeout:.0f} 秒何も届かない")
                    continue
                text = receiving.result()
                if text is None:
                    raise StreamError(f"相手が閉じた (code {ws.close_code})")
                receiving = asyncio.ensure_future(ws.recv())
                recv = self.clock()
                self.last_seen = recv
                self.messages += 1
                for reply in self.handle(stream.feed(text, recv), recv):
                    await ws.send(reply)
                last_message = now = time.monotonic()
                if now - last_settle >= 0.5:
                    self.settle(recv)
                    last_settle = now
                if now - last_commit >= 1.0:
                    self.store.commit()
                    last_commit = now
        finally:
            stopping.cancel()
            if receiving is not None:
                receiving.cancel()
                try:
                    await receiving
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self.store.commit()
            await ws.close()

    async def run(self, stop: Optional[asyncio.Event] = None, max_seconds: Optional[float] = None) -> None:
        stop = stop or asyncio.Event()
        deadline = time.monotonic() + max_seconds if max_seconds is not None else None
        delay = self.backoff[0]
        while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
            began = time.monotonic()
            try:
                await self._session(stop, deadline)
                continue  # 止めるように言われた (上の条件で抜ける)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001  記録は止めない。つなぎ直す
                msg = f"{type(e).__name__}: {e}"
                self.reconnects += 1
                self.store.add_poll(self.clock(), "*", "ws", None, msg[:300], None)
                self.store.commit()
                if time.monotonic() - began > 60:
                    delay = self.backoff[0]  # しばらく続いていた接続が切れただけなら、すぐつなぎ直す
                self.log(f"配信が切れました ({msg[:200]})。{delay:.0f} 秒後につなぎ直します")
            wait = delay if deadline is None else min(delay, max(0.0, deadline - time.monotonic()))
            delay = min(self.backoff[1], delay * 2)
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
