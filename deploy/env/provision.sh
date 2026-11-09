#!/usr/bin/env bash
# -*- coding: utf-8 -*-
# 从「空机器」装到「可跑」—— 跨云复现入口。
#
# 与 tools/p1/instance_setup.sh 的分工：
#   provision.sh       做「安装」：装 torch、装 ComfyUI、拉 custom_nodes、装 pip 依赖
#   instance_setup.sh  做「配置」：推挂载 yaml、禁 Manager、建软链、启动、自检
#
# instance_setup.sh 假设 /root/ComfyUI、/root/miniconda3、custom_nodes 都已存在
# —— 那是因为智云镜像是预先烘焙的。换到别家云，这些一个都没有，必须靠本脚本装出来。
#
# 为什么智云上用不了：智云实例无外网（DNS 解析失败），pip/git 全不可用。
# 智云上请继续用平台镜像 / 自制镜像 / 停机实例；本脚本用于有外网的云。
#
# 用法：
#   bash deploy/env/provision.sh                          # 用默认 manifest
#   bash deploy/env/provision.sh --manifest path.json
#   bash deploy/env/provision.sh --skip-torch             # torch 已装
#   bash deploy/env/provision.sh --skip-nodes             # 只装 ComfyUI
#   bash deploy/env/provision.sh --dry-run                # 只打印计划
#   bash deploy/env/provision.sh --verify-only            # 只跑验证
#
# 环境变量：
#   COMFY_HOME    默认 /root/ComfyUI
#   MODEL_ROOT    默认 /model
#   PYTHON        默认 python3
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
MANIFEST="$HERE/comfyui-wan-p21.env.json"
COMFY_HOME="${COMFY_HOME:-/root/ComfyUI}"
MODEL_ROOT="${MODEL_ROOT:-/model}"
PYTHON="${PYTHON:-python3}"

SKIP_TORCH=0; SKIP_NODES=0; DRY=0; VERIFY_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2;;
    --skip-torch) SKIP_TORCH=1; shift;;
    --skip-nodes) SKIP_NODES=1; shift;;
    --dry-run) DRY=1; shift;;
    --verify-only) VERIFY_ONLY=1; shift;;
    -h|--help) sed -n '2,30p' "$0"; exit 0;;
    *) echo "未知参数: $1" >&2; exit 2;;
  esac
done

[[ -f "$MANIFEST" ]] || { echo "✗ manifest 不存在: $MANIFEST" >&2; exit 2; }

step=0
say()  { step=$((step+1)); printf '\033[36m[%02d]\033[0m %s\n' "$step" "$*"; }
ok()   { printf '     \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '     \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
run()  { if [[ $DRY -eq 1 ]]; then echo "     [dry] $*"; return 0; fi; "$@"; }

# 从 manifest 读一个值；失败时用默认值
mf() {
  local expr="$1" dflt="${2:-}"
  local v
  v=$("$PYTHON" - "$MANIFEST" "$expr" <<'PY' 2>/dev/null
import json, sys
d = json.load(open(sys.argv[1]))
cur = d
for part in sys.argv[2].split('.'):
    if part == '': continue
    if isinstance(cur, list):
        cur = cur[int(part)]
    else:
        cur = cur.get(part)
    if cur is None:
        print(""); raise SystemExit(0)
print(cur)
PY
)
  printf '%s' "${v:-$dflt}"
}

echo "════════════════════════════════════════════════"
echo " 环境 manifest: $MANIFEST"
echo " COMFY_HOME   : $COMFY_HOME"
echo " MODEL_ROOT   : $MODEL_ROOT"
[[ $DRY -eq 1 ]] && echo " 模式         : DRY-RUN（不实际执行）"
[[ $VERIFY_ONLY -eq 1 ]] && echo " 模式         : 仅验证"
echo "════════════════════════════════════════════════"

# ---------------------------------------------------------------- 0. 前置检查
say "检查前置条件"

# 身份守卫：这是「部署机」还是「目标 GPU 机」？
#
# 2026-11-09 事故：在部署机（有外网）上跑本脚本，它把「有外网」当成了
# 「可以是目标机」的判据，于是真的开始往部署机装 torch + ComfyUI，
# 吃了 7GB 磁盘，被超时杀掉才停下。
#
# 判据：部署机是源码树所在地，带 .git + .share_token + 项目自身结构；
# 目标 GPU 机是干净机器，只有 CUDA + Python。
DEPLOY_MARKERS=0
[[ -d "$ROOT/.git" ]] && DEPLOY_MARKERS=$((DEPLOY_MARKERS+1))
[[ -f "$ROOT/.share_token" ]] && DEPLOY_MARKERS=$((DEPLOY_MARKERS+1))
[[ -d "$ROOT/tools/p1" && -d "$ROOT/webui" ]] && DEPLOY_MARKERS=$((DEPLOY_MARKERS+1))
# 还有别的东西在用这个 python（部署机上有 cs-files 等常驻服务）
# dry-run 和 verify-only 不写任何东西，放行（便于在编排机上做静态检查）
if [[ $DEPLOY_MARKERS -ge 2 && $DRY -eq 0 && $VERIFY_ONLY -eq 0 \
      && "${PROVISION_ALLOW_DEPLOY_HOST:-0}" != "1" ]]; then
  die "拒绝在部署机上执行。这是编排机，不是目标 GPU 机。
     检测到 $DEPLOY_MARKERS 个部署机标记：.git / .share_token / tools+p webui
     本脚本会 pip install torch + clone ComfyUI，约 8-15GB，会撑爆部署机的盘。

     在目标 GPU 机上跑：把 deploy/env/ 拷过去，或 git clone 后再执行。
     如果确实要在部署机上装（例如本机就是 GPU 机），加环境变量：
       PROVISION_ALLOW_DEPLOY_HOST=1 bash $0"
fi

have_internet=1
if ! curl -sI --max-time 5 --connect-timeout 3 https://pypi.org >/dev/null 2>&1; then
  warn "pypi.org 不可达 —— 这台机器装不了东西"
  have_internet=0
fi
if ! curl -sI --max-time 5 --connect-timeout 3 https://github.com >/dev/null 2>&1; then
  warn "github.com 不可达 —— 拉不了 ComfyUI / custom_nodes"
  have_internet=0
fi
if [[ $have_internet -eq 0 ]]; then
  if [[ $VERIFY_ONLY -eq 0 && $DRY -eq 0 ]]; then
    die "无外网。若是智云实例：这是平台特征（DNS 都解析不了），
     智云上请用：平台镜像 / 自制镜像（compshareImage-*）/ 停机实例。
     本脚本用于阿里云 PAI、RunPod、自建 K8s 等有外网的平台。"
  else
    warn "无外网（dry-run / verify-only 模式下继续）"
  fi
fi
ok "$PYTHON $($PYTHON -V 2>&1 | cut -d' ' -f2)"

# ---------------------------------------------------------------- 1. 系统依赖
say "系统依赖"
SYS_PKGS=$(mf base.system_packages | tr -d '[]"' | tr ',' ' ')
if command -v apt-get >/dev/null 2>&1; then
  missing=""
  for p in git wget ffmpeg; do
    command -v "$p" >/dev/null 2>&1 || missing="$missing $p"
  done
  if [[ -n "$missing" ]]; then
    run apt-get update -qq
    # shellcheck disable=SC2086
    run apt-get install -y --no-install-recommends $SYS_PKGS
    ok "已安装:$missing"
  else
    ok "git / wget / ffmpeg 已就位"
  fi
else
  warn "非 apt 系统，请自行确保：$SYS_PKGS"
fi
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')" \
  || warn "ffmpeg 缺失（视频节点会失败）"

# ---------------------------------------------------------------- 2. torch
say "PyTorch"
TORCH_PIN=$(mf python.pins.torch "2.8.0+cu128")
TORCH_AUDIO=$(mf python.pins.torchaudio "2.8.0+cu128")
TORCH_VISION=$(mf python.pins.torchvision "0.23.0+cu128")
INDEX=$(mf python.index_extra "https://download.pytorch.org/whl/cu128")

if [[ $SKIP_TORCH -eq 1 ]]; then
  ok "跳过（--skip-torch）"
else
  cur=$("$PYTHON" -c 'import torch; print(torch.__version__)' 2>/dev/null || echo "none")
  want="${TORCH_PIN%%+*}"
  if [[ "$cur" == "$TORCH_PIN" ]]; then
    ok "已是 $cur"
  elif [[ "$cur" == "$want"* ]]; then
    ok "已装 $cur（与目标 $TORCH_PIN 同主版本，跳过）"
  else
    warn "当前 torch=$cur，目标 $TORCH_PIN"
    run "$PYTHON" -m pip install --upgrade pip
    run "$PYTHON" -m pip install --extra-index-url "$INDEX" \
      "torch==$TORCH_PIN" "torchaudio==$TORCH_AUDIO" "torchvision==$TORCH_VISION"
    [[ $DRY -eq 0 ]] && ok "torch $("$PYTHON" -c 'import torch;print(torch.__version__)')" \
                      && ok "cuda $("$PYTHON" -c 'import torch;print(torch.version.cuda)')"
  fi
fi

# xformers 必须不在（ABI 冲突会让 SeedVR2/SUPIR import 失败）
if "$PYTHON" -c 'import xformers' 2>/dev/null; then
  warn "检测到 xformers —— 与 torch 2.8 ABI 不匹配，卸载"
  run "$PYTHON" -m pip uninstall -y xformers
else
  ok "xformers 未安装（正确）"
fi

# ---------------------------------------------------------------- 3. ComfyUI
say "ComfyUI"
COMFY_REPO=$(mf comfyui.repo "https://github.com/comfyanonymous/ComfyUI")
COMFY_COMMIT=$(mf comfyui.commit)
CONSTRAINTS="$ROOT/$(mf python.constraints_file 'docker/constraints.txt')"
if [[ -d "$COMFY_HOME/.git" ]]; then
  cur=$(
    cd "$COMFY_HOME" && git rev-parse HEAD 2>/dev/null || echo unknown
  )
  if [[ "$cur" == "$COMFY_COMMIT" ]]; then
    ok "已是 $COMFY_COMMIT"
  else
    warn "当前 $cur，切换到 $COMFY_COMMIT"
    run git -C "$COMFY_HOME" fetch --all --tags
    run git -C "$COMFY_HOME" checkout -q "$COMFY_COMMIT"
  fi
else
  run git clone "$COMFY_REPO" "$COMFY_HOME"
  run git -C "$COMFY_HOME" checkout -q "$COMFY_COMMIT"
  ok "已克隆并检出 $COMFY_COMMIT"
fi

say "ComfyUI 依赖"
if [[ -f "$COMFY_HOME/requirements.txt" ]]; then
  if [[ -f "$CONSTRAINTS" ]]; then
    run "$PYTHON" -m pip install --extra-index-url "$INDEX" \
      -c "$CONSTRAINTS" -r "$COMFY_HOME/requirements.txt"
    ok "按 $CONSTRAINTS 锁定版本安装"
  else
    warn "constraints 缺失：$CONSTRAINTS，退回不锁版本"
    run "$PYTHON" -m pip install --extra-index-url "$INDEX" -r "$COMFY_HOME/requirements.txt"
  fi
fi

# ---------------------------------------------------------------- 4. custom_nodes
say "custom_nodes"
if [[ $SKIP_NODES -eq 1 ]]; then
  ok "跳过（--skip-nodes）"
else
  # 从 manifest 逐条读，不用 jq（目标机可能没装）
  mapfile -t NODE_ROWS < <("$PYTHON" - "$MANIFEST" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for n in d.get("custom_nodes") or []:
    print(f"{n['dir']}\t{n['repo']}\t{n['commit']}")
PY
)
  [[ ${#NODE_ROWS[@]} -eq 0 ]] && warn "manifest 里没有 custom_nodes"
  for row in "${NODE_ROWS[@]}"; do
    IFS=$'\t' read -r dir repo commit <<< "$row"
    dest="$COMFY_HOME/custom_nodes/$dir"
    if [[ -d "$dest/.git" ]]; then
      cur=$(git -C "$dest" rev-parse HEAD 2>/dev/null || echo unknown)
      if [[ "$cur" == "$commit" ]]; then
        printf '     · %-34s %s\n' "$dir" "$(printf '%.8s' "$commit")"
        continue
      fi
      run git -C "$dest" fetch --depth 1 origin "$commit" >/dev/null 2>&1 || true
      run git -C "$dest" checkout -q "$commit" || warn "$dir checkout 失败"
    else
      run git clone --filter=blob:none "$repo" "$dest" || { warn "$dir clone 失败"; continue; }
      run git -C "$dest" checkout -q "$commit" || warn "$dir checkout 失败"
    fi
    printf '     · %-34s %s\n' "$dir" "$(printf '%.8s' "$commit")"

    # 依赖安装：过滤 git+ / sam2（会在隔离环境另装一份 torch，卡 10 分钟+）
    req="$dest/requirements.txt"
    if [[ -f "$req" ]]; then
      tmp=$(mktemp)
      grep -vE '^\s*(git\+|-e|sam2)' "$req" > "$tmp" 2>/dev/null || true
      if [[ -s "$tmp" ]]; then
        run "$PYTHON" -m pip install --extra-index-url "$INDEX" -c "$CONSTRAINTS" -r "$tmp" \
          || warn "$dir requirements 安装失败（不阻塞，先跑完再补）"
      fi
      rm -f "$tmp"
    fi
  done
  ok "$((${#NODE_ROWS[@]})) 个节点处理完毕"
fi

# ---------------------------------------------------------------- 5. Manager 禁用
say "禁用 ComfyUI-Manager"
MGR="$COMFY_HOME/custom_nodes/ComfyUI-Manager"
if [[ -d "$MGR" ]]; then
  run mv "$MGR" "$MGR.disabled"
  ok "已改名为 .disabled"
else
  ok "不存在或已禁用"
fi

# ---------------------------------------------------------------- 6. 模型目录
say "模型目录"
if [[ -d "$MODEL_ROOT" ]]; then
  ok "$MODEL_ROOT 存在"
  for d in comfyui/models ModelScope HuggingFace; do
    [[ -d "$MODEL_ROOT/$d" ]] && ok "  $d"
  done
  # 建软链（复用 instance_setup.sh 的清单）
  LINKS="$ROOT/tools/p1/compat_links.txt"
  if [[ -f "$LINKS" && $DRY -eq 0 ]]; then
    n=0
    while IFS=$'\t' read -r d name target; do
      [[ -z "$d" ]] && continue
      mkdir -p "$(dirname "$COMFY_HOME/models/$d/$name")"
      if [[ -e "$target" ]]; then
        ln -sf "$target" "$COMFY_HOME/models/$d/$name"
        n=$((n+1))
      fi
    done < "$LINKS"
    ok "建立 $n 条模型软链"
  fi
else
  warn "$MODEL_ROOT 不存在 —— 权重需要在其它地方挂载/下载"
  warn "智云上是自动挂载的 UPFS；别家云请挂 NAS/OSS/COS，或设 MODEL_ROOT 指向下载目录"
fi

# ---------------------------------------------------------------- 7. 验证
say "验证"
VERIFY_PORT=$(mf service.port 8188)
if ! curl -s -m 3 "http://127.0.0.1:$VERIFY_PORT/object_info" >/dev/null 2>&1; then
  warn "ComfyUI 未在 $VERIFY_PORT 监听 —— 请先启动："
  echo "     $(mf service.start_command)"
  echo ""
  echo "  启动后再跑：bash $0 --verify-only"
  exit 0
fi
if [[ $DRY -eq 0 ]]; then
  out=$("$PYTHON" - "$VERIFY_PORT" <<'PY'
import json, sys, urllib.request
port = sys.argv[1]
try:
    oi = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/object_info', timeout=60))
except Exception as e:
    print(f"FAIL object_info: {e}"); raise SystemExit(0)
print(f"OK nodes={len(oi)}")
# 21 个第三方节点抽样
probe = ["ImpactCompare","ImageResizeKJv2","SeedVR2VideoUpscaler","BOOLConstant",
         "UltimateSDUpscale","WanAnimate2ToVideo","WanAnimate2Cache"]
missing = [p for p in probe if p not in oi]
if missing: print("MISSING " + ",".join(missing))
try:
    ul = oi['UNETLoader']['input']['required']['unet_name'][0]
    print(f"OK unet_visible={len(ul)}")
except Exception:
    print("WARN unet_name 取不到（模型可能未挂载）")
PY
)
  echo "$out" | sed 's/^/     /'
  if echo "$out" | grep -q '^FAIL'; then
    die "环境验证失败"
  fi
  echo "$out" | grep -q '^MISSING' && warn "有节点没加载上，检查启动日志的 IMPORT FAILED"
  ok "验证完成"
fi

echo ""
echo "════════════════════════════════════════════════"
echo " ✓ provision 完成"
echo " 下一步（配置 + 启动）：bash tools/p1/instance_setup.sh <实例ID>"
echo " 或手工启动：$(mf service.start_command)"
echo "════════════════════════════════════════════════"
