# T11 收口：LTXV 根因与修复

> 时间：2026-10-28 20:25–20:37 · 4 次建机 · 实扣 **¥0.37**
> 结论：**根因是缺 `LTXVScheduler`**。换官方结构后 97 帧后段崩坏、**73 帧（2.92s@25fps）全程稳定**。

---

## 1. 根因：标准 KSampler 不适用于 LTXV

对照 ComfyUI 官方 workflow（[ltxv_text_to_video_0.9.5.json](https://comfyanonymous.github.io/ComfyUI_examples/ltxv/ltxv_text_to_video_0.9.5.json)），发现三处结构性差异：

| 项 | 我之前写的 | 官方要求 |
|---|---|---|
| **采样器** | `KSampler`（euler / normal） | **`SamplerCustom` + `LTXVScheduler` + `KSamplerSelect("res_multistep")`** |
| 条件帧率 | 24 | **25** |
| CLIP | 用 checkpoint 的（无效） | 独立 `CLIPLoader(t5xxl, type=ltxv)` |

**`LTXVScheduler` 是核心**——它按 latent token 数动态生成 sigma 序列（`steps=30, max_shift=2.05, base_shift=0.95, stretch=true, terminal=0.1`）。
标准 `KSampler` 的固定 euler/normal 调度无法匹配 LTXV 的噪声分布，所以去噪出来是抽象色块。

字段名从 ComfyUI 源码 `comfy_extras/nodes_lt.py` 的 `LTXVScheduler.define_schema()` 确认：

```python
io.Int.Input("steps", default=20)
io.Float.Input("max_shift", default=2.05)
io.Float.Input("base_shift", default=0.95)
io.Boolean.Input(id="stretch", default=True)
io.Float.Input(id="terminal", default=0.1)
io.Latent.Input("latent", optional=True)
```

---

## 2. 修复前后对比

| 次 | 结构 | 帧数 | 亮度 σ | 帧差 | 边缘能量 | 画面 |
|---|---|---|---|---|---|---|
| 1 | KSampler euler | 97 | 1.554 | 0.369 | — | ❌ 抽象噪点 |
| 2 | KSampler steps40/cfg5 | 97 | 3.198 | 0.263 | 0.49 | ❌ 还是噪点 |
| 3 | **官方结构** | 97 | **4.279** | 0.396 | — | ⚠️ 前 2/3 正常，后 1/3 崩 |
| 4 | **官方结构** | **73** | — | 0.15 | — | ✅ **全程稳定** |

第 3 次已经证明结构调整正确（σ 从 1.55 → 4.28，接近 H3 的 4.7），但 97 帧后段人物崩解。
第 4 次降到 73 帧，坐 → 站 → 走全程结构完整。

---

## 3. 一个重要的模型差异

| 模型 | 稳定帧数 | 时长 | 结论 |
|---|---|---|---|
| **H3** | **121 帧** @24fps | 5.17s | 73 帧会崩，**必须高帧** |
| **LTXV 2B** | **73 帧** @25fps | 2.92s | 97 帧会崩，**必须低帧** |

两者需求正好相反。这不是参数问题，是各自的训练长度不同：
- H3 按 121 帧训练，短序列是分布外
- LTXV 2B 在长序列上时序一致性不足（2B 参数量有限）

**实践口径**：
- 要 5 秒 → 用 H3 121 帧
- 要 3 秒 → 用 LTXV 73 帧（2.92s）或 H3 121 帧裁切

---

## 4. 内容告警的一个局限（本轮发现）

73 帧的成功样本触发了 `帧差 0.15 < 0.4` 告警，但**画面完全正常**。

原因：动作集中在后半段，前 36 帧几乎静止，把**全局平均帧差**拉低了。

所以这条告警会误报"动作分布不均"的视频。两种改法：
- 用「分段帧差的最大值」代替全局平均
- 或把阈值降到 0.10，接受漏报

暂时保留现状——告警本意就是提示人眼复核，误报的代价低于漏报。

---

## 5. 入库

```
workflows/ltxv_2b_t2v.api.json   sha256 647c8be5b4b76480…
  ckpt   CheckpointLoaderSimple(ltx-2/ltx-video-2b-v0.9.5)
  clip   CLIPLoader(t5xxl_fp16, type=ltxv)
  cond   LTXVConditioning(frame_rate=25)
  lat    EmptyLTXVLatentVideo(768,512,73)
  sched  LTXVScheduler(steps30, 2.05, 0.95, true, 0.1)
  samp   KSamplerSelect(res_multistep)
  ks     SamplerCustom(cfg 3.0)
  dec    VAEDecode → CreateVideo(25fps) → SaveVideo
```

`workflows/MANIFEST.json` 已更新为 4 份。

## 6. 证据

```
reports/p1/ltxv-debug/
├── attempt1-steps30-cfg3.png     KSampler 结构 → 噪点
├── attempt2-steps40-cfg5.png     KSampler 调参 → 还是噪点
├── attempt3-official.png         官方结构 97 帧 → 前段好、后段崩
└── attempt4-73frames.png         官方结构 73 帧 → 全程稳定 ✅
```
