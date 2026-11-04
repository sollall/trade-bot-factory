#!/usr/bin/env bash
# A① (TRA-22) の 30 日シャドーモード計測が落ちていたら起こし直す見張り。
#
# VPS に置くなら systemd の Restart=always (deploy/funding-shadow.service と同じ形) を使う。
# これは systemd を使わない作業機で 30 日回すためのもので、止まった時間ぶんサンプルが
# 欠けるのを避けたいだけ。再開しても建玉は作り直さない (position テーブルに残っている)。
#
#   setsid nohup scripts/carry-a1-watchdog.sh >> logs/carry-a1-watchdog.log 2>&1 &
#
# 生存判定は pidfile + /proc/<pid>/cmdline で行う。pgrep -f でコマンド行を引くと、
# シェルの履歴や別のターミナルに流れた同じ文字列を自分の計測プロセスと誤認する。
set -uo pipefail

cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
DB=${DB:-data/carry_a1.sqlite}
CONFIG=${CONFIG:-carry.toml}
LOG=${LOG:-logs/carry-a1.log}
PIDFILE=${PIDFILE:-data/carry-a1.pid}
INTERVAL=${INTERVAL:-60}

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) watchdog: $*"; }

alive() {
  local pid
  pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
  [ -n "${pid:-}" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # PID が再利用されて別のプロセスになっていないか確かめる
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'tbf.carry' || return 1
}

log "started (interval ${INTERVAL}s, db ${DB}, pidfile ${PIDFILE})"
while true; do
  if ! alive; then
    log "計測プロセスが居ないので起動します"
    setsid nohup "$PY" -m tbf.carry run --config "$CONFIG" --db "$DB" >> "$LOG" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 10
    if alive; then
      log "起動しました (pid $(cat "$PIDFILE"))"
    else
      log "起動に失敗しました (直近のログ: $(tail -n 1 "$LOG" 2>/dev/null))"
    fi
  fi
  sleep "$INTERVAL"
done
