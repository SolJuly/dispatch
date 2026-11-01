#!/usr/bin/env bash
# 一键发布：守卫 → 同步分享目录 → 重启 cs-files → 健康检查
# 只在部署树（/root/智云视频工作流）且 main 分支上可用。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN="$(cat "$DIR/.share_token" 2>/dev/null || echo '')"

bash "$DIR/scripts/repo-guard.sh"

echo "[1/3] 同步 share/ …"
bash "$DIR/scripts/sync_share.sh" >/dev/null
echo "      ok"

echo "[2/3] 重启 cs-files …"
systemctl restart cs-files
sleep 2
systemctl is-active --quiet cs-files
echo "      ok"

echo "[3/3] 健康检查 …"
code_root=$(curl -sk -o /dev/null -w '%{http_code}' "http://127.0.0.1:8081/${TOKEN}/")
code_api=$(curl -sk -o /dev/null -w '%{http_code}' "http://127.0.0.1:8081/${TOKEN}/api/p2/queue")
echo "      /            -> $code_root"
echo "      /api/p2/queue -> $code_api"
if [ "$code_root" != "200" ] || [ "$code_api" != "200" ]; then
  echo "✗ 健康检查失败，请查看 journalctl -u cs-files -n 50" >&2
  exit 1
fi
echo "✓ 发布完成：$(git -C "$DIR" rev-parse --short HEAD) on main"
