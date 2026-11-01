-- Worker が cron を最後に正常完了した記録 (/healthz・/metrics・デプロイ後の確認で使う)
-- migration は旧バージョンの Worker が動いている間に適用されるので、追加 (ADD / CREATE) だけにする
CREATE TABLE heartbeat (
  name TEXT PRIMARY KEY,          -- 'tick'
  time INTEGER NOT NULL,          -- 完了時刻 (ms)
  build TEXT NOT NULL,            -- デプロイしたコミット
  bots INTEGER NOT NULL,          -- 動かした bot 数
  errors INTEGER NOT NULL,        -- その回にエラーになった bot 数
  duration_ms INTEGER NOT NULL
);
