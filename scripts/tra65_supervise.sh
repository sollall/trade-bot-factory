#!/usr/bin/env bash
# TRA-65 のシャドー収集を 7 日回すための見張り。落ちたら 10 秒後に起こし直す。
#   setsid nohup scripts/tra65_supervise.sh >> logs/tra65-supervise.log 2>&1 &
#
# 再起動のたびに記録に穴が空くが、**埋めない**。store の runs テーブルに start/stop が残り、
# レポートは標本の切れ目を「未測定」として列挙し、年率の分母からも外す。
set -u
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
DB="${DB:-data/jp_maker.sqlite}"
CFG="${CFG:-jp_maker.toml}"
# 記録を止める期限 (epoch 秒)。既定は起動から 7 日。
UNTIL="${UNTIL:-$(( $(date +%s) + 7*86400 ))}"

while [ "$(date +%s)" -lt "$UNTIL" ]; do
  echo "$(date -u +%FT%TZ) starting collector (until $(date -u -d "@$UNTIL" +%FT%TZ))"
  PYTHONPATH=. "$PY" -m tbf.jpmaker run --config "$CFG" --db "$DB" >> logs/tra65-jpmaker.log 2>&1
  code=$?
  echo "$(date -u +%FT%TZ) collector exited with $code"
  [ "$code" = "0" ] && break   # SIGTERM/SIGINT での正常停止は見張りも降りる
  sleep 10
done
echo "$(date -u +%FT%TZ) supervisor done"
