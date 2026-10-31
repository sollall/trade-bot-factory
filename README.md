# trade-bot-factory

`bots/` に **Python** で bot を書いて push するだけで、Cloudflare Workers (Python Workers) 上で動き出す売買 bot の工場です。

- **paper** (ペーパートレード) と **live** (本番) の 2 つの Worker に同じコードをデプロイ
- 各 Worker は毎分 cron で動き、確定足ごとに戦略を実行し、**成績を監視**して閾値を超えたら自動停止 + 通知
- **ローカルでバックテスト** — Cloudflare 上と同じ Python の戦略コード・売買ロジックで検証

```
            ┌──────────── bots/<id>/bot.py ──────────────────┐
            │  bot = Bot(instruments={...}, timeframe=...,    │
            │            strategy=..., deploy=["paper"])      │
            └──────┬──────────────────┬──────────────────────┘
                   │                  │  git push main → GitHub Actions
  python -m tbf.backtest         ┌────┴──────────────┐
   (ローカル・過去データ)          ▼                    ▼
                         trade-bot-paper        trade-bot-live
                         PaperBroker (疑似約定)   取引所ごとの実注文
                         D1: 口座/約定/資産推移     D1: 口座/約定/資産推移
                         └── 監視: DD・日次損失・連敗 → 停止 + Webhook 通知
                         └── ダッシュボード / API / 日次レポート
```

## 構成

| 層 | 場所 | 役割 |
|---|---|---|
| 戦略 | `bots/<id>/bot.py` | `bot = Bot(...)` で銘柄・足・パラメータ・リスク上限・戦略関数を定義 |
| 共通コア | `tbf/core/` | 銘柄 (`instrument.py`)、意思決定 (`step.py`)、口座計算 (`portfolio.py`)、注文の計画と執行 (`execution.py`)、成績 (`metrics.py`)、リスク判定 (`risk.py`)、指標 (`indicators.py`) |
| マーケットデータ | `tbf/market/` | 取引所ごとの価格・足・ファンディング (Binance 現物 / Hyperliquid 先物) |
| バックテスト | `tbf/backtest/` | 足 i の確定で判断し足 i+1 の始値で約定 (先読みなし)。ファンディング・清算・本番と同じリスク停止も再現 |
| Worker 本体 | `tbf/worker/` | cron 処理・D1 永続化・ブローカー (paper/live)・通知・ダッシュボード・API |
| Cloudflare 入口 | `worker/entry.py` | D1 / fetch / Request / Response を `tbf.worker` に橋渡しするだけの薄い層 |
| ビルド | `scripts/build_worker.py` | `tbf/`・`bots/`・`entry.py` を `.build/` に集め、bot 一覧 `bot_registry.py` を生成 |

フレームワークは **標準ライブラリだけ** で書かれていて、Cloudflare 上でもローカルでも同じコードが動きます。
戦略関数は「銘柄ごとの目標の持ち高 (資産に対する比率, マイナスはショート)」を返すだけです。
差分の発注、取引所間の資金配分、手数料・スリッページ、ファンディング、証拠金と清算はフレームワーク側が行うので、
1 銘柄のトレンドフォローからデルタニュートラル・ペアトレードまで、同じ形でバックテスト → paper → live に進めます。

## bot の追加

```bash
python scripts/new_bot.py my_bot        # bots/_template からコピー
```

1 銘柄の現物 bot は `symbol` だけで書けます:

```python
# bots/my_bot/bot.py
from tbf import Bot, Context, Decision, Risk
from tbf.core import indicators as ind


def strategy(ctx: Context):
    fast = ind.sma(ctx.closes, ctx.params["fast"])
    slow = ind.sma(ctx.closes, ctx.params["slow"])
    if fast is None or slow is None:
        return None                       # 何もしない (今の持ち高のまま)
    return Decision(1 if fast > slow else 0, reason="sma cross")   # 数値 (0〜1) だけ返してもよい


bot = Bot(
    id="my_bot",                 # ディレクトリ名と同じ (a-z, 0-9, _)
    symbol="BTCUSDT",            # Binance 現物
    timeframe="1h",              # 1m 5m 15m 30m 1h 4h 1d
    lookback=60,                 # 戦略に渡す過去足の本数
    capital=1000,                # この bot に割り当てる資金 (USD)
    deploy=["paper"],            # 検証が済んだら ["paper", "live"]
    risk=Risk(max_drawdown=0.2, max_daily_loss=0.05, max_consecutive_losses=5, flatten_on_halt=True),
    params={"fast": 10, "slow": 50},
    strategy=strategy,
)
```

複数銘柄・先物を使う bot は `instruments` に別名をつけて並べ、別名ごとの比率を dict で返します
(例: `bots/eth_funding_carry/bot.py` — ETH 現物ロング + 先物ショートでファンディングを受け取る):

```python
from tbf import Bot, Context, Decision, perp, spot

def strategy(ctx: Context):
    apr = ctx.funding_apr("perp", 24)                # 直近 24 時間の平均ファンディング (年率 %)
    if apr is None:
        return None
    if ctx.holding("perp").qty == 0 and apr >= 10:
        w = ctx.neutral_weight()                     # 現物代金 + 先物証拠金で資金を使い切る比率
        return Decision(weights={"spot": w, "perp": -w})
    if ctx.holding("perp").qty != 0 and apr <= 2:
        return {}                                    # 書いていない銘柄は 0 = 全部手仕舞い
    return None

bot = Bot(
    id="my_carry",
    instruments={"spot": spot("ETHUSDT"), "perp": perp("ETH")},   # Binance 現物 / Hyperliquid 先物
    timeframe="1h", lookback=24, capital=10_000,
    leverage=3,                  # 先物の想定元本 / 証拠金
    strategy=strategy,
)
```

**戦略の戻り値**: 資産に対する目標の持ち高比率 (+ ロング / - ショート)

| 戻り値 | 意味 |
|---|---|
| 数値 | 主銘柄 (`instruments` の最初) の比率 |
| `{"別名": 比率, ...}` / `Decision(weights=...)` | 複数銘柄。**書いていない銘柄は 0 (手仕舞い)** |
| `Decision(比率, reason=...)` | 理由つき (通知や記録に残る) |
| `None` | 何もしない (今の持ち高のまま) |

現物はロングのみ (0〜1)、先物は ±`leverage` まで。現物の買い代金 + 先物の証拠金 (想定元本 / `leverage`) が資産を超える場合は全体を縮めます。

**`ctx` で使えるもの**

| | |
|---|---|
| 1 銘柄 (主銘柄) | `candles` `closes` `position` (`qty`, `avg_price`) |
| 複数銘柄 | `series(別名)` `closes_of(別名)` `holding(別名)` `weight(別名)` (現在の比率) |
| 先物 | `funding(別名)` (ファンディング履歴) `funding_apr(別名, 時間)` (平均を年率 % で) |
| 共通 | `time` `equity` `cash` `leverage` `neutral_weight()` `params` `state` (実行をまたいで保存される dict) `mode` |

**銘柄**: `spot("ETHUSDT")` (Binance 現物), `perp("ETH")` (Hyperliquid 無期限先物)。
手数料は取引所ごとの既定値 (基本ティアの taker) で、`spot("ETHUSDT", fees=Fees(0.00075, 3))` のように上書きできます。
先物の維持証拠金率 `maintenance_margin` (既定 2%) は銘柄の最大レバレッジに合わせて設定してください
(Hyperliquid は「最大レバレッジの逆数の半分」。最大 5 倍のアルトなら 10%)。

**本番 (live) で発注できるのは今は Binance 現物だけ** です。Hyperliquid の先物は paper まで動きます。
未対応の銘柄を含む bot を `deploy=["live"]` にするとビルド (デプロイ) の時点で止まります。

> Cloudflare 上の Python は Pyodide です。bot では標準ライブラリと `tbf` だけを使ってください
> (numpy / pandas 等を使う場合は pywrangler でのパッケージ同梱が別途必要です)。

### 推奨フロー

1. `python -m tbf.backtest --bot my_bot` で検証
2. `deploy=["paper"]` で main に push → paper Worker で実データで検証
3. ダッシュボードや日次レポートで成績を確認
4. 良ければ `deploy=["paper", "live"]` にして push → 本番稼働 (GitHub Environment の承認を挟める)

## ローカルバックテスト

```bash
uv sync                                                     # 初回のみ (テスト用の pytest を入れる。本体は依存なし)
python -m tbf.backtest --bot sma_cross --from 2025-01-01 --to 2025-07-01
python -m tbf.backtest --bot eth_funding_carry,eth_funding_hedged --days 180   # 同じ期間で並べて比較
python -m tbf.backtest --bot sma_cross --param fast=20 --param slow=100        # パラメータ上書き
python -m tbf.backtest --bot eth_funding_carry --sweep entry_apr=5,10,20 --sweep leverage=2,3,5   # 総当たり
python -m tbf.backtest --bot sma_cross --csv ./data.csv                        # 手持ちデータ (1 銘柄 bot のみ)
```

- 過去データは Binance (現物の足) と Hyperliquid (先物の足・ファンディング) の公開 API から取得し `data/` にキャッシュ。
  Hyperliquid の足は API の制約で直近 5000 本まで (1 時間足なら約 208 日)
- `--param` / `--sweep` のキーは戦略の `params` か bot の設定 (`leverage`, `capital`, `rebalance_band`, `transfer_fee_usd` など)
- 結果は標準出力と `reports/<id>.json`, `reports/<id>.html` (資産曲線・イベント)、複数 bot なら `reports/compare.html`
- 各足で: 前の足で決めた目標まで始値で売買 → 高値 / 安値で先物の清算判定 → 足の間のファンディングを受け払い →
  終値で評価 → リスク判定 → 戦略。戦略が「現状維持」でも、先物口座の実効レバレッジが `leverage` の
  `rebalance_band` 倍を外れたら配分し直します (取引所間の資金移動は 1 回 `transfer_fee_usd`)
- 清算は「その取引所の先物と証拠金を全て失う」とみなし、残った持ち高は次の足で手仕舞います (保守的な近似)
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
- paper では先物の清算も再現 (清算されたら残りを手仕舞って通知)
- 新しい確定足があれば、先物のファンディングを反映してから戦略を実行し、約定・資産推移を D1 に記録
  (`NOTIFY_FILLS=true` で約定も通知)。戦略が「現状維持」でも先物口座のレバレッジがずれたら配分し直す
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

- 取引所: **Binance 現物** (データ・paper・live) と **Hyperliquid 無期限先物** (データ・paper)。注文は成行。
  マーケットデータは `data-api.binance.vision` / `api.hyperliquid.xyz` (`MARKET_DATA_URL` / `HYPERLIQUID_URL` で変更可)。
  取引所を足すときは `tbf/market/` にデータ取得、`tbf/worker/brokers.py` に発注を実装し、`LIVE_SUPPORTED` に登録します
- live でも bot ごとの口座は D1 上の仮想口座 (`capital` を割り当て、取引所ごとの現金と持ち高) として管理します。
  同じ取引所口座で複数 bot を動かせますが、口座の実残高とは照合しません
- 取引所間の資金移動は、paper では自動 (1 回 `transfer_fee_usd`) ですが、**本番では自動化しません**
  (出金権限のある鍵を置かないため)。必要になったら通知するので手動で移してください
- 先物口座から移せるのは「現金 + 含み損益 - 必要証拠金」まで (クロスマージンの引き出し可能額の近似) としています
- 同じ確定足での二重発注は D1 の `claim_candle` と Binance の `newClientOrderId` で防いでいます
- Python Workers は起動に CPU 時間を使うため、Workers の **Paid プラン** を推奨します
  (Free プランの CPU 上限 10ms/リクエストでは足りない可能性があります)
- cron は 1 分間隔なので、約定は足の確定から最大 1 分程度遅れます (バックテストは次足始値で約定)
