#!/usr/bin/env bash
# 把工作目录同步到分享目录（排除凭证与运行态），平板刷新即可看到最新文件
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p share/_incoming
#   dist/    部署运行时包（271M，脚本生成，不在 share 里没有任何用途）
#   web/     前端源码（手写原生 JS，见 web/README.md）。/ui/ 由 fileserver
#            直接读 web/dist，不经过 share/，同步过去纯属复制一份无用副本。
#   .git/    git 仓库（35M 历史）。share/ 是 Web 根目录，把版本库扔进去纯属
#            白占空间 + 每 10 分钟多复制 35M；nginx 也拦着点开头的路径。
rsync -a --delete \
  --exclude 'share' --exclude '__pycache__' --exclude '.compshare' \
  --exclude 'credentials.env' --exclude '.share_token' --exclude '*.pyc' \
  --exclude 'dsh.nginx.bak.*' \
  --exclude '/dist' --exclude '/web' \
  --exclude '.git' \
  ./ share/
echo "synced -> $(pwd)/share"
ls share
