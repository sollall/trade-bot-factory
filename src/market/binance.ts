import { TIMEFRAME_MS, type Candle, type Timeframe } from "../core/types";

/** 公開マーケットデータ用。地域制限を受けにくい data-api を既定にする */
export const DEFAULT_MARKET_DATA_URL = "https://data-api.binance.vision";

type RawKline = [number, string, string, string, string, string, number, ...unknown[]];

export async function fetchKlines(
  symbol: string,
  timeframe: Timeframe,
  opts: { startTime?: number; endTime?: number; limit?: number; baseUrl?: string } = {},
): Promise<Candle[]> {
  const url = new URL("/api/v3/klines", opts.baseUrl ?? DEFAULT_MARKET_DATA_URL);
  url.searchParams.set("symbol", symbol);
  url.searchParams.set("interval", timeframe);
  url.searchParams.set("limit", String(Math.min(opts.limit ?? 1000, 1000)));
  if (opts.startTime != null) url.searchParams.set("startTime", String(opts.startTime));
  if (opts.endTime != null) url.searchParams.set("endTime", String(opts.endTime));
  const res = await fetch(url);
  if (!res.ok) throw new Error(`klines ${symbol} ${timeframe}: HTTP ${res.status} ${await res.text()}`);
  const raw = (await res.json()) as RawKline[];
  return raw.map((k) => ({
    time: k[0],
    open: Number(k[1]),
    high: Number(k[2]),
    low: Number(k[3]),
    close: Number(k[4]),
    volume: Number(k[5]),
  }));
}

/**
 * 直近の確定足 count 本と、現在価格 (形成中の足の close) を返す。
 */
export async function fetchRecent(
  symbol: string,
  timeframe: Timeframe,
  count: number,
  now: number,
  baseUrl?: string,
): Promise<{ closed: Candle[]; price: number }> {
  const all = await fetchKlines(symbol, timeframe, { limit: count + 1, baseUrl });
  const tf = TIMEFRAME_MS[timeframe];
  const closed = all.filter((c) => c.time + tf <= now);
  const last = all[all.length - 1];
  if (!last) throw new Error(`no klines for ${symbol}`);
  return { closed: closed.slice(-count), price: last.close };
}

/** 期間指定でページングしながら取得 (バックテスト用) */
export async function fetchRange(
  symbol: string,
  timeframe: Timeframe,
  from: number,
  to: number,
  baseUrl?: string,
): Promise<Candle[]> {
  const tf = TIMEFRAME_MS[timeframe];
  const out: Candle[] = [];
  let cursor = from;
  while (cursor < to) {
    const batch = await fetchKlines(symbol, timeframe, { startTime: cursor, endTime: to - 1, limit: 1000, baseUrl });
    if (batch.length === 0) break;
    out.push(...batch);
    cursor = batch[batch.length - 1]!.time + tf;
  }
  return out.filter((c) => c.time + tf <= to);
}

/** 複数シンボルの現在価格をまとめて取得 */
export async function fetchPrices(symbols: string[], baseUrl?: string): Promise<Record<string, number>> {
  const uniq = [...new Set(symbols)];
  if (uniq.length === 0) return {};
  const url = new URL("/api/v3/ticker/price", baseUrl ?? DEFAULT_MARKET_DATA_URL);
  url.searchParams.set("symbols", JSON.stringify(uniq));
  const res = await fetch(url);
  if (!res.ok) throw new Error(`ticker: HTTP ${res.status} ${await res.text()}`);
  const body = (await res.json()) as { symbol: string; price: string }[];
  return Object.fromEntries(body.map((t) => [t.symbol, Number(t.price)]));
}
