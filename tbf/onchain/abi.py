"""最小限の ABI エンコード / デコード (静的型と string の戻り値だけ)"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Sequence

from tbf.onchain.keccak import keccak256


@lru_cache(maxsize=None)
def selector(signature: str) -> bytes:
    """例: selector("transfer(address,uint256)") -> a9059cbb"""
    return keccak256(signature.encode())[:4]


def _word(typ: str, value: Any) -> bytes:
    if typ == "address":
        v = int(value, 16) if isinstance(value, str) else int(value)
        if v >> 160:
            raise ValueError(f"invalid address {value}")
        return v.to_bytes(32, "big")
    if typ.startswith("uint"):
        v = int(value)
        if v < 0 or v >> int(typ[4:] or 256):
            raise ValueError(f"{value} does not fit {typ}")
        return v.to_bytes(32, "big")
    if typ == "bool":
        return int(bool(value)).to_bytes(32, "big")
    raise ValueError(f"unsupported type {typ}")


def encode_call(signature: str, types: Sequence[str], values: Sequence[Any]) -> str:
    """静的型のみ (静的タプルはインライン展開されるので平らに並べて渡す)"""
    if len(types) != len(values):
        raise ValueError("types / values length mismatch")
    return "0x" + (selector(signature) + b"".join(_word(t, v) for t, v in zip(types, values))).hex()


def decode_words(data: str) -> list[int]:
    raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    return [int.from_bytes(raw[i : i + 32], "big") for i in range(0, len(raw) - len(raw) % 32, 32)]


def decode_string(data: str) -> str:
    raw = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    if len(raw) == 32:  # 古いトークンは bytes32 で返す
        return raw.rstrip(b"\x00").decode(errors="replace")
    off = int.from_bytes(raw[:32], "big")
    n = int.from_bytes(raw[off : off + 32], "big")
    return raw[off + 32 : off + 32 + n].decode(errors="replace")
