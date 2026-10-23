import type { EquityPoint, Fill } from "../core/types";

export interface BotRow {
  bot_id: string;
  cash: number;
  qty: number;
  avg_price: number;
  strategy_state: string;
  last_candle: number;
  status: "running" | "halted";
  halt_reason: string | null;
  peak_equity: number;
  day: string;
  day_start_equity: number;
  consecutive_losses: number;
  last_price: number | null;
  updated_at: number;
}

export class Store {
  constructor(private db: D1Database) {}

  async getOrInit(botId: string, capital: number, now: number): Promise<BotRow> {
    await this.db
      .prepare(
        `INSERT OR IGNORE INTO bot_state (bot_id, cash, peak_equity, day_start_equity, updated_at) VALUES (?, ?, ?, ?, ?)`,
      )
      .bind(botId, capital, capital, capital, now)
      .run();
    return (await this.get(botId))!;
  }

  get(botId: string): Promise<BotRow | null> {
    return this.db.prepare(`SELECT * FROM bot_state WHERE bot_id = ?`).bind(botId).first<BotRow>();
  }

  async all(): Promise<BotRow[]> {
    return (await this.db.prepare(`SELECT * FROM bot_state`).all<BotRow>()).results;
  }

  /**
   * 足 candle の処理権を取得する。重複起動した cron が同じ足で二重発注しないためのガード。
   */
  async claimCandle(botId: string, candle: number): Promise<boolean> {
    const r = await this.db
      .prepare(`UPDATE bot_state SET last_candle = ? WHERE bot_id = ? AND last_candle < ?`)
      .bind(candle, botId, candle)
      .run();
    return r.meta.changes > 0;
  }

  /** 監視用フィールドだけ更新する (口座残高は触らないので並行実行でも安全) */
  async saveMonitor(row: BotRow): Promise<void> {
    await this.db
      .prepare(
        `UPDATE bot_state SET peak_equity=?, day=?, day_start_equity=?, last_price=?, updated_at=? WHERE bot_id=?`,
      )
      .bind(row.peak_equity, row.day, row.day_start_equity, row.last_price, row.updated_at, row.bot_id)
      .run();
  }

  /** 口座と戦略状態を更新する。claimCandle で処理権を取った実行だけが呼ぶ */
  async saveAccount(row: BotRow): Promise<void> {
    await this.db
      .prepare(
        `UPDATE bot_state SET cash=?, qty=?, avg_price=?, strategy_state=?, consecutive_losses=?, updated_at=? WHERE bot_id=?`,
      )
      .bind(row.cash, row.qty, row.avg_price, row.strategy_state, row.consecutive_losses, row.updated_at, row.bot_id)
      .run();
  }

  async setStatus(botId: string, status: "running" | "halted", reason: string | null): Promise<void> {
    await this.db.prepare(`UPDATE bot_state SET status=?, halt_reason=? WHERE bot_id=?`).bind(status, reason, botId).run();
  }

  /** 再開。ピークと連敗数をリセットしないと即座にまた停止するため現在値から数え直す */
  async resume(botId: string, equity: number, now: number): Promise<void> {
    await this.db
      .prepare(
        `UPDATE bot_state SET status='running', halt_reason=NULL, peak_equity=?, day_start_equity=?, consecutive_losses=0, updated_at=? WHERE bot_id=?`,
      )
      .bind(equity, equity, now, botId)
      .run();
  }

  async addFill(botId: string, f: Fill, orderId: string | null, reason: string | null): Promise<void> {
    await this.db
      .prepare(
        `INSERT INTO fills (bot_id, time, side, qty, price, fee, realized_pnl, order_id, reason) VALUES (?,?,?,?,?,?,?,?,?)`,
      )
      .bind(botId, f.time, f.side, f.qty, f.price, f.fee, f.realizedPnl, orderId, reason)
      .run();
  }

  async addEquity(botId: string, p: EquityPoint): Promise<void> {
    await this.db
      .prepare(`INSERT OR REPLACE INTO equity (bot_id, time, equity) VALUES (?,?,?)`)
      .bind(botId, p.time, p.equity)
      .run();
  }

  async addEvent(botId: string | null, level: "info" | "warn" | "error", message: string, now: number): Promise<void> {
    await this.db
      .prepare(`INSERT INTO events (bot_id, time, level, message) VALUES (?,?,?,?)`)
      .bind(botId, now, level, message)
      .run();
  }

  async fills(botId: string, limit = 1000): Promise<(Fill & { reason: string | null })[]> {
    const r = await this.db
      .prepare(
        `SELECT time, side, qty, price, fee, realized_pnl AS realizedPnl, reason FROM
         (SELECT * FROM fills WHERE bot_id=? ORDER BY time DESC, id DESC LIMIT ?) ORDER BY time, id`,
      )
      .bind(botId, limit)
      .all<Fill & { reason: string | null }>();
    return r.results;
  }

  async equity(botId: string, since = 0): Promise<EquityPoint[]> {
    const r = await this.db
      .prepare(`SELECT time, equity FROM equity WHERE bot_id=? AND time >= ? ORDER BY time`)
      .bind(botId, since)
      .all<EquityPoint>();
    return r.results;
  }

  async events(botId: string | null, limit = 50) {
    const q = botId
      ? this.db.prepare(`SELECT * FROM events WHERE bot_id=? ORDER BY id DESC LIMIT ?`).bind(botId, limit)
      : this.db.prepare(`SELECT * FROM events ORDER BY id DESC LIMIT ?`).bind(limit);
    return (await q.all()).results;
  }
}
