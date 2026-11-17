# 価格データの取得元

バックテストと Worker (paper / live) が「どこの価格を見ているか」と、その選択の理由。
TRA-81 で paper の 6 bot が全滅した件の調査結果をまとめてある。

## 前提: 発注先と価格の取得元は別

`Instrument` は 2 つの会場を持つ。

| | 意味 | 例 |
|---|---|---|
| `venue` | **発注先**。手数料・スリッページ・本番対応の判定に使う | `gmo` (国内の現物) |
| `price_venue` | **価格・足・ファンディングの取得元**。`price_source` で上書きでき、省略時は `venue` | `binance` |

`tbf/worker/runner.py` の `fetch_prices` / `tick` と `tbf/backtest/data.py` の `load_bot_data` は
どちらも `price_venue` / `price_symbol` しか見ない。国内業者 (gmo / bitbank) の板は JPY 建てで
`tbf/market/` に JPY 足の取り込みがないため、現物レッグは「国内で建てて、価格は海外の USDT 建てで見る」
という組み合わせになっている。

エラー文の `no price for BTCUSDT:spot@gmo(limit)` は `Instrument.__str__` が出すもので、
**`venue` (発注先) を表示している**。価格の取得元ではない。TRA-81 ではここを読み違えて
「gmo の価格取得が未実装」と診断していたが、実際には全 bot の `price_venue` は
`binance` か `hyperliquid` で、未実装の取得元は 1 件もなかった。

デプロイ前に `scripts/build_worker.py` の `price_errors()` が
全 bot の `price_venue` を `tbf.market.MARKET_VENUES` と突き合わせるので、
取得元のない bot はビルドで落ちる。

## Cloudflare Workers から Binance は使えない

TRA-81 の計測。使い捨ての Worker を同じアカウントに置き、**cron 経路から** 毎分叩いた
(fetch ハンドラから測ると colo が自分の最寄りに固定されてしまい、本番の当たり方にならない)。

| ホスト | Workers から | 手元から |
|---|---|---|
| `data-api.binance.vision` | **403** (全 colo) | 200 |
| `api.binance.com` / `api1` / `api2` / `api3` | **403** | 200 |
| `api-gcp.binance.com` | **200 と 451 がばらつく** | 200 |

- `data-api.binance.vision` と `api.binance.com` 系は Cloudflare の出口IPを**一律 403** で弾く。
- `api-gcp.binance.com` だけは通ることがあるが、通らないときは
  **451 Unavailable For Legal Reasons** (地域制限) を返す。

`api-gcp` を既定にして paper にデプロイして実測したところ、
**約 47% の tick が「Binance の全ホストで取得失敗」**になった
(18:20〜18:37 の 19 tick 中 9 tick)。cron がどの colo で動くかは選べないので、
ホストを変えても順番を変えても解決しない。

> **結論: Binance は Workers から安定して使えない。**
> ローカル (バックテスト) からは問題なく 200 で取れるので、
> 「バックテストは通るのに本番だけ毎 tick 落ちる」という形で表に出る。

なお `api-gcp` の中身は `data-api.binance.vision` と同一である
(1h 足 1000 本を突き合わせて完全一致、ticker も同値)。品質の問題ではなく到達性だけの問題。

## 乗り換え候補の実測

### 到達率 (cron 経路・colo 別)

使い捨ての Worker を毎分の cron で回し、13 tick ぶんを colo つきで記録した。
出てきた colo は MAD / KIX / AMS / LHR (非US) と SJC / DFW / ATL / IAD / DEN (US)。

| 取得先 | 全体 | **US colo** | 非US colo | 失敗の内容 |
|---|---|---|---|---|
| Binance (`api-gcp`) | 7/13 | **0/6** | 7/7 | 451 (地域制限) |
| Binance (`data-api...vision`) | 0/13 | 0/6 | 0/7 | 403 (CF 出口IPを一律拒否) |
| Bybit 現物 | 7/13 | **0/6** | 7/7 | 403 |
| OKX 現物 | 6/13 | 4/6 | 2/7 | 429 (共有IPの流量制限) |
| **Coinbase 現物** | **13/13** | **6/6** | **7/7** | — |
| Kraken 現物 | 13/13 | 6/6 | 7/7 | — |
| Hyperliquid (perp) | 10/13 | 4/6 | 6/7 | 429 ×3 |
| GMO 現物 | 13/13 | 6/6 | 7/7 | — |

**Binance と Bybit は US の colo からだけ、きれいに全滅する。** 非US の colo では 7/7 通る。
米国から使えない取引所なので、Cloudflare の US colo で cron が動いた回は必ず落ちる。
サンプル中 US colo は 6/13 (46%) で、paper worker で実測した失敗率 47% と一致する。

- OKX は地域ではなく **429** (Cloudflare の共有出口IPが他の利用者と合算されて流量制限に当たる)。
  地域に関係なく落ちるので、こちらの方が厄介。
- Hyperliquid も 13 回中 3 回 429 が出た。**perp レッグはいまも Hyperliquid なので、
  これは既存の 4 bot に効いている潜在リスク**。単独の取得元にするのは心細い。
- Coinbase / Kraken / GMO は 13/13。米国で規制を受けて営業している事業者と国内業者は
  US colo を弾く理由がないので、筋は通っている。

### 価格の近さ (基準 = Binance 現物、1h 足・直近 1000 時間)

「水準」は基準に対する差の平均。「年率σ」は 1h リターンの差の標準偏差を年率換算したもので、
**戦略の成績に効くのはこちら** (水準のずれは建値が一律ずれるだけ)。

| 取得元 | BTC 水準 | BTC 年率σ | ETH 水準 | ETH 年率σ | 相関 |
|---|---|---|---|---|---|
| **Bybit 現物 (USDT)** | −0.47 bps | **0.77%** | −0.14 bps | **0.85%** | 0.9998 |
| OKX 現物 (USDT) | −0.33 bps | 0.95% | −0.10 bps | 0.94% | 0.9998 |
| Coinbase 現物 (USD) | −12.63 bps | 1.26% | −12.10 bps | 1.37% | 0.9995 |
| Hyperliquid perp (mid) | −3.49 bps | 1.29% | −4.28 bps | 1.54% | 0.9995 |
| GMO 現物 (JPY, 為替未変換) | — | 7.88% | — | 9.91% | 0.98 |

比較の基準として、すでに受け入れている国内外価格差の近似は年率σ 0.94% (TRA-50 §1-5)。
Bybit / OKX はそれより小さく、Coinbase / Hyperliquid は同じ桁。

### バックテストに必要な過去データ

バックテストの既定は `--days 180` (`tbf/backtest/__main__.py`)。

| 取得元 | 1h 足の遡れる深さ | 1 回の最大本数 | 対応 timeframe |
|---|---|---|---|
| Binance | 数年 | 1000 | 7 種すべて |
| Bybit | 2 年以上 | 1000 | 7 種すべて |
| OKX | 2 年以上 | 300 | 7 種すべて |
| **Coinbase** | **2 年以上** | 350 | 1m・5m・15m・1h・1d (**30m・4h が無い**) |
| Hyperliquid | **約 208 日** (5000 本上限) | 5000 | 7 種すべて |
| Kraken | **約 30 日** (720 本) | 720 | — |

Coinbase で 180 日ぶんの 1h 足を実際に取ると、**15 リクエストで 4320 本 (必要数の 100%)**。
15m 足も 2 年前の 1h 足も取れる。現在価格は銘柄ごとに 1 リクエスト、
または `/products/stats` で 516 銘柄まとめて 1 回。
いまの bot が使う timeframe は 1h と 15m だけなので、30m・4h が無い点は現状では効かない。

- Hyperliquid は 180 日の既定はぎりぎり足りるが、余裕が約 28 日しかない。
  perp レッグはいまも Hyperliquid なので、この上限は既に 4 bot に効いている。
- Kraken は過去データが浅すぎてバックテストに使えない (`since` を渡しても直近 720 本しか返らない)。
- GMO は `klines` が**日付単位** (`date=20261115`) なので 180 日ぶんで銘柄あたり約 180 リクエスト。
  さらに JPY 建てなので、USDT 建ての口座計算に乗せるには**1 時間足の USDJPY** が別に必要になる
  (`api.frankfurter.app` は Workers から 200 だが日次更新で粗い)。為替を変換しないままだと
  年率σ 8〜10% のぶれが乗る。

### 実装上の注意

- Bybit の `/v5/market/kline` は **新しい順** に返す。`start` と `end` の両方を渡すと
  その範囲の**最も新しい** 1000 本が返るので、ページングは `end` を遡らせる方向で書く
  (Binance の `startTime` を進める書き方とは逆)。
- Bybit の現在価格は `/v5/market/tickers?category=spot` で全銘柄 (510) が 1 回で取れる。
- どの取得元も「進行中の足」を含めて返すので、`fetch_recent_closed` と同じ
  `c.time + tf <= now` の絞り込みが必要。

## 決めたこと: 現物は Coinbase、無期限先物は Hyperliquid

2026-11-17 にオーナーが①(Coinbase に移す)を選択。到達率 100% と 180 日の履歴の両方を
満たす取得元が Coinbase だけだったため (上の表)。

| | 価格・足の取得元 | 実装 |
|---|---|---|
| 現物 (binance / gmo / bitbank で建てるレッグ) | **Coinbase** (`BTC-USD` / `ETH-USD`) | `tbf/market/coinbase.py` |
| 無期限先物 | Hyperliquid (`BTC` / `ETH`) | `tbf/market/hyperliquid.py` |

全 bot の現物レッグに `price_source=("coinbase", "BTC-USD" | "ETH-USD")` を書いた。
**発注先 (`venue`) は変えていない** ので、手数料・スリッページ・本番対応の判定は今までどおり。

### 取得元を替えてバックテストはどう変わったか

同じ 180 日 (2026-05-21〜2026-11-17)・同じ設定で、価格だけ差し替えて比較した結果
(`差` = Coinbase − Binance)。

| bot | 年率 (Coinbase) | 年率 (Binance) | 差 | DD の差 | シャープの差 | 決済回数の差 |
|---|---|---|---|---|---|---|
| btc_carry_breaker | +5.61% | +5.58% | +0.03pt | +0.03pt | −0.78 | 0 |
| eth_carry_breaker | +5.29% | +5.23% | +0.06pt | +0.02pt | −0.44 | 0 |
| eth_funding_hedged | +4.70% | +4.63% | +0.06pt | +0.02pt | −0.37 | 0 |
| eth_funding_carry | −3.14% | −3.00% | −0.14pt | +0.08pt | +0.05 | 0 |
| rsi_reversion | −18.47% | −18.48% | +0.02pt | −0.02pt | −0.01 | 0 |
| sma_cross | −19.38% | −18.68% | −0.70pt | +0.52pt | −0.03 | +1 |

- **年率はほぼ動かない** (最大 0.70pt、キャリー系は 0.1pt 未満)。売買の回数もほぼ同じ。
- 動くのは**シャープ** (キャリー系で −0.4〜−0.8)。価格の近さの表にある年率σ 1.3% のぶれが
  そのまま分母に乗るためで、向きの偏りではない。採否の判断は年率と DD で見るので、
  この差で結論が変わるものはなかった。
- バックテストと Worker は同じ `default_markets()` = 同じ Coinbase を使うので、
  **この差は「過去の記録との比較」にだけ現れ、本番とバックテストの間には残らない**。

### Coinbase を使う上での制約 (コードに歯止めを入れた)

- **30m・4h 足が無い** (1m / 5m / 15m / 1h / 1d だけ)。`scripts/build_worker.py` の
  `price_errors()` が bot の timeframe を突き合わせ、無い足を使う bot はデプロイ前に落ちる。
- **1 リクエスト 300 本まで** (超えると 400)。`fetch_candles` が 300 本ずつに区切って取る
  (180 日の 1h = 4320 本で 15 リクエスト)。返りは**新しい順**なので古い順に並べ直す。
- **`/candles` は `User-Agent` が無いと 400** (`{"message":"User-Agent header is required."}`)。
  `/ticker` は無くても通るので、**確定足の tick だけが落ちる**という形で出る。
  手元の `urllib` は既定で付けるのでバックテストでは再現せず、Cloudflare の `fetch` は
  付けないので本番だけ落ちた。`tbf/http.py` の `with_user_agent()` を両方の実装が通すようにし、
  `tests/test_entry.py` の fake fetch も本物と同じく User-Agent の無い要求を 400 にした。
- 現在価格は銘柄ごとに 1 リクエスト (`/products/<id>/ticker`)。いまは 2 銘柄なので毎 tick 2 回。
  銘柄が増えたら `/products/stats` (全銘柄 1 回) に替える。
- USD 建てなので USDT 建ての Binance とは水準が −12bps ずれる (上の表)。
  バックテストも Coinbase なので、ずれは建値の水準としてだけ存在し、両者の間には残らない。

## 流量制限 (429) への備え

Hyperliquid は実測で 13 回中 3 回 429 を返した (Cloudflare の出口IPは他の利用者と共有で、
自分が毎分 1 回しか叩いていなくても踏む)。これは perp レッグを持つ 4 bot に効くので、
`tbf/http.py` の `request_retrying()` で **429 / 5xx は 0.25 秒・1 秒・2.5 秒 待って取り直す** (計 4 回)。
Coinbase と Hyperliquid の両方がこれを通る。403 / 451 (地域制限) は待っても直らないので取り直さない。

### 同じ足を重ねて取らない (2026-11-17 13:00 UTC の実測から)

Coinbase に移したあと、**1 時間足が確定する tick だけ** 6 bot 中 2 本が落ちた:

```
GET https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600&...
  -> HTTP 429 {"message":"Public rate limit exceeded"}
```

毎分の tick は現在価格 2 回だけなので通るが、確定足の tick では全 bot が足を取りにいく。
ETH-USD の 1 時間足は 4 bot が、BTC-USD は 2 bot が**それぞれ別に**取っていたので、
同じ内容のリクエストが短時間に重なり、共有出口IPの余裕を超えた。
取り直し (当時 0.25 + 1.0 秒) でも収まらなかった。

対策は取り直しではなく**投げる回数を減らすこと**。`tbf/market/cache.py` の `CachingMarket` が
1 tick のあいだ同じ (銘柄, 足) の取得を 1 回に畳む。tick の最初に bot の定義から
「この tick で誰かが必要とする最大本数」を数えておき (`candles_needed`)、最初に欲しがった bot の
ところでその本数をまとめて取る。あとの bot は手元の足の新しい側を切り出して使うので、
**bot の順番によって回数が変わらない**。包みは `per_tick_markets()` で tick ごとに作り直すため、
tick をまたいで古い足が残ることはない。現在価格とファンディングも同じ tick 内では 1 回。

これで確定足の tick の Coinbase への足のリクエストは **6 回 → 2 回** (銘柄の数) になる。
歯止めは `tests/test_worker.py::test_bots_sharing_a_candle_fetch_it_once_per_tick`
(包みを外すと落ちる) と `tests/test_market_data.py` の 5 節。

### それでも 429 は出る前提で、足の判断を落とさない

畳んだあとの 13:15 UTC の 15 分足でも 1 本 (`rsi_reversion`) が 429 で落ちた。
このときのリクエストは ETH-USD の 15 分足 1 回だけで、取り直しも 4 回している。
**Coinbase の公開APIの余裕は自分の流量と無関係に決まる** (同じ出口IPを使う他の
Cloudflare 利用者と合算される) ので、こちらの回数をどれだけ減らしても 0 にはできない。

問題は 429 そのものより、落ちたときに**その足の判断が永久に飛ぶ**ことだった。
`store.claim_candle()` は二重発注を防ぐために足の処理権を先に取る (`last_candle` を進める) ので、
そのあとの取得で落ちると、同じ足はもう処理されない。1 時間足なら 1 回の売買判断が丸ごと消える。

`tbf/worker/runner.py` はこう直してある。

- 発注より前 (ファンディング・足の取得) で落ちたら **処理権を戻す** (`store.release_candle()`)。
  次の tick (1 分後) に同じ足をやり直すので、判断は最大 1 分遅れるだけで失われない。
  遅れは `execution_quality` の「発注までの時間」に出る。
- その失敗は `CandleRetry` として **`last_tick_errors` には数えず warn イベントだけ残す**
  (取り逃しではないため)。Slack にも流さない。
- **次の足が確定してもまだ取れていない**ときは普通のエラーにする (= `last_tick_errors` に数え、
  Slack に流す)。黙って遅れ続けない。

歯止めは `tests/test_worker.py` の
`test_a_candle_that_could_not_be_fetched_is_retried_on_the_next_tick` と
`test_a_candle_missed_for_a_whole_period_is_reported_as_an_error`。

## いまの設定

| 取得元 | 既定のホスト | Worker の上書き |
|---|---|---|
| Coinbase (現物) | `https://api.exchange.coinbase.com` | `COINBASE_URL` |
| Hyperliquid (先物) | `https://api.hyperliquid.xyz/info` | `HYPERLIQUID_URL` |

`tbf/market/binance.py` は**手元での比較・調査用に残してある** (`MARKET_DATA_HOSTS` を
先頭から試す。先頭は Workers から唯一通ることがある `api-gcp.binance.com`)。
`tbf.market.UNREACHABLE_FROM_WORKERS` に入れてあるので、bot の価格の取得元にすると
`scripts/build_worker.py` がデプロイ前に落とす。

バックテストと Worker は同じ `default_markets()` を使うので、既定では**同じ取得元・同じホスト**になる。
`data/` のキャッシュ名には取得元が入る (`coinbase_spot_BTC-USD_1h_...`) ので、
取得元を変えてもキャッシュは混ざらない。

テストは `tests/test_market_data.py` (上の 1〜5 を固定)。
