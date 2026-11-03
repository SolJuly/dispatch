#!/usr/bin/env bash
# 打一个「可以直接放到独立服务器跑控制台」的运行时包。
#
#   bash scripts/deploy_bundle.sh                 # 生成 dist/p21-deploy-<时间>.tar.gz
#   bash scripts/deploy_bundle.sh --verify        # 生成后解包、起一次服务、请求 /api/p2/version
#   OUT=/tmp/x.tar.gz bash scripts/deploy_bundle.sh
#
# 不包含：.git / share（由 sync_share 重建）/ 连测图（reports/p2/p21/bench, inputs）
#         / 演示图 / 凭证（credentials.env）/ token（.share_token）/ 日志 / 缓存
# 目标机解包后：python3 fileserver.py 即可起（stdlib，无第三方依赖）。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERIFY=0
[[ "${1:-}" == "--verify" ]] && VERIFY=1
OUT=${OUT:-dist/p21-deploy-$(date +%Y%m%d-%H%M).tar.gz}
mkdir -p "$(dirname "$OUT")"

tar cf "$OUT.tmp" \
  --exclude=.git \
  --exclude=share \
  --exclude=dist \
  --exclude=logs \
  --exclude=.compshare \
  --exclude=__pycache__ \
  --exclude='*.pyc' \
  --exclude=credentials.env \
  --exclude=.share_token \
  --exclude=reports/p2/p21/bench \
  --exclude=reports/p2/p21/inputs \
  --exclude=reports/p2/concat-demo \
  --exclude=reports/p1/jobs/*/artifacts \
  --exclude=reports/image-test \
  --exclude=reports/video \
  --exclude=reports/lab \
  --exclude=reports/architecture.png \
  --exclude='*.zip' \
  --exclude='*.tar.gz' \
  --exclude='*.mp4' \
  .

# 包内带一份 BUILD（无 .git 时 /api/p2/version 用它报 commit，运维能对上号）
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
{ git rev-parse --short HEAD 2>/dev/null || echo unknown; } >"$stage/BUILD"
tar rf "$OUT.tmp" -C "$stage" BUILD
gzip -9 -f "$OUT.tmp"
mv "$OUT.tmp.gz" "$OUT"
rm -rf "$stage"

size=$(du -h "$OUT" | cut -f1)
n=$(tar tzf "$OUT" | wc -l)
sha=$(sha256sum "$OUT" | cut -d' ' -f1)
echo "[ok] $OUT"
echo "     $n 个文件 · $size · sha256 ${sha:0:16}…"
echo "     不含 .git / share / 连测图 / 凭证；目标机解包后 python3 fileserver.py 即可"

if [ "$VERIFY" = 1 ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  tar xzf "$OUT" -C "$tmp"
  mkdir -p "$tmp/share"
  port=$(python3 - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PY
)
  SHARE_ROOT="$tmp/share" SHARE_TOKEN_FILE="$tmp/.share_token" \
    SHARE_PORT="$port" SHARE_BASE=/files python3 "$tmp/fileserver.py" >"$tmp/srv.log" 2>&1 &
  pid=$!
  for _ in $(seq 1 30); do
    sleep 0.2
    curl -fsS "http://127.0.0.1:$port/$(cat "$tmp/.share_token" 2>/dev/null || true)/api/p2/version" \
      >"$tmp/ver.json" 2>/dev/null && break
  done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  python3 - "$tmp/ver.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["api"] == "p2.1", d
assert d["features"]["version"] and d["features"]["registry"], d
print(f"     verify ✓ api={d['api']} commit={d['build']['commit']} "
      f"workflows={d['registry'].get('workflows')} models={d['registry'].get('models')}")
PY
  echo "     解包自检通过：包内自带 registry/queue 数据，起服务即可返回版本"
fi
