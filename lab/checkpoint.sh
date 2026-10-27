#!/bin/bash
# 实例端检查点采集：只输出 KEY=VALUE，由本地 tools/checkpoint.py 判定
# ---------------------------------------------------------------------------
# 自适应设计（镜像版本差异不该让探针失真）：
#   · Python 解释器：优先用 ComfyUI 主进程自己的 exe，其次候选列表，最后按
#     "能 import torch" 实测挑选
#   · ComfyUI 日志：候选路径 → ComfyUI 目录下最新 *.log → 主进程 fd 反查 → 浅搜
#   · 启动链根 PPID：从主进程向上追，遇 sshd 判定为"挂在 SSH 上"，遇父进程=1
#     判定为"已脱离 SSH"
#   · ComfyUI 版本：comfyui_version.py → import → pyproject.toml
# 所有探测失败一律回退 "-"，绝不因探测失败中断整轮采集。
# ---------------------------------------------------------------------------
CD=/root/ComfyUI

kv() { printf '%s=%s\n' "$1" "$2"; }

# ---------------------------------------------------------------- 1) 主进程
find_comfy_pid() {
  local p
  for pat in 'main.py --listen' 'ComfyUI.*main\.py' 'main\.py.*--port 8188'; do
    p=$(pgrep -f "$pat" 2>/dev/null | head -1)
    [ -n "$p" ] && { echo "$p"; return; }
  done
  echo ""
}
COMFY_PID=$(find_comfy_pid)

# ---------------------------------------------------------------- 2) Python 解释器
py_has_torch() {
  [ -n "$1" ] && [ -x "$1" ] && "$1" -c 'import torch' >/dev/null 2>&1
}
pick_py() {
  local c
  # a) ComfyUI 主进程自己的解释器（最准）
  if [ -n "$COMFY_PID" ] && [ -e "/proc/$COMFY_PID/exe" ]; then
    c=$(readlink -f "/proc/$COMFY_PID/exe" 2>/dev/null)
    case "$c" in *python*) [ -x "$c" ] && { echo "$c"; return; };; esac
  fi
  # b) 常见候选（含 env 名通配，不写死 comfyui 这个名字）
  for c in \
      /root/miniconda3/envs/comfyui/bin/python3 \
      /root/miniconda3/envs/comfyui/bin/python3.10 \
      /root/miniconda3/bin/python3 \
      /opt/conda/envs/comfyui/bin/python3 \
      /opt/conda/bin/python3 \
      /usr/local/bin/python3 \
      /usr/bin/python3; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  # c) env 目录通配
  for c in /root/miniconda3/envs/*/bin/python3 /opt/conda/envs/*/bin/python3; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  command -v python3 2>/dev/null
}
PY=$(pick_py)
# 若首选解释器 import torch 失败，按"能 import torch"重挑一次
if ! py_has_torch "$PY"; then
  for c in $(ls -d /root/miniconda3/envs/*/bin/python3 /opt/conda/envs/*/bin/python3 2>/dev/null) \
           $(command -v python3 2>/dev/null); do
    if py_has_torch "$c"; then PY="$c"; break; fi
  done
fi
[ -x "$PY" ] || PY=python3

# ---------------------------------------------------------------- 3) 日志文件
pick_log() {
  local f
  # a) 明确候选
  for f in "$CD/comfyui_8188.log" "$CD/comfyui.log" "$CD/comfy.log" \
           /root/comfyui_8188.log /root/comfyui.log \
           /var/log/comfyui.log /var/log/comfyui/comfyui.log; do
    [ -f "$f" ] && { echo "$f"; return; }
  done
  # b) ComfyUI 目录下最新 .log
  f=$(ls -t "$CD"/*.log 2>/dev/null | head -1)
  [ -n "$f" ] && { echo "$f"; return; }
  # c) 从主进程 fd 反查（stdout/stderr 重定向到日志的情况）
  if [ -n "$COMFY_PID" ]; then
    for fd in 1 2; do
      f=$(readlink -f "/proc/$COMFY_PID/fd/$fd" 2>/dev/null)
      case "$f" in *.log) [ -f "$f" ] && { echo "$f"; return; };; esac
    done
  fi
  # d) 限量浅搜（收紧到名字含 comfyui，避免误匹配无关日志）
  f=$(find /root /var/log -maxdepth 4 -type f -name '*comfyui*.log' 2>/dev/null | head -1)
  [ -n "$f" ] && { echo "$f"; return; }
  echo ""
}
LOG=$(pick_log)

# ---------------------------------------------------------------- 4) 启动链根 PPID
# 语义：ComfyUI 的启动链是否脱离 SSH。
#   父进程 = 1            → 已脱离（返回 1）
#   链上出现 sshd         → 挂在 SSH 会话上（返回 sshd 的父 PID，≠1）
#   追踪失败              → "-"
chain_root_ppid() {
  local pid="$1" ppid comm depth=0
  [ -z "$pid" ] && { echo "-"; return; }
  while [ "$depth" -lt 12 ]; do
    [ -r "/proc/$pid/stat" ] || { echo "-"; return; }
    ppid=$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null)
    [ -z "$ppid" ] && { echo "-"; return; }
    comm=$(tr -d '\000' < "/proc/$pid/comm" 2>/dev/null)
    case "$comm" in
      sshd|sshd-session|sshd:*) echo "$ppid"; return ;;
    esac
    [ "$ppid" = "1" ] && { echo 1; return; }
    pid="$ppid"
    depth=$((depth + 1))
  done
  echo "-"
}
COMFY_ROOT_PPID=$(chain_root_ppid "$COMFY_PID")

# ---------------------------------------------------------------- 5) ComfyUI 版本
comfy_ver() {
  local v
  v=$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' "$CD/comfyui_version.py" 2>/dev/null | head -1)
  [ -n "$v" ] && { echo "$v"; return; }
  v=$("$PY" -c 'import sys;sys.path.insert(0,"'"$CD"'");import comfyui_version as m;print(m.__version__)' 2>/dev/null)
  [ -n "$v" ] && { echo "$v"; return; }
  v=$(grep -oE '^version[[:space:]]*=[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"' "$CD/pyproject.toml" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
  [ -n "$v" ] && { echo "$v"; return; }
  echo ""
}

# ================================================================ 采集
kv GPU_NAME   "$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
kv GPU_DRIVER "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
kv GPU_MEM    "$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1)"
kv GPU_UTIL   "$(nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader 2>/dev/null | head -1)"
kv CUDA       "$(nvidia-smi 2>/dev/null | grep -oP 'CUDA Version: \K[0-9.]+' || echo -)"

# 探测结果自证（便于排查"自适应到底选了哪个"）
kv PY_PATH    "$PY"
kv LOG_PATH   "$LOG"
kv COMFY_PID  "$COMFY_PID"

kv PYVER      "$($PY -c 'import sys;print(sys.version.split()[0])' 2>/dev/null || echo -)"
kv TORCH      "$($PY -c 'import torch;print(torch.__version__)' 2>/dev/null || echo -)"
kv TORCH_CUDA "$($PY -c 'import torch;print(torch.cuda.is_available())' 2>/dev/null || echo -)"
kv AV_VER     "$($PY -c 'import av;print(av.__version__)' 2>/dev/null || echo -)"
kv CV2_VER    "$($PY -c 'import cv2;print(cv2.__version__)' 2>/dev/null || echo -)"
kv PIL_VER    "$($PY -c 'import PIL;print(PIL.__version__)' 2>/dev/null || echo -)"
kv FFMPEG     "$(command -v ffmpeg >/dev/null 2>&1 && echo yes || echo no)"
kv RSYNC      "$(command -v rsync >/dev/null 2>&1 && echo yes || echo no)"
kv COMFY_VER  "$(comfy_ver)"
kv COMFY_PROC "$COMFY_PID"
kv COMFY_PPID "$([ -n "$COMFY_PID" ] && awk '{print $4}' /proc/$COMFY_PID/stat 2>/dev/null || echo -)"
kv PORT8188   "$( (timeout 3 bash -c '</dev/tcp/127.0.0.1/8188' && echo up) 2>/dev/null || echo down)"
kv NVML_OK    "$(nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1 && echo yes || echo no)"
kv COMFY_DEVICE "$($PY -c "import urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8188/system_stats'));print(d['devices'][0]['name'])" 2>/dev/null || echo -)"
kv COMFY_ROOT_PPID "$COMFY_ROOT_PPID"
kv SHARED_MNT "$(mountpoint -q /model && echo yes || echo no)"
kv SHARED_CAT "$(ls /model/comfyui/models 2>/dev/null | wc -l)"
kv SHARED_CKPT "$(ls /model/comfyui/models/checkpoints/*.safetensors 2>/dev/null | wc -l)"
kv SHARED_UNET "$(ls /model/comfyui/models/diffusion_models/*.safetensors 2>/dev/null | wc -l)"
kv SHARED_VAE  "$(ls /model/comfyui/models/vae/*.safetensors 2>/dev/null | wc -l)"
kv SDXL_FILE   "$(ls /model/comfyui/models/checkpoints/juggernaut* 2>/dev/null | head -1)"
kv SVD_FILE    "$(ls /model/comfyui/models/checkpoints/svd_xt*.safetensors 2>/dev/null | head -1)"
kv LOCAL_CKPT  "$(ls $CD/models/checkpoints/*.safetensors 2>/dev/null | wc -l)"
kv OUTPUTS     "$(ls $CD/output 2>/dev/null | wc -l)"
kv OUTPUT_DIR  "$([ -d $CD/output ] && echo yes || echo no)"
kv INPUT_DIR   "$([ -d $CD/input ] && echo yes || echo no)"
kv WF_DIR      "$([ -d $CD/user/default/workflows ] && echo yes || echo no)"
kv LOG_FILE    "$([ -n "$LOG" ] && [ -f "$LOG" ] && echo yes || echo no)"
kv LOG_SIZE    "$([ -n "$LOG" ] && stat -c %s "$LOG" 2>/dev/null || echo 0)"
kv LOG_PROMPT  "$([ -n "$LOG" ] && grep -c 'Prompt executed' "$LOG" 2>/dev/null || echo 0)"
# 启动完成标志：比"日志里有没有 Traceback"更能代表服务可用性
kv LOG_STARTED "$([ -n "$LOG" ] && grep -qE 'Starting server|To see the GUI' "$LOG" 2>/dev/null && echo yes || echo no)"
# 异常栈计数：排除 [WARNING] 噪声与 ComfyUI-Manager 的取网失败
# （Manager 连 raw.githubusercontent.com 失败在受限网络下必然发生，不代表本体异常）
kv LOG_ERROR   "$([ -n "$LOG" ] && grep -E 'Traceback|CUDA out of memory|RuntimeError' "$LOG" 2>/dev/null \
                   | grep -vE '\[WARNING\]|ComfyUI-Manager|manager_util|raw\.githubusercontent|aiohttp|ConnectionReset|ssl:default|Cannot connect|asyncio' | wc -l || echo 0)"
kv LOG_MTIME   "$([ -n "$LOG" ] && stat -c %Y "$LOG" 2>/dev/null || echo 0)"
kv DISK_PCT    "$(df / | awk 'NR==2{gsub("%","",$5);print $5}')"
kv DISK_FREE   "$(df -h / | awk 'NR==2{print $4}')"
kv DATA_MNT    "$(df /cloud/cloud-ssd1 2>/dev/null | awk 'NR==2{print $6}' || echo none)"
kv DATA_PCT    "$(df /cloud/cloud-ssd1 2>/dev/null | awk 'NR==2{gsub("%","",$5);print $5}' || echo -)"
kv UPTIME_S    "$(awk '{print int($1)}' /proc/uptime)"
kv NOW         "$(date '+%F %T')"
