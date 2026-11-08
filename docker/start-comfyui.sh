#!/usr/bin/env bash
# 容器入口：前台跑 ComfyUI，监听 0.0.0.0:8188。
set -euo pipefail
cd /root/ComfyUI

# 平台代理在部分环境解析不了，节点 import 时的下载会卡死
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy || true

export COMFYUI_MANAGER_OFFLINE=1
exec python -u main.py --listen 0.0.0.0 --port 8188
