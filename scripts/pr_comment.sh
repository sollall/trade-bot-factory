#!/usr/bin/env bash
# PR のお試し Worker のコメント (先頭に <!-- tbf-preview --> がある 1 件) を読み書きする。gh と GH_TOKEN が必要
#   scripts/pr_comment.sh get <pr> > comment.md      # なければ空
#   scripts/pr_comment.sh put <pr> body.md           # あれば更新、なければ作成
set -euo pipefail
cmd="$1"; pr="$2"; repo="${GITHUB_REPOSITORY:?}"
id=$(gh api "repos/$repo/issues/$pr/comments" --paginate \
      --jq '.[] | select(.body | startswith("<!-- tbf-preview -->")) | .id' | head -n1)
case "$cmd" in
  get) [ -n "$id" ] && gh api "repos/$repo/issues/comments/$id" --jq .body || true ;;
  put)
    if [ -n "$id" ]; then gh api -X PATCH "repos/$repo/issues/comments/$id" -F "body=@$3" >/dev/null
    else gh api -X POST "repos/$repo/issues/$pr/comments" -F "body=@$3" >/dev/null; fi ;;
  *) echo "usage: $0 get|put <pr> [file]" >&2; exit 2 ;;
esac
