# 運用: 止めずにデプロイする・監視する

bot を作って PR を main にマージすると、GitHub Actions が **paper → live** の順に Worker を入れ替えます。
入れ替えの間も cron は止まらず、新しい版が動かなければ自動で直前の版に戻します。

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

## お試し → 採用の流れ (PR ごとの paper Worker)

試したい bot は PR にして **ラベル `paper-trial`** を付けます。開いている `paper-trial` の PR が「お試し中のプール」です。

```
 bot を書いてバックテスト → PR を作り、ラベル paper-trial を付ける
   │  preview.yml
   ▼
 trade-bot-pr-<番号> (Worker) + trade-bot-pr-<番号>-<sha> (D1) を作成 ── paper / live とは別の Worker・D1
   PR で追加・変更した bot だけを paper で動かす (deploy の指定に関係なく。bot の変更がなければ全 bot)
   │
   ├ PR に push → D1 を作り直してデプロイ (成績はリセット)
   ├ 毎日 00:17 UTC → PR のコメントに成績 (資産・リターン・最大 DD・シャープ・取引数・勝率) を更新
   │
   ├ いけてる → deploy=["paper", "live"] などにしてマージ → main の paper / live にデプロイ (下の流れ)
   └ だめ     → PR を閉じる (またはラベルを外す)
              → 最終成績をコメントに残し、Worker と D1 を削除
```

- お試し Worker は `MODE=paper` 固定で、取引所の API キーを持ちません (`PREVIEW` と `live` の組み合わせは起動時に拒否)
- 通知は `[pr-12]` のように PR 番号付き。約定の通知はせず、停止・エラー・日次レポートだけ (`PREVIEW_WEBHOOK_URL` を設定した場合)
- ダッシュボード: `https://trade-bot-pr-<番号>.<sub>.workers.dev/?token=<PREVIEW_ADMIN_TOKEN>`
- マージ後の main の paper / live は新しい口座で始まります (お試しの成績や持ち高は引き継がない)
- 同時に開けるお試し PR の数は Cloudflare の cron trigger 数の上限 (1 Worker 2 つ) と CPU 時間の課金で決まります。
  Workers Paid なら数本〜十数本は問題ない想定です

### 初回の設定

GitHub の Settings で:

| 種類 | 名前 | |
|---|---|---|
| Environment | `preview` | 作るだけ (必要なら Secrets をここに置く) |
| Secret | `PREVIEW_ADMIN_TOKEN` | お試し Worker のダッシュボード / API の認証 (paper / live とは別の値に) |
| Secret | `PREVIEW_WEBHOOK_URL` | (任意) お試し用の通知先。本番の通知チャンネルと分けると見やすい |
| Label | `paper-trial` | Issues → Labels で作成 |

`CLOUDFLARE_API_TOKEN` は main のデプロイと同じものを使います (D1 の作成・削除と Workers の編集・削除の権限)。

### 分離について

アカウントは 1 つのままで、**動くもの (Worker・D1・secret・通知先) は PR ごと・paper・live で全部別** です。
お試しのコードが paper / live の口座データに触れることはありません。

残る共有は Cloudflare の API トークンだけです (トークンは Worker 単位に権限を絞れないため、
PR のワークフローからも技術的には live の Worker を書き換えられる)。自分だけのリポジトリなら実害は小さいですが、
live に大きな資金を入れる段階になったら、live だけ別アカウントにすることを検討してください。
それまでの事故防止として、`live` Environment の Deployment branches を `main` だけに制限しておきます。

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
- 見る場所: `/api/bots` の `execution`、日次レポート、お試し PR のコメント、Grafana の「足の確定から発注まで」「約定価格 対 バックテストの想定」

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

### この先の拡張

- **Alertmanager**: `alerts.yml` のアラートを Discord / Slack に飛ばす (今は Uptime Kuma と Worker 自身の Webhook 通知で代替)
- **ログ**: Workers Logpush や `wrangler tail` を Loki に送ると、Grafana でエラーのスタックトレースまで追える
- **アビトラのサーバー**: `tbf.arb` に `/metrics` を足せば同じ Prometheus / Grafana に載る
- **公開**: チームで見るなら Grafana を Cloudflare Tunnel + Access の後ろに置く (ポートを開けない)
