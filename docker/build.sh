#!/usr/bin/env bash
# 构建并（可选）推送到阿里云 ACR。
#
#   ACR=registry.cn-hangzhou.aliyuncs.com NS=your-namespace TAG=v3 ./docker/build.sh
#   ACR=... NS=... TAG=v3 PUSH=1 ./docker/build.sh
#
# 前置：
#   1) 构建机有 docker，且能访问 github.com（拉 ComfyUI/节点）和 download.pytorch.org
#   2) 已 docker login $ACR
set -euo pipefail

ACR="${ACR:?需要 ACR 地址，例如 registry.cn-hangzhou.aliyuncs.com}"
NS="${NS:?需要命名空间}"
TAG="${TAG:-v3}"
IMAGE="$ACR/$NS/comfyui-p21:$TAG"

cd "$(dirname "$0")/.."
docker build -f docker/Dockerfile -t "$IMAGE" docker/
echo "built: $IMAGE"

if [ "${PUSH:-0}" = "1" ]; then
  docker push "$IMAGE"
  echo "pushed: $IMAGE"
fi
