# P1 · 单任务最小闭环 · 第一步执行计划（底座：job 定义 + 状态机）

> 分支：`p1-mini-single-task-loop`（main 不动）
> 基线：`reports/audit-20261026-1320.md`（P0 只读审计）+ `reports/unknowns.md`（U1–U6）
> 时间：2026-10-27 11:30（Asia/Shanghai）
> 本轮口径：**只做单任务闭环，不做并发批量；本轮不跑测试，只交付底座与计划**

---

## 0. 为什么先做这一块

P0 审计结论第 6 节写得很硬：**无任务队列、无唯一任务 ID、无任务状态机、无输出校验**。
待落地清单里 5 项 ❌ 全部依赖同一根地基：

```
job_id / job.json ──► 状态机 ──► 校验&持久化 ──► 释放实例（串行）
        │                │              │
        └── 幂等/查重 ────┘              └── 产物不落地就销毁 = 永久丢数据
```

没有 job_id，状态机没有主语；没有状态机，"持久化后释放"就没有可判定的前置条件；
校验层没有 job 参数就没有比对基准。所以顺序固定：**底座 → 校验&持久化 → 模型 → 异常**。

---

## 1. 依赖图与推进顺序

| 序 | 阶段 | 依赖 | 本轮状态 | 产出 |
|---|---|---|---|---|
| 1 | 底座：job 定义 + 状态机 | P0 审计、unknowns | **本轮执行** | job/manifest 契约、11 态转移表、守卫、CAS 落盘 |
| 2 | 校验 & 持久化流程 | 1 | 下一步 | ffprobe 解码、参数回读匹配、manifest、串行释放 |
| 3 | 模型问题 | 2 | 顺延 | Wan2.1 黑帧复验（0.36 镜像）、LTXV 首跑、图片存储服务 |
| 4 | 异常专项测试 | 1–3 | 顺延 | SSH 中断 / 上传失败 / 抢占回收 / 8903 等 12 条用例 |

第 2 阶段之前不做任何 GPU 花销动作；第 3 阶段才产生真实算力成本。

---

## 2. 第一步的交付物（本轮实际要写的文件）

```
tools/p1/__init__.py
tools/p1/store.py       # 路径约定 + 原子写（tmp→fsync→rename）+ flock 单任务锁
tools/p1/schema.py      # job / manifest 校验器（纯标准库，环境无 jsonschema）
tools/p1/state.py       # 状态枚举 + 合法转移表 + 守卫 + CAS 更新 + events.jsonl
tools/p1/status.py      # 汇总写 reports/p1/status.json（架构图 H 区数据源）
tools/p1/jobctl.py      # 唯一入口：new / show / status / validate
reports/p1/jobs/<job_id>/{job.json,events.jsonl,manifest.json,artifacts/}
```

约定：
- **不改 `csctl.py`**。`jobctl` 以子进程方式调 `csctl`，8903 重试包在 loop 侧（遵守 unknowns.md「拒绝顺手修」原则）。
- `reports/p1/jobs/` 与 `status.json` 属运行态，加进 `.gitignore`；只提交 `tools/p1/` 与本文档。
- 单任务锁：`reports/p1/.job.lock`，`flock` 非阻塞，拿不到直接退出（不排队、不并发，符合本轮口径）。

---

## 3. job.json 契约（schema `p1.job/v1`）

```json
{
  "schema": "p1.job/v1",
  "job_id": "p1-20261027T1130-a1b2c3",
  "created_at": "2026-10-27T11:30:00+08:00",
  "branch": "p1-mini-single-task-loop",
  "kind": "video",
  "model": {
    "key": "svd_xt",
    "workflow": "workflows/svd_xt_chain.json",
    "workflow_sha256": "<sha256>",
    "checkpoint": "checkpoints/svd_xt.safetensors"
  },
  "params": {
    "width": 832, "height": 480, "frames": 97, "fps": 19.4,
    "seed": 20261026, "steps": 20, "cfg": 1.0,
    "image": "start_shot1.png"
  },
  "params_hash": "sha256:<canonical json of params+model>",
  "expect": {
    "ext": "mp4", "min_bytes": 300000,
    "duration_s": [4.8, 5.2], "width": [832], "height": [480],
    "black_ratio_max": 0.5
  },
  "budget": { "max_minutes": 45, "max_cost": 3.0 },
  "instance": { "required_gpu": ["4090", "V100S"], "uhost_id": null, "zone": null },
  "state": "CREATED",
  "state_seq": 1
}
```

规则：
- `params_hash` = 规范化 JSON（键排序、无空格）的 sha256，**是幂等键**，也是第 4 阶段「参数回读匹配」的比对基准。
- `job.json` 只允许状态机改 `state` / `state_seq` / `instance.*`，`params` 与 `model` 写死不可变。
- `min_bytes` 初始值 300000：依据已实测 SVD 产物 0.63–0.95MB，留 3 倍余量；第 2 阶段用真实产出校准。

## 4. manifest.json 契约（schema `p1.manifest/v1`）

```json
{
  "schema": "p1.manifest/v1",
  "job_id": "p1-20261027T1130-a1b2c3",
  "params_hash": "sha256:<与 job.json 一致>",
  "persisted_at": "2026-10-27T11:56:12+08:00",
  "instance": { "uhost_id": "uhost-xxxx", "gpu": "4090", "zone": "cn-wlcb-01",
                "up_at": "…", "release_at": "…", "minutes": 26.4 },
  "artifacts": [{
    "rel": "artifacts/shot1.mp4", "bytes": 996147, "sha256": "…",
    "probe": { "codec": "h264", "w": 832, "h": 480, "frames": 97,
               "duration": 5.0, "fps": 19.4, "black_ratio": 0.02 },
    "match": { "decode": true, "params": true, "size": true }
  }],
  "checks": { "decode": true, "params_match": true, "size": true, "black_frame": true },
  "verdict": "PASS",
  "cost": { "hourly": 2.15, "minutes": 26.4, "amount": 0.95 },
  "state_history": ["CREATED", "…", "PERSISTED"]
}
```

写盘方式：`manifest.json.tmp` → `fsync` → `rename` → 读回校验（防半截文件导致"看起来已落盘"）。

---

## 5. 状态机（11 态 + 3 失败终态）

### 5.1 主链

| # | 状态 | 进入动作 | 守卫 | 下一步 |
|---|---|---|---|---|
| 1 | `CREATED` | 写 job.json，取单任务锁 | schema 合法；params_hash 未重复 | `PROVISIONING` |
| 2 | `PROVISIONING` | `csctl up`（单台，带 `--hours` 兜底） | 预算闸（余额/日预算/单价） | `READY` |
| 3 | `READY` | 探活：8188 `/object_info`、`/model` 可见数、workflow 存在 | 三项全通 | `SUBMITTED` |
| 4 | `SUBMITTED` | `POST /prompt`，记录 `prompt_id` | 返回 200 + prompt_id | `RUNNING` |
| 5 | `RUNNING` | 轮询 `/history/<prompt_id>` | 未超 `budget.max_minutes` | `FETCHING` |
| 6 | `FETCHING` | `transfer.pull` 到 `artifacts/`，边拉边算 sha256 | 远端产物存在且字节 > 0 | `VERIFYING` |
| 7 | `VERIFYING` | ffprobe + 抽帧黑检 + params 回读 | 见 §6 | `PERSISTED` |
| 8 | `PERSISTED` | 写 manifest.json | 读回一致；`checks` 全 true | `RELEASING` |
| 9 | `RELEASING` | `csctl rm --yes`（8903 重试 ≤3 次） | **必须已 PERSISTED** 或失败原因已记录 | `DONE` |
| 10 | `DONE` | 释放锁，刷新 status.json | manifest 存在且 verdict=PASS | 终态 |

### 5.2 失败支链

`FAILED_PROVISION` / `FAILED_RUN` / `FAILED_FETCH` / `FAILED_VERIFY` → **一律先 `RELEASING` 再 `FAILED`**（实例不留活口，已拉的产物保留在 artifacts/ 供复盘）。

### 5.3 守卫与不变式

- **G1（最关键）**：非 `PERSISTED` 不得进入 `RELEASING`——除 `FAILED_*` 且已尝试归档。这是「产物不会随实例消失」的唯一保证。
- **G2**：`DONE` 必须 `manifest.verdict == PASS`。
- **G3**：所有转移用 CAS：读 `state` 与 `state_seq` → 校验期望值 → 写新值 → `state_seq+1`，防重复执行与并发改写。
- **G4**：单任务锁 + 幂等：同 `params_hash` 已 `DONE` 时 `new` 直接拒绝（`--force` 生成新 job_id）。
- **G5**：每次转移追加 `events.jsonl`：`{ts, from, to, reason, evidence}`，可离线复盘整条时间线。

### 5.4 转移合法性（状态机核心表）

```
CREATED      → PROVISIONING | ABORTED
PROVISIONING → READY | FAILED_PROVISION
READY        → SUBMITTED | FAILED_PROVISION
SUBMITTED    → RUNNING | FAILED_RUN
RUNNING      → FETCHING | FAILED_RUN
FETCHING     → VERIFYING | FAILED_FETCH
VERIFYING    → PERSISTED | FAILED_VERIFY
PERSISTED    → RELEASING
FAILED_*     → RELEASING
RELEASING    → DONE | FAILED
其余组合     → 抛 IllegalTransition（含 CREATED→DONE 等越级）
```

---

## 6. 校验层设计（第 2 阶段实现，本轮只定契约）

| 校验 | 方法 | 通过条件 | 依据 |
|---|---|---|---|
| 解码 | `ffprobe -v error -show_streams` | 有 1 路 video，codec ∈ {h264, hevc} | 本机有 ffmpeg/ffprobe |
| 几何 | 同上 | w/h 命中 `expect` | SVD 实测 832×480 |
| 帧数/时长 | `nb_frames` 或 `duration` | 97 帧 / 5.000s ±0.2 | 4 段链式实测 5.000s |
| 体积 | `stat -c %s` | ≥ `expect.min_bytes` | 0.63–0.95MB 实测 |
| 黑帧 | `signalstats` 抽帧 YAVG | `black_ratio ≤ 0.5` | 复用 `tools/riskmon.py` 逻辑 |
| 参数匹配 | workflow 回读 KSampler/image_size | 与 `params_hash` 一致 | 防「跑偏了但文件非空」 |

校验必须**在本机做**：镜像内无 ffmpeg/rsync（P0 审计 + README 已记录）。

---

## 7. 串行流程（第 2 阶段）：为什么必须是这个顺序

```
provision → run → fetch → verify → persist ──┬──► release → DONE
                                             │
                        verify 失败 ─────────┘──► release → FAILED（产物留档）
```

- `persist` 在 `release` 之前：terminate 会连系统盘一起删（P0 §5、路径速查表），顺序颠倒 = 数据必丢。
- 任一步失败都**不允许跳过 release**：否则实例持续计费（U2 的 8903 已在 RELEASING 内置重试）。
- 单任务串行，不做队列、不做并发；并发留到后续独立分支（U1 的 `up.lock` 问题正是这个原因）。

---

## 8. 架构图实时接入（已完成，本轮可见）

- 数据源：`reports/p1/status.json`（唯一真源，任何状态转移后由 `jobctl status` 刷新）
- 渲染：`tools/arch.py` 新增 **H 区**（通栏横幅，位于 A–E / F 两栏之下、图例之上）
  - 10 个状态芯片，当前态高亮描边
  - 四步推进（1 底座 / 2 校验&持久化 / 3 模型 / 4 异常测试）含百分比
  - 复验项 6 条（✓/▸/·）、阻塞点、用例 0/12、实扣 ¥ / 预算 ¥
- 刷新路径：cron `*/10` 跑 `tools/arch.py` → 写 `architecture.svg/.png/.html` → `sync_share.sh` 推平板
- 手动：`python3 tools/arch.py`；只看数据：`python3 tools/p1/jobctl.py status`
- 断链保护：`status.json` 缺失/损坏时 H 区回落默认值，不影响整图生成

---

## 9. 验收口径（本轮不执行，只登记）

第一步（底座）的验收只看 4 条，全部离线：
- A1 `jobctl new` 产出 `job.json` 且 `validate` 通过
- A2 `jobctl show` 能打印状态链 + events 时间线
- A3 非法转移（含 `CREATED→DONE`）被 `IllegalTransition` 拒绝
- A4 `jobctl status` 刷新后，H 区数据随动（架构图重渲染可见）

### 用例登记（12 条，第 4 阶段执行）

| ID | 场景 | 断言 |
|---|---|---|
| TC-01 | job.json 结构非法（缺 params_hash） | validate 拒绝 |
| TC-02 | 越级转移 CREATED→DONE | IllegalTransition |
| TC-03 | 同 params_hash 重复 new（已 DONE） | 拒绝，除非 --force |
| TC-04 | 未 PERSISTED 调 RELEASING | G1 拦截 |
| TC-05 | 写盘中断（kill -9 留 .tmp） | 恢复后读不到半截 manifest |
| TC-06 | manifest 与 job 的 params_hash 不一致 | verify FAIL |
| TC-07 | 产物 0 字节 | size 校验 FAIL |
| TC-08 | 黑帧视频（Wan2.1 当前症状） | black_frame FAIL 并留证据 |
| TC-09 | SSH 拉一半断开 | FETCHING 可重试，不误入 PERSISTED |
| TC-10 | 上传/下载失败（磁盘满、权限） | FAILED_FETCH + 实例仍被销毁 |
| TC-11 | 过渡期 8903 拒绝 | RELEASING 重试成功，无重复计费 |
| TC-12 | Spot 抢占回收中断推理 | FAILED_RUN，已拉产物保留，实例必销毁 |

---

## 10. 阻塞点（需要你拍板或补料的）

| # | 阻塞 | 影响 | 建议处置 |
|---|---|---|---|
| B1 | **workflow JSON 未入库**（P0 审计：项目内无任何 workflow JSON） | 状态机 SUBMITTED 无输入源，`model.workflow` 填不出 | 第 1 阶段末尾从实例 `/root/ComfyUI/user/default/workflows` pull 一份 SVD-XT 链式 workflow 入库并算 sha256 |
| B2 | 镜像无 rsync/ffmpeg | 校验只能本机做，FETCHING 必须走 tar 流 | 已定：校验在本机，传输用 `tools/transfer.py` |
| B3 | Wan2.1 fp8 黑帧（ComfyUI 0.3.40） | 第 3 阶段前置 | 换 0.36 镜像复验；在此之前 Wan 不进闭环 |
| B4 | 8903 过渡期拒绝（U2） | RELEASING 可能首次失败 | loop 侧重试 ≤3 次、间隔 10s，不改 csctl 内部 |
| B5 | 图片存储服务未定 | 影响 image 类 job 的 artifacts 存放 | 建议复用 `reports/p1/jobs/<id>/artifacts/`，不另起服务 |
| B6 | 产物体积阈值缺基线（仅 SVD 两条样本） | min_bytes 只能定 300KB 粗阈值 | 第 2 阶段用真实产出校准 |

---

## 11. 每轮同步模板（固定 5 行）

```
进展：<本轮完成了什么，对应步骤号>
阻塞：<B# + 现状>
用例：<新增/变更的 TC#，本轮是否执行>
成本：<本轮 GPU 花费 ¥>
下一步：<下一轮第一件事>
```

---

## 12. 成本预算

| 阶段 | GPU 花销 | 说明 |
|---|---|---|
| 第 1 阶段（本轮） | **¥0.00** | 纯本机代码，不建机 |
| 第 2 阶段 | ¥1–2 | 1 次 4090 单任务闭环（约 26 分钟 ≈ ¥0.95），含 1 次重跑余量 |
| 第 3 阶段 | ¥2–3 | Wan 黑帧复验需换镜像重试 |
| 总预算上限 | **¥3.00**（H 区显示） | 超出即中止 |

---

## 13. 本轮已完成的动作（可验证）

1. 新建 `reports/p1/status.json`（P1 进展唯一真源，schema 兼容后续 `jobctl status` 覆盖写）
2. `tools/arch.py` 新增 H 区渲染（`p1_status()` + `p1_section()`），通栏布局，断链回落默认值
3. 重渲染并验证：`reports/architecture.png` / `.svg` / `.html` 已含 H 区（`python3 tools/arch.py --no-sync`）
4. 本文档

> 未做：任何 GPU 动作、任何测试执行、`csctl.py` 改动、main 分支提交。
