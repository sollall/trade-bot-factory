TRA-33。live を宣言した bot が 1 本も無いのに Environment `live` の承認待ちで Deploy 全体が止まるのを直します。

## 変更

- `check` が live に出す bot 数を数えて出力し、**0 本なら `live` ジョブを飛ばす**
  （`scripts/deploy_guard.py count`。出所は `bots/*/bot.py` の `deploy` 指定だけ）
- **承認の関門は残る**: live の bot が 1 本でもあれば、今までどおり承認待ちになる
- `concurrency` の group をブランチ名入り（`deploy-main`）に変更。古い group（`deploy`）で
  承認待ちのまま残っている実行に、後続のデプロイが詰まらないようにする

## なぜ

`bots/` の 6 本はすべて `deploy=["paper"]`。それでも `deploy.yml` は毎回 `live` の段に進み、
Required reviewers の承認待ちで実行が `waiting` のまま終わらない。中身が空の Worker を
1 本置くための承認を人に求めている状態で、その間 `concurrency: group: deploy` により
後続の paper デプロイも列に並んだままになる。

## 検証

- `uv run pytest` → **146 passed**（`tests/test_ops.py::test_count` を追加）
- `scripts/deploy_guard.py count --mode live` = **0** / `--mode paper` = **6**
- `deploy.yml` を YAML として解析し、`live` ジョブの `if` と `needs` を確認

## 影響

マージ後の Deploy は `check` → `paper` →（`live` は飛ばす）で緑になる。
live 側の D1（`trade-bot-live`）は作成済み・migration 0001〜0004 適用済みで、
この PR では触らない。
