#!/usr/bin/env bash
# VPS が自分で GitHub を見に行き、更新があれば取り込む (mm-update.timer から 1 分ごとに、root で動く)。
# deploy/setup-mm-record.sh が /usr/local/sbin/mm-update にコピーして使う。
#
# 決めごと:
#   - 追いかけるブランチは /etc/mm-update.env の BRANCH (無ければ何もしない)。
#   - 取り込むのはコードだけ。このスクリプト自身と systemd の unit は取り込まない
#     (root で動くものを、ブランチに push できる人が書き換えられないように)。
#     それらが変わったときはログに出すので、人間が deploy/setup-mm-record.sh を流し直す。
#   - 記録に関係するファイルが変わったときだけ、記録を起動し直す (数秒の切れ目ができる)。
#     再生・集計だけの変更では起動し直さない。
#   - 取り込んだコードが読み込めない、または起動し直した記録が動かないときは、前の版に戻す。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/trade-bot-factory}"
CONF="${MM_UPDATE_CONF:-/etc/mm-update.env}"
ENV_DIR="${MM_ENV_DIR:-/etc/mm}"
RUN_USER="${MM_RUN_USER:-mm}"
SETTLE_SEC="${MM_SETTLE_SEC:-20}"
# 変わっても記録を起動し直さなくてよいファイル (再生・集計・クォーター)
ANALYSIS_ONLY=(tbf/mm/replay.py tbf/mm/report.py tbf/mm/analyze.py tbf/mm/quoter.py tbf/mm/paper.py)
# 人間が setup を流し直す必要があるファイル
MANUAL=(deploy/mm-update.sh deploy/mm-update.service deploy/mm-update.timer "deploy/mm-record@.service" deploy/setup-mm-record.sh)

log() { echo "mm-update: $*"; }

[ -f "$CONF" ] || { log "$CONF が無いので何もしません"; exit 0; }
# shellcheck disable=SC1090
. "$CONF"
[ -n "${BRANCH:-}" ] || { log "BRANCH が未設定なので何もしません"; exit 0; }

venues=()
for f in "$ENV_DIR"/*.env; do
  [ -f "$f" ] && venues+=("$(basename "$f" .env)")
done

old="$(git -C "$APP_DIR" rev-parse HEAD)"
git -C "$APP_DIR" fetch -q origin "$BRANCH"
new="$(git -C "$APP_DIR" rev-parse FETCH_HEAD)"
if [ "$old" = "$new" ]; then log "更新なし ($BRANCH ${old:0:7})"; exit 0; fi

# 前に進む更新だけ取り込む (ブランチが作り直された・巻き戻されたときは人間が見る)
if ! git -C "$APP_DIR" merge-base --is-ancestor "$old" "$new"; then
  log "ブランチ $BRANCH が今の版 ${old:0:7} の続きではありません (${new:0:7})。取り込みません。人間が確認してください"; exit 1
fi

excludes=()
for p in "${ANALYSIS_ONLY[@]}"; do excludes+=(":(exclude)$p"); done
restart_needed="$(git -C "$APP_DIR" diff --name-only "$old" "$new" -- tbf/ "${excludes[@]}" | head -1)"
manual_changed="$(git -C "$APP_DIR" diff --name-only "$old" "$new" -- "${MANUAL[@]}" | tr '\n' ' ')"

apply() {  # $1 = 版
  git -C "$APP_DIR" checkout -q "$1"
  chown -R root:root "$APP_DIR"
  chmod -R go-w "$APP_DIR"
}

apply "$new"
if ! (cd "$APP_DIR" && runuser -u "$RUN_USER" -- python3 -B -c "import tbf.mm.__main__, tbf.mm.streams, tbf.mm.replay, tbf.mm.report"); then
  apply "$old"
  log "新しい版 ${new:0:7} が読み込めません。${old:0:7} に戻しました (記録は止めていません)"; exit 1
fi

if [ -n "$restart_needed" ] && [ "${#venues[@]}" -gt 0 ]; then
  units=()
  for v in "${venues[@]}"; do units+=("mm-record@$v"); done
  log "記録に関係する変更 ($restart_needed など) があるので起動し直します: ${units[*]}"
  systemctl restart "${units[@]}"
  sleep "$SETTLE_SEC"
  failed=""
  for u in "${units[@]}"; do systemctl is-active -q "$u" || failed="$failed $u"; done
  if [ -n "$failed" ]; then
    apply "$old"
    systemctl restart "${units[@]}"
    log "起動し直した記録が動きません ($failed)。${old:0:7} に戻して起動し直しました"; exit 1
  fi
else
  log "記録に関係する変更は無いので、起動し直しません"
fi

log "取り込みました: ${old:0:7} -> ${new:0:7} ($BRANCH)"
if [ -n "$manual_changed" ]; then
  log "注意: 次のファイルは自動では反映しません。人間が deploy/setup-mm-record.sh を流し直してください: $manual_changed"
fi
