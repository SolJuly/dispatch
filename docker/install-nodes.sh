#!/usr/bin/env bash
# 安装 10 个 active 自定义节点（commit 与 v3 实机一致）。
# 依赖安装失败不直接中断：先让构建跑完，再按日志逐条补。
set -uo pipefail

PIP_INDEX="--extra-index-url https://download.pytorch.org/whl/cu130"
CONSTRAINTS="-c /tmp/constraints.txt"
NODES=/root/ComfyUI/custom_nodes
mkdir -p "$NODES"

install_node() {
  local dir="$1" url="$2" commit="$3"
  echo "==> $dir @ $commit"
  if [ -d "$NODES/$dir/.git" ]; then
    git -C "$NODES/$dir" fetch --depth 1 origin "$commit" || true
    git -C "$NODES/$dir" checkout -q "$commit" || true
  else
    git clone --filter=blob:none "$url" "$NODES/$dir"
    git -C "$NODES/$dir" checkout -q "$commit"
  fi
  if [ -f "$NODES/$dir/requirements.txt" ]; then
    pip install $PIP_INDEX $CONSTRAINTS -r "$NODES/$dir/requirements.txt" \
      || echo "WARN $dir requirements failed"
  fi
}

install_node ComfyUI-Crystools \
  https://github.com/crystian/ComfyUI-Crystools 2f18256c5b5063937106f29a8e0a7db3ae3869b7
install_node ComfyUI-DD-Translation \
  https://github.com/Dontdrunk/ComfyUI-DD-Translation 9cc769b5d5d9fdec4a80533e5d848ec4ff4f4b8a
install_node ComfyUI-Impact-Pack \
  https://github.com/ltdrdata/ComfyUI-Impact-Pack eace4a9059908330936c6514f35f5e0c84935394
install_node ComfyUI-KJNodes \
  https://github.com/kijai/ComfyUI-KJNodes ab72eda7ffad6280422d71ab7d54d3fb87c0ba18
install_node ComfyUI-SUPIR \
  https://github.com/kijai/ComfyUI-SUPIR 99d49e912c905ce1aaf7d15898f550b40fb3e6cc
install_node ComfyUI-SeedVR2_VideoUpscaler \
  https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler 4490bd1f482e026674543386bb2a4d176da245b9
install_node ComfyUI_LayerStyle \
  https://github.com/chflame163/ComfyUI_LayerStyle c7b8f73af0badaf2a59bee6111ecdd004be15ab3
install_node ComfyUI_UltimateSDUpscale \
  https://github.com/ssitu/ComfyUI_UltimateSDUpscale a5547db9e1d07d3318bb21e9e9c474f4c1e9c8df
install_node ComfyUI_essentials \
  https://github.com/cubiq/ComfyUI_essentials 9d9f4bedfc9f0321c19faf71855e228c93bd0dc9
install_node comfyui_face_parsing \
  https://github.com/ComfyNodePRs/PR-comfyui_face_parsing-d39afaa9.git f9f89b3faa2b240257e6a6102469d0a33d031d6d

echo "==> done"
