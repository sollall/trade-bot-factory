"""HTTP の抽象。ローカルは urllib、Cloudflare では JS の fetch を使う"""

from __future__ import annotations

import asyncio
from typing import Optional, Protocol


class Http(Protocol):
    async def request(
        self, method: str, url: str, headers: Optional[dict[str, str]] = None, body: Optional[str] = None
    ) -> tuple[int, str]: ...


class UrllibHttp:
    """ローカル用 (HTTPS_PROXY 等の環境変数も尊重される)"""

    def __init__(self, timeout: float = 30) -> None:
        self.timeout = timeout

    def _do(self, method: str, url: str, headers: Optional[dict[str, str]], body: Optional[str]) -> tuple[int, str]:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            url,
            method=method,
            headers={"user-agent": "trade-bot-factory", **(headers or {})},
            data=body.encode() if body is not None else None,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return res.status, res.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    async def request(self, method, url, headers=None, body=None):
        return await asyncio.to_thread(self._do, method, url, headers, body)
