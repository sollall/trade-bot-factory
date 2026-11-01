from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from dataclasses import dataclass
from typing import Optional, Protocol
from urllib.parse import urlencode

from tbf.core.instrument import Instrument
from tbf.core.portfolio import simulate_fill
from tbf.core.types import Fill, Side
from tbf.http import Http


@dataclass(frozen=True)
class OrderRequest:
    bot_id: str
    alias: str
    instrument: Instrument
    side: Side
    # 数量 (base 通貨)
    qty: float
    # 判断時の参照価格
    ref_price: float
    # 冪等キー (同じキーで二重発注しない)
    client_order_id: str
    now: int


@dataclass(frozen=True)
class BrokerFill:
    fill: Fill
    order_id: Optional[str]


class Broker(Protocol):
    async def execute(self, req: OrderRequest) -> Optional[BrokerFill]:
        """約定しなかった (最小数量未満など) 場合は None"""
        ...


class PaperBroker:
    """現在価格 + スリッページ + 手数料で即時約定 (バックテストと同じモデル)"""

    async def execute(self, req: OrderRequest) -> BrokerFill:
        f = simulate_fill(req.alias, req.side, req.qty, req.ref_price, req.now, req.instrument.fees)  # type: ignore[arg-type]
        return BrokerFill(f, None)


class UnsupportedBroker:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def execute(self, req: OrderRequest) -> Optional[BrokerFill]:
        raise RuntimeError(f"{req.instrument}: {self.reason}")


class RoutingBroker:
    """銘柄の (取引所, 種類) ごとに発注先を振り分ける"""

    def __init__(self, routes: dict[tuple[str, str], Broker], fallback: Broker) -> None:
        self.routes = routes
        self.fallback = fallback

    async def execute(self, req: OrderRequest) -> Optional[BrokerFill]:
        broker = self.routes.get((req.instrument.venue, req.instrument.kind), self.fallback)
        return await broker.execute(req)


# 本番 (live) で発注できる (取引所, 種類)。ここにないものを live にしようとするとビルドで止める
LIVE_SUPPORTED: set[tuple[str, str]] = {("binance", "spot")}


@dataclass(frozen=True)
class SymbolInfo:
    base_asset: str
    quote_asset: str
    step_size: float
    min_qty: float
    min_notional: float


_symbol_cache: dict[str, SymbolInfo] = {}


def floor_to_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    # 浮動小数の誤差で 1 step 落ちないよう少しだけ余裕を持たせる
    return math.floor(qty / step + 1e-9) * step


def format_qty(qty: float, step: float) -> str:
    decimals = max(0, round(-math.log10(step))) if step > 0 else 8
    return f"{qty:.{decimals}f}"


class BinanceBroker:
    """Binance 現物の成行注文"""

    def __init__(self, http: Http, api_key: str, api_secret: str, base_url: str = "https://api.binance.com") -> None:
        self.http = http
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url

    async def execute(self, req: OrderRequest) -> Optional[BrokerFill]:
        inst = req.instrument
        if (inst.venue, inst.kind) != ("binance", "spot"):
            raise RuntimeError(f"BinanceBroker cannot trade {inst}")
        info = await self.symbol_info(inst.symbol)
        qty = floor_to_step(req.qty, info.step_size)
        if qty < info.min_qty or qty * req.ref_price < info.min_notional:
            return None
        res = await self._signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": inst.symbol,
                "side": req.side.upper(),
                "type": "MARKET",
                "quantity": format_qty(qty, info.step_size),
                "newClientOrderId": req.client_order_id,
                "newOrderRespType": "FULL",
            },
        )
        return parse_order(res, req, info)

    async def symbol_info(self, symbol: str) -> SymbolInfo:
        if symbol in _symbol_cache:
            return _symbol_cache[symbol]
        status, text = await self.http.request("GET", f"{self.base_url}/api/v3/exchangeInfo?symbol={symbol}")
        if status != 200:
            raise RuntimeError(f"exchangeInfo {symbol}: HTTP {status} {text[:200]}")
        s = json.loads(text)["symbols"][0]
        filters = {f["filterType"]: f for f in s["filters"]}
        lot = filters.get("LOT_SIZE", {})
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        info = SymbolInfo(
            s["baseAsset"],
            s["quoteAsset"],
            float(lot.get("stepSize", 0)),
            float(lot.get("minQty", 0)),
            float(notional.get("minNotional", 0)),
        )
        _symbol_cache[symbol] = info
        return info

    async def _signed(self, method: str, path: str, params: dict[str, str]) -> dict:
        q = urlencode({**params, "recvWindow": "5000", "timestamp": str(int(time.time() * 1000))})
        sig = hmac.new(self.api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        status, text = await self.http.request(
            method, f"{self.base_url}{path}?{q}&signature={sig}", {"X-MBX-APIKEY": self.api_key}
        )
        if status != 200:
            raise RuntimeError(f"binance {method} {path}: HTTP {status} {text[:300]}")
        return json.loads(text)


def parse_order(res: dict, req: OrderRequest, info: SymbolInfo) -> Optional[BrokerFill]:
    executed = float(res["executedQty"])
    if executed <= 0:
        return None
    quote = float(res["cummulativeQuoteQty"])
    avg = quote / executed
    base_fee = quote_fee = 0.0
    other_fee = False
    for f in res.get("fills", []):
        c = float(f["commission"])
        if f["commissionAsset"] == info.base_asset:
            base_fee += c
        elif f["commissionAsset"] == info.quote_asset:
            quote_fee += c
        elif c > 0:
            other_fee = True
    # base 建て手数料は受取数量から引かれるので数量から除き、quote 換算の手数料として計上
    net_qty = executed - base_fee
    fee = quote_fee + base_fee * avg
    # BNB 等で払った場合は正確に換算できないので料率で見積もる
    if other_fee:
        fee += quote * req.instrument.fees.taker_rate  # type: ignore[union-attr]
    fill = Fill(int(res["transactTime"]), req.side, net_qty, avg, fee, instrument=req.alias)
    return BrokerFill(fill, str(res["orderId"]))
