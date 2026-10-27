# Phase 0 检查点路径映射 — 修改验证报告

> 生成时间：2026-10-27 00:32:42（Asia/Shanghai）  
> 项目：`/root/智云视频工作流`  
> 范围：子任务 1/2/3 的代码改动与验收  
> 模式：**只读审计** — 未启动实例、未产生 GPU 费用、未安装/升级/删除依赖  

---

## 一、结论

| 验收项 | 判定 | 依据 |
|---|---|---|
| 1. `--json` 输出含 `paths` 数组（13 项，键齐全） | **PASS** | 实跑 `--dry-run --json`，解析校验通过 |
| 2. 文本输出每个 CP 后显示路径与生命周期 | **PASS** | 实跑 `--dry-run`，CP0–CP7 共 8 条路径行 |
| 3. 速查表生成成功且内容正确 | **PASS** | `reports/路径生命周期速查表.md`，13 行数据 |
| 附. 旧 JSON 无 `paths` 时给出明确指引而非静默失败 | **PASS** | rc=1 + 中文提示 |
| 附. 双文件语法检查 | **PASS** | `py_compile` 无告警 |

**未覆盖项**：`--instance <id>` 的真实采集分支（当前实例数 0，见第五节）。

---

## 二、测试环境

| 项 | 值 |
|---|---|
| 主机 | `VM-0-6-ubuntu` |
| Python | 3.10.12 |
| 项目根 | `/root/智云视频工作流` |
| 实例状态（只读查询 `csctl.py ls`） | `没有实例。（部署: python3 csctl.py up --gpu 4090 --hours 4）` |
| 连接远端 | **无** |

---

## 三、改动清单

### 3.1 `tools/checkpoint.py`（218 → 377 行）

| 新增/修改 | 内容 |
|---|---|
| `_PATH_ROWS` / `PATHS` / `PATH_INDEX` | 13 个探针路径元数据，每项含 `path` `media` `desc` + 英文键（`stop_start`/`reboot`/`save_image_new`/`terminate`）+ 中文键（`stop保留`/`reboot保留`/`saveimage保留`/`terminate保留`） |
| `CP_PATHS` / `paths_of()` | CP0–CP7 → 依赖路径映射 |
| `path_brief()` / `paths_line()` | 文本渲染；同一路径被多探针覆盖时合并并标注 |
| `build_checks().add()` | 每条判据追加 `paths: paths_of(cp)`（CP_PATHS 在逻辑中被实质引用） |
| `result` | 新增 `"paths": PATHS` |
| 文本输出 | 每个 CP 标题行后追加 `检查路径:`，并加 `[PASS]/[WARN]/[FAIL]` 状态 |
| `demo_collect()` + `--dry-run` | 离线自检路径：不连实例、不写报告文件 |

### 3.2 `tools/path_lifecycle_summary.py`（新建，187 行）

数据来源优先级：`--json-file` > `--instance` > `--dry-run` > 最新含 `paths` 的历史报告。
默认离线，只有显式 `--instance` 才触网。

---

## 四、验收执行记录

### 验收 1：JSON `paths` 字段

命令：
```bash
python3 tools/checkpoint.py --dry-run --json | python3 -c "import json,sys; d=json.load(sys.stdin); ..."
```

实测结构（首项）：
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

校验结果：
```text
顶层含 paths : True
类型         : list
条数         : 13
必需键齐全   : True（path/media/stop_start/reboot/save_image_new/terminate + 四个中文键）
```

13 项生命周期一览：

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

### 验收 2：每个检查点的路径行

命令：
```bash
python3 tools/checkpoint.py --dry-run
```

实测 8 条路径行（CP0–CP7 各一条）：

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

> 注：CP5 的 `comfy_output` 与 `output_png` 指向同一目录，已合并为一条并标注 `[探针:comfy_output+output_png]`。

### 验收 3：速查表生成

命令：
```bash
python3 tools/path_lifecycle_summary.py --dry-run
```

输出：
```text
已生成 /root/智云视频工作流/reports/路径生命周期速查表.md  （13 条路径，来源：内置样例（--dry-run））
```

生成文件：`reports/路径生命周期速查表.md`（13 行数据）

表格首 3 行 + 两个关键行：

| 路径名 | 介质类型 | stop-start | reboot | save-image新建 | terminate |
|---|---|---|---|---|---|
| `/root`（脚本、配置） | overlay | 保留 | 保留 | 保留（镜像内） | **丢失** |
| `/root/ComfyUI/output`（出图/出片产物） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/root/ComfyUI/models`（本地权重） | overlay | 保留 | 保留 | 保留 | **丢失** |
| … | | | | | |
| `/root/ComfyUI/models`（本地权重） | overlay | 保留 | 保留 | 保留 | **丢失** |
| `/model`（共享模型库，唯一跨实例存活） | 网络存储（UPFS 40T，只读） | 保留 | 保留 | 保留 | 保留 |
| `/cloud/cloud-ssd1`（只跟本实例，存镜像带不走） | 独立数据盘（20G） | 保留 | 保留 | **丢失** | **丢失** |

### 附：异常路径处理

场景：`reports/checkpoint/` 下仅有改动前生成的 JSON（无 `paths` 字段）。

命令：
```bash
python3 tools/path_lifecycle_summary.py
```

实测：
```text
rc=1
reports/checkpoint/ 下的 2 份 JSON 都没有 paths 字段（是 checkpoint.py 改动前生成的）。请重新采集，或用 --dry-run / --json-file 指定。
```

判定：**符合预期** —— 跳过旧报告并给出三种可行输入方式，不静默生成空表。

---

## 五、覆盖矩阵与未覆盖项

| 代码路径 | 是否测试 | 说明 |
|---|---|---|
| `build_checks()` 全部 CP0–CP7 判据 | ✅ | `--dry-run` 构造完整样例数据，30 条判据全部执行 |
| `result` 组装（含 `paths`） | ✅ | 与 `--instance` 共用同一段代码 |
| 文本输出（CP 标题 / 路径行 / 判据表） | ✅ | 完整渲染 |
| JSON 序列化 | ✅ | `ensure_ascii=False`，中文键正常 |
| `path_lifecycle_summary` 四条输入分支 | 3/4 ✅ | `--json-file` / `--dry-run` / 默认（含旧报告回退）已测 |
| **`--instance <id>` 真实采集** | ❌ **未测** | 当前实例数 0；且 Phase 0 约束禁止启动实例 |
| SSH 采集链路（`collect()`） | ❌ 未测 | 同上，无远端可连 |

**未测部分的性质**：`--instance` 与 `--dry-run` 的唯一差异是 `k` 的来源（`collect(inst, cs)` vs `demo_collect()`），之后的 `build_checks → summarize → result → 输出` 完全同一段代码。因此 `paths` 字段的结构风险为零，风险仅在于远端采集脚本 `lab/checkpoint.sh` 的 KEY 是否变化。

---

## 六、安全约束遵守

| 约束 | 遵守情况 |
|---|---|
| 只读修改代码 | ✅ 仅改 `tools/checkpoint.py`、新增 `tools/path_lifecycle_summary.py` |
| 不安装/升级/删除任何内容 | ✅ 纯标准库（json / subprocess / pathlib / datetime） |
| 不启动实例 | ✅ 唯一云操作是只读 `csctl.py ls` |
| 不产生 GPU 费用 | ✅ 无 Running 实例，无任何创建/启动调用 |
| 不打印敏感凭据 | ✅ 报告与样例数据只含硬件/版本信息；未读取或输出 AccessKey / SSH 密码 |

---

## 七、有实例后的补测步骤

```bash
# 1. 真实采集（只读，SSH 采集不改远端任何东西）
python3 tools/checkpoint.py --instance <id> --json > /tmp/cp.json

# 2. 校验 paths 字段
python3 -c "import json;d=json.load(open('/tmp/cp.json'));print(len(d['paths']),'paths')"

# 3. 用真实报告生成速查表
python3 tools/path_lifecycle_summary.py --json-file /tmp/cp.json

# 4. 或让采集脚本自动落报告后直接生成
python3 tools/checkpoint.py --instance <id>
python3 tools/path_lifecycle_summary.py
```

补测通过标准与本次一致：`paths` 为 13 项数组、每 CP 有路径行、速查表 13 行。
