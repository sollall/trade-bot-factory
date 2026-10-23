import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fetchRange } from "../market/binance";
import type { Candle, Timeframe } from "../core/types";

/** data/ にキャッシュしつつ過去足を取得する */
export async function loadCandles(
  symbol: string,
  timeframe: Timeframe,
  from: number,
  to: number,
  cacheDir = "data",
): Promise<Candle[]> {
  const file = join(cacheDir, `${symbol}_${timeframe}_${from}_${to}.json`);
  if (existsSync(file)) return JSON.parse(readFileSync(file, "utf8")) as Candle[];
  const candles = await fetchRange(symbol, timeframe, from, to);
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, JSON.stringify(candles));
  return candles;
}

/** time,open,high,low,close,volume のヘッダ付き CSV を読む (time は ms または ISO 文字列) */
export function loadCsv(path: string): Candle[] {
  const [header, ...rows] = readFileSync(path, "utf8").trim().split(/\r?\n/);
  const cols = header!.split(",").map((h) => h.trim().toLowerCase());
  const idx = (k: string) => {
    const i = cols.indexOf(k);
    if (i < 0) throw new Error(`${path}: column "${k}" not found`);
    return i;
  };
  const [t, o, h, l, c, v] = ["time", "open", "high", "low", "close", "volume"].map(idx) as number[];
  return rows.map((row) => {
    const f = row.split(",");
    const rawT = f[t!]!.trim();
    return {
      time: /^\d+$/.test(rawT) ? Number(rawT) : Date.parse(rawT),
      open: Number(f[o!]),
      high: Number(f[h!]),
      low: Number(f[l!]),
      close: Number(f[c!]),
      volume: Number(f[v!]),
    };
  });
}
