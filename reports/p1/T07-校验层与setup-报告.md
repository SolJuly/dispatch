# T07 校验层 + 建机 setup 落地报告

> 时间：2026-10-28 17:00–17:10 · 纯本机，**¥0**
> 交付：`tools/p1/verify.py`（约 240 行）+ `tools/p1/instance_setup.sh`（约 130 行）
> 结论：**复验项从"肉眼"变成 PASS/FAIL，且 5 类坏产物全部能抓住；建机 4 步手工收敛为 1 条命令。**

---

## 1. 校验层 verify.py

### 1.1 六条复验项

| 项 | 判据 | 数据来源 |
|---|---|---|
| `decode` | ffprobe 能解出 video 流 | `-show_entries stream=codec_name,…` |
| `geometry` | 宽高命中 `expect.width/height` | 同上 |
| `frames` | 时长落在 `expect.duration_s` 区间 | 同上 |
| `size` | 字节 ≥ `expect.min_bytes` | `stat` |
| `black` | 黑帧占比 ≤ `expect.black_ratio_max` | `signalstats` YAVG 序列 |
| `params` | workflow 里**写死**的参数与 `job.params` 一致 | 读 workflow JSON |

规则：不适用项记 `None`（图片无 frames/black），不参与判定；`verdict = 适用项全 True`。

### 1.2 参数回读的边界（重要）

`params` 项只能验证 **workflow 里写死的部分**（本例：`steps=20`、`cfg=1.0`、`width/height/length`）。
`{{SEED}}`、`{{PROMPT}}` 这类占位符是运行时注入的，**主动跳过比对**——它们由 `params_hash` 管，不由 workflow 管。

这是设计取舍，不是漏洞：workflow 管"结构+超参"，job 管"每次运行的变量"。

### 1.3 实测

```
=== 正例 ===
  I2V.mp4  PASS  {decode: ✓, geometry: ✓, frames: ✓, size: ✓, black: ✓, params: ✓}

=== 负例（5/5 全部拒绝）===
  ✓ 文件截断 800B        FAIL  failed=[decode, geometry, frames, size, black]
  ✓ 全黑视频             FAIL  failed=[black]
  ✓ steps 与 workflow 不符 FAIL  failed=[params]
  ✓ 体积未达下限          FAIL  failed=[size]
  ✓ 尺寸不符             FAIL  failed=[geometry]
```

端到端（`jobctl verify <job_id>`，真实产物）：

```
  I2V.mp4  →  PASS
    ✓ decode    ffprobe 解码 / 时长 / 帧数
    ✓ geometry  尺寸匹配  768x512
    ✓ frames    帧数 / 时长区间  5.167s / 124帧
    ✓ size      产物体积下限  604398B ≥ 150000B
    ✓ black     黑帧占比  黑帧 0.0% ≤ 0.2
    ✓ params    params_hash 回读匹配
```

### 1.4 踩到并修掉的坑

**黑帧阈值用错色域**：最初用 `YAVG < 16`（full range 的黑电平），结果全黑视频判为"不黑"。
视频是 **limited range（16–235）**，纯黑就是 Y=16。改为 `YAVG < 24`（留 8 级编码噪声余量）后，
全黑视频立刻被 `black` 项抓住。

> 这个坑很典型：`tools/riskmon.py` 里用的是 `YAVG < 2`，那是给 full-range 图的；
> 直接搬到视频上会漏判。verify.py 用独立常量 `BLACK_YAVG = 24`。

---

## 2. 建机 setup：instance_setup.sh

### 2.1 一句话

```bash
bash tools/p1/instance_setup.sh <uhost-id>          # 配置 + 启动 + 自检
bash tools/p1/instance_setup.sh <uhost-id> --check  # 只自检
```

### 2.2 收敛的 4 个手工动作

| # | 原手工步骤 | 脚本动作 | 解决的历史问题 |
|---|---|---|---|
| 1 | push `extra_model_paths.yaml` | 内嵌模板生成 → scp | `/model` 默认不挂载，模型全不可见 |
| 2 | `pkill` + `mv Manager` | 两次 pkill + 目录改名 `.disabled` | Manager 拉 GitHub 概率卡死（4 次建机中 2 次） |
| 3 | `ln -sf` 两个 Qwen 资产 | 幂等软链 | `qwen_image_vae` / `qwen_2.5_vl_fp16` 是断链或错版本 |
| 4 | 手动 `--detach` 启动 + 等 60s | 固定启动参数 + 轮询自检 | entrypoint 抢 8188、启动无反馈 |

### 2.3 自检项（6 个资产全部可见才通过）

```
h3_unet   minimax/DasiwaHybridV2_int8
h3_clip   qwen3vl_32b_minimax_h3_int8
h3_vae    minimax_h3_video_vae_int8_convrot-v2
qwen_unet qwen_image_fp8
qwen_vae  qwen_std_vae.safetensors        ← 软链
qwen_clip qwen25vl7b_std.safetensors      ← 软链
```

最多轮询 25 次 × 4 秒 ≈ 100 秒，超时打印远端日志提示。

### 2.4 验证状态

| 项 | 状态 |
|---|---|
| `bash -n` 语法 | ✅ 通过 |
| 幂等设计（重复跑不报错） | ✅ 用 `2>/dev/null \|\| true` 兜底 |
| **真实建机实测** | ⏳ **未做**——随 T09 首次闭环一起验证（已把 T09 依赖指向 T15） |

不虚报：脚本没在真机上跑过，第一次跑它就是在 T09。

---

## 3. 对后续的影响

| 任务 | 变化 |
|---|---|
| T08 串行释放 | 可直接调 `verify.verify_job()` 决定"能不能进 PERSISTED" |
| T09 首次闭环 | 建机后一条 `instance_setup.sh` 就绪；产物落盘后 `jobctl verify` 自动判 PASS/FAIL |
| T13 异常用例 | TC-06/07/08 已有可执行判据（不再靠肉眼） |

---

## 4. 证据

```
tools/p1/verify.py                 校验层（6 项）
tools/p1/instance_setup.sh         建机一条命令
tools/p1/jobctl.py: verify <job_id>  新增子命令
reports/p1/logs/verify-tests.txt   正例 + 5 负例实测输出
```
