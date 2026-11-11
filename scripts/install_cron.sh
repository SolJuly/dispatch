#!/usr/bin/env bash
# 装运行观测 cron：实例采样（只读）+ 孤儿实例回收 + 多作业视图刷新。
#
# **不再装 csctl.py watch**。它按 GPU 利用率 15 分钟停机，装模型阶段 GPU 也是 0%，
# 会误关；停机判据已交给 p2-reaper（按作业状态 + 生命周期窗口，判据更准，
# 见 deploy/systemd/p2-reaper.service）。两套判据同时开着时 watch 会抢先关掉
# reaper 判定"还在长任务空转窗口内"的机器，三档设计形同虚设。
#
# 但 watch 顺手还往 watch.log 写采样，而那是实例页/watchdog 唯一的数据源。
# 撤掉它 52 小时没数据就是这个原因。所以采样拆成独立工具 tools/p2/sampler.py：
# 只读、不停机，见其文件头。
#
# 用法: bash scripts/install_cron.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$DIR/.compshare/watch.log"
PY="$(command -v python3)"
mkdir -p "$DIR/.compshare" "$DIR/logs"

# 实例采样（只读，不动手）—— 写 watch.log，实例页的 GPU/CPU/花费全靠它
SAMPLE="*/5 * * * * cd $DIR && $PY tools/p2/sampler.py >> $DIR/logs/sampler.log 2>&1"
# P2：孤儿实例回收（P1-5）+ 多作业视图刷新（架构图 I 区数据源）
WDLINE="*/5 * * * * cd $DIR && $PY tools/p2/batchctl.py watchdog >> $DIR/logs/watchdog.log 2>&1"
S2LINE="*/10 * * * * cd $DIR && $PY tools/p2/status2.py >> $DIR/logs/status2.log 2>&1"

# 去掉旧的同类任务，再追加（避免重复）。
# 同时清掉历史遗留的 csctl.py watch —— 它不该再出现在任何人的 crontab 里。
( crontab -l 2>/dev/null | grep -vE 'csctl.py watch|p2/sampler.py|p2/batchctl.py watchdog|p2/status2.py' || true; \
  echo "$SAMPLE"; echo "$WDLINE"; echo "$S2LINE" ) | crontab -

echo "已安装 crontab："
crontab -l | grep -E 'p2/sampler.py|p2/batchctl.py watchdog|p2/status2.py'
echo
echo "采样日志: tail -f $DIR/logs/sampler.log"
echo "采样数据: tail -f $LOG"
echo "卸载: crontab -l | grep -vE 'p2/sampler.py|p2/batchctl.py watchdog|p2/status2.py' | crontab -"
echo
cat <<'TIP'
停机/销毁由 systemd 的 p2-reaper.timer 负责（每 5 分钟，按作业状态判定）。
查看判定：python3 tools/p2/reaper_cli.py status
TIP
