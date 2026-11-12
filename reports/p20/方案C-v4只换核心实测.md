# 方案 C 实测：v4 镜像只换核心到 0.43.0

日期：2026-11-12
成本：约 ¥0.9（含两次意外关机后的重启）
结论：**可行，且比换底座省 ¥6.5/月**

---

## 一句话

在现有 v4 镜像上，只换 ComfyUI 核心代码 + 4 个配套包，**节点和权重一个不动**，
就能升到 0.43.0。节点 1757 → 1818，**零丢失**，现有工作流**零影响**。

---

## 一、为什么值得试这个方案

| 方案 | 体积 | 存储费 | 手工步骤 |
|---|---|---|---|
| A：换 0.43.0 底座 | ~100GB | ¥24/月 | 零（平台自带） |
| B：升 0.45.x | ~100GB | ¥24/月 | 换源码 + 装依赖 + 绕 GitHub 代理 |
| **C：v4 镜像只换核心** | **~73GB** | **¥17.5/月** | **换核心 + 装 4 个包** |

方案 C 每年轻 ¥78。代价是多两步手工操作。

---

## 二、最小改动集（这是本方案的核心产出）

### 必须一起做的两件事

**① 换核心代码目录**

0.43.0 新增了几个目录，漏一个就起不来。实测踩到：

```
ImportError: cannot import name 'governance' from 'app'
```

必须覆盖的目录清单：

```
app/  api_server/  alembic_db/  blueprints/  comfy_api_nodes/
comfy/  comfy_api/  comfy_config/  comfy_execution/  comfy_extras/
comfy_aimdo/  comfy_kitchen/  middleware/  utils/
```

以及文件：`nodes.py server.py main.py node_helpers.py protocol.py
comfyui_version.py pyproject.toml requirements.txt openapi.yaml`

**② 这 4 个配套包必须一起升**（少了就起不来）

| 包 | v4 现有 | 0.43.0 要求 |
|---|---|---|
| `comfyui-frontend-package` | 1.53.6 | **1.57.4** |
| `comfyui-workflow-templates` | 0.11.62 | **0.11.97** |
| `comfy-aimdo` | 0.5.5 | **0.6.0** |
| `comfy-kitchen` | 0.2.35 | **0.2.40** |

只换核心不升包，会直接崩：

```
File "comfy/text_encoders/llama.py", line 9
    from comfy_kitchen.int8_decode import Int8DecodeCache
ModuleNotFoundError: No module named 'comfy_kitchen.int8_decode'
```

装包用 `--no-deps`，避免连带升级 torch 等其他依赖：

```bash
pip install --no-deps \
  'comfyui-frontend-package==1.57.4' \
  'comfyui-workflow-templates==0.11.97' \
  'comfy-aimdo==0.6.0' \
  'comfy-kitchen==0.2.40'
```

### 不用动的

- `custom_nodes/`（13 个包，289MB）
- `models/`（14GB，全是软链）
- `user/`
- torch 2.8.0+cu128（**没升级，也没必要升**）

---

## 三、实测结果

### 基线：v4 镜像原样

```
节点数: 1757        ← 与生产记录完全一致
核心:   3c80da7f (v0.36.0 系)
torch:  2.8.0+cu128
节点包: 13 个
```

### 换核心后：1818 节点

```
净增: 61 个
丢失: 0 个   ✅
```

启动只剩一个无害警告：

```
WARNING: Installed comfyui-embedded-docs version 0.5.11
         is lower than the recommended version 0.5.15.
[INFO] Starting server
[INFO] To see the GUI go to: http://0.0.0.0:8190
```

`comfyui-embedded-docs` 是文档包，不影响节点，可以不升。

### 我们实际用到的 161 个节点

```
两版都有: 158 个
新版没有:   1 个（BBoxDetectorLoader(FaceParsing)，本来就是自定义节点缺失）
```

**签名变化 4 个，全部是纯增量：**

| 节点 | 变化 | 影响 |
|---|---|---|
| `TextGenerate` | output `[STRING]` → `[STRING, STRING]`<br>optional 加 `system_prompt` | 无 |
| `TextGenerateLTX2Prompt` | 同上 | 无 |
| `WanImageToVideo` | optional 加 `ref_pad_image` | 无 |
| `QwenImageDiffsynthControlnet` | optional 加 `start_percent` / `end_percent` | 无 |

**为什么输出多一个 STRING 不影响我们**——查了全部引用点：

```
15 处引用，全部用 idx 0，没有一处用 idx 1
```

新增的输出排在索引 1（末尾），索引 0 不变，按位置取的连接全部安全。

---

## 四、验证边界（必须说清）

这次验证了什么：

- ✅ 核心能启动
- ✅ 节点零丢失（1757 → 1818）
- ✅ 接口契约（object_info）兼容
- ✅ 我们 161 个节点的签名没被破坏

**没验证的：**

- ❌ **没有跑任何真实推理**。显存调度、模型加载、实际出图都没测
- ❌ 13 个自定义节点只验了"能加载"，没验"能算"
- ❌ 0.43.0 的 `qwen_image_2_1` 节点能列出，但没验证能否真的生成

**下一步必须做**：跑 3~5 个工作流冒烟（图/视频/放大各一），约 ¥2~3。

---

## 五、过程中踩的三个坑（做落地脚本时要写进去）

**① 实例访问不了 GitHub**

```
环境变量: https_proxy=http://mihomo.kube-system.svc.cluster.local:7890
这个 host 解析不了 → git clone/fetch 全失败
去掉代理也不行: gnutls_handshake() failed
```

解法：本机 `git archive v0.43.0 | gzip` 打 13MB 包，scp 推过去。

**② pip 撞 PEP 668**

```
error: externally-managed-environment
```

解法：ComfyUI 跑在 `/root/miniconda3/bin/python`，用它的 pip，
并加 `env -u http_proxy -u https_proxy`。

**③ 实例两次意外关机**

定时关机设的是 10:09，但 09:08 起机后 09:21 就关了。不是我的操作，
也不是 reaper（那台机器不在 p1-/p2- 命名空间）。

**这会让长任务（换核心 + 装包 + 验证）被打断。** 落地脚本要能断点续跑。

---

## 六、结论与建议

**方案 C 成立。** 三选一的话：

```
如果只想要 qwen_image_2_1 解锁：
  → 方案 C（省 ¥6.5/月，多两步手工）

如果追求操作最简单：
  → 方案 A（换 0.43.0 底座，零手工，¥24/月）

如果要 0.45 的新模型（海螺03/Breeze语音）：
  → 方案 B，但建议等 0.45.2 稳定
```

**无论哪个方案，落地前都要补一步：跑真实推理冒烟。**
这次只验了接口，没验算得对不对。

---

## 七、证据

| 文件 | 内容 |
|---|---|
| `reports/p20/evidence/oi-v4-core43.json` | v4+0.43核心 的 1818 节点定义 |
| `reports/p20/evidence/oi-0430.json` | 纯 0.43.0 底座 的 1060 节点 |
| `reports/p20/evidence/oi-0450.json` | 0.45.0 的 1094 节点 |
| `reports/p2/p21/evidence/objectinfo-v4.json` | 原 v4 的 1757 节点（既有） |

实例 `uhost-1xlp0lbuo6m6` 已关机，磁盘保留（含已装好的 v4+0.43 环境，
下次开机可直接跑推理验证，不用重装）。
