# 视频引擎修复验证（2026-11-07 17:40–18:05）

验证环境：本机（无 GPU，用同构的 shell/ffmpeg/进程模拟远端），命令走真实 `bash -c` 包装，
与 `ssh host "<cmd>"` 的远端执行方式等价。

## 1. 崩溃检测（F1）

探活命令改为 `pgrep -af '[m]ain\.py --listen'` + `/system_stats`，用真进程/真端口跑三种状态：

| 场景 | 构造方式 | 结果 |
|---|---|---|
| 活着 | `python3 main.py --listen 0.0.0.0 --port 8188` | `True` |
| 进程改名但服务在 | `python3 app.py`，`/system_stats` 回 200 | `True` |
| 已死 | 端口清空、无进程 | `False` |
| ssh 不通 | 单测构造 rc=255 + 连接拒绝文本 | `None`（不误杀） |

**旧实现对照**：同一条命令串下，远端 `bash -c` 的 cmdline 里带着 `ComfyUI.*main.py`，
`pgrep` 把包装 shell 自己列出来，`"main.py" in out` 恒真 → 旧代码在"已经死透"的机器上
仍然判"活着"。实测输出确认了这一点（rc=127 也照样判活）。

修复过程中额外揪出一个坑：`curl` 连不上时退出码是 7，原先"rc != 0 且无进程 → None"
会让死机场景一直等到预算上限。现在命令尾部 `; true`，并改为"看到 `---PROC---` 标记就说明
远端命令跑完了，进程和端口都没有就是 `False`"。

## 2. 驱动视频 trim 与估价（F2）

素材：`testsrc 640x360@60fps`，**563 帧 / 9.38s**（对应事故里那条 10.8s 手机视频）。

```
注册表声明为视频的参数: {'image_2'}        ← 蒸馏版动作迁移
旧口径（只认 params["video"]）: ratio=1.00 → 4.6 分钟 / ¥0.25
新口径（注册表 type==video）  : ratio=7.31 → 33.8 分钟 / ¥1.29
```

trim 之后（`trim_seconds=4`，重采样 16fps）：

```
image_2: in-x.mp4 → in-x-t4-16fps.mp4
ffprobe: 64 帧 / 16 fps（原 563 帧 / 60 fps）
估价: 3.8 分钟 / ¥0.22
```

预算闸门（把 `max_minutes` 调到 10 做对照）：

```
BudgetError: 预估 33.8 分钟超过该工作流运行上限 10 分钟（跑到上限会被判超时，实例时间照扣）
```

结论：估价从"按模板默认 77 帧"变成"按真实 563 帧"，偏差 7.3×；同一素材 trim 到 4 秒后
回落到 3.8 分钟，与 30b5a28 声称的"4 分钟 ¥0.18"量级一致。

## 3. 时长期望（F3）

```
视频工作流：27 个
带 duration_s 闸门：14 个（原 6 个，本次从图里推导新增 8 个）
```

例：`pack_video_ltx2_3_i2v` → 126 帧 / 25fps = 5.04s；`minimax_h3_r2v` → 124 帧 / 24fps = 5.17s。
循环批处理模板（`ImageFromBatch` 大窗）与缺 `expression` 的 math 节点主动放弃，不猜。

## 4. 主产物判定（F4）

实测产物目录同时放 `00-primary-main.mp4`（5s 真视频）与 `01-cover.png`（单帧封面）：

```
verdict=PASS  primary=00-primary-main.mp4  kind=video  frames=True size=True
```

legacy 作业（无前缀）走扩展名兜底：`01-cover.png` + `02-main.mp4` → primary=`02-main.mp4`，kind=video。

## 5. 上传链路（F7/F8）

真 HTTP multipart：

```
POST /<token>/api/p2/uploads  → 201 {"name":"in-8377bb60.mp4","bytes":16806}
reports/p2/p21/inputs/in-8377bb60.mp4   inode 52381  nlink=2
share/inputs/in-8377bb60.mp4            inode 52381  nlink=2
```

不再整块 `read(128MB)`；两份目录是硬链接，不再是两份拷贝。

## 6. 注册表与测试

```
p20ctl validate  54 工作流 / 49 模型 / verified 31 / experimental 24 / incompatible 0  全部通过
绑定一致性        断链 0
tools/p20/tests  104 OK
tools/p1/tests    31 OK（新增 27）
tools/webui/tests 55 OK
compileall       exit 0
```

## 7. 真机验证（19:24–19:36，智云 4090 ×2 台，合计 ≈¥0.06）

平台 API 就在工作区（`csctl.py`），余额 ¥10.29、4090 现货 45 张，两台 4090 实例跑完即销。

### 7.1 崩溃检测（F1）—— 现场通过

```
19:26:20  作业 p1-...1fe583 提交 prompt_id=147ef63e…（zimage_turbo_t2i 512×512）
19:26:41  csctl ssh --exec "pkill -f '[m]ain.py --listen'"   ← 手动杀掉 ComfyUI
19:26:43  ✗ ComfyUI 进程已退出（第 22s，疑似显存不足/崩溃）
19:26:47  job.state = FAILED_RUN
```

**从杀进程到落 FAILED_RUN：2 秒**（下一轮 history 查询失败即触发，不用等 30s）。
修复前的行为是继续等到 `max_minutes`（20 分钟）才超时——这次实测证明了差别。

附带确认：失败的实例没有自动销毁，日志提示"已保留（15 分钟后自动关机兜底）"，我随后用 `--abort` 释放，没有产生空转费用。

### 7.2 trim + 估价（F2）—— 现场通过

```
素材：640×360@60fps，563 帧 / 9.38s
提交：--set trim_seconds=4
spec：image_2 = in-7af16da6-t4-16fps.mp4        ← 建 spec 时就截好了
ffprobe：64 帧 / 16fps
19:29:17  输入已上传：image_2=in-7af16da6-t4-16fps.mp4   ← 截短后的文件真的进了实例
19:34:35  推理完成：2 个文件，耗时 317s
产物：02-ComfyUI_00001_.mp4 = 1986×848 / 64 帧 / 4.0s   ← 对比视频与截短输入等长
```

如果 trim 没生效，对比视频会是 563 帧 / 9.38s，作业要跑 20+ 分钟。实测 4.0s / 317s 收尾，
与 30b5a28 声称的"4 秒一轮、约 4 分钟"同一量级。

### 7.3 主产物判定（F4）—— 现场通过

```
00-primary-p1-..._00001_.mp4   ← output 节点产物，被选为 primary
02-ComfyUI_00001_.mp4          ← 附加产物，只记录为 secondary
日志：00-primary-…: FAIL  failed=['freeze']
      02-ComfyUI_…: PASS  failed=—
```

前缀机制在两路输出的真实作业上生效，没有出现"封面抢主位"。

### 7.4 freeze 门禁：已按语义分档处理（21:42–21:50，零 GPU 成本）

**先纠正上一版的粗略判断**：翻出 `reports/p2/freeze-calibration.md`，0.20 不是拍脑袋——
它是在「文/图生视频」语料上标定的时间维崩坏门槛：坏样本 0.21 / 0.26 / 1.0 全部命中，
好样本恒 0。所以**全局放宽会放过真正崩坏的片**，不能那么改。

**真实缺陷**在 `render.expect_for`：对 23 个未自带声明的视频工作流一刀切注入
`{ratio_max:0.2, longest_max_s:1.0}`，没有区分「生成类」与「素材驱动类」。
后者（动作迁移/超分/插帧/抠像/视频重绘）的静止度继承自输入，静态素材产静止片是合法结果。

**改动**（4 个文件 + 1 个测试文件，均已同步 `share/`）：

| 文件 | 改动 |
|---|---|
| `tools/p2/freeze.py` | 新增 `is_full_freeze()`；`verdict(..., mode=)` 支持 `full_freeze_only`：只拦整片冻结，中度静止降级为 details/warnings |
| `tools/p20/render.py` | 新增 `_is_material_driven()`（按注册表 `type=video` 输入或图里 LoadVideo 判定，不猜工作流名）；`expect_for` 按类注入 `FREEZE_GENERATIVE` / `FREEZE_MATERIAL_DRIVEN` |
| `tools/p1/verify.py` | 把 `mode` 传给 `verdict`；`content_warnings` 对降级样本仍出「静止占比高」提示 |
| `tools/p2/tests/test_freeze.py` | 新增 `MaterialDrivenTest` 5 条 |

**分类结果**（27 个视频工作流）：
- 生成类 14 个 + 自带声明 4 个 → **保留 0.2 硬门禁**（不动）
- 素材驱动 9 个（animate2 两个、vace_v2v/inpainting、scail2 两个、ltx2_3_ic_lora、void_inpainting、seedvr2 视频）→ `full_freeze_only`

**真实样本回归**：

```
静止源图 animate2  ratio=0.716 → PASS（降级，仍出告警）
全片冻结 shot1     ratio=1.0   → FAIL（照拦）
生成类崩坏 A       ratio=0.260 → FAIL（硬门禁照拦）
生成类好样本 B     ratio=0.0   → PASS
```

**端到端**：对 19:27 那条 `FAILED_VERIFY` 作业执行 `--salvage`（本地重放，未建机）：

```
重算 expect → freeze 变成 {mode: full_freeze_only, ...}
00-primary-…mp4: PASS    02-ComfyUI_…mp4: PASS
state FAILED → DONE，manifest 已落盘，未产生新费用
```

salvage 后主产物 `downgraded=True`，告警仍为
「冻结时长占比 71.6% > 20%，画面可能静止/卡帧」——**放行但不瞒报**，UI 可以据此提示换素材。

### 7.5 费用

```
19:24 开机 → 19:27 销毁（zimage 验证）：≈¥0.03
19:27 开机 → 19:35 销毁（animate2 验证）：≈¥0.03
余额 10.29 → 10.23，两台实例跑完即销，当前 burn ¥0.00/h
```

### 7.6 VAE 名字：已确认真机原名，已修正，已真机复验

**上机确认（21:0x，探针实例 `uhost-1xf9oxql7ozw` 后即销毁）**：

```
/model/ModelScope/Comfy-Org/z_image_turbo/split_files/vae/ae.safetensors   335,304,388 B
共享库全盘 find -iname '*ziam*' → 空
/root/ComfyUI/models/vae/ae.safetensors        -> 上面那个文件  inode=2915678961491975696
/root/ComfyUI/models/vae/ziamge_ae.safetensors -> 上面那个文件  inode=2915678961491975696
```

结论：共享库原名就是 **`ae.safetensors`**（12 张工作流图里的 VAELoader 也写的是它）；
`ziamge_ae` 是 11-05 手写模型卡时的拼写错误（`zimage` 打成 `ziamge`），靠镜像里烤的别名软链才没崩。
自动补链 `find /model -name ziamge_ae.safetensors` 自然找不到 → 19:26 那条警告的根因。

**改动（registry/models 两张卡）**：
`files.vae.name` 与 `match` 统一为 `ae.safetensors` / `{"equals":"ae.safetensors"}`；
`p20ctl import` 重建索引；`setup.links` 保留 `ziamge_ae` 别名软链仅为兼容老作业
`render.json` 里已写死的旧名（不删，删了老作业 resume 会崩）。

**四处对齐校验（离线）**：渲染图 VAELoader=`ae.safetensors` ✓；自动补链 find 命令不再含 ziamge ✓；
`asset_rules` 用 object_info-v4 缓存命中 `ae.safetensors` ✓；兼容软链仍在 ✓。

**真机复验（21:18–21:21，作业 `p1-20261107T2117-5e1a95`）**：

```
[21:20:05] 模型软链 4/4 就绪
[21:20:10]   自动软链已就绪（按文件名匹配 /model）     ← 不再有"没找到 ziamge_ae"警告
[21:20:12] ComfyUI 就绪，3 项模型资产可见
[21:20:14] 已提交 prompt_id=b4525fbc…
[21:20:48] 推理完成：1 个文件，耗时 33s
[21:20:49]   00-primary-…png: PASS
[21:20:50] ✓ 闭环完成
render.json → 节点 4 VAELoader.vae_name = "ae.safetensors"
```

`state=DONE`，产物 342,690 B，实例跑完即销。`p20ctl validate` 全过、绑定断链 0。
`share/registry/models/` 下两张卡与 `registry.db` 已同步（部署面一致）。

## 8. 结论

F1 / F2 / F4 已在真机闭环；VAE 名字已上机确认、修正并复验（7.6）；
freeze 门禁已按生成类/素材驱动类分档，并用 `--salvage` 把那条 FAILED 作业零成本救成 DONE（7.4）；
同一任务的重复提交防护已落地，真双进程并发验证「只建 1 个作业」（第 9 节）。

累计 3 台 4090 实例跑完即销毁，余额 10.29 → 9.78，当前 burn ¥0.00/h，无残留实例。

**全量测试 247 条绿**（p20 104 / p1 39 / webui 57 / p2 47），
`p20ctl validate` 54 工作流全过，绑定断链 0，改动均已同步 `share/`。

**唯一剩余项**：按你的要求，代码暂不提交 git。

---

## 9. 重复提交防护（22:20–23:00）

用户提出「防止同一任务重复提交」。读完整条提交链，确认现有 `jobctl new` 的查重有 4 个真实漏洞：

| # | 旧行为 | 后果 |
|---|---|---|
| 1 | 查重与建目录之间没有锁 | 双击/多标签并发提交 → 两个进程都查到「无重复」→ 各建一个作业、各开一台 GPU |
| 2 | 「DONE 拦住、进行中的放行」 | 作业正在跑时重复提交恰恰被放行，等于同时跑两份（最烧钱的场景） |
| 3 | 查重键 `params_hash` 含 `expect` | 校验口径一升级（freeze 分档、时长推导）hash 就变，历史 DONE 查不出来 |
| 4 | spec 文件名按秒命名 + webui 把重复当 500 | 同秒两请求互相覆盖；重复时用户看到「建作业失败」而不是「已有相同作业」 |

### 实现

- `tools/p1/store.py`：新增 `SUBMIT_LOCK_FILE`（.submit.lock，与执行锁 .job.lock 分开）、
  `DEDUP_REUSE_MARK`、`task_dedup_key()`（只含 workflow/model/checkpoint/params，
  **不含 expect/budget**）、`find_same_task()`（直接遍历真实目录，老作业无键时现算，零迁移）。
  `JobLock` 接受 str/Path，`list_jobs` 容错 str。
- `tools/p1/jobctl.py`：`cmd_new` 用提交锁把「查重 → 复用/拒绝 → mkdir 占位」原子化。
  进行中的同参数作业 → **幂等复用**（打印 `DEDUP_REUSE <job_id>` 并直接返回 0，不新建）；
  DONE 的拒绝（409 语义）；`--force` 允许显式重跑。落盘 `task_dedup_key`。
  删掉旧的 `_find_same_hash`。
- `webui/api.py`：`create_job(force=...)`；spec 文件名加 PID+微秒防覆盖；
  识别复用标记 → `reused=True`、`run=False`、**绝不 spawn 第二个 run.py**；
  DONE 重复返回 409；`rerun_jobs` 传 `force=True`。
- `tools/p20/p20ctl.py`：识别复用标记不重复 spawn；新增 `--force`。
- `tools/p2/batchctl.py`：`_new_job` 返回 `reused` 标志，复用作业跳过重复入队。
- 测试：`tools/p1/tests/test_submit_dedup.py`（8 条，含 4 线程并发只建 1 个、
  老作业现算、DONE 拒绝、force 重跑、不同参数区分、webui 复用不 spawn）；
  `test_api.py` 补 2 条（409 / force 透传）；修 `test_lock_queue.py` 漏还原 `list_jobs` 的测试污染。

### 验证

```
真双进程并发提交（各自独立解释器 + 同一 .submit.lock）：
  进程1 rc=0  ✓ 作业已建：p1-20261107T2254-bddbed
  进程2 rc=0  DEDUP_REUSE p1-20261107T2254-bddbed（未新建、未重复建机）
  作业目录数 = 1

全量测试 247 条绿（p20 104 / p1 39 / webui 57 / p2 47）
```

### 边界（明确保留，不是遗漏）

- **CLI `p20ctl new` 不传 seed 时随机生成 seed**：那本身就是「要一张新图」，参数确实变了，
  不算重复任务；webui 走 `registry.resolve`（seed 默认 0），双击时参数完全一致，去重生效。
- **`--force` / `rerun_jobs`** 是显式重跑口子，不会被去重拦住。
- **老作业**没有 `task_dedup_key`，`find_same_task` 按 model+params 现算，无需数据迁移。
