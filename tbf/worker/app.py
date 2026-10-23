"""Cloudflare に依存しない Worker 本体。entry.py から DB と HTTP を注入して使う"""

from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from tbf.core.bot import Bot
from tbf.core.metrics import compute_metrics
from tbf.http import Http
from tbf.market.binance import DEFAULT_MARKET_DATA_URL, BinanceMarket
from tbf.worker.brokers import BinanceBroker, Broker, PaperBroker
from tbf.worker.dashboard import BotSummary, render_dashboard
from tbf.worker.db import DB
from tbf.worker.notify import Notifier
from tbf.worker.runner import Runtime, bots_for_mode, daily_report, halt, tick
from tbf.worker.store import Store

DAILY_CRON = "5 0 * * *"


@dataclass
class Settings:
    mode: str
    market_data_url: str = DEFAULT_MARKET_DATA_URL
    binance_api_url: str = "https://api.binance.com"
    binance_api_key: Optional[str] = None
    binance_api_secret: Optional[str] = None
    webhook_url: Optional[str] = None
    notify_fills: bool = True
    admin_token: Optional[str] = None

    @classmethod
    def from_env(cls, get: Callable[[str], Optional[str]]) -> "Settings":
        mode = get("MODE") or ""
        if mode not in ("paper", "live"):
            raise ValueError(f'MODE must be "paper" or "live" (got "{mode}")')
        return cls(
            mode=mode,
            market_data_url=get("MARKET_DATA_URL") or DEFAULT_MARKET_DATA_URL,
            binance_api_url=get("BINANCE_API_URL") or "https://api.binance.com",
            binance_api_key=get("BINANCE_API_KEY"),
            binance_api_secret=get("BINANCE_API_SECRET"),
            webhook_url=get("WEBHOOK_URL"),
            notify_fills=(get("NOTIFY_FILLS") or "true") == "true",
            admin_token=get("ADMIN_TOKEN"),
        )


@dataclass
class HttpResponse:
    status: int
    body: str
    content_type: str = "application/json"


def _json(data: Any, status: int = 200) -> HttpResponse:
    return HttpResponse(status, json.dumps(data, indent=2, ensure_ascii=False, default=str))


class App:
    def __init__(self, bots: Sequence[Bot], settings: Settings, db: DB, http: Http) -> None:
        self.settings = settings
        self.bots = bots_for_mode(bots, settings.mode)
        self.store = Store(db)
        self.http = http

    def _broker(self) -> Broker:
        s = self.settings
        if s.mode == "paper":
            return PaperBroker()
        if not s.binance_api_key or not s.binance_api_secret:
            raise RuntimeError("live mode requires BINANCE_API_KEY / BINANCE_API_SECRET secrets")
        return BinanceBroker(self.http, s.binance_api_key, s.binance_api_secret, s.binance_api_url)

    def runtime(self, now: int) -> Runtime:
        s = self.settings
        return Runtime(
            mode=s.mode,
            store=self.store,
            market=BinanceMarket(self.http, s.market_data_url),
            broker=self._broker(),
            notifier=Notifier(self.http, s.webhook_url, s.mode),
            now=now,
            notify_fills=s.notify_fills,
        )

    # ---- cron ----
    async def scheduled(self, cron: str, now: int) -> None:
        if not self.bots:
            return
        rt = self.runtime(now)
        if cron == DAILY_CRON:
            await daily_report(self.bots, rt)
        else:
            await tick(self.bots, rt)

    # ---- HTTP ----
    def _authorized(self, headers: dict[str, str], query: dict[str, list[str]]) -> bool:
        token = self.settings.admin_token
        if not token:
            return True
        auth = headers.get("authorization", "")
        given = auth[7:] if auth.startswith("Bearer ") else (query.get("token") or [""])[0]
        return hmac.compare_digest(given, token)

    async def _summary(self, bot: Bot, now: int) -> BotSummary:
        row = await self.store.get(bot.id)
        curve = await self.store.equity(bot.id)
        fills = await self.store.fills(bot.id)
        return BotSummary(
            bot=bot,
            row=row,
            equity=row.equity() if row else bot.capital,
            metrics=compute_metrics(curve, fills),
            curve=[p for p in curve if p.time >= now - 30 * 86_400_000],
        )

    async def _price(self, bot: Bot) -> float:
        prices = await BinanceMarket(self.http, self.settings.market_data_url).prices([bot.symbol])
        return prices.get(bot.symbol, 0.0)

    async def handle(self, method: str, url: str, headers: dict[str, str], now: Optional[int] = None) -> HttpResponse:
        now = now if now is not None else int(time.time() * 1000)
        u = urlparse(url)
        query = parse_qs(u.query)
        if not self._authorized({k.lower(): v for k, v in headers.items()}, query):
            return HttpResponse(401, "unauthorized", "text/plain")
        parts = [p for p in u.path.split("/") if p]

        if method == "GET" and not parts:
            items = [await self._summary(b, now) for b in self.bots]
            return HttpResponse(200, render_dashboard(self.settings.mode, items), "text/html; charset=utf-8")
        if not parts or parts[0] != "api":
            return HttpResponse(404, "not found", "text/plain")

        if method == "GET" and parts[1:] == ["bots"]:
            out = []
            for b in self.bots:
                s = await self._summary(b, now)
                status = s.row.status if s.row else "pending"
                out.append({"id": b.id, "symbol": b.symbol, "status": status, "equity": s.equity, "metrics": s.metrics.to_dict()})
            return _json(out)
        if method == "POST" and parts[1:] == ["tick"]:
            await tick(self.bots, self.runtime(now))
            return _json({"ok": True})
        if method == "GET" and parts[1:] == ["events"]:
            return _json(await self.store.events(None, 100))

        if len(parts) >= 3 and parts[1] == "bots":
            bot = next((b for b in self.bots if b.id == parts[2]), None)
            if not bot:
                return _json({"error": "bot not found"}, 404)
            action = parts[3] if len(parts) > 3 else None

            if method == "GET" and action is None:
                s = await self._summary(bot, now)
                return _json(
                    {
                        "id": bot.id,
                        "description": bot.description,
                        "symbol": bot.symbol,
                        "timeframe": bot.timeframe,
                        "params": bot.params,
                        "risk": bot.risk.__dict__,
                        "state": s.row.to_dict() if s.row else None,
                        "equity": s.equity,
                        "metrics": s.metrics.to_dict(),
                        "recent_fills": list(reversed(await self.store.fill_rows(bot.id, 50))),
                        "events": await self.store.events(bot.id, 50),
                    }
                )
            if method == "POST" and action == "halt":
                await self.store.get_or_init(bot.id, bot.capital, now)
                flatten = (query.get("flatten") or [""])[0] == "true"
                await halt(bot, "manual", flatten, await self._price(bot), self.runtime(now))
                return _json({"ok": True, "flatten": flatten})
            if method == "POST" and action == "resume":
                row = await self.store.get(bot.id)
                if not row:
                    return _json({"error": "bot has not run yet"}, 400)
                price = await self._price(bot) or row.last_price or 0.0
                await self.store.resume(bot.id, row.equity(price), now)
                await self.store.add_event(bot.id, "info", "resumed", now)
                return _json({"ok": True})
        return _json({"error": "not found"}, 404)
