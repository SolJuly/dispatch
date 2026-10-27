#!/bin/bash
# 存储生命周期探针
#   probe_storage.sh init          写入基线标记，打印各路径状态
#   probe_storage.sh check         比对基线，输出 TSV: 标签<TAB>状态<TAB>值
# 标记内容统一为基线 ID，用于判断"文件还在且内容未变"
set -u
MARK=".csprobe_marker"

# 标签|路径
ITEMS=(
  "root|/root/$MARK"
  "comfy_output|/root/ComfyUI/output/$MARK"
  "comfy_models|/root/ComfyUI/models/$MARK"
  "custom_nodes|/root/ComfyUI/custom_nodes/$MARK"
  "workflows|/root/ComfyUI/user/default/workflows/$MARK"
  "conda_env|/root/miniconda3/$MARK"
  "tmp|/tmp/$MARK"
  "varlog|/var/log/$MARK"
  "etc|/etc/$MARK"
  "shared_model|/model/$MARK"
  "data_disk|/cloud/cloud-ssd1/$MARK"
  "comfy_log|/root/ComfyUI/$MARK"
  "output_png|/root/ComfyUI/output/$MARK"
)

cmd_init() {
  local id="$1"
  echo "BASELINE_ID=$id"
  for it in "${ITEMS[@]}"; do
    local tag="${it%%|*}" p="${it#*|}"
    local d; d="$(dirname "$p")"
    if [ ! -d "$d" ]; then
      printf '%s\tNODIR\t%s\n' "$tag" "$d"
      continue
    fi
    if printf '%s\n' "$id" > "$p" 2>/dev/null; then
      printf '%s\tOK\t%s\n' "$tag" "$p"
    else
      printf '%s\tREADONLY\t%s\n' "$tag" "$p"
    fi
  done
}

cmd_check() {
  local id="$1"
  for it in "${ITEMS[@]}"; do
    local tag="${it%%|*}" p="${it#*|}"
    local d; d="$(dirname "$p")"
    if [ ! -d "$d" ]; then
      printf '%s\tNODIR\t%s\n' "$tag" "$d"
      continue
    fi
    if [ ! -e "$p" ]; then
      printf '%s\tMISS\t%s\n' "$tag" "-"
      continue
    fi
    local got; got="$(cat "$p" 2>/dev/null)"
    if [ "$got" = "$id" ]; then
      printf '%s\tINTACT\t%s\n' "$tag" "$got"
    else
      printf '%s\tCHANGED\t%s\n' "$tag" "$got"
    fi
  done
}

case "${1:-}" in
  init)  cmd_init "${2:-AUDIT-$(date +%s)}" ;;
  check) cmd_check "${2:?需要基线 ID}" ;;
  *) echo "用法: $0 init [ID] | check <ID>"; exit 2 ;;
esac
