-- 約定の質 (Workers で十分かを判断するための計測)。旧版の Worker が書く行では NULL のまま
-- バックテストが仮定する約定の基準価格 (判断に使った確定足の終値 ≒ 次の足の始値)
ALTER TABLE fills ADD COLUMN bench_price REAL;
-- 足の確定から発注までの遅れ (ms)
ALTER TABLE fills ADD COLUMN delay_ms INTEGER;
-- バックテストの約定価格 (基準価格 + 同じスリッページモデル) との差 (bps, + は不利)
ALTER TABLE fills ADD COLUMN gap_bps REAL;
-- cron の予定時刻から実際に動き出すまでの遅れ (ms)
ALTER TABLE heartbeat ADD COLUMN lag_ms INTEGER NOT NULL DEFAULT 0;
