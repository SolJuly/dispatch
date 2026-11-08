#!/usr/bin/env bash
# C 模式：在目标机补齐「能建作业、能起 GPU」的执行链。
# 前提：B 模式已装好（cs-files 在跑）、compshare/credentials.env 已 scp 到位。
#
#   bash install_c_mode.sh                     # 装依赖 + 验证凭证 + 装 crontab
#   bash install_c_mode.sh --no-cron           # 只装依赖/验证
#
# 做的事：
#   1. apt 装 sshpass（run.py scp/ssh 用）、ffmpeg（视频取帧/校验）、zip
#   2. 建 .compshare（实例锁/账本）、logs
#   3. 用 csctl.py ls 验证凭证与 CompShare API 连通
#   4. 装 crontab：空闲关机 watch / 孤儿回收 watchdog / 状态快照 status2
#   5. 用本地 API 建一条 run=false 的作业做 dry-run（不花钱），确认 jobctl 链通
set -euo pipefail

DIR=${DIR:-/root/智云视频工作流}
WITH_CRON=1
[[ "${1:-}" == "--no-cron" ]] && WITH_CRON=0

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "请用 root 执行"
[ -f "$DIR/fileserver.py" ] || die "$DIR 不是部署目录"
[ -f "$DIR/compshare/credentials.env" ] || die "缺 $DIR/compshare/credentials.env（先在开发机 scp 过来，chmod 600）"
chmod 600 "$DIR/compshare/credentials.env"

say "安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq sshpass ffmpeg zip rsync python3-pip >/dev/null
for b in sshpass ffmpeg zip rsync python3 pip3; do
  command -v "$b" >/dev/null 2>&1 || die "依赖没装上：$b"
done
echo "  sshpass/ffmpeg/zip/rsync/pip ok"

say "安装 Python 依赖（ucloud SDK，GPU 建机用）"
# 只有 ucloud 一个第三方包；锁到开发机验证过的版本，装不上再退回最新
if ! python3 -c "import ucloud" >/dev/null 2>&1; then
  pip3 install --break-system-packages -q "ucloud-sdk-python3==${UCLOUD_VER:-0.11.149}" \
    || pip3 install --break-system-packages -q ucloud-sdk-python3 \
    || die "ucloud SDK 安装失败"
fi
python3 - <<'PY'
import importlib.metadata as md
print("  ucloud-sdk:", md.version("ucloud-sdk-python3"))
PY

say "准备运行目录"
mkdir -p "$DIR/.compshare" "$DIR/logs"
chmod 700 "$DIR/.compshare"

say "验证 CompShare 凭证与连通性"
cd "$DIR"
if ! out=$(python3 csctl.py ls 2>&1); then
  echo "$out" | tail -10
  die "csctl.py ls 失败（凭证不对或出网不通）"
fi
echo "$out" | tail -3

if [ "$WITH_CRON" = 1 ]; then
  say "安装 crontab（闲置关机 / 孤儿回收 / 状态快照）"
  bash scripts/install_cron.sh 15 | tail -5
fi

say "dry-run：建一条 run=false 的作业（不占 GPU、不花钱）"
TOKEN=$(cat "$DIR/.share_token")
resp=$(curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"workflow":"sdxl_t2i","params":{"prompt":"deploy smoke test","seed":42},"run":false}' \
  "http://127.0.0.1:8081/$TOKEN/api/p2/jobs")
echo "  $resp" | head -c 300; echo
JID=$(printf '%s' "$resp" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("job_id") or "")' 2>/dev/null || true)
[ -n "$JID" ] || die "建作业失败（见上）"
[ -f "$DIR/reports/p1/jobs/$JID/job.json" ] || die "job.json 没落盘：reports/p1/jobs/$JID"
echo "  ✓ 落盘 reports/p1/jobs/$JID/job.json"

say "C 模式就绪"
cat <<EOF
  建作业 dry-run 已通（$JID，状态 CREATED，未执行）
  真要跑：curl -X POST ... -d '{"workflow":"sdxl_t2i","params":{...},"run":true}'
  花费参考：4090 按量 ¥0.34/h，SDXL 单图 P50 4.8s
  清理测试作业：rm -rf $DIR/reports/p1/jobs/$JID
EOF
