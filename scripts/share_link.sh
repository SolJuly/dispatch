#!/usr/bin/env bash
# 把指定文件同步到分享目录，并打印「预览 + 下载」两个链接。
#
#   bash scripts/share_link.sh reports/p2/P2-任务执行计划.md reports/p2/tasks.json
#   bash scripts/share_link.sh --table reports/p2/*.md      # 直接输出 Markdown 表格行
#
# 链接规则（由 fileserver.py 决定，别手拼）：
#   预览   {BASE}/{TOKEN}/f/{urlencoded relpath}
#   下载   {BASE}/{TOKEN}/f/{urlencoded relpath}?dl=1
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

BASE="https://123.207.74.155/files"
TOKEN="$(cat .share_token 2>/dev/null || true)"
[[ -n "$TOKEN" ]] || { echo "✗ 找不到 .share_token" >&2; exit 1; }

TABLE=0
[[ "${1:-}" == "--table" ]] && { TABLE=1; shift; }
[[ $# -gt 0 ]] || { echo "用法: bash scripts/share_link.sh [--table] <文件…>" >&2; exit 2; }

# 先同步，保证链接不是 404
bash scripts/sync_share.sh >/dev/null

python3 - "$TABLE" "$BASE" "$TOKEN" "$@" <<'PY'
import os, sys, urllib.parse
table, base, token, files = sys.argv[1] == "1", sys.argv[2], sys.argv[3], sys.argv[4:]
for f in files:
    rel = os.path.relpath(os.path.abspath(f), os.getcwd())
    if rel.startswith(".."):
        print(f"⚠ 不在仓库内，跳过：{f}", file=sys.stderr); continue
    q = urllib.parse.quote(rel)
    pre, dl = f"{base}/{token}/f/{q}", f"{base}/{token}/f/{q}?dl=1"
    if table:
        name = os.path.basename(rel)
        print(f"| `{name}` | [预览]({pre}) | [下载]({dl}) |")
    else:
        print(f"{rel}\n  预览 {pre}\n  下载 {dl}")
PY
