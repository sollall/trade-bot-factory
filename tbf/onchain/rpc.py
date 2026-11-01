"""JSON-RPC クライアント (バッチ対応・遅延計測つき)"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from tbf.http import Http


class RpcError(RuntimeError):
    pass


@dataclass
class Call:
    method: str
    params: list[Any]


class Rpc:
    def __init__(self, http: Http, url: str) -> None:
        self.http = http
        self.url = url
        # 直近のリクエスト往復時間 (ms)
        self.last_latency_ms: float = 0.0

    async def batch(self, calls: list[Call]) -> list[Any]:
        """1 回の HTTP で複数呼ぶ。個別のエラーは RpcError を値として返す"""
        payload = [{"jsonrpc": "2.0", "id": i, "method": c.method, "params": c.params} for i, c in enumerate(calls)]
        t0 = time.perf_counter()
        status, text = await self.http.request("POST", self.url, {"content-type": "application/json"}, json.dumps(payload))
        self.last_latency_ms = (time.perf_counter() - t0) * 1000
        if status != 200:
            raise RpcError(f"{self.url}: HTTP {status} {text[:200]}")
        body = json.loads(text)
        if isinstance(body, dict):  # バッチ非対応のノードはエラーを 1 件だけ返す
            raise RpcError(f"{self.url}: {body.get('error')}")
        out: list[Any] = [None] * len(calls)
        for r in body:
            i = r.get("id")
            if not isinstance(i, int) or not 0 <= i < len(calls):
                continue
            out[i] = RpcError(str(r["error"])) if "error" in r else r.get("result")
        return out

    async def call(self, method: str, params: Optional[list[Any]] = None) -> Any:
        (res,) = await self.batch([Call(method, params or [])])
        if isinstance(res, RpcError):
            raise res
        return res


def eth_call(to: str, data: str) -> Call:
    return Call("eth_call", [{"to": to, "data": data}, "latest"])
