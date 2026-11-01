#!/usr/bin/env bash
# 装「空闲自动关机」守护：每 5 分钟体检一次，GPU 闲置 15 分钟就关机（关机不收算力费）
# 用法: bash scripts/install_cron.sh [闲置分钟数]   # 默认 15
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IDLE_MIN="${1:-15}"
LOG="$DIR/.compshare/watch.log"
PY="$(command -v python3)"
mkdir -p "$DIR/.compshare"

LINE="*/5 * * * * cd $DIR && $PY csctl.py watch --once --ssh --idle-minutes $IDLE_MIN >> $LOG 2>&1"
# P2：孤儿实例回收（P1-5）+ 多作业视图刷新（架构图 I 区数据源）
WDLINE="*/5 * * * * cd $DIR && $PY tools/p2/batchctl.py watchdog >> $DIR/logs/watchdog.log 2>&1"
S2LINE="*/10 * * * * cd $DIR && $PY tools/p2/status2.py >> $DIR/logs/status2.log 2>&1"

# 去掉旧的本工具任务，再追加（避免重复）
( crontab -l 2>/dev/null | grep -vE 'csctl.py watch|p2/batchctl.py watchdog|p2/status2.py' || true; \
  echo "$LINE"; echo "$WDLINE"; echo "$S2LINE" ) | crontab -

echo "已安装 crontab："
crontab -l | grep -E 'csctl.py watch|p2/batchctl.py watchdog|p2/status2.py'
echo
echo "日志: tail -f $LOG"
echo "卸载: crontab -l | grep -vE 'csctl.py watch|p2/batchctl.py watchdog|p2/status2.py' | crontab -"
echo
cat <<'TIP'
可选：想更实时（每分钟）就把 */5 改成 *；想连磁盘也不留，把 --stop-action stop
改成 --stop-action terminate（会删实例，慎用）。
TIP
