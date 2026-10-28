# 验证收口：Qwen-Image 闭环 + 7 个 bug 清零

> 时间：2026-10-28 18:24–18:35 · 3 次 Qwen 尝试 · 实扣 **¥0.37**
> 结论：**7 个 bug 全部验证完毕；顺带发现并修复了入库 workflow 的参数错误。**

---

## 1. Qwen-Image 闭环（成功）

```
作业    p1-20261028T1830-46342b
实例    uhost-1x0ucjsfkay7 · 4090_48G
耗时    1.8 分钟（建机 70s + setup 37s + 推理 65s + 收尾 9s）
花费    ¥0.114
产物    934KB PNG · 1024×1024
校验    PASS（decode / geometry / size / params 四项，图片类不适用 frames/black）
状态链  CREATED → … → PERSISTED → RELEASING → DONE
```

**画质**：正是 prompt 要求的"坐着看手机"构图（上一轮 t2i 出的是站姿），光影、针织纹理、手部结构都正常。

**setup 的 `--need=qwen` 分支**：37 秒通过，只校验 Qwen 三项资产（unet/vae/clip），不因 H3 资产而阻塞。**bug4 的 qwen 分支至此验证完毕。**

---

## 2. 三次尝试里挖出的两个新问题

### 问题 A：入库 workflow 的模型名是断链（真 bug）

第一次 Qwen 提交后立刻报：

```
VAELoader: Model in folder 'vae' with filename 'qwen_image_vae.safetensors' not found.
```

**根因**：导出 `workflows/qwen_image_t2i.api.json` 时，用的是脚本里 `qwen_graph()` 的原始常量，而那两个名字在共享库里是断链：

| 字段 | 入库时写的 | 实际可用 |
|---|---|---|
| `vae_name` | `qwen_image_vae.safetensors`（断链） | `qwen_std_vae.safetensors`（软链） |
| `clip_name` | `qwen_2.5_vl_fp16.safetensors`（3B，2048 维） | `qwen25vl7b_std.safetensors`（7B，3584 维） |

**这说明"入库件"缺一道验证**——文件写好了、sha256 算对了，但内容是错的。

修复：
- 改 workflow 两个字段 → 新 sha256 `16fd3eeb…`
- 同步 `MANIFEST.json`
- 教训：**入库前必须在真机上验证一次,不能只做静态检查**

### 问题 B：库存检查通过≠下单成功（竞态）

```
18:27:31  4090_48G 无现货，最多等 300s…
18:27:48  ✗ 建机失败：226604 This GPU type is currently out of resources
```

库存刚显示有货，`create` 时已被别人抢走。已在 `step_provision` 加 **4 次重试**（每次间隔 20s），与 `--wait-stock` 互补。

---

## 3. 7 个 bug 最终状态

| # | Bug | 状态 |
|---|---|---|
| 1 | 无货直接下单 | ✅ 已验证（`--wait-stock` + 4 次建机重试） |
| 2 | 异常没包装 | ✅ 已验证 |
| 3 | 自检判据被污染 | ✅ 已验证 |
| 4 | 自检不区分任务 | ✅ **H3 + qwen 两条分支都已验证** |
| 5 | Running≠sshd 就绪 | ✅ 已验证（多次 0–10s 就绪） |
| 6 | JSON 混入杂物 | ✅ 已验证 |
| 7 | manifest 用过快照 | ✅ 已验证（真跑，instance 字段完整） |

**清零。**

## 4. 新增的两条修复

| 修复 | 位置 |
|---|---|
| 入库 workflow 的 vae/clip 名字 | `workflows/qwen_image_t2i.api.json` + `MANIFEST.json` |
| 建机竞态重试（226604） | `run.py` `CREATE_RETRY = 4`，间隔 20s |

## 5. 两次闭环对比

| 指标 | H3 t2v 闭环 | Qwen t2i 闭环 |
|---|---|---|
| 总耗时 | 2.9 分钟 | **1.8 分钟** |
| setup | 40s | **37s** |
| 推理 | 127s | 65s |
| 花费 | ¥0.1837 | **¥0.114** |
| 校验项 | 6 项（含 black/frames） | 4 项（图片无 frames/black） |

setup 稳定在 37–40 秒，说明拆小后的版本是可复用的。

## 6. 证据

```
reports/p1/jobs/p1-20261028T1830-46342b/    Qwen 闭环（job/events/manifest/artifacts）
reports/p1/logs/tc11-8903.txt               8903 验证
reports/p1/logs/setup-step2-repro.txt       多行脚本复现
reports/p1/验证-8903与bug7.md                前两项报告
```

## 7. 剩余

| 项 | 状态 |
|---|---|
| T10 Wan2.1 黑帧 | 按你的要求**留到最后** |
| T12 图片存储服务 | Qwen 闭环已通，但"缩略图/落点策略"未做 |
| T11 LTXV | 22B AV 模型加载失败，未解 |
| T13 异常 12 条 | 待做 |
