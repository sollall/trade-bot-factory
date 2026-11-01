"""Cloudflare に依存しない Worker 本体。entry.py から DB と HTTP を注入して使う"""

from __future__ import annotations

import hmac
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from tbf.core.bot import Bot
from tbf.core.metrics import compute_metrics
from tbf.http import Http
from tbf.market import VenueMarket, default_markets
from tbf.market.binance import DEFAULT_MARKET_DATA_URL
from tbf.market.hyperliquid import INFO_URL
from tbf.worker.brokers import BinanceBroker, Broker, PaperBroker, RoutingBroker, UnsupportedBroker
from tbf.worker.dashboard import BotSummary, render_dashboard
from tbf.worker.db import DB
from tbf.worker.notify import Notifier
from tbf.worker.runner import Runtime, bots_for_mode, daily_report, fetch_prices, halt, tick
from tbf.worker.store import Store

DAILY_CRON = "5 0 * * *"


@dataclass
class Settings:
    mode: str
    market_data_url: str = DEFAULT_MARKET_DATA_URL
    hyperliquid_url: str = INFO_URL
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
            hyperliquid_url=get("HYPERLIQUID_URL") or INFO_URL,
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
    def __init__(
        self,
        bots: Sequence[Bot],
        settings: Settings,
        db: DB,
        http: Http,
        markets: Optional[dict[str, VenueMarket]] = None,
    ) -> None:
        self.settings = settings
        self.bots = bots_for_mode(bots, settings.mode)
        self.store = Store(db)
        self.http = http
        self.markets = markets or default_markets(http, settings.market_data_url, settings.hyperliquid_url)

    def _broker(self) -> Broker:
        s = self.settings
        if s.mode == "paper":
            return PaperBroker()
        routes: dict[tuple[str, str], Broker] = {}
        if s.binance_api_key and s.binance_api_secret:
            routes[("binance", "spot")] = BinanceBroker(self.http, s.binance_api_key, s.binance_api_secret, s.binance_api_url)
        else:
            routes[("binance", "spot")] = UnsupportedBroker("BINANCE_API_KEY / BINANCE_API_SECRET が未設定です")
        return RoutingBroker(routes, UnsupportedBroker("この取引所・種類の本番発注は未対応です"))

    def runtime(self, now: int) -> Runtime:
        s = self.settings
        return Runtime(
            mode=s.mode,
            store=self.store,
            markets=self.markets,
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
            equity=row.equity(bot) if row else bot.capital,
            metrics=compute_metrics(curve, fills),
            curve=[p for p in curve if p.time >= now - 30 * 86_400_000],
        )

    async def _prices(self, bot: Bot, now: int) -> dict[str, float]:
        by_venue = await fetch_prices([bot], self.runtime(now))
        return {a: by_venue.get(i.venue, {}).get(i.symbol, 0.0) for a, i in bot.instruments.items()}

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
                out.append(
                    {
                        "id": b.id,
                        "instruments": {a: str(i) for a, i in b.instruments.items()},
                        "status": s.row.status if s.row else "pending",
                        "equity": s.equity,
                        "metrics": s.metrics.to_dict(),
                    }
                )
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
                        "instruments": {a: str(i) for a, i in bot.instruments.items()},
                        "timeframe": bot.timeframe,
                        "leverage": bot.leverage,
                        "params": bot.params,
                        "risk": asdict(bot.risk),
                        "state": s.row.to_dict() if s.row else None,
                        "equity": s.equity,
                        "metrics": s.metrics.to_dict(),
                        "recent_fills": list(reversed(await self.store.fill_rows(bot.id, 50))),
                        "events": await self.store.events(bot.id, 50),
                    }
                )
            if method == "POST" and action == "halt":
                await self.store.get_or_init(bot, now)
                flatten = (query.get("flatten") or [""])[0] == "true"
                await halt(bot, "manual", flatten, await self._prices(bot, now), self.runtime(now))
                return _json({"ok": True, "flatten": flatten})
            if method == "POST" and action == "resume":
                row = await self.store.get(bot.id)
                if not row:
                    return _json({"error": "bot has not run yet"}, 400)
                prices = {a: p for a, p in (await self._prices(bot, now)).items() if p > 0}
                await self.store.resume(bot.id, row.equity(bot, prices), now)
                await self.store.add_event(bot.id, "info", "resumed", now)
                return _json({"ok": True})
        return _json({"error": "not found"}, 404)
