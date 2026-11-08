# Z-Image-Turbo 接入与默认切换（P2.2 起手）

> 日期：2026-11-05 09:00–09:15（Asia/Shanghai）
> 目标：用你提供的 `02_zimage_turbo_t2i_hq.api.json` 替换旧的文生图默认工作流，并跑通真机验证
> 结论：**真机 PASS（2048×2048）**，控制台默认已切到 `zimage_turbo_t2i_hq`；旧 qwen/SDXL t2i 已从控制台隐藏。首跑成本 **¥0.1827**（含两次提交失败的重试）。

---

## 1. 交付物

| 类型 | 文件 | 说明 |
|---|---|---|
| 图 | `workflows/zimage_turbo_t2i_hq.api.json` | 由附件 `02_...(1).json` 注册表化：去掉可选的 1024 base `SaveImage`（原节点 10），保留 2x 细化与最终 2048 输出 |
| 模型 | `registry/models/zimage_turbo.json` | `z_image_turbo_bf16` + `qwen_3_4b` + `ziamge_ae` 三件套 |
| 工作流 | `registry/workflows/zimage_turbo_t2i_hq.json` | 8 个参数：prompt / seed / width / height / steps / cfg / sampler / scheduler；seed/steps/cfg/sampler/scheduler 同时绑两个 KSampler |
| 兼容性 | `registry/compat.json` | `zimage_turbo_t2i_hq × zimage_turbo → verified`，证据 `p1-20261105T0857-ed3d5d` |
| 代码 | `tools/p20/render.py` | 新增 `expect_scale`：输出经 2x 放大时，校验期望跟着 ×2（否则 geometry 必红） |
| 代码 | `webui/api.py` `webui/pages.py` | 隐藏 `deprecated` 工作流；默认 t2i 优先 Z-Image；i2i 页签固定到 `sdxl_img2img` |

## 2. 真机证据

| 项 | 值 |
|---|---|
| 作业 | `p1-20261105T0857-ed3d5d`（4090） |
| 结果 | DONE / manifest PASS |
| 输出 | 2048×2048 PNG，3,519,056 B |
| 推理耗时 | 59 s（基础 1024 + 2x 细化 2048） |
| 显存 | 见 `manifest`/实例；本作业未 OOM |
| 成本 | **¥0.1827**（5.1 min，含两次失败提交的实例时间） |
| 余额 | ¥20.27 → ¥20.07（同时段还有每日固定扣费，见 11 号审计 §7） |

产物：`/pub/jobs/p1-20261105T0857-ed3d5d/01-…png`；本地 `reports/p1/jobs/p1-20261105T0857-ed3d5d/artifacts/`。

## 3. 验证中发现并修掉的问题（都是"看起来接好了但跑不通"）

1. **UNET 必须带子目录**：`/object_info` 里实际值是 `z_image_turbo/z_image_turbo_bf16.safetensors`，但资产检查用的是 `contains`，根名也能"可见"；渲染却用根名提交，ComfyUI 返回 400 `value_not_in_list`。已把 `files.unet.name` 改成带子目录的真实值。
2. **`ae.safetensors` 是断链**：共享库里 `ae.safetensors` 指向不存在的 HiDream 路径，`VAELoader` 列表里有它、加载时报 `not found`。真实文件是 **`ziamge_ae.safetensors`（335,304,388 B）**，已改用该文件。
3. **输出 2048 但期望 1024**：首次校验 geometry FAIL。已加 `expect_scale: {width:2,height:2}`，并补单测 `test_expect_scale_for_2x_upscale`。
4. **原图有两个 `SaveImage`**：节点 10（1024 base）会让一个作业出两张图、缩略图/产物统计口径变复杂；已删掉，只保留节点 15（2048 final）。
5. **`ModelSamplingAuraFlow.sampling="flow"`**：本机 ComfyUI 接受该输入（没有报错），保留；若换更老的镜像需复核。

## 4. 当前控制台状态

| 页签 | 使用的工作流 | 状态 |
|---|---|---|
| 文生图 | `zimage_turbo_t2i_hq` | ✅ 新，默认 |
| 图生图 | `sdxl_img2img` | ⚠️ 旧（待 Z-Image 版替换） |
| 局部重绘 | 占位（`href="#"`） | ⚠️ 旧 `sdxl_inpaint` 仍在 registry，但页签未接 |
| 下拉里的其他 | `detail_polish`、`upscale_4x` | ⚠️ 旧（待替换） |

旧 t2i（`qwen_image_t2i`、`qwen_image_t2i_landscape`、`qwen_image_t2i_portrait`、`sdxl_t2i`、`sdxl_t2i_lora`）已标记 `deprecated: true`：

- 控制台下拉不再展示；
- registry/API 仍保留，供历史作业复现与回滚（**没有物理删除**；等 i2i/inpaint/放大都替换完，再一次性移入归档目录）。

## 5. 参数面（已按"单任务/流水线"收窄）

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| prompt | — | — | 必填；蒸馏模型不吃负面词 |
| seed | — | — | 同时写 8/13 两个 KSampler |
| width / height | 1024 | 512–1536，32 对齐 | 基础图；最终输出 ×2 |
| steps | 8 | 4–12 | 8–9 足够，>20 反而降质 |
| cfg | 1.0 | 0.5–2.0 | **必须 1.0**；调高会过饱和 |
| sampler | res_multistep | 5 个枚举 | 默认按配方 |
| scheduler | simple | 4 个枚举 | 默认按配方 |

刻意没暴露：`denoise`（基础 1.0 / 细化 0.35 锁死）、`batch_size`（本版固定 1，避免 2048 细化 OOM）、`scale_by`（固定 2.0）。

## 6. 下一步

1. **给我 Z-Image 的 img2img / inpaint / upscale / ControlNet JSON**，我按同一套流程接：注册表化 → 资产核对 → 真机一张 → compat verify → 切页签。
2. 这些旧工作流先留着可用，但不再是默认；替换完一条就 `deprecated` 一条，全部替换后统一物理归档。
3. 质量评分卡仍然缺（11 号审计 P0-1）；这次只是"能跑通"的 1 张样本，不等于批量质量验收。建议在下一轮连测时一起做。

## 7. 回滚

- 控制台默认：把 `webui/pages.py` 的 `zimage_turbo_t2i_hq` 优先项去掉，或给旧 t2i 去掉 `deprecated` 标记；
- 重启：`sudo systemctl restart cs-files`（本机通过 D-Bus 重启过一次，当前 pid 见 `/api/p2/version`）；
- 新文件是增量的，删掉 `registry/workflows/zimage_turbo_t2i_hq.json`、`registry/models/zimage_turbo.json`、`workflows/zimage_turbo_t2i_hq.api.json` 即回到 09:00 前状态。

---

*本次接入使用 4090 真机 1 台，已释放；无实例残留。*
