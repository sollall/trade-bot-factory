# TRA-66: 国内取引所の新規取扱発表イベント — 計測コード

発表履歴（`data/jp_listings.toml`）と価格反応の実測を再現するためのコード。
公開API・公開ページの読み取りのみ。鍵・口座・発注は一切不要。

## 実行順

```
python3 build_events.py     # events.json を生成（発表履歴。一次情報から手で確定した値）
python3 run_measure.py      # 分単位のTが取れた発表を Binance 1分足で計測 → measurements.json
python3 run_dateonly.py     # 時刻不明バケットを日単位で計測 → measurements_dateonly.json
python3 stats.py            # 合否の線への当てはめ
python3 gen_toml.py         # data/jp_listings.toml を生成
```

## 注意

- `klines.py` は Binance `/api/v3/klines` を `raw/kl/` にキャッシュする。
- Hyperliquid の `candleSnapshot` は 1分足の履歴が直近3.5日程度しか返らない。
  過去イベントの分単位計測には使えないため、`measure.hl_series_1h` で1時間足にフォールバックしている。
- Binance の `status=BREAK` の銘柄（TON, MKR, OMG, RNDR 等）は1分足が一切返らない。
  「last price が返るから取引できる」ではないことに注意。該当銘柄は計測不能として扱う。
