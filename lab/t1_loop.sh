#!/bin/bash
# 远端：秒级计数循环，用于验证 SSH 断开后进程是否存活
LOG="${1:-/root/t1_default.log}"
i=0
while [ "$i" -lt 200 ]; do
  i=$((i+1))
  echo "$i $(date +%s)" >> "$LOG"
  sleep 1
done
