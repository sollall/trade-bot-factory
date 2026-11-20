# trade-bot-factory

`bots/` に Python で売買 bot を書いて push すると、Cloudflare Python Workers (paper / live) で動く。
構成と使い方は `README.md`、デプロイと運用は `docs/ops.md`、価格の取得元は `docs/market-data.md`。

## 話し方

シンプルに話す。結論を先に、短く。専門用語を並べず、普通の言葉で。日本語で。

## 守ること

- **main へのマージ = paper → live のデプロイ。** main に直接 push しない。PR を作るのは頼まれたときだけ。
  デプロイが動かないのは、文書・`.claude/`・`docs/`・`deploy/` だけの変更のとき (`deploy.yml` の `paths-ignore`)。
- **本番の操作は人間がやる。** `wrangler deploy` (dry-run 以外) / `rollback` / `secret`、D1 のリモート操作、
  `npm run deploy:*` / `db:migrate:*`、bot の停止・再開 (`/api/bots/<id>/halt`・`/resume`) は実行しない。
  必要だと思ったら、理由とコマンドを添えて伝える。
- **`deploy` に `"live"` を足すのは、人間が明示的に指示したときだけ。** 新しい bot は `["paper"]` まで。
- **歯止めの固定値は変えない。** コードに「採用基準の固定値。変更には人間の承認が必要」とある値
  (`*_carry_breaker` の閾値など) は、変えない・`params` に移さない・スイープしない。
- **本体は標準ライブラリだけ。** Cloudflare 上は Pyodide なので依存を足さない。bot が import してよいのは
  標準ライブラリと `tbf` だけ。
- **migration は追加だけ。** 列を消す・名前を変えるときの手順は `docs/ops.md`。
- **bot の削除・`deploy` から外す・instruments の別名の変更は、先に持ち高の手仕舞いが要る。**
  勝手にやらず、必要だと伝える。
- 鍵やトークンをコード・ログ・コミットに書かない。

## 確認

```bash
uv sync
uv run pytest
uv run python scripts/build_worker.py     # bot の読み込み・価格の取得元・live 対応のチェックも兼ねる
python -m tbf.backtest --bot <id> --days 180
```

バックテストと一部のテストは公開マーケットデータ API に出ていく。ネットワークが閉じた環境で落ちたら、
変更のせいか届かないだけかを切り分ける。「手元では通るのに Workers だけで落ちる」が何度も起きているので
(`docs/market-data.md`)、手元で確かめたことと未確認のことを分けて伝える。

## 仕事の振り分け (`.claude/agents/`)

小さな作業は自分でやる。まとまった作業や、独立した目が要るときに任せる。

| エージェント | 任せること |
|---|---|
| `strategist` | `bots/` に戦略を書く・変える、バックテストで一次評価 |
| `validator` | 候補を独立に疑って検証する。paper-trial に出す前と live に上げる前は必ず通す |
| `platform-engineer` | `tbf/`・`worker/`・`migrations/`・`scripts/`・CI の実装と修正 |
| `ops-investigator` | 動いている Worker の不調を読み取りだけで切り分ける |

bot を書いた本人 (自分や `strategist`) の報告だけで採否を決めない。採否の判断は人間がする。

## リポジトリに無いもの

コード中の「採用基準 §…」「ゲートA」「TRA-xx」が指す文書は、以前の作業環境にありここには無い。
中身を推測で補わない。必要なら人間に聞く。
