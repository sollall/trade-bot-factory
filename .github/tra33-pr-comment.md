この PR はエージェント（リーダー / TRA-33）が GitHub Actions 経由で開いています。
エージェントは GitHub の API トークンを持たず push しかできないため、この一度だけの
workflow（`agent/tra33-open-pr` ブランチ）から `GITHUB_TOKEN` で作成・コメント・マージを行います。

### マージ後に起きること

1. `Deploy` が走り、`check` → `paper` →（`live` は飛ばす）で緑になる
2. TRA-30 の警報『Cloudflare へのデプロイが通っていない』は
   「最後の Deploy 実行が success か」（`tbf_deploy_wf_last_ok`）を見ているので、
   取り込みを流せば収まる

### 残る見た目の問題（害なし）

5 回目の Deploy 実行（`live` の承認待ち）は `waiting` のまま一覧に残り、30 日で期限切れになる。
承認もキャンセルも GitHub の認証が必要なため、この PR では解消できない。
live を宣言した bot は 0 本なので、仮に誰かが承認しても注文は出ない。

### ラベルについて

`paper-trial` ラベルは付けないこと。付けた時点で `preview.yml` がお試し Worker を作り、
D1 を作り直して成績をリセットする。
