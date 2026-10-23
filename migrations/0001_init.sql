-- bot ごとの仮想口座と実行状態
CREATE TABLE bot_state (
  bot_id TEXT PRIMARY KEY,
  cash REAL NOT NULL,
  qty REAL NOT NULL DEFAULT 0,
  avg_price REAL NOT NULL DEFAULT 0,
  strategy_state TEXT NOT NULL DEFAULT '{}',
  -- 最後に戦略を評価した確定足の開始時刻 (ms)
  last_candle INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running', -- running | halted
  halt_reason TEXT,
  peak_equity REAL NOT NULL,
  day TEXT NOT NULL DEFAULT '',
  day_start_equity REAL NOT NULL,
  consecutive_losses INTEGER NOT NULL DEFAULT 0,
  last_price REAL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT NOT NULL,
  time INTEGER NOT NULL,
  side TEXT NOT NULL,
  qty REAL NOT NULL,
  price REAL NOT NULL,
  fee REAL NOT NULL,
  realized_pnl REAL NOT NULL,
  order_id TEXT,
  reason TEXT
);
CREATE INDEX fills_bot_time ON fills (bot_id, time);

-- 確定足ごとの資産推移
CREATE TABLE equity (
  bot_id TEXT NOT NULL,
  time INTEGER NOT NULL,
  equity REAL NOT NULL,
  PRIMARY KEY (bot_id, time)
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id TEXT,
  time INTEGER NOT NULL,
  level TEXT NOT NULL, -- info | warn | error
  message TEXT NOT NULL
);
CREATE INDEX events_time ON events (time);
