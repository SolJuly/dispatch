#!/usr/bin/env bash
# 安全重启文件服务（share/fileserver.py）。
#
# ## 为什么需要这个脚本
#
# 2026-11-11 23:47，我用 `pkill -f "fileserver.py"` 重启服务，**误杀了正在跑的作业**。
#
# 事故链条：
#   1. `pkill -f <模式>` 匹配的是**完整命令行**，不是进程名。
#   2. 我的每次工具调用都是 `bash -c "<整条命令>"`，而那条命令里写着
#      `pkill -f "fileserver.py"` —— 于是**这个 bash 自己也被匹配上**并收到 SIGTERM。
#   3. 更糟的是它不止杀自己：凡是命令行里含这串字的进程都会中枪
#      （包括 `grep fileserver.py`、包括任何带这个参数的编辑/测试进程）。
#   4. 那一次，正在跑的作业 p1-20261111T2326-f76311（已推理 1095 秒、花了 ¥0.65）
#      收到 SIGTERM 退出；它的信号兜底把实例定在 10 分钟后关机，
#      到 23:57 才真正停机，白烧了 ¥1.08 且任务半途而废。
#
# ## 正确做法
#
# 按 **PID + 精确路径** 定位，绝不用 `pkill -f`：
#   · `pgrep -f` 同样有误伤问题，所以用 `ps` 找出 args 完全等于目标的那一个
#   · 杀之前确认它真的还在（避免杀到刚重启的新进程）
#   · 新进程用 setsid 独立会话，与当前 shell 解耦
#
# 用法：
#   bash scripts/restart_fileserver.sh          # 重启
#   bash scripts/restart_fileserver.sh --status # 只看状态
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${SHARE_PORT:-8081}"
LOG="$DIR/logs/fileserver.log"
PY="$(command -v python3)"

# 精确匹配：找出所有 `... fileserver.py` 的 python 进程。
#
# 不用 `pkill -f` / `pgrep -f`：它们匹配**完整命令行**，会把"命令行里带这串字"
# 的一切都算上（包括正在执行重启命令的 shell 本身）。2026-11-11 23:47 就是这么
# 误杀了一个正在推理的作业。
#
# 也不用"args 完全相等"的严格比较：实测进程的 cmdline 是相对路径
# （`python3 fileserver.py`，因为启动时 cwd 就是仓库根），而脚本里算的是
# 绝对路径 —— 严格相等反而匹配不到，会重复启动撞 `Address already in use`。
#
# 判据改为：① 第一个参数是 python*；② 最后一个参数的文件名是 fileserver.py；
#           ③ 该进程的工作目录（/proc/<pid>/cwd）就是本仓库。
find_pids() {
  local pid exe cwd
  for pid in $(ps -eo pid=,args= | awk '/[p]ython.*fileserver\.py/ {print $1}'); do
    [ -d "/proc/$pid" ] || continue
    exe="$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | head -1 || true)"
    case "$exe" in
      *python*) ;;
      *) continue ;;
    esac
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    if [ "$cwd" = "$DIR" ]; then
      echo "$pid"
    fi
  done
}

show_status() {
  local pids
  if systemctl list-unit-files 2>/dev/null | grep -q '^cs-files\.service'; then
    echo "systemd cs-files: $(systemctl is-active cs-files 2>/dev/null || true)"
  fi
  pids="$(find_pids || true)"
  if [ -z "$pids" ]; then
    echo "文件服务：未运行"
    return 1
  fi
  echo "文件服务：运行中（PID $(echo "$pids" | tr '\n' ' ')）"
  ss -ltnp 2>/dev/null | grep ":$PORT " || echo "  ⚠ 端口 $PORT 未监听"
}

if [ "${1:-}" = "--status" ]; then
  show_status
  exit 0
fi

# ---- 有 systemd 单元就交给 systemd（2026-11-12 修）----
#
# 为什么必须优先走 systemctl：本脚本下半段是"杀掉旧进程 + setsid 起一个新的"。
# 如果 cs-files.service 也在管同一个端口，就变成**两个进程抢 8081**：
#   systemd 的进程绑不上 → 每 2 秒重启一次 → 一天崩 3 万多轮
#   而本脚本起的那个孤儿（PPID=1）占着端口，看起来"服务是好的"
# 真机踩到：`systemctl is-active cs-files` 是 activating(auto-restart)，
# journal 里刷满 `OSError: [Errno 98] Address already in use`，
# 而站点因为孤儿进程还活着所以"看起来正常"——这种假象最危险。
if systemctl list-unit-files 2>/dev/null | grep -q '^cs-files\.service'; then
  echo "== 用 systemd 重启（cs-files.service）=="
  systemctl restart cs-files
  sleep 2
  systemctl is-active --quiet cs-files || {
    echo "✗ cs-files 起不来，看 journalctl -u cs-files -n 50"; exit 1; }
  TOKEN="$(cat "$DIR/.share_token" 2>/dev/null || echo '')"
  if [ -n "$TOKEN" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
            "http://127.0.0.1:$PORT/$TOKEN/api/p2/version" || echo 000)"
    echo "  /api/p2/version → $code"
    [ "$code" = "200" ] || { echo "✗ 接口不通"; exit 1; }
  fi
  echo "✓ 完成（systemd）"
  exit 0
fi

echo "== 没有 systemd 单元，走独立进程 =="
echo "== 停旧进程（按 PID，不用 pkill -f）=="
pids="$(find_pids || true)"
if [ -z "$pids" ]; then
  echo "  没有在跑的文件服务"
else
  for pid in $pids; do
    echo "  kill $pid"
    kill "$pid" 2>/dev/null || true
  done
  # 等它真的退出，最多 5 秒；**只对这些具体 PID** 用 -9，绝不用模式匹配
  for i in $(seq 1 10); do
    still="$(find_pids || true)"
    [ -z "$still" ] && break
    sleep 0.5
  done
  still="$(find_pids || true)"
  if [ -n "$still" ]; then
    echo "  5 秒未退出，强制结束：$still"
    for pid in $still; do kill -9 "$pid" 2>/dev/null || true; done
    sleep 1
  fi
fi

echo "== 启动新进程 =="
mkdir -p "$DIR/logs"
# setsid：脱离当前会话，这样当前 shell 结束/被信号打断时它照常活着
setsid nohup env SHARE_ROOT="$DIR/share" SHARE_PORT="$PORT" \
  "$PY" "$DIR/fileserver.py" >> "$LOG" 2>&1 < /dev/null &
sleep 3

echo "== 校验 =="
show_status || { echo "✗ 启动失败，看 $LOG"; tail -20 "$LOG"; exit 1; }

TOKEN="$(cat "$DIR/.share_token" 2>/dev/null || echo '')"
if [ -n "$TOKEN" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
          "http://127.0.0.1:$PORT/$TOKEN/api/p2/version" || echo 000)"
  echo "  /api/p2/version → $code"
  [ "$code" = "200" ] || { echo "✗ 接口不通"; exit 1; }
fi
echo "✓ 完成"
