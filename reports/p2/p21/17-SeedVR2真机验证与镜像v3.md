# 4090 复核 SeedVR2 + 镜像 v3 + 注册表接入

> 日期：2026-11-05 17:13–17:52（Asia/Shanghai）
> 目标：接报告 16 的第 1 条，抢 4090 复核 SeedVR2 三个节点，顺带把权重和镜像落实
> 结果：**SeedVR2 在 4090 上跑通**（1024→2048，作业 PASS）；根因不是 bf16，是 **xformers 与 torch 的 ABI 不匹配**；新镜像 `p2-comfy-media-v3` 已生成；`seedvr2_3b_upscale` 已进注册表并 `compat verify`
> 成本：4090 复核/调试 ¥1.32 + 正式作业 ¥0.0502

---

## 1. 结论先说

1. **报告 16 里“V100S 缺 bf16”的判断不准确**。4090 上同样 `IMPORT FAILED`，真实报错是：
   `xformers/flash_attn_3/_C.so: undefined symbol: _ZN3c104cuda29c10_cuda_check_implementationEiPKcS2_ib`
   —— `xformers 0.0.32.post2` 是按旧 torch 编的，而镜像是 `torch 2.10.0+cu130`；`diffusers` 一 import `xformers.ops` 就把整个 SeedVR2 包带崩，与显卡架构无关。
2. **`pip uninstall -y xformers` 后恢复**：`1757` 个 class、**0 个 IMPORT FAILED**，`SeedVR2LoadDiTModel` / `SeedVR2LoadVAEModel` / `SeedVR2VideoUpscaler` 三个节点全部出现；ComfyUI 改用 PyTorch SDPA。
3. **权重不用下载**：共享库 `/model/ModelScope/mirror013/SeedVR2_comfyUI/` 里已有 3B/7B fp8 + `ema_vae_fp16.safetensors`，镜像里 `/root/ComfyUI/models/SEEDVR2/` 已有软链；`SeedVR2LoadDiTModel` / `SeedVR2LoadVAEModel` 的候选列表都能看见。
4. **核心 UNETLoader 不认 SeedVR2**：`comfy/model_detection` 能识别出 `image_model=seedvr2`，但没有对应 model class，报 `Could not detect model type`。所以工作流走 numz 自定义节点，不走 `UNETLoader + VAELoader`。
5. **真机出图**：1080×1080 → 2048×2048，模型 3B fp8，1 步，单图 21 秒；出图与输入构图一致、细节明显更实（见 `evidence/seedvr2-photo-2048.png`）。

## 2. 4090 复核过程

| 步骤 | 结果 |
|---|---|
| 第一台 4090（`uhost-1xc62xw7tm41`） | 开机后 `nvidia-smi` 报 `Failed to initialize NVML: Unknown Error`，ComfyUI 未起来；销毁重开（平台个别宿主问题，非镜像问题） |
| 第二台 4090（`uhost-1xc6shrbp6e8`，24G） | 正常；`p2-comfy-media-v2` 启动，`1753` class，SeedVR2 `IMPORT FAILED` |
| 定位 | 日志指到 `xformers/flash_attn_3/_C.so` 符号缺失；`torch 2.10.0+cu130` vs `xformers 0.0.32.post2` |
| 修复 | `pip uninstall -y xformers`（不降 torch、不装新 xformers） |
| 复测 | `1757` class、`0 IMPORT FAILED`；`SeedVR2Conditioning` / `SeedVR2Preprocess` / `SeedVR2PostProcessing` 等核心节点也在 |

## 3. 权重与路径

```
/model/ModelScope/mirror013/SeedVR2_comfyUI/      （只读共享库）
  ema_vae_fp16.safetensors                        501,324,814 B
  seedvr2_ema_3b_fp8_e4m3fn.safetensors         3,391,544,696 B
  seedvr2_ema_7b_fp8_e4m3fn.safetensors         8,239,729,704 B
  seedvr2_ema_7b_sharp_fp8_e4m3fn.safetensors   8,239,729,704 B

/root/ComfyUI/models/SEEDVR2/                    （镜像内软链，指向上面）
```

> 注意：`/model` 是**只读** UPFS 挂载（`fuse.upfs (ro)`），不能在实例上往里补文件；新增权重只能靠镜像或每次下载。SeedVR2 这三个文件是平台库自带，所以不用动。

## 4. 真机验证

手工 API 图（`SeedVR2LoadDiTModel` + `SeedVR2LoadVAEModel` + `SeedVR2VideoUpscaler`）：

| 项 | 值 |
|---|---|
| prompt | `e0e51364-509d-4cae-ae2b-96481340928d` |
| 输入 | `in-f9bd4904.jpg` 1080×1080 |
| 参数 | `resolution=2048`、`seed=42`、`color_correction=lab`、`batch_size=1` |
| 输出 | `photo_00001_.png` **2048×2048 RGB**，4.8 MB |
| 证据 | `evidence/seedvr2-photo-2048.png`、`evidence/seedvr2-4090-run.json` |

## 5. 镜像 v3

| 项 | 值 |
|---|---|
| 名称 / ID | `p2-comfy-media-v3` / `compshareImage-1xc7km2mb0ob` |
| 基础 | `p2-comfy-media-v2`（`compshareImage-1xbzk9oqquks`） |
| 变更 | 仅移除 `xformers`（不兼容 ABI），其余与 v2 一致 |
| 验证 | 用 v3 建机跑正式作业：`镜像 p2-comfy-media-v3`、`2 项模型资产可见`、`5 节点渲染`、PASS |
| 配置 | `reports/p2/image.json` 的 `base` / `media` 已切到 v3 |

> v2 保留可回滚；v1/v2/v3 都是 ~210 GB 镜像，注意存储占用。

## 6. 注册表接入

- 模型：`registry/models/seedvr2_3b.json`
  - `dit` → `SeedVR2LoadDiTModel.model`，匹配 `seedvr2_ema_3b_fp8`
  - `vae` → `SeedVR2LoadVAEModel.model`，匹配 `ema_vae_fp16`
- 工作流：`registry/workflows/seedvr2_3b_upscale.json` + `workflows/seedvr2_3b_upscale.api.json`
  - 参数：`image`（必填）、`resolution`（默认 2048）、`seed`、`color_correction`
  - 输出：SaveImage 单图 PNG
- 正式作业：`p1-20261105T1739-94934d` → **DONE / PASS**
  - 2048×2048 PNG，4,997,918 B，`sha256:7c570735…`
  - 实例 `uhost-1xc7qaoqvtqo`，1.4 分钟，**¥0.0502**
- 兼容性：`p20ctl compat verify --workflow seedvr2_3b_upscale --model seedvr2_3b --evidence p1-20261105T1739-94934d` → **verified**
- 注册表：58 工作流 / 48 模型 / **verified 19** / equivalent 2 / experimental 37

## 7. 成本

| 项 | 金额 |
|---|---|
| 第一台 4090（NVML 坏宿主，~10 分钟 + 空转） | ≈¥0.49 |
| 第二台 4090（探测 + 下载尝试 + 两次出图 + 快照，~40 分钟） | ≈¥0.83 |
| 正式作业（v3，1.4 分钟） | ¥0.0502 |
| 镜像 v3 快照 | 无额外算力费（占存储） |
| 余额 | **¥16.27** |

## 8. 还没做的

1. **SUPIR**：节点能 import，但模板的 `ModelPatchLoader` 没有文件输入（私有配置），`juggernautXL_v9Rdphoto2Lightning` / `qwen3.5_4b_bf16` 也不在库里；要单独拼图或补权重。
2. **facegen**：还差 `BBoxDetectorLoader(FaceParsing)`、Juggernaut XL v9、8xNMKD、皮肤 LoRA 等；节点和权重都不齐。
3. **Qwen 2.1**：`QwenImage21Cache` / `TextEncodeQwenImage21` 仍是核心缺口，需要升级 ComfyUI 或打补丁，风险高，未动。
4. **视频 SeedVR2**：同一套节点，`batch_size` 要按 4n+1（默认 5）；本次只验了单图，视频放大没跑。
5. **V100S 上的旧结论要更正**：报告 16 的“V100S 缺 bf16”应改为“xformers ABI 不匹配”；V100S 装 v3 后大概率也能加载（未复测）。

## 9. 回滚

`reports/p2/image.json` 改回 v2（`compshareImage-1xbzk9oqquks`）即可；v3 镜像保留不影响现有流程。去掉 `seedvr2_3b_upscale` 的 `verified` 状态需直接改 `registry/compat.json`（不推荐）。

## 10. 镜像内容核验（3090 新实例，18:03）

用 v3 直接新建一台 3090（不手工跑任何安装命令），确认“节点到底在不在镜像里”：

| 检查 | 结果 |
|---|---|
| `readlink -f /root/ComfyUI/custom_nodes/*` | 13 个启用 + 2 个 `.disabled`，**全部解析到自身**（真实目录，不是 `/model` 软链） |
| 体积 / 时间 | `custom_nodes` 合计 **368 MB**，目录 mtime `11-05 13:53`（v2 构建时）→ 在镜像层 |
| pip 依赖 | `lightning-utilities 0.15.3`、`torchmetrics 1.9.0`、`ftfy 6.3.1` 均在 `/root/miniconda3/.../site-packages`；`xformers` 已不存在 |
| `/object_info` | **1757 class**、**0 IMPORT FAILED** |
| 关键节点 | `SeedVR2LoadDiTModel/LoadVAEModel/VideoUpscaler`、`SUPIRApply`、`UltimateSDUpscale`、`ImageResizeKJv2`、`FaceParsingModelLoader(FaceParsing)`、`GrowMaskWithBlur`、`ImpactCompare`、`LayerUtility: If `、`SimpleMath+`、`BOOLConstant` 全部存在 |
| 例外 | `BBoxDetectorLoader(FaceParsing)` 仍报 `KeyError: 'ultralytics_bbox'`：节点代码在，但 `folder_paths` 没注册这个目录，`/object_info` 取不到 → facegen 的缺口是配置/权重，不是“没装” |

**不在镜像里的**：
- 模型权重（`/model` 是只读 UPFS，镜像里只是软链）；
- `instance_setup.sh` 每次开机临时做的两个 Qwen 软链（`qwen_std_vae`、`qwen25vl7b_std`）。

核验原始输出：`evidence/v3-image-inspect.txt`。成本：3090 约 4 分钟，**¥0.02**。
