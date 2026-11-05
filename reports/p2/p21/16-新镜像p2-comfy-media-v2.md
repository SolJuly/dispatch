# 新镜像 p2-comfy-media-v2（补自定义节点）

> 日期：2026-11-05 13:41–14:03（Asia/Shanghai）
> 目标：按第 2 条建议，做一版带 FaceParsing / SeedVR2 / SUPIR / UltimateSDUpscale / KJNodes / Impact / LayerStyle 的镜像
> 结果：镜像已生成并启动验证；**节点数 993 → 1753**；SeedVR2 在 V100S 上因 bf16 无法加载（需 4090 复核），Qwen 2.1 核心节点仍缺
> 成本：建镜像实例 ¥0.31 + 验证实例 ¥0.04

---

## 1. 镜像

| 项 | 值 |
|---|---|
| 名称 | `p2-comfy-media-v2` |
| ID | `compshareImage-1xbzk9oqquks` |
| 基础 | `p2-comfy-media-v1`（`compshareImage-1x65o3j08tdw`） |
| ComfyUI | 0.36.x |
| 启动脚本 | `/start.d/comfyui.sh` 已加 `unset http_proxy …`（原镜像继承的 `mihomo.kube-system…` 代理在实例上无法解析，会让节点 import 时的下载全部失败） |

已写入 `reports/p2/image.json`：`base` 与 `media` 都指向 v2；后续 `run.py` 建机会自动用新镜像。

## 2. 装了什么

从 GitHub 克隆并装依赖（`--no-deps` 避免升级 torch）：

| 包 | 提供的关键节点 |
|---|---|
| `kijai/ComfyUI-KJNodes` | `ImageResizeKJv2`、`GrowMaskWithBlur` 等 |
| `cubiq/ComfyUI_essentials` | `SimpleMath+`、`BOOLConstant` 等 |
| `ltdrdata/ComfyUI-Impact-Pack` | `ImpactCompare`、`GrowMaskWithBlur` 等 |
| `chflame163/ComfyUI_LayerStyle` | `LayerUtility: If` 等 |
| `ssitu/ComfyUI_UltimateSDUpscale` | `UltimateSDUpscale` |
| `kijai/ComfyUI-SUPIR` | `SUPIRApply`（补 `lightning_utilities`、`torchmetrics`、`ftfy`） |
| `numz/ComfyUI-SeedVR2_VideoUpscaler` | `SeedVR2LoadDiTModel`、`SeedVR2LoadVAEModel`、`SeedVR2VideoUpscaler`（文件已装，V100S 上 import 失败，见下） |
| `ComfyNodePRs/PR-comfyui_face_parsing-d39afaa9` | `BBoxDetect(FaceParsing)`、`FaceParsingModelLoader(FaceParsing)`、`ImageCropWithBBox`、`ImageInsertWithBBox` 等 |

> 说明：`isurulkh/comfyui_face_parsing_New` 是另一套命名（`FaceParsingModelLoaderNew`），不匹配模板，已改名为 `.disabled`；改用 PR 版 `comfyui_face_parsing`。

## 3. 启动验证（V100S 32G，新镜像）

- `/object_info` 的 class 数：**1753**（原 993）。
- 已出现：FaceParsing 9 个节点（除 `BBoxDetectorLoader`）、`SUPIRApply`、`UltimateSDUpscale`、`ImageResizeKJv2`、`ImpactCompare`、`GrowMaskWithBlur`、`LayerUtility: If`、`SimpleMath+`、`BOOLConstant`。
- **SeedVR2 三个节点仍缺**：`numz` 包在 import 时探测 bf16，V100S（sm_70）报 `no kernel image`；文件已装进镜像，换 4090（sm_89）应能加载，但本次没抢到 4090，未复核。
- **Qwen 2.1 两个节点仍缺**：`QwenImage21Cache` / `TextEncodeQwenImage21` 是 ComfyUI 核心新功能（官方模板要求 comfy-core 0.19.1），不在 0.36 镜像里；要升 ComfyUI 或单独打补丁，本次未动。
- **`BBoxDetectorLoader(FaceParsing)` 仍缺**：PR 版没有这个 loader；facegen 模板仍需改图或换包。

## 4. 结论

- 第 2 条建议的“新镜像”已经落地：**SUPIR / UltimateSDUpscale / KJNodes / Impact / LayerStyle / 大部分 FaceParsing 节点可用**。
- 但“有新镜像”≠“模板能跑”：**SeedVR2/SUPIR/facegen/Qwen Edit 仍缺权重**（第 4 步），Qwen 2.1 还缺核心节点，SeedVR2 还缺 4090 复核。
- 下一步优先级：
  1. 抢一台 4090，用新镜像复核 SeedVR2 三个节点是否能加载；
  2. 补 SeedVR2 / SUPIR / facegen 的权重；
  3. Qwen 2.1 若要跑，需升级 ComfyUI 核心或打 Qwen 2.1 补丁（风险较高，单独评估）。

## 5. 回滚

`reports/p2/image.json` 改回 v1 的两个 layer 即可；v2 镜像保留不影响现有流程。
