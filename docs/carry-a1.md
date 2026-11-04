# A① HL HYPE 現物ロング × perp ショート のシャドーモード計測 (TRA-22)

売買しない。注文系 API を呼ばない。秘密鍵も API キーも使わない。公開 info API の読み取りだけ。
`tbf/carry/` に閉じており、`tbf/funding/` (TRA-10 が常駐稼働中) と `tbf/monitor/` には触らない。
sqlite も TRA-10 の `data/funding.sqlite` とは別ファイル (`data/carry_a1.sqlite`)。

```
python -m tbf.carry check                 # @107 と HYPE perp に接続でき、板と履歴が取れるか確認
python -m tbf.carry run                   # 常駐して記録 (5分間隔)
python -m tbf.carry report --days 30      # 実現net年率・シャープ・最大DD・証拠金の推移
python -m tbf.carry csv                   # 日次 net を reports/carry/a1_daily.csv に書き出す
```

`run` は起動時に必ず `check` を通す。識別や手数料の食い違いに気づかないまま 30 日回すのを防ぐため。

## 資本規約 (★ 変更しない)

    C = N + N/3 = $10,000   (現物ロング N = $7,500 + perp 証拠金 $2,500)

**年率・シャープ・最大DD の分母は常に C。** 片脚名目 N で割った利回りは出さない。
`tbf/carry/config.py` が読み込み時に `N + N/3 == C` を検算し、崩れていれば起動しない。

「HL 現物残高が perp 証拠金に算入されるか」は未確定 (TRA-14 §8)。オーナー判断により
保守側 = **算入されない**前提で進める。したがって証拠金率は perp 口座だけで評価する:

    perp 自己資本 = N/3 + perp 含み損益 + 受取ファンディング − perp 脚の建玉コスト
    証拠金率     = perp 自己資本 / (枚数 × perp マーク価格)

この前提では、ベーシスが一切動かず現物側に同額の含み益が出ていても、**HYPE が建玉時より
+11.1% で証拠金率 20% 割れ、+27.0% で維持証拠金 5% に到達する**。C を現物 N と証拠金 N/3 で
使い切っているので、規約上は証拠金の追加に回せる現金が存在しない。

## 損益の定義 (依頼票のシャープ算出規約)

    net = 受取ファンディング累計 − ベーシス変動の評価損益 − 往復コスト(建玉時に一括計上)

2 脚合計の評価損益はベーシスの変化だけで決まる:

    現物ロング    = S × (spot_t − spot_0)
    perp ショート = S × (perp_0 − perp_t)
    合計          = −S × [(perp_t − spot_t) − (perp_0 − spot_0)]

シャープは日次リターン (分母 C) の 平均 ÷ 標準偏差 × √365、rf = 0。
標本 30 日は短いので、σ の標準誤差とシャープの標準誤差 (±1SE) を必ず併記する。

## 既知の罠 (このモジュールで閉じているもの)

| 罠 | どこで閉じたか |
|---|---|
| `spotMetaAndAssetCtxs` は universe 349 件 / assetCtxs 1188 件で長さが一致せず、位置 zip が静かにズレる (`universe[105].name = "@107"` / `assetCtxs[105].coin = "@105"`) | `hl.InfoClient.spot_ctx` が `coin` で結合。`check` が実際のズレを毎回表示する。回帰テストあり |
| `l2Book` は上位 20 段しか返さない | `Depth` が段数と合計金額を持ち、歩き切れなければ `avg_px = None` (= 未測定)。ゼロ埋めしない |
| `fundingHistory` は 1 レスポンス 500 件上限 | `hl.InfoClient.funding_history` がカーソルでページング |
| `candleSnapshot` は時間足ごとに最新 5000 本まで (`@107` の 1h 足に 180 日を指定すると null) | このモジュールは足を使わない |
| 表示価格 ≠ 約定可能価格 | コストは必ず板を歩いた平均約定価格から出す。最良気配は使わない |
| 集計済みの公式数値は信用しない | ファンディングは `fundingHistory` の 1 時間値を自分で積む。年率も自分で出す |
| ティッカーが一致しても別資産を指す (HL 現物 `@20` "PUMP" は perp の PUMP と 4,655 倍乖離) | `recorder.verify_identity` が `spotMeta` で `@107` = token 150 "Hyperliquid" / token 0 USDC を毎回確認 |

## 一次情報 (すべて POST https://api.hyperliquid.xyz/info、読み取りのみ)

| 項目 | リクエスト | 2026-11-04 時点の値 |
|---|---|---|
| `@107` の中身 | `{"type":"spotMeta"}` | `tokens: [150, 0]`、token 150 = HYPE / fullName "Hyperliquid" / tokenId `0x0d01dc56dcaaca66ad901c959b4011ec`、token 0 = USDC |
| 現物の価格・出来高 | `{"type":"spotMetaAndAssetCtxs"}` | `coin` で結合した `@107` の `markPx` / `midPx` / `dayNtlVlm` |
| perp の価格・ファンディング | `{"type":"metaAndAssetCtxs"}` | HYPE: `funding` 0.0000125/1h (年率 +10.95%)、`maxLeverage` 10、`marginTableId` 52 |
| 維持証拠金 | `{"type":"marginTable","id":52}` | "tiered 10x (2)": ≥$0 で 10x -> 当初証拠金 10%、維持証拠金はその半分 5% |
| 手数料 | `{"type":"userFees","user":"0x0000...0000"}` | `feeSchedule.cross` 0.00045 (perp taker 4.5bps) / `feeSchedule.spotCross` 0.0007 (現物 taker 7bps) |
| 板 | `{"type":"l2Book","coin":"@107"}` / `"HYPE"` | 上位 20 段 |
| ファンディング履歴 | `{"type":"fundingHistory","coin":"HYPE","startTime":...,"endTime":...}` | 1 時間値、1 レスポンス 500 件上限 |

`userFees` はゼロアドレスを照会しているだけで、残高にも署名にも関係しない。返る `feeSchedule`
は口座に依らない最下位 tier の表なので、これを一次情報として設定値と突き合わせている。

## レート制限

1 ポーリングの info 重みは `spotMetaAndAssetCtxs` 20 + `metaAndAssetCtxs` 20 + `l2Book` ×2 (各 2)
+ `fundingHistory` 20 = 最大 64。5 分間隔なので 1 分あたり 13 程度で、上限 1200/分 に対して余裕がある。
429/418 を受けたら `Recorder` がその回だけ間隔を 4 倍に伸ばす。
