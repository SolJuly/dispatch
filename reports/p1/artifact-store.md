# 产物存储与展示（图片优先）

> 建立：2026-10-28 20:05 · 分支 `p1-mini-single-task-loop`
> 范围：**本轮只做图片**；视频产物会列出但暂不展示（后续接 ffmpeg 抽 poster 帧）

---

## 1. 落点（已经统一，不区分媒体类型）

```
reports/p1/jobs/<job_id>/
├── job.json          作业定义（params_hash / instance / expect）
├── events.jsonl      状态转移时间线
├── manifest.json     产物清单 + 6 项 checks + 成本
├── artifacts/        产物本体（png / jpg / mp4 都在这）
└── thumbs/           缩略图（可重建，不入 git）
```

拉取逻辑在 `run.py: step_fetch()`，**没有任何 media-type 分支**——mp4 和 png 走同一条路径，只按序号加前缀防重名（`01-xxx.png`）。

---

## 2. 拉取方式与参数（回答三个问题）

### 2.1 怎么拉

用 **scp**，不走 `csctl pull`（后者有目录探测超时的坑，曾把文件误判成目录）。

```python
["sshpass", "-p", password, "scp", "-q", "-P", "23",
 "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
 "-o", "ConnectTimeout=15", "-c", "aes128-gcm@openssh.com",
 f"root@{host}:/root/ComfyUI/output/{sub}/{fn}", str(local)]
```

- 认证：密码（实例 `Describe` 返回，经 sshpass 传入）
- 端口：23（镜像把 sshd 改到了 23）
- 逐文件拉，不打包（图片通常 <2MB，视频 <1MB）

### 2.2 超时

| 参数 | 值 | 说明 |
|---|---|---|
| `SCP_TIMEOUT` | **300 秒** | 单文件。100MB @10MB/s 只需 10s，留足余量 |
| `SCP_RETRY` | **2 次** | 失败重试，间隔 5 秒 |
| `ConnectTimeout` | 15 秒 | ssh 建连超时 |

**之前是 1800 秒且不重试**——单文件卡 30 分钟，网络抖一下就整个 job 失败。已改。

### 2.3 失败时的状态

```
FETCHING ──失败──► FAILED_FETCH ──► RELEASING ──► FAILED
```

- 抛 `RunError(..., state.FAILED_FETCH)`
- 错误信息带进度：`拉取失败（已成功 2/5）：/root/ComfyUI/output/xxx.png`
- **实例必定销毁**（`FAIL_BRANCH → RELEASING` 是状态机允许的唯一出口，G1 的例外分支）
- 已拉成功的文件**保留在 `artifacts/`**，供复盘；残缺文件会被删除（重试前 `unlink`），避免下次误判成已存在

---

## 3. 缩略图

`tools/p1/gallery.py`，用 **ffmpeg** 而不是 PIL（本机没装 PIL）：

```bash
ffmpeg -i src.png -vf scale=480:-1 -frames:v 1 -q:v 3 dst.jpg
```

- 目标宽度 480px（平板看清晰，体积约 20–50KB）
- 存 `jobs/<id>/thumbs/<原文件名>.jpg`
- 已存在则跳过（`--no-thumb` 可只重建 HTML）

## 4. 画廊页

`reports/p1/gallery.html`，按 job 分组：

```
┌─────────────────────────────────────────┐
│ p1-20261028T1830-46342b   [PASS]        │
│ qwen_image · 1.8 分钟 · ¥0.114          │
│ [decode][geometry][size][params]        │
│ ┌────────┐                              │
│ │ 缩略图  │  934 KB                      │
│ └────────┘                              │
└─────────────────────────────────────────┘
```

- 每个 job 一张卡：状态徽章、模型、耗时、花费、6 项 checks 标签
- 图片点开看原图
- 视频位置显示"视频暂不展示"占位块（保留位置，后续接 poster）

访问：
- 本机 `reports/p1/gallery.html`
- 平板 `https://123.207.74.155/files/<token>/f/reports/p1/gallery.html`

## 5. 保留策略

| 对象 | 策略 | 理由 |
|---|---|---|
| `artifacts/` | **永久保留** | 单张图 <1MB、单条视频 <1MB，一年也就几百 MB |
| `thumbs/` | 可随时删、可重建 | 派生数据，`--no-thumb` 之外都能重生成 |
| 失败 job 的产物 | 保留 7 天后人工清理 | 复盘要用，但别无限堆积 |
| 实例上的帧序列 | **不拉** | SVD 那种几百张 PNG，拉回来价值低；需要时重跑 |

> `thumbs/` 与 `jobs/` 已在 `.gitignore` 里（运行态），画廊 HTML 是可再生页面。

## 6. 边界（本轮没做的）

| 未做 | 原因 |
|---|---|
| 视频缩略图（抽 poster 帧） | 按你的要求先做图片 |
| 视频在线播放 | 同上；`gallery.py` 已识别 `VID_EXT`，只差渲染分支 |
| 产物去重/压缩 | 目前原样保留 |
| 跨 job 对比视图 | 单任务阶段用不上 |
