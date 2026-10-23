# trade-bot-factory

`bots/` に bot を置いて push するだけで Cloudflare Workers 上で動き出す、売買 bot の工場です。

- **paper** (ペーパートレード) と **live** (本番) の 2 つの Worker に同じコードをデプロイ
- 各 Worker は毎分 cron で動き、確定足ごとに戦略を実行し、**成績を監視**して閾値を超えたら自動停止 + 通知
- **ローカルでバックテスト** — 本番と同じ戦略コード・同じ売買ロジックで検証

```
            ┌──────────── bots/<id>/index.ts ────────────┐
            │  defineBot({ symbol, timeframe, strategy,   │
            │              deploy: ["paper", "live"], ...}) │
            └──────┬──────────────────┬──────────────────┘
                   │                  │  git push main → GitHub Actions
     npm run backtest            ┌────┴──────────────┐
   (ローカル・過去データ)          ▼                    ▼
                         trade-bot-paper        trade-bot-live
                         PaperBroker (疑似約定)   BinanceBroker (実注文)
                         D1: 口座/約定/資産推移     D1: 口座/約定/資産推移
                         └── 監視: DD・日次損失・連敗 → 停止 + Webhook 通知
                         └── ダッシュボード / API / 日次レポート
```

## 仕組み

| 層 | 場所 | 役割 |
|---|---|---|
| 戦略 | `bots/<id>/index.ts` | `defineBot()` で銘柄・足・パラメータ・リスク上限・戦略関数を定義 |
| 共通コア | `src/core/` | 意思決定 (`step.ts`)、口座計算 (`portfolio.ts`)、成績 (`metrics.ts`)、リスク判定 (`risk.ts`) |
| バックテスト | `src/backtest/` | 足 i の確定で判断し足 i+1 の始値で約定 (先読みなし)。本番と同じリスク停止も再現 |
| Worker | `src/worker/` | cron 実行・D1 永続化・ブローカー (paper/live)・通知・ダッシュボード |
| レジストリ | `scripts/gen-registry.ts` | `bots/*` を走査して `src/generated/bots.ts` を生成 (ビルド時に自動実行) |

戦略関数は「資産に対する目標保有比率 `targetExposure` (0〜1)」を返すだけです。
差分の発注、手数料・スリッページ、口座管理はフレームワーク側が行うので、
バックテスト・paper・live で挙動が一致します。

## bot の追加

```bash
npm run new-bot -- my-bot        # bots/_template からコピー
```

```ts
// bots/my-bot/index.ts
import { defineBot } from "../../src/core/bot";
import { sma } from "../../src/core/indicators";

export default defineBot({
  id: "my-bot",                  // ディレクトリ名と同じにする
  symbol: "BTCUSDT",
  timeframe: "1h",               // 1m 5m 15m 30m 1h 4h 1d
  lookback: 60,                  // 戦略に渡す過去足の本数
  capital: 1000,                 // この bot に割り当てる資金 (quote 通貨)
  deploy: ["paper"],             // 検証が済んだら ["paper", "live"]
  risk: { maxDrawdown: 0.2, maxDailyLoss: 0.05, maxConsecutiveLosses: 5, flattenOnHalt: true },
  fees: { takerRate: 0.001, slippageBps: 5 },   // 省略可
  params: { fast: 10, slow: 50 },
  strategy: ({ candles, params, position, state }) => {
    const closes = candles.map((c) => c.close);
    const f = sma(closes, params.fast), s = sma(closes, params.slow);
    if (f == null || s == null) return;           // 何もしない
    return { targetExposure: f > s ? 1 : 0, reason: "sma cross" };
  },
});
```

`state` は実行をまたいで D1 に保存される自由領域です (JSON 化できる値のみ)。

### 推奨フロー

1. `npm run backtest -- --bot my-bot` で検証
2. `deploy: ["paper"]` で main に push → paper Worker で実運用データで検証
3. ダッシュボードや日次レポートで成績を確認
4. 良ければ `deploy: ["paper", "live"]` にして push → 本番稼働 (GitHub Environment の承認を挟める)

## ローカルバックテスト

```bash
npm install
npm run backtest -- --bot sma-cross --from 2025-01-01 --to 2025-07-01
npm run backtest -- --bot all --days 90
npm run backtest -- --bot sma-cross --param fast=20 --param slow=100   # パラメータ上書き
npm run backtest -- --bot sma-cross --csv ./data.csv                  # 手持ちデータ
```

- 過去足は Binance の公開 API から取得し `data/` にキャッシュ
- 結果は標準出力と `reports/<id>.json`, `reports/<id>.html` (資産曲線) に出力
- CSV は `time,open,high,low,close,volume` (time は ms か ISO 文字列)

## Cloudflare のセットアップ (初回のみ)

```bash
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
型チェック → テスト → migration → paper デプロイ → live デプロイ を行います。

## 監視

各 Worker は毎分すべての bot について:

- 現在価格で資産を評価し、ピーク・日初資産を更新
- `risk` の上限 (最大ドローダウン / 日次損失 / 連敗数) を超えたら **停止**、`flattenOnHalt` なら成行で手仕舞い、Webhook に通知
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

## 開発

```bash
npm test             # ユニットテスト
npm run typecheck
npm run dev          # wrangler dev (paper)。http://localhost:8787/__scheduled で cron を発火
npx wrangler d1 migrations apply trade-bot-paper --env paper --local   # ローカル D1 の初期化
```

## 前提と制約

- 取引所は **Binance 現物** (ロングのみ・成行)。マーケットデータは `data-api.binance.vision`、
  発注は `api.binance.com` (`MARKET_DATA_URL` / `BINANCE_API_URL` で変更可)。
  他の取引所は `src/worker/brokers/types.ts` の `Broker` と `src/market/` を実装すれば差し替えられます
- live でも bot ごとの口座は D1 上の仮想口座 (`capital` を割り当て) として管理します。
  同じ取引所口座で複数 bot を動かせますが、口座の実残高とは照合しません
- 同じ確定足での二重発注は D1 の `claimCandle` と Binance の `newClientOrderId` で防いでいます
- cron は 1 分間隔なので、約定は足の確定から最大 1 分程度遅れます (バックテストは次足始値で約定)
