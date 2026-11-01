#!/usr/bin/env bash
# 安い VPS (Ubuntu 24.04, 1 vCPU / 1GB 程度) をシャドーモード用にセットアップする。
#   使い方 (root で): curl -fsSL <この raw URL> | bash   または  sudo bash deploy/setup-server.sh
# 事前に: VPS 作成時に SSH 公開鍵を登録しておくこと (パスワードログインは無効化する)
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/sollall/trade-bot-factory.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/trade-bot-factory

if [ "$(id -u)" -ne 0 ]; then echo "run as root"; exit 1; fi
if [ ! -s /root/.ssh/authorized_keys ] && ! ls /home/*/.ssh/authorized_keys >/dev/null 2>&1; then
  echo "SSH 公開鍵が登録されていません。パスワードログインを無効化すると締め出されるので中止します"; exit 1
fi

echo "== パッケージ更新と自動セキュリティ更新"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3 ufw unattended-upgrades fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades

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
id arb >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin arb
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" fetch origin "$BRANCH" && git -C "$APP_DIR" checkout -q "origin/$BRANCH"
else
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi
# コードは root 所有 (bot から書き換えられないように)
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"

echo "== 環境変数ファイル"
install -d -m 0700 -o root -g root /etc/arb
if [ ! -f /etc/arb/env ]; then
  cat > /etc/arb/env <<'ENV'
# ARB_RPC_ARBITRUM=https://arb-mainnet.g.alchemy.com/v2/xxxx
# ARB_RPC_BASE=https://base-mainnet.g.alchemy.com/v2/xxxx
# ARB_RPC_OPTIMISM=https://opt-mainnet.g.alchemy.com/v2/xxxx
# WEBHOOK_URL=https://discord.com/api/webhooks/...
ENV
fi
chown root:root /etc/arb/env
chmod 0600 /etc/arb/env

echo "== 設定チェック"
# /etc/arb/env は root しか読めないので、root が読んで arb ユーザーに環境変数として渡す
sudo -u arb env $(grep -v '^\s*#' /etc/arb/env | xargs -r) \
  bash -c "cd $APP_DIR && python3 -m tbf.arb check --db /tmp/arb-check.sqlite"

echo "== systemd"
install -m 0644 "$APP_DIR/deploy/arb-shadow.service" /etc/systemd/system/arb-shadow.service
systemctl daemon-reload
systemctl enable --now arb-shadow

cat <<MSG

セットアップ完了。
  状態:     systemctl status arb-shadow
  ログ:     journalctl -u arb-shadow -f
  レポート: cd $APP_DIR && sudo -u arb python3 -m tbf.arb report --db /var/lib/arb/arb.sqlite --hours 24
  更新:     sudo bash $APP_DIR/deploy/setup-server.sh   (最新の $BRANCH を取り込み再起動)
RPC や Webhook を設定したら /etc/arb/env を編集して: systemctl restart arb-shadow
MSG
