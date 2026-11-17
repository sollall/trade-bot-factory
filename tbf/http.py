"""HTTP の抽象。ローカルは urllib、Cloudflare では JS の fetch を使う"""

from __future__ import annotations

import asyncio
from typing import Optional, Protocol


class Http(Protocol):
    async def request(
        self, method: str, url: str, headers: Optional[dict[str, str]] = None, body: Optional[str] = None
    ) -> tuple[int, str]: ...


#: どの実装でも必ず付ける User-Agent。Coinbase の /products/<id>/candles は
#: User-Agent が無いと 400 ("User-Agent header is required.") を返す。urllib は既定で付けるので
#: 手元 (バックテスト) では通り、Workers の fetch は付けないので本番だけ落ちる (TRA-81)。
USER_AGENT = "trade-bot-factory"


def with_user_agent(headers: Optional[dict[str, str]]) -> dict[str, str]:
    """呼び出し側が明示した User-Agent はそのまま使う"""
    return {"user-agent": USER_AGENT, **(headers or {})}


# 取り直す価値のある応答。429 は流量制限で、Cloudflare Workers の出口IP は他の利用者と
# 共有なので自分が静かでも踏むことがある (TRA-81 の実測で Hyperliquid が 13 回中 3 回)。
RETRY_STATUSES = (429, 500, 502, 503, 504)
#: 取り直す前に待つ秒数。回数はこの長さ + 1 (= 3 回) で、増える待ち時間は最大 1.25 秒。
#: cron は毎分なので、同じ tick の中で取り直しても次の tick には間に合う。
RETRY_DELAYS = (0.25, 1.0)


async def request_retrying(
    http: Http,
    method: str,
    url: str,
    headers: Optional[dict[str, str]] = None,
    body: Optional[str] = None,
    delays: tuple[float, ...] = RETRY_DELAYS,
) -> tuple[int, str]:
    """429 / 5xx なら少し待って取り直す。最後の応答をそのまま返す (判断は呼び出し側)"""
    for delay in delays:
        status, text = await http.request(method, url, headers, body)
        if status not in RETRY_STATUSES:
            return status, text
        await asyncio.sleep(delay)
    return await http.request(method, url, headers, body)


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
            headers=with_user_agent(headers),
            data=body.encode() if body is not None else None,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return res.status, res.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    async def request(self, method, url, headers=None, body=None):
        return await asyncio.to_thread(self._do, method, url, headers, body)
