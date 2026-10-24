"""
Ethereum の keccak256 (標準ライブラリの sha3_256 とはパディングが違う)。
依存を増やさないための純 Python 実装 (Keccak チームの CompactFIPS202 を元にしたもの)。
"""

from __future__ import annotations

_MASK = (1 << 64) - 1


def _rol(a: int, n: int) -> int:
    n %= 64
    return ((a >> (64 - n)) | (a << n)) & _MASK if n else a


def _keccak_f(lanes: list[list[int]]) -> list[list[int]]:
    r = 1
    for _ in range(24):
        c = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)]
        d = [c[(x + 4) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        lanes = [[lanes[x][y] ^ d[x] for y in range(5)] for x in range(5)]
        x, y = 1, 0
        cur = lanes[x][y]
        for t in range(24):
            x, y = y, (2 * x + 3 * y) % 5
            cur, lanes[x][y] = lanes[x][y], _rol(cur, (t + 1) * (t + 2) // 2)
        for y in range(5):
            row = [lanes[x][y] for x in range(5)]
            for x in range(5):
                lanes[x][y] = row[x] ^ ((~row[(x + 1) % 5]) & row[(x + 2) % 5])
        for j in range(7):
            r = ((r << 1) ^ ((r >> 7) * 0x71)) % 256
            if r & 2:
                lanes[0][0] ^= 1 << ((1 << j) - 1)
    return lanes


def keccak256(data: bytes) -> bytes:
    rate = 136
    state = bytearray(200)
    msg = bytearray(data) + b"\x01"
    msg += b"\x00" * (-len(msg) % rate)
    msg[-1] |= 0x80
    for off in range(0, len(msg), rate):
        for i in range(rate):
            state[i] ^= msg[off + i]
        lanes = [[int.from_bytes(state[8 * (x + 5 * y) : 8 * (x + 5 * y) + 8], "little") for y in range(5)] for x in range(5)]
        lanes = _keccak_f(lanes)
        for x in range(5):
            for y in range(5):
                state[8 * (x + 5 * y) : 8 * (x + 5 * y) + 8] = lanes[x][y].to_bytes(8, "little")
    return bytes(state[:32])
