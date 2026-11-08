# Qwen-Image-Edit 2509 + SeedVR2 视频放大接入

> 日期：2026-11-05 18:03–18:27（Asia/Shanghai）
> 目标：把报告 17 第 8 节里“还没做的”继续往前推：Qwen Edit、视频 SeedVR2
> 结果：**两条都跑通并进注册表**（`qwen_image_edit_2509`、`seedvr2_3b_upscale_video`），各一条 DONE/PASS 作业；顺手修了两个通用 bug
> 成本：4090 手工调试 ¥0.29 + 两条作业 ¥0.222

---

## 1. 结论

1. **Qwen-Image-Edit 2509 其实可以用**。审计里它被判“不可跑”，是因为 pack 图用的是裸文件名（`qwen_image_edit_2509_fp8_e4m3fn` / `qwen_2.5_vl_7b_fp8_scaled` / `qwen_image_vae`），这三个在实例上都是断链；真实权重在 `/model` 里，换成实际可见的名字（或补一条软链）就能跑。而且 pack 的 `TextEncodeQwenImageEditPlus` 没接 prompt，本身也是坏的。
2. **24G 4090 能跑 Qwen Edit**：20.4GB fp8 UNET + 16.6GB bf16 文本编码器 + 0.25GB VAE + Lightning 4 步 LoRA。ComfyUI 在文本编码后会卸载 CLIP，峰值没爆。
3. **SeedVR2 视频放大也通了**：768×512 / 73 帧 → **1620×1080 / 73 帧**，保留帧率与音频，105 秒。
4. 顺手修了两个通用问题（见第 4 节），都会影响以后所有视频作业。

## 2. Qwen-Image-Edit 2509

### 2.1 为什么 pack 跑不了

| pack 里写的文件 | 实例上的实际情况 |
|---|---|
| `qwen_image_edit_2509_fp8_e4m3fn.safetensors` | 不存在；真实文件在 `.../Qwen-Image-Edit_ComfyUI/split_files/diffusion_models/`（20.4GB） |
| `qwen_2.5_vl_7b_fp8_scaled.safetensors` | 断链（指向 `/model/HuggingFace/...`）；可用的等价物是 `qwen25vl7b_std.safetensors`（16.6GB bf16） |
| `qwen_image_vae.safetensors` | 断链；可用 `qwen_std_vae.safetensors`（253MB） |
| `TextEncodeQwenImageEditPlus` | 有节点，但 pack 图没给 `prompt`（必填） |

### 2.2 新工作流

`workflows/qwen_image_edit_2509.api.json`：

```
LoadImage → FluxKontextImageScale(≈1MP)
UNETLoader(qwen_image_edit_2509_fp8_e4m3fn)
  → LoraLoaderModelOnly(Lightning-4steps-V1.0-bf16) → ModelSamplingAuraFlow(3.0) → CFGNorm
CLIPLoader(qwen25vl7b_std, type=qwen_image) + VAELoader(qwen_std_vae)
  → TextEncodeQwenImageEditPlus(prompt, image1) ×2（正/负）
VAEEncode → KSampler(4 步, cfg 1.0) → VAEDecode → SaveImage
```

模型配置 `registry/models/qwen_image_edit_2509.json` 用 `setup.links` 把裸文件名软链到 `/model` 真实文件；文本编码器/VAE 直接用 `instance_setup.sh` 每次开机建的 `qwen25vl7b_std` / `qwen_std_vae`。

### 2.3 真机验证

| 项 | 值 |
|---|---|
| 作业 | `p1-20261105T1817-70e8f8` **DONE / PASS** |
| 输入 | 1080×1080 动漫人物 |
| prompt | “把背景换成夜晚的雪景街道，人物保持不变，光线自然” |
| 输出 | 1024×1024 PNG，1,437,428 B |
| 耗时 / 成本 | 2.0 分钟（推理 67s） / **¥0.0717** |
| 兼容性 | `compat verify` → **verified** |
| 效果 | 背景换成雪夜街道，人物、发色、服装都保持；见 `evidence/qwen-edit-2509-4090.png` |

## 3. SeedVR2 视频放大

### 3.1 工作流

`workflows/seedvr2_3b_upscale_video.api.json`：

```
LoadVideo → GetVideoComponents
  ├─ images → SeedVR2VideoUpscaler(resolution=短边, batch_size=5, color=lab)
  │             → CreateVideo(fps/bit_depth/audio 回填) → SaveVideo(mp4)
```

参数：`video`、`resolution`（默认 1080）、`seed`、`batch_size`（默认 5，4n+1）、`color_correction`。

### 3.2 真机验证

| 项 | 值 |
|---|---|
| 作业 | `p1-20261105T1820-5abacf` **DONE / PASS** |
| 输入 | 768×512、73 帧、24fps、3.04s |
| 输出 | **1620×1080**、73 帧、24fps、3.042s、802,715 B |
| 耗时 / 成本 | 4.2 分钟（推理 105s） / **¥0.1505** |
| 兼容性 | `compat verify` → **verified** |
| 效果 | 见 `evidence/seedvr2-video-1080.mp4` / `evidence/seedvr2-video-frame.png` |

## 4. 顺手修的两个通用 bug

### 4.1 `extract_files` 把输入回显当成产物

`LoadVideo` 会在 `/history` 的 `outputs` 里回显输入文件（`type: input`），`extract_files` 原来不区分类型，拉取时去 `/root/ComfyUI/output/in-xxx.mp4` 找文件 → 必然 404，作业卡在 FETCHING。

修复：只收 `type == "output"`（`temp` 预览也一并过滤），并去重。新增 `tools/p20/tests/test_comfy_files.py`（3 条单测）。

### 4.2 `jobctl` 给 registry 视频作业硬塞时长/冻结阈值

`SPEC_DEFAULTS` 里视频默认 `duration_s [4.8, 5.2] + freeze`。对“放大原视频”这种产出，时长跟着输入走（3.04s），套 5 秒模板必挂 `frames`。

修复：spec 带 `model.registry` 时，`expect` 不再继承 `duration_s`/`freeze`，以 workflow 配置的 `expect_defaults` 为准。

## 5. 成本

| 项 | 金额 |
|---|---|
| 4090 手工调试（Qwen Edit / 视频各一次 + 探测，约 9 分钟） | ≈¥0.29 |
| Qwen Edit 作业 | ¥0.0717 |
| SeedVR2 视频作业 | ¥0.1505 |
| 余额 | **¥15.74** |

## 6. 注册表现状

- 工作流 **60** / 模型 **49** / **verified 21** / equivalent 2 / experimental 37
- 新增 verified：`qwen_image_edit_2509`、`seedvr2_3b_upscale_video`
- 单测：p20 **93** + webui **41** + p2 **42** = 176，全绿

## 7. 还没做的

1. **Qwen Edit 2511**：权重在库（`qwen_image_edit_2511_fp8mixed.safetensors` 20.5GB），只需把模型换一下再验一条，能力比 2509 新。
2. **SUPIR**：仍卡在模板的 `ModelPatchLoader` 私有配置 + 缺 Juggernaut v9 / qwen3.5 CLIP。
3. **facegen**：`BBoxDetectorLoader(FaceParsing)` 的 `ultralytics_bbox` 目录没注册，权重也缺。
4. **Qwen 2.1**：`QwenImage21Cache` / `TextEncodeQwenImage21` 需要升级核心。
5. **长视频/带音频素材**：这次只测了 3 秒无声片段，音频回填链路（`CreateVideo.audio`）还没在真机上验过。
