#!/usr/bin/env bash
# 把分享服务恢复到「当前仓库代码」，并清掉占着 8081 的残留进程。
#
# 背景：/files/ 由 systemd 的 cs-files.service 提供（ExecStart 指向本仓库 fileserver.py）。
# 历史会话里遗留的 python fileserver 进程会一直占着 8081，导致新进程 bind 失败、
# 对外仍是旧代码。这个脚本要在**宿主机 pid namespace** 里跑（沙箱内看不到那些进程）。
#
# 用法（宿主机）：
#   bash scripts/p21_restore_share.sh
# 从沙箱里触发（推荐，等同宿主机执行）：
#   busctl --system call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
#     org.freedesktop.systemd1.Manager StartUnit ss cs-files-restore.service replace
set -uo pipefail

DIR=/root/智云视频工作流
LOG=${LOG:-$DIR/logs/cs-files-restore.log}
mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
echo "===== $(date '+%F %T') restore start ====="

cd "$DIR" || { echo "✗ 找不到 $DIR"; exit 1; }
TOKEN=$(cat .share_token 2>/dev/null || true)
[ -n "$TOKEN" ] || { echo "✗ 缺 .share_token"; exit 1; }

echo "[1/4] 清理残留 fileserver 进程"
pgrep -af 'fileserver\.py' || echo "  （无）"
if [ "${1:-}" = "--check" ]; then
  echo "[check] 只看不杀；cs-files 状态：$(systemctl is-active cs-files 2>&1)"
  echo "[check] 8081 监听："
  ss -ltnp 2>/dev/null | grep -E ':8081\b' || echo "  （空闲）"
  exit 0
fi
pkill -f 'fileserver\.py' 2>/dev/null || true
sleep 1
if pgrep -f 'fileserver\.py' >/dev/null 2>&1; then
  echo "  仍在运行 → SIGKILL"
  pkill -9 -f 'fileserver\.py' 2>/dev/null || true
  sleep 1
fi

echo "[2/4] 同步 share/"
bash scripts/sync_share.sh >/dev/null || { echo "✗ sync_share 失败"; exit 1; }

echo "[3/4] 重启 cs-files"
systemctl daemon-reload
systemctl enable cs-files >/dev/null 2>&1 || true
systemctl restart cs-files
sleep 2
if ! systemctl is-active --quiet cs-files; then
  echo "✗ cs-files 未起来："
  systemctl status cs-files --no-pager | tail -20
  exit 1
fi

echo "[4/4] 健康检查"
code_root=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8081/$TOKEN/")
code_ver=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8081/$TOKEN/api/p2/version")
code_ui=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8081/$TOKEN/ui/")
echo "  /                  -> $code_root"
echo "  /api/p2/version    -> $code_ver"
echo "  /ui/               -> $code_ui"
curl -s "http://127.0.0.1:8081/$TOKEN/api/p2/version" | head -c 400; echo
if [ "$code_root" != 200 ] || [ "$code_ver" != 200 ] || [ "$code_ui" != 200 ]; then
  echo "✗ 健康检查失败，看看：journalctl -u cs-files -n 50"
  exit 1
fi
echo "✓ restore done（对外 https://123.207.74.155/files/$TOKEN/）"
