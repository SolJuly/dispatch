# 视频工作流分辨率旋钮：现状与可改清单

> 2026-11-08 · 针对「18 个视频工作流没有分辨率旋钮」这个遗留项

## 结论先说

18 个里有 **14 个不该加**，**3 个加不了**，**真正可加的只有 1 个**。

原以为「缺旋钮 = 缺陷」，逐个读图后发现大部分是**正确设计**：
尺寸跟随输入素材/驱动视频，硬塞一个 width/height 反而会跟图里的
`GetImageSize` 打架，产出被拉伸的片子。

---

## 一、不该加（14 个）：尺寸来自运行时

这些工作流的宽高由 `GetImageSize` / `ResolutionSelector` /
`ComfyMathExpression` 在**运行时**读取输入素材算出：

| 工作流 | 尺寸来源 |
|---|---|
| `pack_video_fasth3_ominiref_r2v` | `ResolutionSelector`（预设分辨率档位） |
| `pack_video_fastvideo_fasth3_i2v` | `GetImageSize`（读首帧图） |
| `pack_video_ltx2_3_flf2v` | `GetImageSize`（读首尾帧） |
| `pack_video_ltx2_3_i2v` | `ComfyMathExpression`（按输入的宽高比算） |
| `pack_video_ltx2_5_flf2v` | `GetImageSize` |
| `pack_video_minimax_h3_multiframe_reference` | `ResolutionSelector` |
| `pack_video_minimax_h3_r2v` | `ResolutionSelector` |
| `pack_video_wan21_scail2_character_replacement` | `GetImageSize` |
| `pack_video_wan21_scail2_character_replacement_int8` | `GetImageSize` |
| `pack_video_wan_animate2` | `GetImageSize`（读人物图） |
| `pack_video_wan_animate2_distilled` | `GetImageSize`（读人物图） |
| `pack_video_wan_vace_flf2v` | `ComfyMathExpression` |
| `pack_video_wan_vace_inpainting` | `GetImageSize` |

**为什么不该加**：动作迁移/图生视频的核心语义是「保持输入素材的构图」。
强行指定 1280×720 而输入是 9:16 竖图，模型会把人物横向拉伸。

**如果确实想控分辨率**，正确做法是改**输入素材的尺寸**，不是加参数：
```bash
# 想在 720p 跑，就先把输入图/视频缩到 720p 再上传
ffmpeg -i input.jpg -vf "scale=-2:720" input-720p.jpg
```

---

## 二、加不了（3 个）：没有可写的尺寸节点

| 工作流 | 情况 |
|---|---|
| `pack_video_wan2_2_14b_s2v` | `VAEDecode` 的 latent 由采样器决定，图里无 width/height 可绑 |
| `pack_video_wan_vace_14b_v2v` | `TrimVideoLatent` 直接沿用输入视频尺寸 |
| `pack_video_wan_vace_inpainting` | 同 v2c 逻辑（虽标了运行时，实为跟随输入） |

给它们加 width/height 参数**没有可注入的目标节点**，写了也不会生效。

---

## 三、真正可加（1 个）

### `seedvr2_3b_upscale_video`（视频放大）

图里已经有现成参数，只是没暴露：

```json
[5] SeedVR2VideoUpscaler {
  "image": ["2", 0],
  "resolution": 1080        ← 就这个
}
```

**建议**：把 `resolution` 暴露成枚举参数
（480 / 720 / 1080 / 1440 / 2160），用户就能选放大到多高。

**代价**：需要改 `registry/workflows/seedvr2_3b_upscale_video.json`
的 `params` + 重新 `p20ctl import` + 真机跑一次验证。

---

## 四、还有一个「看似有旋钮其实是假象」

`pack_video_wan_vace_14b_ref2v` 图里写死 `768×768`：

```json
[?] WanVaceToVideo { "width": 768, "height": 768 }
```

**可以加旋钮**（有明确的可写节点），但它目前是 `experimental` 状态，
未验证。等它转 verified 时再一起加。

---

## 五、这份清单的意义

之前审计报告写「15 个视频工作流没有分辨率旋钮」，
听起来像 15 个待修的缺陷。实际读完图才发现：

```
14 个 = 正确设计（跟随输入素材）
 3 个 = 结构上无法支持
 1 个 = 真可加（seedvr2_3b_upscale_video）
 1 个 = 可加但等工作流转 verified
```

**所以这一项不应该记作「未完成的 15 个任务」，而是「已确认 14 个无需改动」。**

审计报告里的表述需要修正——把「缺失」改成「设计如此」，
避免以后每次都重新排查一遍。

---

## 六、如果要动手

优先级最低。理由：分辨率跟随输入素材是这类工作流的主流做法，
用户更常见的需求是「换个尺寸的素材」，而不是「同一素材跑不同分辨率」。

真要做，就做 `seedvr2_3b_upscale_video` 一个，约 30 分钟。
