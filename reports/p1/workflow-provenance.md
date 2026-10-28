# Workflow 资产台账（P1 入库件）

> 生成：2026-10-27 22:20 · 分支 `p1-mini-single-task-loop`
> 机器清单：[workflows/MANIFEST.json](../workflows/MANIFEST.json)
> 规则：**只入库已验证达标的 workflow**；`prompt/seed/首帧/输出前缀` 用占位符，结构与超参固定 → sha256 稳定

---

## 1. 入库件

| 文件 | 节点 | sha256（前 16 位） | 用途 | 验证结论 |
|---|---|---|---|---|
| `h3_t2v_121f.api.json` | 10 | `339e1f0e3a50a41d` | H3 文生视频 5.17s | ✅ t2v 121 帧稳定（81.2s） |
| `h3_i2v_121f.api.json` | 11 | `5253947d2cdafe91` | H3 图生视频 5.17s | ✅ i2v 121 帧稳定（138.2s） |
| `qwen_image_t2i.api.json` | 9 | `a3794a2315ddf8b6` | Qwen-Image 文生图 1024² | ✅ 出图质量达标（~35s） |

原 `svd_xt_chain.PLACEHOLDER.json` 已删除——SVD 4 段链式方案因构图漂移废弃（见 `reports/p1/video-quality/baseline.md`）。

## 2. 占位符约定

| 占位符 | 出现位置 | 运行时由谁填 |
|---|---|---|
| `{{PROMPT}}` | `cond.prompt` / `pos.text` | job.params.prompt |
| `{{NEG_PROMPT}}` | `neg.prompt` / `neg.text` | job.params.neg_prompt |
| `{{SEED}}` | `ks.seed` | job.params.seed |
| `{{FIRST_FRAME}}` | `img.image`（仅 i2v） | job.params.image |
| `{{OUTPUT_PREFIX}}` | `save.filename_prefix` | `{job_id}_{seq}` |

**为什么要占位符**：workflow 文件是"结构指纹"，进 `job.model.workflow_sha256` 参与幂等键。
若把 prompt/seed 写死，改一次提示词 sha256 就变，幂等键失效；参数变化应由 `params_hash` 管。

## 3. 固定超参（已验证）

### H3（t2v / i2v 共用）

```
unet     minimax/DasiwaMinimaxH3_dasiwaHybridV2_int8.safetensors
clip     qwen3vl_32b_minimax_h3_int8_convrot.safetensors   (type=minimax)
vae      minimax_h3_video_vae_int8_convrot-v2.safetensors
steps    20        cfg 1.0        sampler euler / simple
shift    shift_video 5.0 / shift_audio 1.0
frames   121       fps 24         → 5.17s
size     768×512
```

### Qwen-Image

```
unet  qwen/qwen_image_fp8_e4m3fn_scaled.safetensors
clip  qwen25vl7b_std.safetensors（软链自 FireRedTeam/qwen2.5vl-7b-bf16，3584 维）
vae   qwen_std_vae.safetensors（软链自 FireRedTeam/qwen_image_vae，2D VAE）
steps 20   cfg 2.5   euler / simple   1024×1024
```

### 环境依赖（重启后需重建）

| 项 | 操作 | 原因 |
|---|---|---|
| 共享库挂载 | 写 `extra_model_paths.yaml` 后重启 ComfyUI | 镜像默认不挂 `/model` |
| Manager | `custom_nodes/ComfyUI-Manager` → `.disabled` | 否则卡在拉 GitHub 节点表，8188 不监听 |
| Qwen 两个软链 | `qwen_std_vae` / `qwen25vl7b_std` | 列表里的同名文件是断链；`qwen_2.5_vl_fp16` 是 3B（2048 维）非 7B |

## 4. 为什么 121 帧

| 帧数 | 时长 | 结果 |
|---|---|---|
| 73 | 3.04s | ❌ 后段崩坏（4步→噪点；20步→上半身崩解） |
| **121** | 5.17s | ✅ 结构完整，t2v/i2v 都稳 |

H3 按 121 帧训练，73 帧是分布外长度。**要 3 秒就用 121 帧生成再裁前 73 帧**（裁切≠拼接）。

证据：`reports/p1/h3-diag/README.md`、`reports/p1/h3-final/compare-t2v-i2v.png`。

## 5. 复算校验

```bash
sha256sum workflows/*.api.json
python3 -c "import json;d=json.load(open('workflows/MANIFEST.json'));[print(w['key'],w['sha256'][:16]) for w in d['workflows']]"
```

## 6. 与 job 的衔接

```json
"model": {
  "key": "h3_t2v",
  "workflow": "workflows/h3_t2v_121f.api.json",
  "workflow_sha256": "<上面那串>",
  "checkpoint": "minimax/DasiwaMinimaxH3_dasiwaHybridV2_int8.safetensors"
}
```

`workflow_sha256` 进 `params_hash`，所以：**同一份 workflow + 同一组参数 = 同一个幂等键**，
换 workflow 结构或换超参即视为新作业。
