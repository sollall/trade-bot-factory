export interface Env {
  DB: D1Database;
  /** "paper" | "live" (wrangler.toml の env ごとに設定) */
  MODE: string;
  /** 公開マーケットデータの取得先 */
  MARKET_DATA_URL?: string;
  /** 取引 API (live のみ) */
  BINANCE_API_URL?: string;
  BINANCE_API_KEY?: string;
  BINANCE_API_SECRET?: string;
  /** Discord / Slack の Incoming Webhook URL */
  WEBHOOK_URL?: string;
  /** "true" なら約定ごとに通知 */
  NOTIFY_FILLS?: string;
  /** API / ダッシュボードの認証トークン。未設定なら認証なし */
  ADMIN_TOKEN?: string;
}
