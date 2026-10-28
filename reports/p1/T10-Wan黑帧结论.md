# T10 结果：Wan2.1 黑帧已解决

> 时间：2026-10-28 20:43–21:02 · 3 次尝试 · 实扣 **¥0.97**
> 结论：**黑帧问题在 0.36 镜像 + 官方 fp8 权重下不复现**。同时定位到社区 NVFP4 4bit 版不可用。

---

## 1. 结果对比

| 次 | unet | 耗时 | 亮度 σ | 帧差 | 黑帧率 | 画面 |
|---|---|---|---|---|---|---|
| 1 | `Dasiwa...NVFP4`（社区 4bit） | 3s（缓存） | 5.37 | 3.64 | 0.0 | ❌ 过曝+噪点+后段崩坏 |
| 2 | **`wan2.1/wan2.1_i2v_480p_14B_fp8_e4m3fn`**（官方） | 240s | **6.37** | **1.10** | 0.0 | ✅ **全程正常** |

**官方 fp8 版**：坐沙发看手机 → 起身 → 走向窗边，全程有细节、有光影、动作连贯，无噪点、无过曝、无崩坏。

## 2. 黑帧问题的结论

**0.36 镜像 + 官方 fp8 权重 = 黑帧不复现。**

P0 时代记录的"Wan2.1 出纯黑"是 **0.3.40 + fp8 权重 + int8 umt5** 的组合问题。现在：
- ComfyUI 0.36.0
- unet：官方 `wan2.1_i2v_480p_14B_fp8_e4m3fn`
- clip：`umt5_xxl_int8_convrot`
- vae：`Wan2_1_VAE_bf16`

组合正常出片，黑帧率 0.0。

## 3. 附带发现：社区量化版不能碰

`Wan/DasiwaWAN22I2V14BLightspeed_boundbiteHighV10_NVFP4.safetensors` 是 **NVFP4（4bit）** 量化社区微调版，实测输出过曝、噪点密集、后段崩坏。

帧差 3.64（官方版 1.10、H3 约 0.6）——画面在剧烈跳变，符合"噪点翻滚"的观感。

**选型规则**：Wan 系列优先 `wan2.1/` 官方目录下的 fp8_scaled / fp8_e4m3fn，避开带 `NVFP4`、`4bit`、`int4` 字样的社区量化版。

## 4. 结构上的两个坑（已记住）

| 坑 | 现象 | 解法 |
|---|---|---|
| **clip_vision 类型不匹配** | `received_type(CLIP_VISION) mismatch input_type(CLIP_VISION_OUTPUT)` | 必须经 `CLIPVisionEncode` 编码，不能直接连 `CLIPVisionLoader` |
| **umt5 社区版是空壳** | `Cannot copy out of meta tensor; no data!` | `..._fully_uncensored` 那个只有 meta，换 `umt5_xxl_int8_convrot` |

## 5. 入库

```
workflows/wan21_i2v_480p.api.json   sha256 4b93e798148a5749…
  unet   wan2.1/wan2.1_i2v_480p_14B_fp8_e4m3fn
  clip   umt5_xxl_int8_convrot (type=wan)
  vae    Wan2_1_VAE_bf16
  cvis   clip_vision_h → CLIPVisionEncode
  i2v    WanImageToVideo(832×480, 49帧)
  ks     KSampler(euler/simple, steps20, cfg3.5)
  vid    CreateVideo(16fps) → SaveVideo
```

`MANIFEST.json` 现在 **5 份**：h3_t2v / h3_i2v / qwen_image / ltxv_2b / wan21_i2v。

## 6. 五个视频/图片模型的最终口径

| 模型 | 结构 | 稳定参数 | 时长 | 备注 |
|---|---|---|---|---|
| **H3** | MiniMaxH3ImageToVideo + SigmaShift | **121 帧** @24fps | 5.17s | 73 帧会崩 |
| **LTXV 2B** | SamplerCustom + LTXVScheduler | **73 帧** @25fps | 2.92s | 97 帧会崩 |
| **Wan2.1** | WanImageToVideo + CLIPVisionEncode | **49 帧** @16fps | 3.06s | 必须官方 fp8 |
| **Qwen-Image** | UNETLoader + 7B encoder | 1024×1024 | — | 图片 |

## 7. 证据

```
reports/p1/wan-result/
├── wan_i2v.mp4                社区 NVFP4 版（过曝+噪点）
├── wan-strip.png              它的帧条
├── wan_official_fp8.mp4       ✅ 官方 fp8 版（正常）
└── wan-official-strip.png     它的帧条
lab/wan_probe.py               探测+执行脚本（自动挑官方 fp8、自动补 CLIPVisionEncode）
```
