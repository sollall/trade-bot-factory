from __future__ import annotations

import json
from typing import Optional

from tbf.http import Http


class Notifier:
    """Discord / Slack 互換の Incoming Webhook。失敗しても本処理は止めない"""

    def __init__(self, http: Http, webhook_url: Optional[str], mode: str) -> None:
        self.http = http
        self.url = webhook_url
        self.mode = mode

    async def send(self, message: str) -> None:
        if not self.url:
            return
        text = f"[{self.mode}] {message}"
        try:
            await self.http.request(
                "POST", self.url, {"content-type": "application/json"}, json.dumps({"content": text, "text": text})
            )
        except Exception as e:  # noqa: BLE001
            print("notify failed", e)
