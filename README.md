# trade-bot-factory

`bots/` に **Python** で bot を書いて push するだけで、Cloudflare Workers (Python Workers) 上で動き出す売買 bot の工場です。

- **paper** (ペーパートレード) と **live** (本番) の 2 つの Worker に同じコードをデプロイ
- 各 Worker は毎分 cron で動き、確定足ごとに戦略を実行し、**成績を監視**して閾値を超えたら自動停止 + 通知
- **ローカルでバックテスト** — Cloudflare 上と同じ Python の戦略コード・売買ロジックで検証

```
            ┌──────────── bots/<id>/bot.py ─────────────┐
            │  bot = Bot(symbol=..., timeframe=...,      │
            │            strategy=..., deploy=["paper"]) │
            └──────┬──────────────────┬─────────────────┘
                   │                  │  git push main → GitHub Actions
  python -m tbf.backtest         ┌────┴──────────────┐
   (ローカル・過去データ)          ▼                    ▼
                         trade-bot-paper        trade-bot-live
                         PaperBroker (疑似約定)   BinanceBroker (実注文)
                         D1: 口座/約定/資産推移     D1: 口座/約定/資産推移
                         └── 監視: DD・日次損失・連敗 → 停止 + Webhook 通知
                         └── ダッシュボード / API / 日次レポート
```

## 構成

| 層 | 場所 | 役割 |
|---|---|---|
| 戦略 | `bots/<id>/bot.py` | `bot = Bot(...)` で銘柄・足・パラメータ・リスク上限・戦略関数を定義 |
| 共通コア | `tbf/core/` | 意思決定 (`step.py`)、口座計算 (`portfolio.py`)、成績 (`metrics.py`)、リスク判定 (`risk.py`)、指標 (`indicators.py`) |
| バックテスト | `tbf/backtest/` | 足 i の確定で判断し足 i+1 の始値で約定 (先読みなし)。本番と同じリスク停止も再現 |
| Worker 本体 | `tbf/worker/` | cron 処理・D1 永続化・ブローカー (paper/live)・通知・ダッシュボード・API |
| Cloudflare 入口 | `worker/entry.py` | D1 / fetch / Request / Response を `tbf.worker` に橋渡しするだけの薄い層 |
| ビルド | `scripts/build_worker.py` | `tbf/`・`bots/`・`entry.py` を `.build/` に集め、bot 一覧 `bot_registry.py` を生成 |

フレームワークは **標準ライブラリだけ** で書かれていて、Cloudflare 上でもローカルでも同じコードが動きます。
戦略関数は「資産に対する目標保有比率 (0〜1)」を返すだけです。差分の発注、手数料・スリッページ、口座管理は
フレームワーク側が行うので、バックテスト・paper・live で挙動が一致します。

## bot の追加

```bash
python scripts/new_bot.py my_bot        # bots/_template からコピー
```

```python
# bots/my_bot/bot.py
from tbf import Bot, Context, Decision, Risk
from tbf.core import indicators as ind


def strategy(ctx: Context):
    fast = ind.sma(ctx.closes, ctx.params["fast"])
    slow = ind.sma(ctx.closes, ctx.params["slow"])
    if fast is None or slow is None:
        return None                       # 何もしない
    return Decision(1 if fast > slow else 0, reason="sma cross")   # 数値 (0〜1) だけ返してもよい


bot = Bot(
    id="my_bot",                 # ディレクトリ名と同じ (a-z, 0-9, _)
    symbol="BTCUSDT",
    timeframe="1h",              # 1m 5m 15m 30m 1h 4h 1d
    lookback=60,                 # 戦略に渡す過去足の本数
    capital=1000,                # この bot に割り当てる資金 (quote 通貨)
    deploy=["paper"],            # 検証が済んだら ["paper", "live"]
    risk=Risk(max_drawdown=0.2, max_daily_loss=0.05, max_consecutive_losses=5, flatten_on_halt=True),
    params={"fast": 10, "slow": 50},
    strategy=strategy,
)
```

`ctx` で使えるもの: `candles` (確定足, 古い順), `closes`, `position` (`qty`, `avg_price`), `cash`, `equity`,
`params`, `state` (実行をまたいで D1 に保存される dict。JSON 化できる値のみ), `mode` (`backtest` / `paper` / `live`)。

> Cloudflare 上の Python は Pyodide です。bot では標準ライブラリと `tbf` だけを使ってください
> (numpy / pandas 等を使う場合は pywrangler でのパッケージ同梱が別途必要です)。

### 推奨フロー

1. `python -m tbf.backtest --bot my_bot` で検証
2. `deploy=["paper"]` で main に push → paper Worker で実データで検証
3. ダッシュボードや日次レポートで成績を確認
4. 良ければ `deploy=["paper", "live"]` にして push → 本番稼働 (GitHub Environment の承認を挟める)

## ローカルバックテスト

```bash
uv sync                                            # 初回のみ (テスト用の pytest を入れる。本体は依存なし)
python -m tbf.backtest --bot sma_cross --from 2025-01-01 --to 2025-07-01
python -m tbf.backtest --bot all --days 90
python -m tbf.backtest --bot sma_cross --param fast=20 --param slow=100   # パラメータ上書き
python -m tbf.backtest --bot sma_cross --csv ./data.csv                  # 手持ちデータ
```

- 過去足は Binance の公開 API から取得し `data/` にキャッシュ
- 結果は標準出力と `reports/<id>.json`, `reports/<id>.html` (資産曲線) に出力
- CSV は `time,open,high,low,close,volume` (time は ms か ISO 文字列)

## Cloudflare のセットアップ (初回のみ)

```bash
npm install                  # wrangler
npx wrangler login
npm run setup:cloudflare     # D1 (paper/live) を作成し wrangler.toml の database_id を書き換え、migration を適用
```

secrets を登録:

```bash
npx wrangler secret put ADMIN_TOKEN --env paper    # ダッシュボード/API の認証トークン
npx wrangler secret put WEBHOOK_URL --env paper    # Discord / Slack Incoming Webhook (任意)
npx wrangler secret put ADMIN_TOKEN --env live
npx wrangler secret put WEBHOOK_URL --env live
npx wrangler secret put BINANCE_API_KEY --env live     # 出金権限なし・IP 制限推奨
npx wrangler secret put BINANCE_API_SECRET --env live
```

GitHub リポジトリの Settings で:

- Secrets: `CLOUDFLARE_API_TOKEN` (Workers と D1 の編集権限), `CLOUDFLARE_ACCOUNT_ID`
- Environments: `paper` と `live` を作成。`live` に Required reviewers を付けると本番デプロイ前に承認が必要になる

`wrangler.toml` をコミットして main に push すると `.github/workflows/deploy.yml` が
テスト → ビルド → migration → paper デプロイ → live デプロイ を行います。
手元からデプロイする場合は `npm run deploy:paper` / `npm run deploy:live`。

## 監視

各 Worker は毎分すべての bot について:

- 現在価格で資産を評価し、ピーク・日初資産を更新
- `risk` の上限 (最大ドローダウン / 日次損失 / 連敗数) を超えたら **停止**、`flatten_on_halt` なら成行で手仕舞い、Webhook に通知
- 新しい確定足があれば戦略を実行し、約定・資産推移を D1 に記録 (`NOTIFY_FILLS=true` で約定も通知)
- 毎日 00:05 UTC に全 bot の成績サマリを通知

エンドポイント (`ADMIN_TOKEN` 設定時は `Authorization: Bearer <token>` か `?token=` が必要):

| | |
|---|---|
| `GET /` | ダッシュボード (状態・損益・DD・勝率・シャープ・30 日の資産推移) |
| `GET /api/bots` | 全 bot の成績 |
| `GET /api/bots/:id` | 詳細 (状態・直近約定・イベント) |
| `GET /api/events` | 停止・エラー等のイベント |
| `POST /api/bots/:id/halt?flatten=true` | 手動停止 (flatten で手仕舞い) |
| `POST /api/bots/:id/resume` | 再開 (ピーク・連敗数は現在値から数え直し) |
| `POST /api/tick` | cron を待たずに 1 回実行 |

ダッシュボードをブラウザで見るなら、トークンを URL に載せるより
Cloudflare Access で Worker のドメインを保護するのがおすすめです。

## チェーン間アビトラ (常時起動サーバー, シャドーモード)

Cloudflare の cron では間に合わない速度の戦略は、安い VPS で常時動かします。
まずは売買せずに機会を測る **シャドーモード** です (秘密鍵不要)。

```bash
python -m tbf.arb check     # 設定確認
python -m tbf.arb run       # 監視
python -m tbf.arb report    # 集計
```

設定は `arb.toml`、サーバー設置は `deploy/setup-server.sh`。詳細とセキュリティのチェックリストは [docs/arb.md](docs/arb.md)。

## 開発

```bash
uv run pytest                       # テスト (コア / Worker 本体を sqlite で / entry.py をスタブで)
python scripts/build_worker.py      # .build/ を生成 (bot の読み込みチェックも兼ねる)
npm run build && npx wrangler dev --env paper --test-scheduled   # ローカル実行 (/__scheduled で cron 発火)
npx wrangler d1 migrations apply trade-bot-paper --env paper --local   # ローカル D1 の初期化
```

## 前提と制約

- 取引所は **Binance 現物** (ロングのみ・成行)。マーケットデータは `data-api.binance.vision`、
  発注は `api.binance.com` (`MARKET_DATA_URL` / `BINANCE_API_URL` で変更可)。
  他の取引所は `tbf/worker/brokers.py` の `Broker` と `tbf/market/` を実装すれば差し替えられます
- live でも bot ごとの口座は D1 上の仮想口座 (`capital` を割り当て) として管理します。
  同じ取引所口座で複数 bot を動かせますが、口座の実残高とは照合しません
- 同じ確定足での二重発注は D1 の `claim_candle` と Binance の `newClientOrderId` で防いでいます
- Python Workers は起動に CPU 時間を使うため、Workers の **Paid プラン** を推奨します
  (Free プランの CPU 上限 10ms/リクエストでは足りない可能性があります)
- cron は 1 分間隔なので、約定は足の確定から最大 1 分程度遅れます (バックテストは次足始値で約定)
