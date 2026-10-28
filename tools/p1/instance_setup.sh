#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 一条命令把新实例配到「可跑」状态。
#
#   bash tools/p1/instance_setup.sh <uhost-id>          # 全套配置 + 启动 + 自检
#   bash tools/p1/instance_setup.sh <uhost-id> --check --need=h3   # 只自检 H3 资产
#   bash tools/p1/instance_setup.sh <uhost-id> --need=qwen         # 全套但只验 Qwen 资产
#
# 解决的四件事（都是实测踩过的）：
#   1. /model 共享库默认不挂载          → 推 extra_model_paths.yaml
#   2. ComfyUI-Manager 拉 GitHub 概率卡死 → 目录改名 .disabled
#   3. Qwen 两个资产是断链                → 软链到真实文件
#   4. entrypoint 与手工启动抢 8188       → 先清进程再启动，并轮询健康
set -euo pipefail

ID="${1:-}"
MODE="${2:-}"
if [[ -z "$ID" ]]; then
  echo "用法: bash tools/p1/instance_setup.sh <uhost-id> [--check]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CS="python3 $ROOT/csctl.py"
LOG=/root/comfy_run.log

say() { printf '\033[36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------- 自检（--check 模式只跑这一段） ----------
NEED="${2:-all}"; [[ "$NEED" == --need=* ]] && NEED="${NEED#--need=}"
check() {
  local ok=0 out
  for i in $(seq 1 25); do
    local out
    out=$($CS ssh "$ID" --exec "source /root/miniconda3/bin/activate && python3 -c \"
import json,sys,urllib.request
try:
    oi=json.load(urllib.request.urlopen('http://127.0.0.1:8188/object_info',timeout=30))
except Exception as e:
    print('DOWN', e); raise SystemExit(1)
ul=oi['UNETLoader']['input']['required']['unet_name'][0]
cl=oi['CLIPLoader']['input']['required']['clip_name'][0]
vl=oi['VAELoader']['input']['required']['vae_name'][0]
need={
 'h3_unet': any('dasiwaHybridV2' in u for u in ul),
 'h3_clip': any('qwen3vl_32b_minimax_h3_int8' in c for c in cl),
 'h3_vae' : any('minimax_h3_video_vae_int8' in v for v in vl),
 'qwen_unet': any('qwen_image_fp8' in u for u in ul),
 'qwen_vae' : any(v=='qwen_std_vae.safetensors' for v in vl),
 'qwen_clip': any(c=='qwen25vl7b_std.safetensors' for c in cl),
}
NEED_OS = sys.argv[1] if len(sys.argv)>1 else 'all'
if NEED_OS == 'h3':
    need = {k:v for k,v in need.items() if k.startswith('h3_')}
elif NEED_OS == 'qwen':
    need = {k:v for k,v in need.items() if k.startswith('qwen_')}
print('OK' if all(need.values()) else 'MISSING:'+','.join(k for k,v in need.items() if not v))
\" '$NEED'" 2>/dev/null | sed 's/\x1b\\[[0-9;]*m//g') || true
    # csctl ssh 的输出尾部带 "[exit=N]"，不能直接比对整串，用 grep 取判定词
    if printf '%s' "$out" | grep -qE '(^|[^A-Z])OK($|[^A-Z])'; then ok=1; break; fi
    if printf '%s' "$out" | grep -q 'MISSING:'; then
      say "资产缺失：$(printf '%s' "$out" | grep -o 'MISSING:[a-z_,]*' | head -1)"
    fi
    [[ $i -eq 1 ]] && say "等待 ComfyUI 就绪（第 1 次探活）…"
    sleep 4
  done
  if [[ $ok -eq 1 ]]; then
    say "✓ 自检通过：ComfyUI 就绪，6 项资产全部可见"
    return 0
  fi
  say "✗ 自检未通过：$out"
  say "  远端日志：$CS ssh $ID --exec 'tail -20 $LOG'"
  return 1
}

if [[ "$MODE" == "--check" ]]; then
  check
  exit $?
fi

# ---------- 1. 生成并推送挂载配置 ----------
YAML="$(mktemp /tmp/p1_model_paths.XXXXXX.yaml)"
cat >"$YAML" <<'YAML'
# P1 统一挂载：/model 共享库（只读 40T）
comfyui_shared:
    base_path: /model/comfyui/
    checkpoints: models/checkpoints/
    clip: models/clip/
    clip_vision: models/clip_vision/
    configs: models/configs/
    controlnet: models/controlnet/
    diffusion_models: |
        models/diffusion_models
        models/unet
    embeddings: models/embeddings/
    loras: models/loras/
    style_models: models/style_models/
    text_encoders: models/text_encoders/
    upscale_models: models/upscale_models/
    vae: models/vae/
YAML
trap 'rm -f "$YAML"' EXIT

say "推送挂载配置…"
# 用 sshpass scp 直传（csctl push 走 transfer.py，实测在部分实例上会失败）
if [[ -n "${P1_PASSWORD:-}" && -n "${P1_HOST:-}" ]]; then
  sshpass -p "$P1_PASSWORD" scp -q -P "${P1_PORT:-23}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=15 -c aes128-gcm@openssh.com \
    "$YAML" "root@${P1_HOST}:/root/ComfyUI/extra_model_paths.yaml" 2>/dev/null \
    || die "scp 推送 extra_model_paths.yaml 失败"
else
  $CS push "$ID" "$YAML" /root/ComfyUI/extra_model_paths.yaml >/dev/null \
    || die "推送 extra_model_paths.yaml 失败"
fi

# ---------- 2. 远端准备：停进程 / 禁 Manager / 建软链 ----------
say "停旧进程 / 禁 Manager / 建软链（逐条执行，便于定位）…"
$CS ssh "$ID" --exec "pkill -f '[m]ain.py --listen' 2>/dev/null; sleep 2; pkill -f '[m]ain.py --listen' 2>/dev/null; echo killed" >/dev/null 2>&1 || true
$CS ssh "$ID" --exec "if [ -d /root/ComfyUI/custom_nodes/ComfyUI-Manager ]; then mv /root/ComfyUI/custom_nodes/ComfyUI-Manager /root/ComfyUI/custom_nodes/ComfyUI-Manager.disabled; echo mgr-moved; else echo mgr-absent; fi" >/dev/null 2>&1 || true
$CS ssh "$ID" --exec "ln -sf /model/ModelScope/FireRedTeam/FireRed-Image-Edit-1.1-ComfyUI/qwen_image_vae.safetensors /root/ComfyUI/models/vae/qwen_std_vae.safetensors; ln -sf /model/ModelScope/FireRedTeam/FireRed-Image-Edit-1.1-ComfyUI/qwen2.5vl-7b-bf16.safetensors /root/ComfyUI/models/text_encoders/qwen25vl7b_std.safetensors; echo links-done" >/dev/null 2>&1 || true
say "  远端准备完成"

# ---------- 3. 启动 ----------
say "启动 ComfyUI（离线模式，无 Manager）…"
$CS ssh "$ID" --exec "source /root/miniconda3/bin/activate && cd /root/ComfyUI && COMFYUI_MANAGER_OFFLINE=1 python -u main.py --listen 0.0.0.0 --port 8188" \
  --detach --log "$LOG" >/dev/null || die "启动失败"

# ---------- 4. 自检 ----------
say "等待就绪并自检（最多 100 秒）…"
check || die "环境未就绪"

say "✓ 实例 $ID 就绪"
echo
echo "  跑任务：python3 tools/p1/jobctl.py new --spec <spec.json>"
echo "  看日志：python3 csctl.py ssh $ID --exec \"tail -20 $LOG\""
