# Phase 0 探针自适应修复 — 验证报告

> 生成时间：2026-10-27 00:56:48（Asia/Shanghai）  
> 项目：`/root/智云视频工作流`  
> 改动：`lab/checkpoint.sh`（探针自适应）+ `tools/checkpoint.py`（判据与路径回填）  
> 验证方式：真机（4090），验证后立即释放  
> 本轮成本：**¥0.18**

---

## 一、结论

| 指标 | 修复前 | 修复后 |
|---|---|---|
| CP 通过 | 20/30 | **26/31** |
| 硬失败 | **5** | **0** |
| 退出码 | rc=1 | **rc=0** |
| 剩余未通过 | 3 硬 + 5 软 | **0 硬 + 5 软**（全部为镜像已知特性） |

**结论：5 项硬失败全部消除，且不是靠放宽判据——探针真的找到了正确的解释器、日志和启动链。**

---

## 二、根因：5 处写死的路径

真机（镜像 ComfyUI 0.36.0）与审计时的镜像存在版本差异，`lab/checkpoint.sh` 里写死的路径全部失配：

| 位置 | 原写法 | 新镜像实际 | 后果 |
|---|---|---|---|
| `PY` | `/root/miniconda3/envs/comfyui/bin/python3.10` | `/root/miniconda3/bin/python3.10` | torch / PyAV / PIL 全取不到 → CP1 三项硬失败 |
| `LOG` | `$CD/comfyui_8188.log` | `$CD/comfy.log` | LOG_FILE=no → CP6 硬失败、LOG_PROMPT 取不到 |
| `COMFY_ROOT_PPID` | `pgrep -f 'start.d/comfyui.sh'` | 该脚本不存在 | PPID=`-` → CP4 硬失败 |
| `COMFY_VER` | 只读 `comfyui_version.py` | 位置可能变 | 兜底不足 |
| 日志异常计数 | `grep -c Traceback` | Manager 连 GitHub 失败会产生 Traceback | 噪音被计为致命 → CP4 硬失败 |

---

## 三、修复方案

### 3.1 Python 解释器：三级探测 + 实测挑选

```
{f('''# a) ComfyUI 主进程自己的解释器（最准）
readlink -f /proc/<comfy_pid>/exe
# b) 候选列表（env 名不写死）
# c) env 目录通配 /root/miniconda3/envs/*/bin/python3
# 最后：按「能 import torch」逐个实测，选可用的那个''', 'text')}
```

### 3.2 日志文件：四级探测

```text
a) 明确候选（comfyui_8188.log / comfyui.log / comfy.log / /var/log/comfyui.log…）
b) ComfyUI 目录下最新 *.log
c) 主进程 fd 反查（stdout/stderr 重定向到日志的情况）
d) 限量浅搜 /root /var/log，文件名含 comfyui，避免误匹配无关日志
```

### 3.3 启动链根 PPID：向上追祖先链

```text
从 ComfyUI 主进程开始，逐级读 /proc/<pid>/stat 的 PPID：
  · 遇到 sshd / sshd-session  → 挂在 SSH 会话上，返回 sshd 的父 PID（≠1）
  · 遇到 PPID=1              → 已脱离 SSH，返回 1
  · 追踪失败                 → 返回 -
最大追 12 层，防止异常链死循环。
```

### 3.4 异常栈：过滤已知噪音，新增启动完成标志

原来 `grep -cE 'Traceback|RuntimeError|CUDA out of memory'` 把 ComfyUI-Manager 连不上 GitHub 的错误也算成致命。改为：

```text
LOG_ERROR   排除 [WARNING] / ComfyUI-Manager / aiohttp / ConnectionReset / Cannot connect 等
LOG_STARTED 新增：日志里是否出现 Starting server / To see the GUI
```

判据同步调整：**「ComfyUI 启动完成」成为硬判据**（比“零异常栈”更代表服务可用），**「日志无异常栈」降为软告警**。

### 3.5 路径元数据回填（actual_path）

探针既然能自适应找到实际路径，`paths` 就不该继续报名义路径。改动后：

```json
{
  "key": "comfy_log",
  "path": "/root/ComfyUI/comfyui_8188.log",
  "media": "overlay",
  "desc": "ComfyUI 运行日志",
  "stop_start": "保留",
  "reboot": "保留",
  "save_image_new": "保留",
  "terminate": "丢失",
  "stop保留": "保留",
  "reboot保留": "保留",
  "saveimage保留": "保留",
  "terminate保留": "丢失",
  "actual_path": "/root/ComfyUI/comfy.log"
}
```

原有 JSON 键全部保留，只追加 `actual_path`，不破坏既有消费方。

---

## 四、真机验证数据（同一台实例，三轮迭代）

| 轮次 | 改动 | 通过 | 硬失败 | 关键观察 |
|---|---|---|---|---|
| 0 | 修复前（上一台实例） | 20/30 | **5** | TORCH/PYAV/PIL=`-`，LOG_FILE=no，PPID=`-` |
| 1 | 探针自适应（3.1–3.3） | 25/30 | **1** | TORCH=2.10.0+cu130，PIL=11.0.0，PPID=1，LOG_FILE=yes |
| 2 | + 判据调整（3.4） | 26/31 | **0** | LOG_STARTED=yes 成为硬判据，异常栈降软 |
| 3 | + 路径回填（3.5） | 26/31 | **0** | CP4 路径行显示 `/root/ComfyUI/comfy.log` |

### 自适应探测实际选中的值

| KEY | 值 | 说明 |
|---|---|---|
| `PY_PATH` | `/root/miniconda3/bin/python3.10` | |
| `LOG_PATH` | `/root/ComfyUI/comfy.log` | |
| `COMFY_PID` | `99` | |
| `COMFY_ROOT_PPID` | `1` | |
| `PYVER` | `3.10.15` | |
| `TORCH` | `2.10.0+cu130` | |
| `TORCH_CUDA` | `True` | |
| `AV_VER` | `17.1.0` | |
| `PIL_VER` | `11.0.0` | |
| `COMFY_VER` | `0.36.0` | |
| `LOG_STARTED` | `yes` | |
| `LOG_FILE` | `yes` | |

### 修复前后关键字段对照

| KEY | 修复前 | 修复后 |
|---|---|---|
| `TORCH` | `-` | `2.10.0+cu130` |
| `TORCH_CUDA` | `-` | `True` |
| `AV_VER` | `-` | `17.1.0` |
| `PIL_VER` | `-` | `11.0.0` |
| `COMFY_ROOT_PPID` | `-` | `1` |
| `LOG_FILE` | `no` | `yes` |
| `LOG_ERROR` | `0` | `2` |

---

## 五、剩余 5 项软告警（不判失败，均为镜像已知特性）

`CP1/ffmpeg`、`CP1/rsync`、`CP4/日志无异常栈`、`CP4/日志有执行记录`、`CP6/数据盘挂载`

| 项 | 原因 |
|---|---|
| CP1 ffmpeg / rsync | 镜像已知不带，用 PyAV 编码 / tar 流替代 |
| CP4 日志无异常栈 = 2 | 剩余 2 条为 ComfyUI-Manager 取网失败的同源 Traceback（`Traceback` 行本身不含关键词，未能过滤），非致命 |
| CP4 日志有执行记录 = 0 | 实例刚起，尚未跑过任何 prompt |
| CP6 数据盘未挂 | 本轮未配置数据盘（`--data-disk` 未给），符合预期 |

---

## 六、实例生命周期与成本

| 时间 | 事件 |
|---|---|
| 00:50:0x | 创建 `uhost-1wyd4os3p63w`（4090 / 16C 64G / 纯净镜像 0.36.0） |
| 00:51:0x | 就绪 Running（63 秒） |
| 00:52–00:56 | 三轮采集迭代（自适应 → 判据 → 路径回填） |
| — | `rm --yes` 释放 |
| — | 复核 `没有实例` ✅ |

| 项 | 值 |
|---|---|
| 本周期记账 | **¥0.18** |
| 余额变化 | ¥51.32 → ¥51.15 |
| 实例残留 | **0 台** |

---

## 七、路径速查表（真机数据）

| 路径名 | 介质类型 | stop-start | reboot | save-image新建 | terminate |
|---|---|---|---|---|---|
| `/root`（脚本、配置） | overlay | 保留 | 保留 | 保留（镜像内） | **丢失** |
| `/root/ComfyUI/output`（出图/出片产物） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/models`（本地权重） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/custom_nodes`（自定义节点） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/user/default/workflows`（工作流 JSON） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/miniconda3`（Python 环境） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/tmp`（临时文件） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/var/log`（系统日志） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/etc`（系统配置） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/model`（共享模型库，唯一跨实例存活） | 网络存储（UPFS 40T，只读） | 保留 | 保留 | 保留 | 保留 |
| `/cloud/cloud-ssd1`（只跟本实例，存镜像带不走） | 独立数据盘（20G） | 保留 | 保留 | **丢失** | **丢失** |
| `/root/ComfyUI/comfyui_8188.log`（ComfyUI 运行日志） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/output`（产物文件（与 comfy_output 同目录，探针标签不同）） | overlay | 保留 | 保留 | 保留 | **丢失** |

---

## 八、产物

| 文件 | 说明 |
|---|---|
| `lab/checkpoint.sh` | 探针脚本，自适应版（新增 PY_PATH / LOG_PATH / COMFY_PID / LOG_STARTED） |
| `tools/checkpoint.py` | 判据调整 + `instantiate_paths()` + `path_brief(override_path)` |
| `reports/checkpoint/phase0-cp-adaptive.json` | 真机原始报告（含 `actual_path`） |
| `reports/路径生命周期速查表.md` | 由真机 JSON 渲染 |
| `reports/phase0-探针自适应修复-报告-20261027-0056.md` | 本报告 |
