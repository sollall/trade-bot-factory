import type { Broker, BrokerFill, OrderRequest } from "./types";

interface SymbolInfo {
  baseAsset: string;
  quoteAsset: string;
  stepSize: number;
  minQty: number;
  minNotional: number;
}

interface OrderResponse {
  orderId: number;
  status: string;
  executedQty: string;
  cummulativeQuoteQty: string;
  transactTime: number;
  fills?: { price: string; qty: string; commission: string; commissionAsset: string }[];
}

const symbolCache = new Map<string, SymbolInfo>();

/** Binance 現物の成行注文 */
export class BinanceBroker implements Broker {
  constructor(
    private apiKey: string,
    private apiSecret: string,
    private baseUrl = "https://api.binance.com",
  ) {}

  async execute(req: OrderRequest): Promise<BrokerFill | null> {
    const info = await this.symbolInfo(req.symbol);
    const qty = floorToStep(req.qty, info.stepSize);
    if (qty < info.minQty || qty * req.refPrice < info.minNotional) return null;

    const res = await this.signed<OrderResponse>("POST", "/api/v3/order", {
      symbol: req.symbol,
      side: req.side === "buy" ? "BUY" : "SELL",
      type: "MARKET",
      quantity: trimNumber(qty, info.stepSize),
      newClientOrderId: req.clientOrderId,
      newOrderRespType: "FULL",
    });

    const executed = Number(res.executedQty);
    if (!(executed > 0)) return null;
    const quote = Number(res.cummulativeQuoteQty);
    const avg = quote / executed;

    let baseFee = 0;
    let quoteFee = 0;
    let otherFee = false;
    for (const f of res.fills ?? []) {
      const c = Number(f.commission);
      if (f.commissionAsset === info.baseAsset) baseFee += c;
      else if (f.commissionAsset === info.quoteAsset) quoteFee += c;
      else if (c > 0) otherFee = true;
    }
    // base 建て手数料は受取数量から引かれるので、その分を数量から除いて quote 換算の手数料にする
    const netQty = executed - baseFee;
    let fee = quoteFee + baseFee * avg;
    // BNB 等で払った場合は正確な換算ができないので料率で見積もる
    if (otherFee) fee += quote * req.takerRate;

    return { time: res.transactTime, side: req.side, qty: netQty, price: avg, fee, orderId: String(res.orderId) };
  }

  private async symbolInfo(symbol: string): Promise<SymbolInfo> {
    const cached = symbolCache.get(symbol);
    if (cached) return cached;
    const res = await fetch(`${this.baseUrl}/api/v3/exchangeInfo?symbol=${symbol}`);
    if (!res.ok) throw new Error(`exchangeInfo ${symbol}: HTTP ${res.status}`);
    const body = (await res.json()) as {
      symbols: { baseAsset: string; quoteAsset: string; filters: Record<string, string>[] }[];
    };
    const s = body.symbols[0];
    if (!s) throw new Error(`unknown symbol ${symbol}`);
    const filter = (t: string) => s.filters.find((f) => f.filterType === t);
    const lot = filter("LOT_SIZE");
    const notional = filter("NOTIONAL") ?? filter("MIN_NOTIONAL");
    const info: SymbolInfo = {
      baseAsset: s.baseAsset,
      quoteAsset: s.quoteAsset,
      stepSize: Number(lot?.stepSize ?? 0),
      minQty: Number(lot?.minQty ?? 0),
      minNotional: Number(notional?.minNotional ?? 0),
    };
    symbolCache.set(symbol, info);
    return info;
  }

  private async signed<T>(method: string, path: string, params: Record<string, string>): Promise<T> {
    const qs = new URLSearchParams({ ...params, recvWindow: "5000", timestamp: String(Date.now()) });
    qs.set("signature", await hmacHex(this.apiSecret, qs.toString()));
    const res = await fetch(`${this.baseUrl}${path}?${qs}`, { method, headers: { "X-MBX-APIKEY": this.apiKey } });
    const text = await res.text();
    if (!res.ok) throw new Error(`binance ${method} ${path}: HTTP ${res.status} ${text}`);
    return JSON.parse(text) as T;
  }
}

export function floorToStep(qty: number, step: number): number {
  if (!(step > 0)) return qty;
  // 浮動小数の誤差で 1 step 落ちないよう少しだけ余裕を持たせる
  return Math.floor(qty / step + 1e-9) * step;
}

function trimNumber(qty: number, step: number): string {
  const decimals = step > 0 ? Math.max(0, Math.round(-Math.log10(step))) : 8;
  return qty.toFixed(decimals);
}

async function hmacHex(secret: string, data: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(data));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
