#!/usr/bin/env bash
# 安い VPS (Ubuntu 24.04, 1 vCPU / 1GB, ディスク 20GB 以上) で、GMO コインと bitbank の配信を記録し続ける。
# 発注はしない。鍵も要らない (公開 API の読み取りだけ)。
#   使い方 (root で): BRANCH=<ブランチ名> bash deploy/setup-mm-record.sh
# 事前に: VPS 作成時に SSH 公開鍵を登録しておくこと (パスワードログインは無効化する)
# deploy/setup-server.sh (アビトラ用) を実行済みのサーバーで流してもよい (同じ設定を上書きするだけ)。
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/sollall/trade-bot-factory.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/trade-bot-factory
GMO_COINS="${GMO_COINS:-BTC_JPY,ETH_JPY,XRP_JPY}"
BITBANK_COINS="${BITBANK_COINS:-xrp_jpy,eth_jpy,sol_jpy}"

if [ "$(id -u)" -ne 0 ]; then echo "run as root"; exit 1; fi
if [ ! -s /root/.ssh/authorized_keys ] && ! ls /home/*/.ssh/authorized_keys >/dev/null 2>&1; then
  echo "SSH 公開鍵が登録されていません。パスワードログインを無効化すると締め出されるので中止します"; exit 1
fi

echo "== パッケージ更新と自動セキュリティ更新"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 ufw unattended-upgrades fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else "Python 3.12 以上が必要です (Ubuntu 24.04 を使ってください)")'

echo "== SSH: 鍵認証のみ・パスワード禁止"
cat > /etc/ssh/sshd_config.d/90-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
MaxAuthTries 3
CONF
systemctl reload ssh || systemctl reload sshd

echo "== ファイアウォール: 外からは SSH のみ"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "== 実行ユーザー (ログイン不可) とコード"
id mm >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin mm
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin "$BRANCH" && git -C "$APP_DIR" checkout -q FETCH_HEAD
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
# コードは root 所有 (記録のプロセスから書き換えられないように)
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"
if [ ! -f "$APP_DIR/tbf/mm/streams.py" ]; then
  echo "ブランチ $BRANCH に配信の記録 (tbf/mm/streams.py) がありません。BRANCH= を確かめてください"; exit 1
fi

echo "== 設定ファイル (銘柄)"
install -d -m 0700 -o root -g root /etc/mm
[ -f /etc/mm/gmo.env ] || echo "MM_COINS=$GMO_COINS" > /etc/mm/gmo.env
[ -f /etc/mm/bitbank.env ] || echo "MM_COINS=$BITBANK_COINS" > /etc/mm/bitbank.env
chmod 0600 /etc/mm/gmo.env /etc/mm/bitbank.env

echo "== 1 分だけ試しに記録 (会場につながるかの確認)"
for venue in gmo bitbank; do
  coins="$(. "/etc/mm/$venue.env" && echo "$MM_COINS")"
  check="/tmp/mm-check-$venue.sqlite"
  rm -f "$check" "$check-wal" "$check-shm"
  sudo -u mm bash -c "cd $APP_DIR && python3 -m tbf.mm record --venue $venue --source ws --coins $coins --db $check --minutes 1"
  sudo -u mm bash -c "cd $APP_DIR && python3 -m tbf.mm report --venue $venue --db $check" | head -12
  rm -f "$check" "$check-wal" "$check-shm"
done

echo "== systemd"
install -m 0644 "$APP_DIR/deploy/mm-record@.service" /etc/systemd/system/mm-record@.service
systemctl daemon-reload
systemctl enable mm-record@gmo mm-record@bitbank
systemctl restart mm-record@gmo mm-record@bitbank

cat <<MSG

セットアップ完了。記録は /var/lib/mm/gmo.sqlite と /var/lib/mm/bitbank.sqlite に貯まります (合わせて 1 日 0.3GB ほど)。
  状態:   systemctl status 'mm-record@*'
  ログ:   journalctl -u mm-record@gmo -u mm-record@bitbank -f
  集計:   cd $APP_DIR && sudo -u mm python3 -m tbf.mm report --venue gmo --db /var/lib/mm/gmo.sqlite --hours 24
  残り:   df -h /var/lib/mm
  更新:   BRANCH=$BRANCH bash $APP_DIR/deploy/setup-mm-record.sh   (最新を取り込み再起動)
銘柄を変えるときは /etc/mm/<会場>.env を編集して: systemctl restart mm-record@<会場>
止めるとき: systemctl disable --now mm-record@gmo mm-record@bitbank
MSG
