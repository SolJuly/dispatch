# ComfyUI 0.45.0 升级论证：真机实测

日期：2026-11-12
方式：起一台实例实测，**不跑任何推理**，只对 object_info
成本：**¥0.54**（10 分钟 4090 按量）

---

## 结论先行

**升级可行，风险比预想低得多。** 四项硬指标全过：

| 检查项 | 结果 |
|---|---|
| 节点删除 | **0 个**（1060 → 1094，净增 34） |
| 我们用的 142 个节点，**输出类型变化** | **0 个** ← 最担心的静默错误不存在 |
| 我们用的 142 个节点，**输入参数增减** | **0 个**（必填没多没少，也没互换） |
| 自定义节点 import 失败 | **0 个**（36 个全加载成功） |

顺带挖到一个**比升级本身更有价值**的发现：我们 experimental 里卡住的 3 个工作流，
缺的节点在 0.43.0 就有了——**问题不是版本不够新，是 v4 镜像的核心版本太旧**。

---

## 一、实测方法

```
起机（0.43.0 底座，平台最高）→ 抓基线 object_info（1060 节点）
→ 换成 0.45.0 源码 + 保留原 custom_nodes → 启动成功
→ 抓新版 object_info（1094 节点）→ 逐节点比对
→ 关机
```

**没有跑任何工作流**，所以没有推理开销，也没有 GPU 损耗。

### 遇到的三个环境坑（都已绕过）

| # | 问题 | 处理 |
|---|---|---|
| 1 | 实例访问不了 GitHub（代理 `mihomo.kube-system.svc.cluster.local:7890` 不通，直连 TLS 被断） | 本机已有 0.45.0 仓库，`git archive` 打包 13MB，scp 推过去 |
| 2 | `pip install` 撞 PEP 668（externally-managed-environment） | ComfyUI 跑在 `/root/miniconda3/bin/python`，用它自己的 pip |
| 3 | 实例中途意外关机一次（定时关机设的是 09:15，非我操作） | 重启，数据都在磁盘上 |

> 环境变量里的代理是一直存在的坑：`env | grep -i proxy` 会看到
> `mihomo.kube-system.svc.cluster.local:7890`，那个 host 根本解析不了。
> 任何联网操作都得 `env -u http_proxy -u https_proxy ...`。

---

## 二、版本对比：三个版本号，一个对不上

| 你说的 | 实际查到 |
|---|---|
| ComfyUI **0.45.0** | ✅ 存在（2026-11-10），最新已到 0.45.2 |
| 模板包 **0.11.104** | ⚠️ 存在，但 **0.45.0 要求的是 0.11.105** |
| 我们的模板包 | ❌ 完全不是这套 —— 是 `workflow-templates-core 0.3.350` 等 **7 个分包** |

0.45.0 的 `requirements.txt` 写死：

```
comfyui-frontend-package==1.58.5
comfyui-workflow-templates==0.11.105     ← 单包
comfyui-embedded-docs==0.5.16
comfy-kitchen==0.2.41
```

而 0.43.0 底座自带：

```
comfyui_frontend_package  1.57.6     （差 0.9 个小版本）
comfyui_workflow_templates 0.11.100  （差 5 个）
comfyui-embedded-docs     0.5.15
comfy-kitchen             0.2.40
```

**只差几个小版本。** 启动时给了警告，但**没有崩溃**，功能正常：

```
WARNING: Installed comfyui-frontend-package version 1.57.6
         is lower than the recommended version 1.58.5.
WARNING: Installed comfyui-workflow-templates version 0.11.100
         is lower than the recommended version 0.11.105.
...
[INFO] Starting server
[INFO] To see the GUI go to: http://0.0.0.0:8189
```

---

## 三、节点对比（核心证据）

### 3.1 总量

```
0.43.0 → 1060 个节点
0.45.0 → 1094 个节点
净增 34 个，删除 0 个
```

### 3.2 新增的 34 个全是新模型支持

```
BreezeTextToSpeech / BreezeVoiceClone / BreezeVoiceDesign   ← 语音
MinimaxHailuo03MaxCameraControlNode / ...ExtendVideo / ...LipSync  ← 海螺03
MiniMaxH3ImageConditioning                                  ← H3 图条件
OpenAIGPTImageGIFNode / V3 / SVGNode                        ← OpenAI 图
BatchCameras / CreateCameraOrbit / BakeTextureFromViews     ← 3D 相机
```

跟我们现有 54 个工作流**没有交集**（不冲突也不新增能力）。

### 3.3 我们用的 142 个节点：零破坏

```
输出类型变化:     0 个   ← 静默错误的唯一来源，不存在
必填参数被删:     0 个
新增必填参数:     0 个
必填↔可选互换:    0 个
```

**这是最好的结果。** 意味着现有工作流图**一行都不用改**。

### 3.4 自定义节点：36 个全过

```
0.43.0: 36 个自定义节点
0.45.0: 36 个（零损失、零新增）
IMPORT FAILED / ImportError 计数: 0
```

V2→V3 节点格式换代（我们之前担心的）**没有破坏兼容层**。

---

## 四、比升级更有价值的发现

之前审计报告里写「3 个 experimental 工作流缺节点，跑不了」：

```
pack_image_qwen_image_2_1_image_edit      → QwenImage21Cache, TextEncodeQwenImage21
pack_image_qwen_image_2_1_background_removal → 同上
pack_templates_hellorob_facegen_skindetail_upscale → BBoxDetectorLoader(FaceParsing)
```

实测发现：

```
QwenImage21Cache        0.43.0 就有 ✅   0.45.0 也有 ✅   模块: comfy_extras.nodes_qwen
TextEncodeQwenImage21   0.43.0 就有 ✅   0.45.0 也有 ✅   模块: comfy_extras.nodes_qwen
```

**这两个节点不是"新版本才有"，是"我们的镜像版本太旧"。**

v4 镜像的核心是 `3c80da7f`（0.3.75 系，2026-09-18），而这两个节点在 0.43.0（2026-10-29）就有了。

**所以那 2 个工作流不用等 0.45.0，升到 0.43.0 就能解锁。**

第三个（`BBoxDetectorLoader(FaceParsing)`）仍然缺——那是 face_parsing 自定义节点的问题，
`comfyui_face_parsing` 的 PR 分支没这个类。两个版本都没有，需要单独处理。

---

## 五、会多出多少（回答原问题）

### 体积

| 方案 | 底座 | 估算体积 | 存储费 |
|---|---|---|---|
| 现状 v4 | 0.3.75（71GB） | 70GB | ¥17/月 |
| 升 0.45.0，沿用 0.3.75 底座 | 71GB | **~73GB**（+3） | ¥17.5/月 |
| 升 0.43.0/0.45.0，换 0.43.0 底座 | 100GB | **~100GB**（+30） | **¥24/月** |

0.45.0 源码本身只比 0.43.0 大一点（541 vs 500+ 个 py 文件），**代码体积可以忽略**。
唯一的大头是底座体积差。

### 版本跨度

```
v0.3.75 → v0.45.0 = 65 个 tag，跨 53 天
新格式节点（V3）：181 → 555 个（3 倍）
```

但因为兼容层完整，**跨度大不代表会坏**。

---

## 六、真正的风险（三条，都不在代码层面）

**① 平台没有 0.45.0 底座**

```
平台最高：ComfyUI基础镜像纯净版0.43.0（compshareImage-1x98m25rrumc，100GB，11-03）
```

且 `docker/README.md` 早写过：CompShare 只能用自己的镜像格式，不能从 OCI 拉。

所以路径是：0.43.0 底座起机 → 换 0.45.0 源码 → save 成 v5 镜像。
（这次实测走的就是这条路，可行。）

**② 0.45.0 才两天大**

```
v0.45.0  2026-11-10
v0.45.1
v0.45.2  ← 两天连发两个补丁
```

上游自己在快速修。建议**等到 0.45.x 稳定几天**，或者直接用 0.45.2。

**③ 没跑过真实推理**

这次只验了 object_info（接口契约）。**推理路径、显存调度、模型加载都没测**。
那需要跑现有 54 个工作流做回归——另一笔成本，得单独排。

---

## 七、建议

**分两步走，不要一步到位：**

**第一步（低成本、高收益）：升到 0.43.0**

- 底座现成，不用换（平台提供 0.43.0）
- 直接解锁 `qwen_image_2_1` 两个工作流
- 风险最低（官方底座自带版本，不是我们手工跳）

**第二步（可选）：升到 0.45.x**

- 等 0.45.2 稳定几天再做
- 或者跳过 0.45，等 0.46
- 收益主要是新模型支持（海螺03、Breeze 语音），我们暂时不需要

**无论哪一步，都要做的验证：**

1. `object_info` 对比（这次的方法，¥0.5）
2. 跑 3~5 个工作流冒烟（图、视频、放大各一个，¥2~3）
3. 全量 54 个回归（¥10~15，最后再做）

---

## 八、证据文件

| 文件 | 内容 |
|---|---|
| `reports/p20/evidence/oi-0430.json` | 0.43.0 的 1060 个节点定义（基线） |
| `reports/p20/evidence/oi-0450.json` | 0.45.0 的 1094 个节点定义（对比） |

两份都是真实实例上抓的 `/object_info`，可离线复现对比。

实例 `uhost-1xln2wzbbl7g` 已关机（磁盘保留，含两棵树：`/root/ComfyUI` = 0.45.0，
`/root/ComfyUI_043` = 0.43.0 原版）。要继续验证可以直接开机，不用重装。
