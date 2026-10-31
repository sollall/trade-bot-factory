-- 複数銘柄・複数取引所に対応する
-- 口座 (取引所ごとの現金と銘柄ごとの持ち高) は JSON で 1 行にまとめて持つ
ALTER TABLE bot_state ADD COLUMN portfolio TEXT;
-- 最後に戦略が返した目標比率 (レバレッジのずれを戻すときに使う)
ALTER TABLE bot_state ADD COLUMN targets TEXT;
-- 銘柄ごとの最新価格 (監視用)
ALTER TABLE bot_state ADD COLUMN last_prices TEXT;
-- ここまで反映したファンディングの時刻
ALTER TABLE bot_state ADD COLUMN last_funding INTEGER NOT NULL DEFAULT 0;
-- 口座を更新するたびに増やす版番号 (同時実行での上書きを防ぐ)
ALTER TABLE bot_state ADD COLUMN account_version INTEGER NOT NULL DEFAULT 0;

ALTER TABLE fills ADD COLUMN instrument TEXT;
ALTER TABLE fills ADD COLUMN reduce INTEGER NOT NULL DEFAULT 0;
