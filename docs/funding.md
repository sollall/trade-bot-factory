# DEX 間ファンディング・スクリーナー (シャドーモード)

複数の perp DEX のファンディングレートを年率に正規化して比べ、「会場 A でショート / 会場 B でロング」を
同額建てたときに、**手数料とスリッページを引いても黒字になる差があるか**を記録します。
**売買はしません。注文系 API は呼びません。秘密鍵も API キーも使いません。** 公開 API の読み取りだけです。

目的は、資金を入れる前に「公開記事に出ている年率 30% 級のファンディング差が、自分の執行条件で再現するか」を
実測することです。ポイントは評価額ゼロで扱います (ポイント目的の往復売買は手数料が確定コストなので期待値マイナス)。

## 仕組み

```
 会場ごとの poll_interval_sec で
 ┌─────────────┐
 │ hyperliquid │ 1h 建て  metaAndAssetCtxs + l2Book
 │ lighter     │ 8h 建て  funding-rates + orderBookDetails + orderBookOrders
 │ paradex     │ 8h 建て  markets/summary + orderbook
 │ dydx        │ 1h 建て  perpetualMarkets + orderbooks
 │ binance     │ 8h 建て  premiumIndex          ← 参照のみ (売買しない。ペアに入れない)
 └──────┬──────┘
        │  建てが会場ごとに違うので、まず年率に正規化する
        │     funding_apr = rate / interval_hours * 24 * 365
        ▼
 全ての (ショート会場, ロング会場) × 銘柄 × サイズ について
   gross_apr      = funding_apr(ショート側) - funding_apr(ロング側)
   entry_cost_bps = taker(A) + taker(B) + slip(A の買い板) + slip(B の売り板)
   exit_cost_bps  = taker(A) + taker(B) + slip(A の売り板) + slip(B の買い板)
   breakeven_days = (entry + exit) / (gross_apr * 10000 / 365)
   net_apr(hold)  = gross_apr - (entry + exit) / 10000 * 365 / hold
        ▼
 sqlite に記録: ファンディング / スプレッド (バケット集計) / エピソード / 稼働性 / 板不足
```

- **スリッページは向きを区別します。** 入るときショート脚は売り (買い板を食う)、ロング脚は買い (売り板を食う)。
  出るときは逆です。板は `$1,000 / $5,000 / $20,000 / $100,000` を成行で入れた場合の推定平均約定価格から bps にします
- **板が足りないサイズは評価しません。** 「食べられるところまでの平均」を返すとスリッページを小さく見せてしまうので、
  足りない場合は `depth_misses` に回数だけ記録します
- **エピソード**: `net_apr(14d)` が `min_net_apr` 以上だった状態が何秒続いたか。
  一瞬だけ出る大きな差は実際には建てられないので、継続時間を見ないと意味がありません
  (`tbf/arb/opportunity.py` の `EpisodeTracker` と同じ考え方)
- 会場が落ちている間に古い値で見せかけの機会が出ないよう、`max_stale_sec` より古いスナップショットは計算から外します

## ファンディングの建て (ここを間違えると 8 倍ずれる)

| 会場 | API | 建て | 確認方法 |
|---|---|---|---|
| hyperliquid | `metaAndAssetCtxs` の `funding` | 1h | `fundingHistory` が 1 時間刻み |
| lighter | `/api/v1/funding-rates` の `rate` | 8h | 下の検算 |
| paradex | `/v1/markets/summary` の `funding_rate` | 8h | **API が `funding_interval_minutes` を返すので `check` / `run` で設定と突き合わせる** |
| dydx | `/v4/perpetualMarkets` の `nextFundingRate` | 1h | - |
| binance | `/fapi/v1/premiumIndex` の `lastFundingRate` | 8h | `nextFundingTime` が 8 時間間隔 |

Lighter だけ 2 つの endpoint で建てが違うので検算しました (`tests/test_funding.py` で fixture に固定してあります):

- `/api/v1/funding-rates` の `rate` = `9.6e-05` (BTC)
- 同時刻の `/api/v1/fundings?resolution=1h` の `rate` = `"0.0012"`、`value` = `0.9232` (mark ≈ 76,500)
- `9.6e-05 / 8 = 1.2e-05` = `0.0012%` = `0.9232 / 76500` → **`funding-rates` は 8 時間建ての分数、`fundings` は 1 時間あたりの％**

正規化が合っているかの実地チェックは「**同時刻の全会場の年率が近い値に揃うか**」です。
揃わない会場があれば、まず `funding_interval_hours` の設定ミスを疑ってください。

## 使い方

```bash
python -m tbf.funding check                # 会場につながるか・記号が存在するか・建てが設定と合っているか
python -m tbf.funding run                  # 常駐して記録 (Ctrl-C で停止)
python -m tbf.funding report --days 7      # 会場ペアのランキング
```

設定は 2 つに分かれています:

| ファイル | 中身 |
|---|---|
| `funding.toml` | 銘柄・サイズ・保有期間・閾値・ポーリング間隔の既定・レポートの窓数 |
| `data/venues.toml` | 会場の一次情報 (エンドポイント・**建て**・手数料とその出所・ポーリング間隔・地域制限・ポイント規則) |

会場を増やすときは `data/venues.toml` に `[[venues]]` を足すだけです。`adapter` が既存のものなら Python の変更は不要。
新しい API 形式なら `tbf/funding/venues.py` の `ADAPTERS` にアダプタを 1 つ足します。
`FUNDING_URL_<会場名の大文字>` で `base_url` を上書きできます。

`reference_only = true` の会場 (海外 CEX など) はファンディングの水準を読むだけで、板も取らず、会場ペアにも入りません。

### レート制限

`poll_interval_sec` は会場ごとに設定でき、既定は保守的な 15 秒です (1 ポーリングあたり 3〜4 リクエスト)。
HTTP 429 / 418 を受けた会場は、その会場だけ一時的に間隔が 4 倍に伸びます。BAN されると計測が止まるので、
間隔を詰めるときは会場ごとの公開上限を確認してください。

## 常駐させる

```bash
# 手元で試す
nohup python -m tbf.funding run > logs/funding-shadow.log 2>&1 &

# VPS (deploy/setup-server.sh と同じ流儀)
sudo cp deploy/funding-shadow.service /etc/systemd/system/
sudo systemctl enable --now funding-shadow
```

`arb-shadow` と同じサンドボックス設定です。秘密鍵も API キーも持たないので、`EnvironmentFile` は Webhook だけ。

## 結果の読み方

`report` の各節:

0. **計測期間とカバー率** — バケットの欠けはそのまま取りこぼしです。カバー率が低い結果は水準の推定として弱い
1. **会場ペア別ランキング** — `net_apr(14d)` 降順。`粗 min/max` が符号を跨いでいたら、その差は安定していません
2. **保有期間別** — 往復コストは固定なので、短く持つほど不利になります。7 日で赤字なら 7 日で解消する運用はできません
3. **銘柄別・サイズ別** — サイズを上げると net が落ちます。落ち方が急な会場は板が薄い
4. **稼働性** — エラー率・p50/p95・429 到達。落ちている時間が長い会場は実運用で片脚だけ残るリスクがある
5. **符号反転** — 反転するとショート側が受け取りから支払いに回ります。解消ルールの設計に必要
6. **ウォークフォワード** — 下記
7. **板不足** — そのサイズを板で約定できなかった回数

### 全期間の平均を見ないこと

全期間を 1 回通した平均は「どのペアが良かったか」を後から知っている前提なので、実運用の見込みにはなりません。
`report` は期間を `walkforward_windows` 個の窓に分け、**前の窓で最良だったペアを次の窓で持った場合 (OOS)** と、
**その窓を後から見た最良ペア (hindsight)** を 1 行ずつ並べます。`効率 = OOS / 後見最良`。

効率が低い、あるいは OOS が黒字の窓が少ない場合、「最良ペアの年率が高かった」ことには意味がありません。
ペアの入れ替えが必要だと分かるのは次の窓に入ってからなので、実際に取れるのは OOS の側です。

## 実際に資金を入れる前に

このフェーズでは資金を入れません。進める場合でも、少なくとも:

- [ ] 手数料を `fee_source` が `doc` の会場について、自分の口座の tier で実測に差し替える
- [ ] 片脚だけ約定した場合の扱い (どちらを先に出すか、失敗時に残る方向リスク) を決める
- [ ] 符号反転と清算の両方に解消ルールを用意する (`liquidation_bps` は `venues.toml` にある)
- [ ] 地域制限 (`region_restricted`) と資金の出入りの経路を確認する
- [ ] ポイントは評価額ゼロで損益を計算する (遡及没収の実例があるため)
