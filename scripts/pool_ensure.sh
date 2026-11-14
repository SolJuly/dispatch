#!/usr/bin/env bash
# 预热池 ensure 包装：给 cron 用。
#
# 为什么需要一层包装：
#   · cron 的 PATH 很短，python3 要用绝对路径
#   · ensure 失败（无货 / 配额满 / 网络抖动）不能让 cron 报错刷日志，
#     这里吞掉非零退出，只把输出写日志
#   · 加 flock，避免上一轮 ensure 还在建实例时被下一轮重复触发
#
# 用法（cron）：
#   */15 * * * * /root/智云视频工作流/scripts/pool_ensure.sh
#
# 手动执行也可，参数透传给 pool.py ensure：
#   scripts/pool_ensure.sh --size 2 --gpu 4090
set -uo pipefail

CANON="/root/智云视频工作流"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POOL="$HERE/tools/p2/pool.py"
LOG="$HERE/logs/pool.log"
LOCK="/tmp/dsh-pool-ensure.lock"

mkdir -p "$(dirname "$LOG")"

# 只在部署树跑（与 repo-guard.sh 同一口径）：worktree 里跑 ensure 会用
# 不同 cwd 的 pool.json，导致池记录分裂成两份。
if [ "$HERE" != "$CANON" ]; then
  echo "[$(date '+%F %T')] skip：当前树是 $HERE，部署树是 $CANON" >> "$LOG"
  exit 0
fi

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date '+%F %T')] skip：上一轮 ensure 仍在运行" >> "$LOG"
  exit 0
fi

{
  echo "===== $(date '+%F %T') pool ensure $* ====="
  /usr/bin/python3 "$POOL" ensure "$@" 2>&1
  rc=$?
  echo "----- rc=$rc -----"
} >> "$LOG" 2>&1

# 池子补货失败（无货、配额满）是常态，不当作错误退出，免得 cron 每小时发一封失败邮件
exit 0
