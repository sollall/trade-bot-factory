"""標準ライブラリだけの小さな WebSocket クライアント (RFC 6455 のうち、配信を受けるのに要る分だけ)。

できること: ws:// と wss://、HTTPS プロキシ越し (CONNECT) と直結、テキストの送受信、送信のマスク、
分割フレームの組み立て、ping への pong、close のやりとり。
やらないこと: 圧縮 (permessage-deflate)、サブプロトコル、認証。相手は公開の配信だけ。

プロキシは環境変数 (HTTPS_PROXY / NO_PROXY) に従う。TLS の検証は切らない (CA は SSL_CERT_FILE があればそれ)。
Worker には載せない (tbf/mm は scripts/build_worker.py が除外している)。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct
import urllib.parse
import urllib.request
from typing import Optional

from tbf.http import USER_AGENT

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
#: 1 通の上限 (分割を組み立てた後)。壊れた相手や間違った接続先でメモリを食い尽くさないため
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


class WebSocketError(RuntimeError):
    """つながらない、握手に失敗した、決まりに合わないフレームが来た"""


def accept_key(key: str) -> str:
    """握手の Sec-WebSocket-Key に対して、相手が返すべき Sec-WebSocket-Accept"""
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def apply_mask(payload: bytes, mask: bytes) -> bytes:
    if not payload:
        return payload
    key = (mask * (len(payload) // 4 + 1))[: len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(len(payload), "big")


def encode_frame(opcode: int, payload: bytes = b"", *, mask: Optional[bytes] = None, fin: bool = True) -> bytes:
    """フレームを 1 つ組み立てる。mask (4 バイト) を渡すとマスクする (クライアントから送るものは必ずマスクする決まり)"""
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    bit = 0x80 if mask is not None else 0
    if n < 126:
        head += bytes([bit | n])
    elif n < 1 << 16:
        head += bytes([bit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", n)
    if mask is None:
        return head + payload
    return head + mask + apply_mask(payload, mask)


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """フレームを 1 つ読む -> (最後の断片か, opcode, 中身)。マスクされていれば外す"""
    b0, b1 = await reader.readexactly(2)
    if b0 & 0x70:
        raise WebSocketError("予約ビットが立ったフレーム (圧縮などの拡張は扱わない)")
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack("!H", await reader.readexactly(2))
    elif n == 127:
        (n,) = struct.unpack("!Q", await reader.readexactly(8))
    if n > MAX_MESSAGE_BYTES:
        raise WebSocketError(f"フレームが大きすぎる ({n} バイト)")
    mask = await reader.readexactly(4) if b1 & 0x80 else None
    payload = await reader.readexactly(n)
    return bool(b0 & 0x80), b0 & 0x0F, apply_mask(payload, mask) if mask else payload


async def _read_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str], str]:
    """HTTP の応答の頭 (ステータス行とヘッダ) を読む"""
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise WebSocketError(f"HTTP の応答として読めない: {lines[0][:100]!r}")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return int(parts[1]), headers, lines[0]


def proxy_for(url: str) -> Optional[tuple[str, int]]:
    """この URL に使うプロキシ (host, port)。環境変数に従う。使わないなら None"""
    u = urllib.parse.urlsplit(url)
    if urllib.request.proxy_bypass(u.hostname or ""):
        return None
    proxy = urllib.request.getproxies().get("https" if u.scheme == "wss" else "http")
    if not proxy:
        return None
    p = urllib.parse.urlsplit(proxy if "//" in proxy else "//" + proxy)
    return (p.hostname or "", p.port or 8080)


class WebSocket:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer
        self.closed = False
        self.close_code: Optional[int] = None

    @classmethod
    async def connect(cls, url: str, *, timeout: float = 20.0, headers: Optional[dict[str, str]] = None) -> "WebSocket":
        return await asyncio.wait_for(cls._connect(url, headers or {}), timeout)

    @classmethod
    async def _connect(cls, url: str, extra: dict[str, str]) -> "WebSocket":
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise WebSocketError(f"ws:// か wss:// の URL ではない: {url}")
        secure = u.scheme == "wss"
        host, port = u.hostname, u.port or (443 if secure else 80)
        proxy = proxy_for(url)
        ctx = ssl.create_default_context() if secure else None  # 検証は既定のまま (SSL_CERT_FILE があればそれを使う)
        writer = None
        try:
            if proxy:
                reader, writer = await asyncio.open_connection(*proxy)
                writer.write(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\nUser-Agent: {USER_AGENT}\r\n\r\n".encode())
                await writer.drain()
                status, _, line = await _read_head(reader)
                if status != 200:
                    raise WebSocketError(f"プロキシが CONNECT {host}:{port} を通さない: {line}")
                if secure:
                    await writer.start_tls(ctx, server_hostname=host)
            else:
                reader, writer = await asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if secure else None)
            key = base64.b64encode(os.urandom(16)).decode()
            path = (u.path or "/") + ("?" + u.query if u.query else "")
            default_port = port == (443 if secure else 80)
            head = {
                "Host": host if default_port else f"{host}:{port}", "Upgrade": "websocket", "Connection": "Upgrade",
                "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13", "User-Agent": USER_AGENT, **extra,
            }
            writer.write((f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in head.items()) + "\r\n").encode())
            await writer.drain()
            status, got, line = await _read_head(reader)
            if status != 101:
                body = b""
                try:
                    body = await asyncio.wait_for(reader.read(300), 1.0)
                except (asyncio.TimeoutError, OSError):
                    pass
                raise WebSocketError(f"WebSocket への切り替えを断られた: {line} {body[:200]!r}")
            if got.get("sec-websocket-accept") != accept_key(key):
                raise WebSocketError("握手の応答 (Sec-WebSocket-Accept) が合わない")
            return cls(reader, writer)
        except BaseException:
            if writer is not None:
                writer.close()
            raise

    async def _send(self, opcode: int, payload: bytes) -> None:
        self.writer.write(encode_frame(opcode, payload, mask=os.urandom(4)))
        await self.writer.drain()

    async def send(self, text: str) -> None:
        await self._send(OP_TEXT, text.encode())

    async def ping(self, payload: bytes = b"") -> None:
        await self._send(OP_PING, payload)

    async def recv(self) -> Optional[str]:
        """次の 1 通 (テキスト)。相手が閉じたら None。ping には自動で pong を返す。
        回線が切れたら OSError / asyncio.IncompleteReadError をそのまま上げる"""
        parts: list[bytes] = []
        size = 0
        while True:
            if self.closed:
                return None
            fin, opcode, payload = await read_frame(self.reader)
            if opcode == OP_PING:
                await self._send(OP_PONG, payload)
            elif opcode == OP_PONG:
                pass
            elif opcode == OP_CLOSE:
                self.close_code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                self.closed = True
                try:
                    await self._send(OP_CLOSE, payload[:2])
                except OSError:
                    pass
                return None
            elif opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                if (opcode == OP_CONT) != bool(parts):
                    raise WebSocketError("分割フレームの順番がおかしい")
                parts.append(payload)
                size += len(payload)
                if size > MAX_MESSAGE_BYTES:
                    raise WebSocketError("1 通が大きすぎる")
                if fin:
                    return b"".join(parts).decode("utf-8", errors="replace")
            else:
                raise WebSocketError(f"知らない opcode {opcode}")

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                await self._send(OP_CLOSE, struct.pack("!H", 1000))
            except OSError:
                pass
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 2.0)
        except (asyncio.TimeoutError, OSError, ssl.SSLError):
            pass
