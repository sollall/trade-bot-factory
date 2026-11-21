# 運用: 止めずにデプロイする・監視する

bot を作って PR を main にマージすると、GitHub Actions が **paper → live** の順に Worker を入れ替えます。
入れ替えの間も cron は止まらず、新しい版が動かなければ自動で直前の版に戻します。
文書 (`*.md`)・`.claude/`・`docs/`・`deploy/` だけの変更ではデプロイは動きません (Worker に入らないため。
動かしたいときは Actions から Deploy を手動で実行します)。

```
 PR マージ (main)
   │
   ▼ deploy.yml
 check ─ pytest / D1 設定確認 / bot の読み込み・live 対応チェック (build_worker.py)
   │
   ▼ deploy-worker.yml (paper)                       ▼ deploy-worker.yml (live, 承認を挟める)
 1 preflight  デプロイから外れる bot・消える銘柄に持ち高が残っていないか   ← live は止める
 2 bookmark   D1 Time Travel の現時点を記録 (データを戻すときの目印)
 3 migrate    D1 migration (追加のみ。旧版が動いたままでも壊れない)
 4 deploy     wrangler deploy (原子的に切り替え)
 5 verify     /healthz で「新しい build の cron が完了した」ことを確認 (最大 5 分)
 6 rollback   5 が失敗したら wrangler rollback で直前の版へ
```

## なぜ途切れないか

| 心配 | 対策 |
|---|---|
| デプロイ中に cron が止まる | `wrangler deploy` は原子的。実行中の cron は旧版で最後まで走り、次の分から新版になる。cron の取りこぼしはない |
| 新旧が同時に動いて二重発注 | 足ごとの処理権 (`claim_candle`) と口座の版番号 (`account_version`) で、同じ足の発注・口座の上書きは起きない |
| 新しい版が Cloudflare 上でだけ壊れる (Pyodide の差など) | 5 verify で新 build の cron 完了を確認し、だめなら 6 で自動ロールバック |
| 1 つの bot のバグで全体が止まる | bot ごとに例外を捕まえて他の bot は動き続ける (エラーはイベント記録 + 通知)。`/healthz` の `last_tick_errors` と `/metrics` の `tbf_bot_errors_1h` にも出る |
| migration で旧版が壊れる | migration は旧版が動いている間に適用されるので追加 (CREATE / ADD COLUMN) だけ。`tests/test_migrations.py` が DROP / RENAME / UPDATE / DELETE を落とす |
| bot を消したらポジションが放置される | 1 preflight が D1 を見て止める (live)。デプロイ後も `/metrics` の `tbf_unmanaged_position_qty` と日次通知で知らせる |
| paper で壊れたものが live に行く | live のジョブは paper の verify 成功後にしか動かない。live に Required reviewers を付ければ人の承認も挟める |
| Worker が何かの理由で動かなくなる | 毎分 D1 に完了記録 (heartbeat) を書く。外部監視が `/healthz` (止まると 503) か `HEARTBEAT_URL` への ping で検知 |

## 初回の設定

GitHub の Settings → Environments → `paper` / `live` それぞれに:

| 種類 | 名前 | 値 |
|---|---|---|
| Variable | `WORKER_URL` | `https://trade-bot-paper.<sub>.workers.dev` など。**設定しないと verify と自動ロールバックが動きません** |
| Variable | `ALLOW_UNMANAGED` | (任意・一時的) 持ち高が残っていても外してよい bot id。取引所側で処理済みのときだけ |

`CLOUDFLARE_API_TOKEN` には Workers Scripts の編集と D1 の編集 (preflight の読み取りと migration) の権限が必要です。

Worker 側の任意の secret:

```bash
npx wrangler secret put HEARTBEAT_URL --env live   # cron が完了するたびに GET する URL
```

Healthchecks.io (OSS・セルフホスト可) の ping URL や、Uptime Kuma の Push モニターの URL を入れると、
「cron が止まった」ことを外から検知できます (Worker 内部からは自分が止まったことを通知できないため)。

Cloudflare Access でドメインを保護している場合は、`/healthz` と `/metrics` のパスを Bypass にしてください
(`/metrics` は `ADMIN_TOKEN` で保護されます。`/healthz` は build と最終実行時刻だけを返し、成績は出しません)。

## bot を変えるときのルール

- **追加**: そのまま PR → マージ。最初の cron で `capital` の口座が作られる
- **パラメータ・ロジックの変更**: そのままでよい。持ち高と `ctx.state` は引き継がれる
- **削除 / `deploy` から外す / instruments の別名を変える・消す**:
  持ち高があると誰も管理しなくなるので、**マージ前に** 今動いている版で手仕舞う
  ```bash
  curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" "$WORKER_URL/api/bots/<id>/halt?flatten=true"
  ```
  忘れても live の preflight が止めます (paper は警告のみ)
- **migration**: 追加だけ。列を消す・名前を変えるときは「新しい列を追加してデプロイ → 次のリリースで古い列を消す」の 2 回に分け、
  2 回目のファイルに `-- tbf:allow-breaking` と理由を書く

## 手動の操作

```bash
npx wrangler deployments list --env live          # デプロイ履歴 (message に commit が入っている)
npx wrangler rollback --env live                  # 直前の版に戻す
npx wrangler d1 time-travel restore trade-bot-live --env live --bookmark <bookmark>   # D1 を戻す (最終手段)
```

D1 を戻すと、その後の約定記録も消えます (取引所側の実際の持ち高とずれる) ので、live では最後の手段です。

## 監視のエンドポイント

| | 認証 | |
|---|---|---|
| `GET /healthz` | なし | `{"ok", "build", "last_tick_build", "age_sec", "bots", "last_tick_errors"}`。cron が 3 分以上完了していなければ 503 |
| `GET /metrics` | `ADMIN_TOKEN` | Prometheus 形式 |
| `GET /` `GET /api/...` | `ADMIN_TOKEN` | 既存のダッシュボードと API (README) |

主なメトリクス (すべて `mode` ラベル付き):

| | |
|---|---|
| `tbf_heartbeat_age_seconds` / `tbf_last_tick_errors` / `tbf_last_tick_duration_seconds` | Worker の cron の状態 |
| `tbf_build_info{build}` | 動いている commit |
| `tbf_bot_equity_usd` / `tbf_bot_return_ratio` / `tbf_bot_daily_return_ratio` / `tbf_bot_drawdown_ratio` | 成績 (最後に取れた価格で評価) |
| `tbf_bot_running` / `tbf_bot_consecutive_losses` / `tbf_bot_errors_1h` | 状態 |
| `tbf_bot_position_qty{instrument}` | 持ち高 |
| `tbf_bot_last_candle_timestamp_seconds` / `tbf_bot_timeframe_seconds` | 戦略が新しい足で動いているか |
| `tbf_unmanaged_position_qty{bot,instrument}` | デプロイから外れた bot に残った持ち高 |
| `tbf_last_tick_lag_seconds` | cron の予定時刻から実際に動き出すまでの遅れ |
| `tbf_bot_exec_delay_seconds{stat=avg\|p95\|max}` / `tbf_bot_exec_gap_bps` / `tbf_bot_exec_gap_usd_30d` | 約定の質 (下記) |

## Workers の遅延で足りているか (約定の質)

Workers は毎分の cron で動くので、VM で常駐させるより発注が遅れます (cron の起動待ち + Python の起動 + 取引所との通信)。
それが成績にどれだけ効いているかを、戦略の判断による約定ごとに記録しています (停止時の手仕舞いなどは除く)。

| | 意味 |
|---|---|
| **発注まで** (`delay`) | 判断に使った足が確定してから発注するまでの秒数 |
| **対バックテスト** (`gap_bps`) | 実際の約定価格と、バックテストが仮定する約定価格 (確定足の終値 ≒ 次の足の始値 + 同じスリッページ) の差。約定代金で加重、**+ は不利** |
| `gap_usd` | その差を金額にしたもの (バックテストより何ドル不利だったか) |

- paper の約定は「発注時点の価格 + 想定スリッページ」なので、`gap_bps` はほぼ **遅れの間に動いた値段** です。
  live では実際のスリッページとの差も含みます
- 1 回ごとの値は上下にばらつくので、**30 日の平均** を見ます。0 付近なら遅れは効いていません。
  手数料 1 回分 (Binance 現物 10bps) に近い・超えるなら、短い足の戦略や VM での常駐を検討する目安です
  (Prometheus のアラート `ExecutionGapHigh`)
- 見る場所: `/api/bots` の `execution`、日次レポート、Grafana の「足の確定から発注まで」「約定価格 対 バックテストの想定」

## OSS の監視基盤 (`deploy/monitoring/`)

Prometheus (収集・保存) + Grafana (ダッシュボード) + Uptime Kuma (死活監視・通知) を docker compose で動かします。
アビトラ用の VPS (`deploy/setup-server.sh`) と同じサーバーに置けます。どれも `127.0.0.1` にだけ公開するので、
ファイアウォールは SSH のみのままで構いません。

```bash
# サーバーで (Docker を入れておく)
cd /opt/trade-bot-factory/deploy/monitoring
mkdir -p secrets prometheus/secrets
openssl rand -base64 24 > secrets/grafana_admin_password
echo -n '<paper の ADMIN_TOKEN>' > prometheus/secrets/paper_token
echo -n '<live の ADMIN_TOKEN>'  > prometheus/secrets/live_token
chmod 600 secrets/* prometheus/secrets/*
vi prometheus/prometheus.yml     # targets を自分の Worker のホスト名に
docker compose up -d
```

```bash
# 手元から SSH トンネルで開く
ssh -L 3000:localhost:3000 -L 3001:localhost:3001 -L 9090:localhost:9090 <server>
```

- Grafana http://localhost:3000 (admin / `secrets/grafana_admin_password`): ダッシュボード「trade-bot-factory」が自動で入る。
  資産・リターン・ドローダウン・持ち高・連敗数・停止中の bot・エラー・管理外の持ち高・cron の遅れ・デプロイ中の build
- Uptime Kuma http://localhost:3001: 初回にユーザー作成 → HTTP モニターで `<WORKER_URL>/healthz` を 1 分間隔
  (paper と live)。通知先に Discord / Slack / Telegram などを設定
- Prometheus http://localhost:9090/alerts: `prometheus/alerts.yml` のアラート (cron 停止・bot 停止・エラー多発・戦略の遅れ・管理外の持ち高)

### Grafana をブラウザから直接見る (Cloudflare Tunnel + Access)

SSH トンネルなしで `https://grafana.<自分のドメイン>` から開けるようにします。サーバーのポートは開けません
(`cloudflared` がサーバーから Cloudflare へ外向きに接続するだけ)。**Cloudflare に載せたドメインが 1 つ必要です。**

1. **トンネルを作る**: Cloudflare の Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared。
   表示されるトークン (`eyJ...`) を控える
2. **公開先を決める**: 同じトンネルの Public Hostname に追加。
   Subdomain `grafana` / Domain 自分のドメイン / Service `HTTP` `grafana:3000`
   (`cloudflared` は compose の中で動くので、`localhost` ではなくサービス名で指す)
3. **見られる人を絞る (必ず 4 より先に)**: Zero Trust → Access → Applications → Add an application → Self-hosted。
   Application domain に `grafana.<自分のドメイン>`、Policy は Allow で自分のメールだけ。
   これを飛ばすと、Grafana のログイン画面がインターネットにそのまま出ます
4. **サーバーで起動**:

   ```bash
   cd /opt/trade-bot-factory/deploy/monitoring
   echo 'TUNNEL_TOKEN=<1 のトークン>' > secrets/cloudflared.env && chmod 600 secrets/cloudflared.env
   echo 'GRAFANA_ROOT_URL=https://grafana.<自分のドメイン>/' >> .env
   docker compose --profile tunnel up -d
   ```

5. `https://grafana.<自分のドメイン>` を開く → Cloudflare のメール認証 → Grafana のログイン (admin / `secrets/grafana_admin_password`)

止めるときは `docker compose --profile tunnel stop cloudflared`。Uptime Kuma と Prometheus は公開していません
(足すなら 2 と 3 を同じ要領で。Prometheus は認証が無いので Access が必須)。

### この先の拡張

- **Alertmanager**: `alerts.yml` のアラートを Discord / Slack に飛ばす (今は Uptime Kuma と Worker 自身の Webhook 通知で代替)
- **ログ**: Workers Logpush や `wrangler tail` を Loki に送ると、Grafana でエラーのスタックトレースまで追える
- **アビトラのサーバー**: `tbf.arb` に `/metrics` を足せば同じ Prometheus / Grafana に載る
