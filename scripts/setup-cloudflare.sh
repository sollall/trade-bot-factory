#!/usr/bin/env bash
# 初回のみ: D1 データベースを作成し、wrangler.toml の database_id を書き換え、マイグレーションを適用する。
# 事前に `npx wrangler login` しておくこと。
set -euo pipefail
cd "$(dirname "$0")/.."

for env in paper live; do
  name="trade-bot-$env"
  id=$(npx wrangler d1 list --json | node -e "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>{const db=JSON.parse(s).find(d=>d.name==='$name');process.stdout.write(db?db.uuid:'')})")
  if [ -z "$id" ]; then
    npx wrangler d1 create "$name" >/dev/null
    id=$(npx wrangler d1 list --json | node -e "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>process.stdout.write(JSON.parse(s).find(d=>d.name==='$name').uuid))")
  fi
  echo "$name -> $id"
  # [[env.<env>.d1_databases]] ブロックの database_id を置換
  node -e "
    const fs=require('fs');const f='wrangler.toml';let t=fs.readFileSync(f,'utf8');
    t=t.replace(/(database_name = \"$name\"\ndatabase_id = \")[^\"]*/, '\$1$id');
    fs.writeFileSync(f,t);"
  npx wrangler d1 migrations apply "$name" --env "$env" --remote
done

cat <<MSG

次に secrets を設定してください:
  npx wrangler secret put ADMIN_TOKEN --env paper
  npx wrangler secret put WEBHOOK_URL --env paper      # 任意 (Discord/Slack)
  npx wrangler secret put ADMIN_TOKEN --env live
  npx wrangler secret put WEBHOOK_URL --env live
  npx wrangler secret put BINANCE_API_KEY --env live
  npx wrangler secret put BINANCE_API_SECRET --env live
wrangler.toml の変更をコミットすれば、以降は main への push で自動デプロイされます。
MSG
