#!/usr/bin/env bash
# 在目标服务器上安装「控制台」运行时（B 模式：只读 API + 页面）。
# 在目标机以 root 执行；幂等，可反复跑。
#
#   bash install_cs.sh --token-file /root/.share_token
#   bash install_cs.sh --check                 # 只体检，不改任何东西
#   bash install_cs.sh --token-file ... --no-nginx
#
# 依赖：python3 >= 3.10、systemd、apt（装 nginx 用）
set -euo pipefail

DIR=/root/智云视频工作流
PORT=8081
BASE=/files
TOKEN_FILE=$DIR/.share_token
NO_NGINX=0
CHECK=0
for a in "$@"; do
  case "$a" in
    --token-file=*) TOKEN_FILE="${a#*=}" ;;
    --token-file)   shift; TOKEN_FILE="${1:-}" ;;
    --dir=*)        DIR="${a#*=}" ;;
    --port=*)       PORT="${a#*=}" ;;
    --no-nginx)     NO_NGINX=1 ;;
    --check)        CHECK=1 ;;
    -h|--help)      sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$a" >&2; exit 2 ;;
  esac
done

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || echo "  缺少：$1"; }

say "环境体检"
[ "$(id -u)" = 0 ] || die "请用 root 执行"
[ -f "$DIR/fileserver.py" ] || die "$DIR 里没有 fileserver.py（先把部署包解压到这里）"
PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo none)
echo "  python3      : $PYV"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)' 2>/dev/null \
  || die "需要 python3 >= 3.10（当前 $PYV）；Ubuntu 20.04 请先升级或用 22.04 镜像"
echo "  systemd      : $(systemctl --version 2>/dev/null | head -1 | cut -d' ' -f2)"
NGX=""
command -v nginx >/dev/null 2>&1 && NGX=$(nginx -v 2>&1 | head -1)
echo "  nginx        : ${NGX:-未安装}"
echo "  rsync/scp    : $(command -v rsync || echo 缺) / $(command -v scp || echo 缺)"
echo "  8081 占用    : $(ss -ltnp 2>/dev/null | grep -c ":$PORT " || true) 个监听"
echo "  磁盘         : $(df -h / | awk 'NR==2{print $4" 可用"}')"

if [ "$CHECK" = 1 ]; then
  say "只体检，未做任何修改"
  exit 0
fi

say "服务用户与目录"
mkdir -p "$DIR/share"
[ -f "$TOKEN_FILE" ] || die "缺 token 文件：$TOKEN_FILE（部署脚本会 scp 过来）"

say "同步 share/"
( cd "$DIR" && bash scripts/sync_share.sh >/dev/null ) || die "sync_share 失败"

say "安装 cs-files.service"
cat >/etc/systemd/system/cs-files.service <<EOF
[Unit]
Description=CS file share (tablet friendly)
After=network.target

[Service]
Type=simple
WorkingDirectory=$DIR
Environment=SHARE_ROOT=$DIR/share
Environment=SHARE_PORT=$PORT
Environment=SHARE_BIND=127.0.0.1
Environment=SHARE_BASE=$BASE
ExecStart=/usr/bin/python3 $DIR/fileserver.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable cs-files >/dev/null 2>&1 || true
systemctl restart cs-files
sleep 2
systemctl is-active --quiet cs-files || { systemctl status cs-files --no-pager | tail -15; die "cs-files 没起来"; }

TOKEN=$(cat "$TOKEN_FILE")
say "本机健康检查"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/$TOKEN/api/p2/version")
echo "  /api/p2/version -> $code"
[ "$code" = 200 ] || { tail -20 "$DIR"/logs/*.log 2>/dev/null; die "接口没通"; }

if [ "$NO_NGINX" = 0 ]; then
  say "配置 nginx"
  command -v nginx >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq nginx; }
  mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
  cat >/etc/nginx/sites-available/cs-p21 <<EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    client_max_body_size 200m;

    location = / {
        return 302 $BASE/$TOKEN/;
    }

    location $BASE/ {
        proxy_pass http://127.0.0.1:$PORT/;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
EOF
  if [ -d /srv/dsh-pub ]; then
    cat >>/etc/nginx/sites-available/cs-p21 <<'EOF'

    location /pub/ {
        alias /srv/dsh-pub/;
        autoindex off;
    }
EOF
  fi
  echo "}" >>/etc/nginx/sites-available/cs-p21
  ln -sf /etc/nginx/sites-available/cs-p21 /etc/nginx/sites-enabled/cs-p21
  # 已有 default 站点会和我们的 default_server 冲突
  if [ -e /etc/nginx/sites-enabled/default ]; then
    mv /etc/nginx/sites-enabled/default "/etc/nginx/sites-enabled/default.disabled.$(date +%s)"
    echo "  已停用 sites-enabled/default（备份保留）"
  fi
  nginx -t || die "nginx 配置校验失败（见上）"
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
  sleep 1
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1$BASE/$TOKEN/api/p2/version")
  echo "  http://127.0.0.1$BASE/... -> $code"
  [ "$code" = 200 ] || die "nginx 反代没通"
fi

say "完成"
curl -s "http://127.0.0.1:$PORT/$TOKEN/api/p2/version" | head -c 300; echo
echo "  对外：http://<公网IP>$BASE/$TOKEN/"
echo "  服务：systemctl status cs-files  |  nginx -t"
