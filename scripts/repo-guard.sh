#!/usr/bin/env bash
# 部署守卫：只有「主工作树 + main 分支 + 工作区干净」才允许发布/重启服务。
# 存在的理由：2026-11-01 两个会话共用一个工作树，一个 checkout + 一个 git add -A
# 就把后端提交送到了 UI 分支、把 UI 文件送进了后端提交。这条守卫把「发布」和「开发」分开。
set -euo pipefail

CANON="/root/智云视频工作流"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
branch="$(git -C "$here" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
dirty="$(git -C "$here" status --porcelain 2>/dev/null || true)"

if [ "$here" != "$CANON" ]; then
  echo "✗ 拒绝发布：当前工作树是 $here，部署树是 $CANON" >&2
  echo "  开发请在自己的 worktree 里做；发布只从部署树执行。" >&2
  exit 1
fi
if [ "$branch" != "main" ]; then
  echo "✗ 拒绝发布：部署树当前在分支 $branch，不是 main" >&2
  echo "  先 git checkout main 并合入要发布的改动。" >&2
  exit 1
fi
if [ -n "$dirty" ]; then
  echo "✗ 拒绝发布：工作区有未提交改动：" >&2
  echo "$dirty" | sed 's/^/    /' >&2
  exit 1
fi

echo "✓ 部署守卫通过：$here @ main（clean）"
