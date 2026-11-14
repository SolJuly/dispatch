# 工作流淘汰与合并方案

> 建立：2026-11-14 13:45
> 分支：`feat/demo-optimize`
> 依据：`registry/workflows/` 54 个注册工作流 + `reports/p2/p21/evidence/missing-weights.txt`（152 行缺失清单）

---

## 一、核心事实（先看这个）

```
注册工作流：54 个
  ├─ 已验证可用：          1 个  (h3_i2v_121f)
  ├─ 无标记（老资产）：    21 个
  └─ 标注「未本地真机验证」：32 个
       └─ 其中连权重都没有：23 个  ← 根本跑不了
```

**结论：54 个工作流里，能真正跑起来的不到 20 个。**

那 23 个「权重都缺」的工作流，不是「质量差」，是**导入时就没打算跑**——从 ComfyUI 官方模板批量导入注册表，但对应的 safetensors 从来没下载到 `/model` 共享库。

---

## 二、按模型族归类 + 处置建议

### A 类 · 保留（权重齐、有实测、能立刻用）

| 族 | 工作流 | 依据 |
|---|---|---|
| **ZImage Turbo** | `zimage_turbo_t2i` / `_hq` / `_img2img` / `_batch4` / `_controlnet_canny` | 权重在，8 步出图，实测 3s/张 |
| **SDXL** | `sdxl_t2i` / `_lora` / `_img2img` / `_inpaint` | 权重在，4 个都无未验证标记 |
| **Qwen-Image** | `qwen_image_t2i` / `_landscape` / `_portrait` / `_edit_2509` | 权重在，t2i 是主力 |
| **FLUX** | `flux2_klein_4b_fast` | 权重在，4 步蒸馏 |
| **放大/修复** | `seedvr2_3b_upscale` / `_video` / `upscale_4x` | 权重在 |
| **MiniMax H3** | `h3_t2v_121f` / `h3_i2v_121f` | 权重在，**唯一 verified** |
| **LTXV** | `ltxv_2b_t2v` | 权重在 |
| **Wan 2.1** | `wan21_i2v_480p` | 权重在（fp8 必需） |
| **Wan 2.2** | `pack_video_wan2_2_14b_i2v` / `_flf2v` / `_s2v` | 权重在（但实测 0/4 成功，需排查） |
| **精修** | `detail_polish` | 权重在 |

**A 类合计：约 25 个**

---

### B 类 · 立即淘汰（权重根本不存在）

**这 23 个跑不了，留着只会让用户点了报错、让界面显得很丰富但全是坑。**

| 族 | 工作流 | 缺什么 |
|---|---|---|
| **Wan VACE**（4） | `pack_video_wan_vace_14b_ref2v` / `_v2v` / `_flf2v` / `_inpainting` | 整个 VACE 权重目录缺失 |
| **LTX 2.3/2.5**（6） | `pack_video_ltx2_3_i2v` / `_flf2v` / `_ic_lora` / `_id_lora` / `pack_video_ltx2_5_flf2v` / `pack_template_ltx2_3_ic_lora_ingredients` | LTX 全系缺 11 行 + ltx25_tree 28 行 |
| **MiniMax 扩展**（4） | `pack_video_minimax_h3_r2v` / `_multiframe_reference` / `pack_video_fasth3_ominiref_r2v` / `pack_video_fastvideo_fasth3_i2v` | minimax 缺 12 行 + fasth3 缺 9 行 |
| **Wan Animate**（2） | `pack_video_wan_animate2` / `_distilled` | wanai 缺失 |
| **SCAIL-2**（2） | `pack_video_wan21_scail2_character_replacement` / `_int8` | scail2 缺 5 行 |
| **Qwen 扩展**（3） | `pack_image_qwen_image_layered` / `pack_image_qwen_image_2_1_image_edit` / `_background_removal` | qwen_layered 缺、qwen21 缺 9 行 |
| **放大扩展**（3） | `pack_utility_seedvr2_7b_int8_upscale_image` / `pack_utility_image_upscale_supir` / `pack_templates_hellorob_facegen_skindetail_upscale` | seedvr7b 缺、skin_lora 缺 |
| **Void**（1） | `pack_utility_void_video_inpainting` | void 缺失 |

**B 类合计：25 个**（比前面算的 23 多 2 个，因为 Supir 和 Qwen2.1 image_edit 也缺）

---

### C 类 · 需要决策（权重在，但没验证过或跑过失败）

| 工作流 | 情况 | 建议 |
|---|---|---|
| `pack_video_wan2_2_14b_i2v` | 权重在，**实测 0/4 成功** | 先留，排查失败原因 |
| `pack_video_wan2_2_14b_flf2v` | 权重在，未单独实测 | 留，跟着 i2v 一起验 |
| `pack_video_wan2_2_14b_s2v` | 权重在，未实测 | 留，对口型是刚需 |
| `pack_utility_z_image_turbo_2k_upscaler_app` | 权重在，未验证 | 留，ZImage 族扩展示范 |
| `pack_template_qwen_image_illustration_lora` | 权重在，未验证 | 留，风格化示例 |
| `pack_image_flux2_klein_image_edit_4b_distilled` | 权重在，未验证 | 留，换装/多图编辑有需求 |
| `pack_image_qwen_image_edit_2511_int8` | 权重在，未验证 | 与 `qwen_image_edit_2509` **功能重叠**，见下 |

**C 类合计：7 个**

---

## 三、合并方案

### 合并 1 · Qwen-Image 编辑三合一

现状：三个工作流做同一件事（图生图编辑）

```
qwen_image_edit_2509          （权重在，主力）
pack_image_qwen_image_edit_2511_int8   （更新版本，权重在，未验证）
pack_image_qwen_image_2_1_image_edit   （2.1 版，权重缺）
```

**处置**：保留 `qwen_image_edit_2509` 作为默认，把 `_2511_int8` 作为「高版本可选」保留但标 `experimental`；删 `_2_1_image_edit`（权重缺）。

**结果：3 → 2**

### 合并 2 · 文生图尺寸变体合并

现状：

```
qwen_image_t2i              （1024 方图）
qwen_image_t2i_landscape    （横图，复用同一 graph）
qwen_image_t2i_portrait     （竖图，复用同一 graph）
```

**这三个共用 `qwen_image_t2i.api.json`**，唯一区别是默认尺寸参数。

**处置**：合并成 1 个 `qwen_image_t2i`，用 `width`/`height` 参数控制；在参数契约里给三个预设值（方形/横/竖）。

**结果：3 → 1**

### 合并 3 · ZImage 出图变体合并

现状：

```
zimage_turbo_t2i        （8 步基线）
zimage_turbo_t2i_hq     （14 节点，多一层细化）
zimage_turbo_batch4     （batch_size=4）
zimage_turbo_img2img    （图生图）
zimage_turbo_controlnet_canny  （ControlNet）
```

**处置探讨**：
- `t2i` + `t2i_hq` → **可以合并**（同样的输入，只差 steps/细化层数），用 `hq: bool` 参数切换。但节点图不同（10 vs 14），技术上要合并得改 graph。
- `batch4` → **保留独立**（batch_size 是可参数化的，但输出契约不同：1 张 vs 4 张，验证逻辑不一样）
- `img2img` → **保留独立**（输入契约不同：需要底图）
- `controlnet_canny` → **保留独立**（需要控制图）

**结果：5 → 5**（保守）或 **5 → 4**（合并 t2i/t2i_hq，需改 graph）

**建议：先不动**。这是主力族，改坏了影响最大。

### 合并 4 · 放大三选一

现状：

```
upscale_4x                    （4x-UltraSharp，模型放大）
seedvr2_3b_upscale            （SeedVR2 3B，AI 还原放大）
pack_utility_z_image_turbo_2k_upscaler_app  （ZImage 2K 精绘）
```

**这三个是不同技术路线，不能合并**：
- `upscale_4x` = 传统模型放大（快，不增细节）
- `seedvr2_3b_upscale` = AI 超分（慢，补细节）
- `z_image_turbo_2k` = 重绘式精绘（最慢，改内容）

**结果：保留 3 个**，但要在 UI 上说明「快/中/慢」和适用场景。

### 合并 5 · 视频 i2v 族合并

现状（都是图生视频）：

```
h3_i2v_121f            （MiniMax H3，权重在，verified）
wan21_i2v_480p         （Wan 2.1，权重在）
pack_video_wan2_2_14b_i2v  （Wan 2.2，权重在，实测 0/4）
ltxv_2b_t2v            （LTXV，文生视频，不是 i2v）
pack_video_ltx2_3_i2v  （权重缺，淘汰）
```

**处置**：前三个**不能合并**（不同模型、不同质量/速度特性）。但**可以归组**：UI 上做成「图生视频」一个入口，下拉选模型（H3 快 / Wan2.1 稳 / Wan2.2 新）。

**结果：数量不变，但 UI 归类**

---

## 四、最终方案汇总

| 处置 | 数量 | 说明 |
|---|---|---|
| **淘汰** | **25** | 权重根本不存在 |
| **合并** | **4 → 1**（Qwen t2i 尺寸变体）+ **3 → 2**（Qwen edit） | 净减 4 个 |
| **保留** | **25** | A 类 + C 类 |
| **合计** | **54 → 25** | **减少 54%** |

### 淘汰后的清单（25 个保留）

**图像（14）**
```
zimage_turbo_t2i            zimage_turbo_t2i_hq
zimage_turbo_img2img        zimage_turbo_batch4
zimage_turbo_controlnet_canny
sdxl_t2i                    sdxl_t2i_lora
sdxl_img2img                sdxl_inpaint
qwen_image_t2i              （合并 landscape/portrait）
qwen_image_edit_2509        pack_image_qwen_image_edit_2511_int8
flux2_klein_4b_fast         pack_image_flux2_klein_image_edit_4b_distilled
detail_polish               pack_template_qwen_image_illustration_lora
pack_utility_z_image_turbo_2k_upscaler_app
```

**视频（11）**
```
h3_t2v_121f                 h3_i2v_121f
ltxv_2b_t2v                 wan21_i2v_480p
pack_video_wan2_2_14b_i2v   pack_video_wan2_2_14b_flf2v
pack_video_wan2_2_14b_s2v
seedvr2_3b_upscale          seedvr2_3b_upscale_video
upscale_4x
```

---

## 五、执行方式（不破坏现有资产）

### 关键原则：**不删文件，只打标记**

```json
// 淘汰的工作流，在 JSON 里加一个字段
{
  "key": "pack_video_wan_vace_inpainting",
  "status": "retired",
  "retired_reason": "模型权重不在共享库（wan_vace 目录全缺），无从验证",
  "retired_at": "2026-11-14"
}
```

**为什么这么做**：
1. 文件还在，以后权重下来了改一个字段就能复活
2. `registry.db` 重建时自动过滤 `status != retired`
3. 提交历史完整，可追溯

### 三步走

**第一步：打标记（纯 JSON 改动，零风险）**
```
1. 25 个淘汰 → status: "retired" + reason
2. 4 个合并 → 保留主工作流，其余标 status: "merged_into: <key>"
3. 重建 registry.db
4. 前端只显示 status == "active" 的
```

**第二步：前端分组（不改后端）**
```
创作台改成「图像 / 视频」两个大 tab
每个 tab 下按族分组（ZImage / SDXL / Qwen / H3 / Wan / 放大）
```

**第三步：验证**
```
跑一次 p20ctl validate，确认剩余 25 个全部通过契约校验
```

---

## 六、需要你确认

| # | 决策点 | 我的建议 |
|---|---|---|
| **E1** | 淘汰 25 个是否同意 | 同意（权重都没有，留着是坑） |
| **E2** | Qwen t2i 三合一是否同意 | 同意（共用 graph，纯参数差异） |
| **E3** | Qwen edit 三合二是否同意 | 同意 |
| **E4** | ZImage t2i/t2i_hq 要不要合并 | **建议先不动**（主力族，改 graph 风险高） |
| **E5** | 淘汰方式：打标记 vs 真删文件 | **打标记**（可复活、可追溯） |

---

## 七、附：为什么这 25 个「权重不存在」

`reports/p2/p21/evidence/missing-weights.txt` 显示，缺的不是一两个文件，而是**整个模型族的目录**：

| 缺什么 | 行数 |
|---|---|
| `ltx25_tree` | 28 行（整棵树） |
| `ltx_all` | 11 行 |
| `minimax` | 12 行 |
| `wan_dirs` | 10 行 |
| `qwen21` | 9 行 |
| `fasth3` | 9 行 |
| `ltx2_tree` | 11 行 |
| 其他 | 62 行 |

**这说明**：当初批量导入 ComfyUI 官方工作流模板时，**只导了 JSON 结构，没下权重**。所以那 32 个「未验证」本质上不是「没来得及验证」，是**从来没打算跑**。

**淘汰它们不是砍功能，是把「看起来有 54 个」变成「确实能跑 25 个」。**
