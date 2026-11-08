#!/usr/bin/env bash
# 把控制台部署到独立服务器（从开发机执行）。
#
#   bash scripts/deploy_to_server.sh --check                 # 只体检目标机
#   bash scripts/deploy_to_server.sh                         # B 模式：传包 + 安装 + 验证
#   HOST=1.2.3.4 SSH_PORT=22 KEY=... bash scripts/deploy_to_server.sh
#
# 需要：目标机 root 已授权本机公钥（.compshare/ssh/p21_deploy_ed25519.pub），
#       安全组放行 80（部署后再放行 443 上证书）。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HOST=${HOST:?请设置 HOST=服务器IP}
SSH_USER=${SSH_USER:-root}
SSH_PORT=${SSH_PORT:-22}
# 阿里云轻量/ECS 的「密钥对」只收 RSA/ECDSA（不支持 ed25519），默认用 RSA
KEY=${KEY:-.compshare/ssh/p21_deploy_rsa}
KNOWN_HOSTS=${KNOWN_HOSTS:-.compshare/ssh/p21_known_hosts}
REMOTE_DIR=${REMOTE_DIR:-/root/智云视频工作流}
BUNDLE=${BUNDLE:-$(ls -t dist/p21-deploy-*.tar.gz 2>/dev/null | head -1 || true)}
CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

SSH_OPTS=(-p "$SSH_PORT" -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10
          -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN_HOSTS")
ssh_() { ssh "${SSH_OPTS[@]}" "$SSH_USER@$HOST" "$@"; }
scp_() { scp -P "$SSH_PORT" -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 \
             -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$KNOWN_HOSTS" "$@"; }

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }

say "连通性"
ssh_ 'echo "  target: $(hostname)  $(. /etc/os-release; echo "$PRETTY_NAME")"' || {
  echo "✗ SSH 不通：确认公钥已加到 $SSH_USER@$HOST 的 ~/.ssh/authorized_keys，端口 $SSH_PORT 放行" >&2
  exit 1
}

if [ "$CHECK" = 1 ]; then
  say "目标机体检（只读）"
  ssh_ 'bash -s -- --check' < deploy/remote/install_cs.sh || true
  exit 0
fi

[ -n "$BUNDLE" ] && [ -f "$BUNDLE" ] || { echo "✗ 找不到部署包，先跑 bash scripts/deploy_bundle.sh" >&2; exit 1; }
TOKEN=$(cat .share_token)
say "上传 $(basename "$BUNDLE")（$(du -h "$BUNDLE" | cut -f1)）"
scp_ "$BUNDLE" "$SSH_USER@$HOST:/root/_p21_deploy.tar.gz"
scp_ deploy/remote/install_cs.sh "$SSH_USER@$HOST:/root/_p21_install.sh"
scp_ .share_token "$SSH_USER@$HOST:/root/_p21_token"

say "解包到 $REMOTE_DIR"
ssh_ "set -e; mkdir -p '$REMOTE_DIR'; tar xzf /root/_p21_deploy.tar.gz -C '$REMOTE_DIR'; \
      install -m 600 /root/_p21_token '$REMOTE_DIR/.share_token'; \
      rm -f /root/_p21_deploy.tar.gz /root/_p21_token; \
      ls '$REMOTE_DIR/fileserver.py' '$REMOTE_DIR/.share_token'"

if [ "${WITH_PUB:-0}" = 1 ]; then
  say "同步 /srv/dsh-pub（产物图 + /pub 文档，约 56 MB）"
  ssh_ "mkdir -p /srv/dsh-pub"
  rsync -az --delete-excluded \
    -e "ssh -p $SSH_PORT -i $KEY -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS" \
    /srv/dsh-pub/jobs /srv/dsh-pub/api /srv/dsh-pub/p20 /srv/dsh-pub/p21 \
    "$SSH_USER@$HOST:/srv/dsh-pub/" || { echo "✗ /srv/dsh-pub 同步失败" >&2; exit 1; }
  ssh_ "chmod -R a+rX /srv/dsh-pub"
fi

say "安装服务（systemd + nginx）"
ssh_ "bash /root/_p21_install.sh --dir '$REMOTE_DIR' --token-file '$REMOTE_DIR/.share_token'" \
  < /dev/null || { echo "✗ 远程安装失败（见上）" >&2; exit 1; }

if [ "${WITH_SEED:-0}" = 1 ]; then
  say "种子数据：同步任务台账与队列（只在首次部署/需要带历史时用）"
  ssh_ "mkdir -p '$REMOTE_DIR/reports/p1/jobs' '$REMOTE_DIR/reports/p2'"
  # 先 checkpoint，避免只拷 queue.db 丢掉 WAL 里的新行
  python3 - <<'PYCHK' 2>/dev/null || true
import sqlite3
c = sqlite3.connect("reports/p2/queue.db")
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.close()
PYCHK
  rsync -az -e "ssh -p $SSH_PORT -i $KEY -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=$KNOWN_HOSTS" \
    reports/p1/jobs/ "$SSH_USER@$HOST:$REMOTE_DIR/reports/p1/jobs/"
  scp_ reports/p2/queue.db "$SSH_USER@$HOST:$REMOTE_DIR/reports/p2/queue.db"
  ssh_ "chmod 644 '$REMOTE_DIR/reports/p2/queue.db'"
fi

if [ "${WITH_GPU:-0}" = 1 ]; then
  say "C 模式：上传 GPU 凭证并安装执行链"
  [ -f compshare/credentials.env ] || { echo "✗ 开发机没有 compshare/credentials.env" >&2; exit 1; }
  ssh_ "mkdir -p '$REMOTE_DIR/compshare'"
  scp_ compshare/credentials.env "$SSH_USER@$HOST:$REMOTE_DIR/compshare/credentials.env"
  ssh_ "chmod 600 '$REMOTE_DIR/compshare/credentials.env'"
  scp_ deploy/remote/install_c_mode.sh "$SSH_USER@$HOST:/root/_p21_c.sh"
  ssh_ "bash /root/_p21_c.sh; rc=\$?; rm -f /root/_p21_c.sh; exit \$rc" < /dev/null \
    || { echo "✗ C 模式安装失败（见上）" >&2; exit 1; }
fi

say "公网验证"
for p in "api/p2/version" "app/create" "app/jobs" "api/p2/jobs" "api/p2/registry/stats"; do
  printf '  %-20s ' "$p"
  curl -s -o /dev/null -m 10 -w '%{http_code}\n' "http://$HOST/files/$TOKEN/$p" || echo "unreachable"
done
echo
echo "  控制台：http://$HOST/files/$TOKEN/"
echo "  版本  ：http://$HOST/files/$TOKEN/api/p2/version"
