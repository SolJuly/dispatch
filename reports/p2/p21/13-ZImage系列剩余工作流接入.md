# Z-Image 系列剩余 5 条工作流接入（01 / 03 / 04 / 05 / 06）

> 日期：2026-11-05 12:18–12:37（Asia/Shanghai）
> 输入：`comfyui_workflows_json.zip` 里的 `workflows/01–06`；02 号 HQ 已在 09:03 接入
> 结论：**5 条全部真机 PASS 并 verified**；控制台默认 t2i 仍是 02 HQ，i2i 页签已切到 03 号 Z-Image img2img
> 成本：5 条合计 **¥0.3870**（含 Flux2 两次失败提交、ControlNet 一次资产/抓取重试）

---

## 1. 五条工作流

| # | key | 用途 | 实现要点 | 证据作业 | 成本 |
|---|---|---|---|---|---|
| 01 | `zimage_turbo_t2i` | 1024 快速基线 | 10 节点，8 步 cfg1.0，无 2x | `p1-20261105T1221-a9464d` | ¥0.0466 |
| 03 | `zimage_turbo_img2img` | 图生图 | LoadImage → VAEEncode → denoise 0.55 | `p1-20261105T1218-c198ca` | ¥0.0537 |
| 04 | `flux2_klein_4b_fast` | FLUX.2 Klein 4B 快速 | 13 节点，4 步 euler，SamplerCustomAdvanced | `p1-20261105T1227-611afe` | ¥0.1147 |
| 05 | `zimage_turbo_batch4` | 一次 4 张候选 | batch_size=4，固定 seed | `p1-20261105T1224-958aaf` | ¥0.0645 |
| 06 | `zimage_turbo_controlnet_canny` | ControlNet Canny 构图 | Canny → Fun Union ControlNet patch | `p1-20261105T1231-246093` | ¥0.1075 |

全部 `manifest verdict=PASS`；01/03/04/06 输出 1024×1024，05 输出 4×1024×1024。

## 2. 接入时发现并修掉的问题

1. **复用已归一化的输入图会 self-copy 崩溃**：`p20ctl._normalize_image` 对 `reports/p2/p21/inputs/in-*.png` 再 `copy2` 到自身，抛 `SameFileError`。已加 `p.resolve() != dst.resolve()` 判断。
2. **Flux2 的 UNET 在子目录**：共享库实际是 `flux-2-klein-4b/flux-2-klein-4b.safetensors`，不是 `flux-2-klein-4b-fp8`；资产检查用 `contains` 会误过，提交才 400 `value_not_in_list`。已改成真实子目录名。
3. **`flux2-vae.safetensors` 是断链**：指向不存在的 `ModelScope/Comfy-Org/flux2-dev/...`。真实 VAE 在 `ModelScope/black-forest-labs/flux-3-image-klein-comfy/vae/flux2-vae.safetensors`（三份同名文件 sha256 完全一致）；已用模型配置的 `setup.links` 在 setup 阶段重链。
4. **ControlNet patch 不在 `ModelPatchLoader` 候选里**：真实文件是 `/model/ModelScope/PAI/Z-Image-Turbo-Fun-Controlnet-Union/Z-Image-Turbo-Fun-Controlnet-Union.safetensors`；已用 `setup.links` 链到 `ComfyUI/models/model_patches/`。
5. **`PreviewImage` 会产出 temp 文件**：06 号原图里有一个预览节点，ComfyUI history 里会出现 `ComfyUI_temp_*.png`（temp 目录），`step_fetch` 去 output 目录抓会 404。已从图里删掉该预览节点（它不参与最终输出）。

## 3. 当前控制台

| 页签 / 入口 | 工作流 | 状态 |
|---|---|---|
| 文生图（默认） | `zimage_turbo_t2i_hq`（02，2048） | ✅ 默认 |
| 文生图下拉 | `zimage_turbo_t2i` / `zimage_turbo_batch4` / `flux2_klein_4b_fast` | ✅ 可选 |
| 图生图 | `zimage_turbo_img2img` | ✅ 已切（旧 `sdxl_img2img` 已 deprecated 隐藏） |
| 局部重绘 | 仍为占位；旧 `sdxl_inpaint` 保留 | ⚠️ 待替换 |
| ControlNet | `zimage_turbo_controlnet_canny` | ✅ 下拉可选（无独立页签） |
| 旧 detail_polish / upscale_4x | 保留 | ⚠️ 待替换 |

旧 `qwen_image_t2i`、`qwen_image_t2i_landscape`、`qwen_image_t2i_portrait`、`sdxl_t2i`、`sdxl_t2i_lora`、`sdxl_img2img` 均为 `deprecated: true`，控制台不展示；配置文件留作历史作业复现/回滚。

## 4. 与官方模板包（38 条 pack）的关系

12:06 另有一批 `pack_*` 官方模板导入（38 条，全 `experimental`，见 `08-官方模板包导入报告.md`）。
本次**不混入**那批：遗留 5 条用独立 key、独立模型配置，已 verified；`pack_*` 仍是 experimental，需要各自真机验证后再转正。
registry 当前：**57 工作流 / 47 模型 / verified 16 / equivalent 2 / experimental 39**。

## 5. 验证与测试

- 5 条全部 `p20ctl compat verify` 通过，证据作业在上表。
- 171 个单测全绿（P2.0 88 + 控制台 41 + 队列 42）。
- `p20ctl validate`：✓ 全部通过（39 条 experimental 告警来自 pack，符合预期）。

## 6. 下一步

1. **局部重绘 / 放大**还没有 Z-Image 版本；旧 `sdxl_inpaint`、`upscale_4x` 暂留。
2. 若要继续，优先做 `pack_*` 里 image 类的真机验证（z_image_turbo、flux2_klein、qwen_image edit），把 experimental 转 verified。
3. 质量评分卡仍缺（11 号审计 P0-1）；这 5 条也只是每条 1 张样本。

## 7. 回滚

- 5 条都是新增 key，不动旧工作流；删对应 `registry/workflows/zimage_turbo_{t2i,img2img,batch4,controlnet_canny}.json`、`flux2_klein_4b_fast.json`、两个模型配置和图即可。
- 旧 `sdxl_img2img` 的 `deprecated` 标记去掉即可恢复控制台展示。

---

*本次 5 条各起 1 台 4090，全部已释放；无实例残留。*
