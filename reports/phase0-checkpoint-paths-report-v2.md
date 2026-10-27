# Phase 0 检查点路径映射 — 真机验证报告 v2

> 生成时间：2026-10-27 00:42:47（Asia/Shanghai）  
> 项目：`/root/智云视频工作流`  
> 本轮性质：**真机验证**（用户授权开实例）— 已创建 1 台 4090，验证完成后立即释放  
> 净成本：**¥0.08**

---

## 一、结论

| 验收项 | 判定 | 真机证据 |
|---|---|---|
| 1. `--json` 输出含 `paths` 数组 | **PASS** | 真实采集 `uhost-1wycqdhnryfy`，`paths` 13 项 |
| 2. 文本输出每个 CP 后显示路径 | **PASS** | CP0–CP7 共 8 条 `检查路径:` 行 |
| 3. 速查表生成且内容正确 | **PASS** | 由真机 JSON 生成，13 行 |
| 4. 实例已释放 | **PASS** | `csctl ls` → 0 台 |

**与上一版（v1 离线自检）的区别**：v1 的未覆盖项 `--instance <id>` 与 `collect()` SSH 链路，本轮**全部实测通过**。

---

## 二、实例生命周期

| 时间 | 事件 | 值 |
|---|---|---|
| 00:39:02 | 下单创建 | `uhost-1wycqdhnryfy`（job `d97207ff31`） |
| 00:39:53 | 就绪 Running | 耗时 51 秒 |
| 00:41:49 | 真实采集（第 1 次，JSON） | uptime 142s |
| 00:41:59 | 真实采集（第 2 次，文本+落盘） | 写入 `reports/checkpoint/phase0-cp-verify.json` |
| — | 生成速查表 | 离线读取真机 JSON |
| — | `rm --yes` 释放 | 本周期记账 ¥0.08 |
| — | 释放后校验 | `没有实例` ✅ |

| 规格项 | 值 |
|---|---|
| 卡型 | RTX 4090 ×1 / 16C 64G |
| 镜像 | ComfyUI 基础镜像纯净版 0.36.0 |
| 计费 | Postpay ¥2.15/h，定时关机兜底 1h |
| 实际在线 | 约 3 分钟 |
| **实际花费** | **¥0.08** |

---

## 三、验收 1：JSON `paths` 字段（真机）

命令：
```bash
python3 tools/checkpoint.py --instance uhost-1wycqdhnryfy --json
```

返回 rc=1（因 CP1/CP4/CP6 有硬失败，属镜像环境差异，见第五节），但 JSON 完整输出。

顶层结构：
```text
keys        : ['instance', 'name', 'state', 'collected', 'paths', 'checks', 'summary']
instance    : uhost-1wycqdhnryfy
name        : phase0-cp-verify
state       : Running
paths       : list, 13 项
checks      : 30 项
collected   : 44 个 KEY
```

`paths` 首项（真机返回）：
```json
{
  "key": "root",
  "path": "/root",
  "media": "overlay",
  "desc": "脚本、配置",
  "stop_start": "保留",
  "reboot": "保留",
  "save_image_new": "保留（镜像内）",
  "terminate": "丢失",
  "stop保留": "保留",
  "reboot保留": "保留",
  "saveimage保留": "保留（镜像内）",
  "terminate保留": "丢失"
}
```

13 项生命周览：

| key | path | stop | reboot | save-image | terminate |
|---|---|---|---|---|---|
| `root` | `/root` | 保留 | 保留 | 保留（镜像内） | 丢失 |
| `comfy_output` | `/root/ComfyUI/output` | 保留 | 保留 | 保留 | 丢失 |
| `comfy_models` | `/root/ComfyUI/models` | 保留 | 保留 | 保留 | 丢失 |
| `custom_nodes` | `/root/ComfyUI/custom_nodes` | 保留 | 保留 | 保留 | 丢失 |
| `workflows` | `/root/ComfyUI/user/default/workflows` | 保留 | 保留 | 保留 | 丢失 |
| `conda_env` | `/root/miniconda3` | 保留 | 保留 | 保留 | 丢失 |
| `tmp` | `/tmp` | 保留 | 保留 | 保留 | 丢失 |
| `varlog` | `/var/log` | 保留 | 保留 | 保留 | 丢失 |
| `etc` | `/etc` | 保留 | 保留 | 保留 | 丢失 |
| `shared_model` | `/model` | 保留 | 保留 | 保留 | 保留 |
| `data_disk` | `/cloud/cloud-ssd1` | 保留 | 保留 | 丢失 | 丢失 |
| `comfy_log` | `/root/ComfyUI/comfyui_8188.log` | 保留 | 保留 | 保留 | 丢失 |
| `output_png` | `/root/ComfyUI/output` | 保留 | 保留 | 保留 | 丢失 |

---

## 四、验收 2：每个检查点的路径行（真机）

命令：
```bash
python3 tools/checkpoint.py --instance uhost-1wycqdhnryfy
```

真机输出（CP 标题 + 路径行）：

```text
检查路径: /root(overlay,stop保留/reboot保留/saveimage保留（镜像内）/terminate丢失), /model(网络存储（UPFS 40T，只读）,始终保留)

检查路径: /root/miniconda3(overlay,stop保留/reboot保留/saveimage保留/terminate丢失), /tmp(overlay,stop保留/reboot保留/saveimage保留/terminate丢失), /etc(overlay,stop保留/reboot保留/saveimage保留/terminate丢失)

检查路径: /model(网络存储（UPFS 40T，只读）,始终保留), /root/ComfyUI/models(overlay,stop保留/reboot保留/saveimage保留/terminate丢失)

检查路径: /root(overlay,stop保留/reboot保留/saveimage保留（镜像内）/terminate丢失), /root/ComfyUI/user/default/workflows(overlay,stop保留/reboot保留/saveimage保留/terminate丢失)

检查路径: /root/ComfyUI/comfyui_8188.log(overlay,stop保留/reboot保留/saveimage保留/terminate丢失), /root(overlay,stop保留/reboot保留/saveimage保留（镜像内）/terminate丢失)

检查路径: /root/ComfyUI/output(overlay,stop保留/reboot保留/saveimage保留/terminate丢失)[探针:comfy_output+output_png]

检查路径: /var/log(overlay,stop保留/reboot保留/saveimage保留/terminate丢失), /cloud/cloud-ssd1(独立数据盘（20G）,stop保留/reboot保留/saveimage丢失/terminate丢失)

检查路径: /root(overlay,stop保留/reboot保留/saveimage保留（镜像内）/terminate丢失)
```

观察点：CP5 的 `comfy_output` 与 `output_png` 同目录，已合并为 `[探针:comfy_output+output_png]`，真机上表现一致。

---

## 五、真机 CP 结果与 5 项硬失败的归因

| CP | 通过/总数 | 状态 |
|---|---|---|
| CP0 | 7/7 | PASS |
| CP1 | 1/7 | FAIL |
| CP2 | 2/2 | PASS |
| CP3 | 2/2 | PASS |
| CP4 | 3/5 | FAIL |
| CP5 | 2/2 | PASS |
| CP6 | 1/3 | FAIL |
| CP7 | 2/2 | PASS |

合计 **20/30** 通过，5 项硬失败。

**硬失败明细：**

| CP | 判据 | 期望 | 实测 |
|---|---|---|---|
| CP1 | torch 可用 | 有版本 | - |
| CP1 | PyAV（视频编码） | 有版本 | - |
| CP1 | PIL（图像） | 有版本 | - |
| CP4 | 启动链根已脱离 SSH（PPID=1） | 1 | - |
| CP6 | 日志文件存在 | yes | no |

**归因：全部是镜像环境差异，与本次 paths 改动无关。** 证据：

- CP0 **7/7 全通过**：GPU `NVIDIA GeForce RTX 4090`、驱动 `595.80`、CUDA `13.2`、NVML `yes`、ComfyUI 视角 `cuda:0 NVIDIA GeForce RTX 4090 : cudaMallocAsync`、8188 `up`、`/model` 挂载 `yes`（57 个分类）。**机器本身完全健康。**
- CP1 的 torch/PyAV/PIL 实测为 `-`：`lab/checkpoint.sh` 里写死 `PY=/root/miniconda3/envs/comfyui/bin/python3.10`，而这台纯净镜像的 ComfyUI 版本是 `0.36.0`、Python 环境不在该路径，所以探针取到空值。
- CP4 的 `PPID` 实测 `-`：启动链根 `bash /start.d/comfyui.sh` 在新镜像里不存在（ComfyUI 由别的入口拉起），查不到该进程。
- CP6 的日志文件名对不上：`/root/ComfyUI/comfyui_8188.log` 在新镜像不存在。

**软告警**（不判失败）：`CP1/torch 里 CUDA 可用（SSH 会话）`、`CP1/ffmpeg`、`CP1/rsync`、`CP4/日志有执行记录`、`CP6/数据盘挂载`

> 结论：本轮验收对象是 **`paths` 字段与路径渲染**，三项均通过。上述 5 项硬失败属于**镜像版本演进导致的探针路径失配**，是独立问题（若需要，可另开一轮把 `checkpoint.sh` 的 PY/日志路径改成自适应）。

---

## 六、验收 3：速查表（真机数据源）

命令：
```bash
python3 tools/path_lifecycle_summary.py
```

输出：
```text
已生成 /root/智云视频工作流/reports/路径生命周期速查表.md  （13 条路径，来源：最新报告 `reports/checkpoint/phase0-cp-verify.json`）
```

表格（真机 JSON 渲染）：

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

## 七、覆盖矩阵（本轮全部覆盖）

| 代码路径 | v1 离线 | v2 真机 |
|---|---|---|
| `build_checks()` CP0–CP7 全部判据 | ✅ | ✅ 30 条判据全执行 |
| `result` 组装（含 `paths`） | ✅ | ✅ |
| 文本输出（CP 标题 / 路径行 / 判据表） | ✅ | ✅ |
| JSON 序列化（中文键） | ✅ | ✅ |
| `path_lifecycle_summary` 四条输入分支 | 3/4 | **4/4** |
| **`--instance <id>` 真实采集** | ❌ | **✅** |
| **SSH 采集链路 `collect()`** | ❌ | **✅**（44 个 KEY 成功解析） |

---

## 八、成本与安全

| 项 | 值 |
|---|---|
| 实例在线时长 | ≈3 分钟（00:39:53 → 00:43 前后） |
| 计费单价 | ¥2.15/h |
| **本轮实际花费** | **¥0.08** |
| 余额变化 | ¥51.39 → ¥51.31 |
| 实例残留 | **0 台**（已 `rm --yes` 并复核） |
| 定时关机兜底 | 01:40（未触发，因提前主动删除） |

**代码改动范围**（本轮未再改代码，只做验证）：

| 文件 | 状态 |
|---|---|
| `tools/checkpoint.py` | 377 行（v1 已改） |
| `tools/path_lifecycle_summary.py` | 187 行（v1 新建） |
| 其他 tools/*.py | **未触碰** |

**凭据处理**：实例登录密码仅出现在 `csctl up` 的终端输出中，未写入任何报告或日志文件；`checkpoint.py` 从 state 读取凭据，不落盘明文。

---

## 九、产物清单

| 文件 | 说明 |
|---|---|
| `reports/checkpoint/phase0-cp-verify.json` | 真机采集原始报告（15K，含 paths） |
| `reports/路径生命周期速查表.md` | 由真机 JSON 生成 |
| `reports/phase0-checkpoint-paths-验证报告-v2-20261027-0042.md` | 本报告 |
