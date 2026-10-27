#!/usr/bin/env bash
# 把工作目录同步到分享目录（排除凭证与运行态），平板刷新即可看到最新文件
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p share/_incoming
rsync -a --delete \
  --exclude 'share' --exclude '__pycache__' --exclude '.compshare' \
  --exclude 'credentials.env' --exclude '.share_token' --exclude '*.pyc' \
  --exclude 'dsh.nginx.bak.*' \
  ./ share/
echo "synced -> $(pwd)/share"
ls share
