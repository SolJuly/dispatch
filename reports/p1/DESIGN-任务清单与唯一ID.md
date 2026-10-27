# P1 · 任务清单与唯一 ID · 设计方案（待批）

> 分支：`p1-mini-single-task-loop`（main 不动）
> 状态：**方案稿，未执行**。批准后才落 `tools/p1/*.py` 与 `reports/p1/tasks.json`
> 时间：2026-10-27 13:15（Asia/Shanghai）
> 依据：`reports/audit-20261026-1320.md` §6/§7、`reports/unknowns.md` U1–U6、`reports/p1/PLAN-第一步-底座.md`

---

## 1. 先分清两个「任务」，否则 ID 一定打架

这是本方案唯一必须先定的事。

| | 开发任务 Task | 执行作业 Job |
|---|---|---|
| 是什么 | 我这一轮轮推进的工作项：「实现状态机」「Wan 黑帧复验」 | 一次真实 GPU 闭环：建机→推理→拉产物→校验→销毁 |
| 谁读 | 人（你审核、我同步） | 机器（状态机、幂等、成本归集） |
| 生命周期 | 跨多轮会话，天级 | 一次闭环，约 30–60 分钟 |
| 状态集 | `todo / doing / blocked / done / dropped`（5 态） | `CREATED … DONE`（11 态 + 失败支链） |
| 成本 | 0 或少量 | 直接烧钱，必须归集 |
| 唯一性 | 人工规划，编号稳定 | 运行期生成，必须机器唯一 |

**结论：两套 ID 空间，前缀不同，绝不混用。**
P0 审计说的「无唯一任务 ID」指的是右边这一列（Job）；左边这一列是我和你协作的账本。

---

## 2. ID 体系总表

| 实体 | 格式 | 示例 | 生成方式 | 唯一性保证 |
|---|---|---|---|---|
| 阶段 | `P<数字>` | `P1` | 人工固定 | 枚举 |
| 开发任务 | `P1-T<两位>`，子项 `P1-T03.2` | `P1-T03` | 人工排号，只增不改 | 写盘前查重 |
| 执行作业 | `p1-YYYYMMDDTHHMM-<6hex>` | `p1-20261027T1310-7f3a2c` | 时间前缀 + 随机后缀 | `mkdir` 原子性（EEXIST 即冲突重取） |
| 执行尝试 | `<job_id>/attempt-<两>位` | `…/attempt-02` | 作业内自增 | 作业内独占 |
| 状态事件 | `#<四位序号>` | `#0007` | 追加自增 | events.jsonl 行序 |
| 产物 | `<两位序号>-<原文件名>` + `sha256` | `01-shot1.mp4` | 拉取时生成 | 内容哈希即身份 |
| 云实例 | `uhost-<平台 ID>` | `uhost-1wxnmqheyazw` | 平台返回 | 平台权威，不另造 |
| 幂等键 | `params_hash` = `sha256:` + 64 hex | `sha256:9f2c…` | 规范化 JSON 计算 | 业务层查重 |

### 2.1 为什么是这个格式（说清代价）

**`job_id` = `p1-` + 时间 + 6hex**
- 时间前缀 → 字典序 ≈ 时间序，`ls` 天然按发生顺序排，排障不用排序字段
- 6 hex = 1677 万组合，单机单任务串行下碰撞概率可忽略；真撞了 `mkdir` 直接报错重取
- 不用 `uuid4`：不可读、不可排序、贴在聊天里没法一眼定位
- 不用纯自增：删号/重装会回绕，且看不出时间
- **不把卡型/模型编码进 ID**：参数会变（4090 换 V100S、SVD 换 Wan），ID 必须稳定；参数信息在 `job.json` 里

**`task_id` = `P1-T03`**
- 人工可引用（"T03 那块""T07 卡在 B1"），diff 友好
- 不带时间戳：任务是规划对象，不是执行记录；时间在 `rounds[]` 里
- 子项用点号（`P1-T03.2`），正则 `^P1-T\d{2}(\.\d+)?$`

**幂等键 `params_hash`**
- 规范化 JSON（键排序、无空格、不含 `job_id`/时间）的 sha256
- 覆盖：`model{key,workflow_sha256,checkpoint}` + `params{…}` + `expect{…}`
- 作用有三：① 防重复烧钱；② 校验层「参数回读匹配」的比对基准；③ manifest 与 job 的一致性锚点

---

## 3. 三道唯一性关卡（缺一不可）

```
① 生成层：mkdir jobs/<job_id> → EEXIST 即冲突，重取后缀（不可能静默覆盖）
② 业务层：params_hash 已存在且 verdict=PASS → 拒绝新 job（--force 才放行，且生成新 job_id）
③ 存储层：validate 强制 job.json.job_id == 目录名，manifest.job_id == job.job_id
```

第 ③ 条是给「手工改目录 / 拷贝粘贴造出两份」兜底的——文件系统不会替你检查这个。

---

## 4. 任务清单的数据结构

单文件全量快照 `reports/p1/tasks.json`（原子写），并由它渲染 `reports/p1/tasks.md` 给平板看。

**为什么不用 jsonl/DB**：任务量约 15 条、人读为主、要能 diff 审阅；追加事件留给 Job 层的 `events.jsonl`。引入 SQLite 会多出迁移、锁、rsync 三件事，收益为零。

```json
{
  "schema": "p1.tasks/v1",
  "phase": "P1",
  "updated_at": "2026-10-27T13:15:00+08:00",
  "tasks": [{
    "id": "P1-T03",
    "title": "状态机核心：11 态 + 守卫 + CAS + events",
    "status": "todo",
    "phase_step": 1,
    "depends_on": ["P1-T02"],
    "deliverables": ["tools/p1/state.py", "tools/p1/store.py"],
    "acceptance": ["A3 越级转移被拒", "events.jsonl 行序=state_seq"],
    "blockers": [],
    "cases": ["TC-02", "TC-04"],
    "budget_cny": 0,
    "rounds": [],
    "evidence": []
  }]
}
```

规则：
- `status` 只五值；`blocked` 必须同时有 `blockers[]` 非空（防止"卡住了但没人知道卡在哪"）
- `depends_on` 必须无环，`validate` 里做拓扑排序检查
- `rounds[]` 是每轮同步的落点：`{round, at, note, cost}`，替代散落在聊天里的进展
- `evidence[]` 放可复核的路径（报告、日志、截图），不写结论性形容词

---

## 5. P1 任务清单（15 条，含 1 条已完成）

排序依据：**会产生费用损失的 / 会丢数据的 / 只是让结论更完备的**，依次靠前。依赖关系保证「底座 → 校验&持久化 → 模型 → 异常」。

| ID | 任务 | 依赖 | 验收要点 | 预算 | 对应你的清单 |
|---|---|---|---|---|---|
| `P1-T00` | 实例停止/销毁能力（已实现，仅登记为 verified） | — | `stop`/`rm` 已有实测证据 | ¥0 | ✅ 已完成 |
| `P1-T01` | 台账对齐：把 ✅⚠️❌ 三项落成 task + 关联 P0 条目 | — | 每条有 id、有验收、无孤儿条目 | ¥0 | 全量 |
| `P1-T02` | job/manifest 契约 + schema 校验器（纯标准库） | T01 | A1：非法 job.json 被拒 | ¥0 | ❌ job.json / manifest |
| `P1-T03` | 状态机核心：11 态 + 守卫 G1–G5 + CAS + events.jsonl | T02 | A3：越级转移被拒；events 与 state_seq 对齐 | ¥0 | ❌ 全链路状态机 |
| `P1-T04` | `jobctl` CLI：`new/show/status/tasks/next/validate` | T02,T03 | A2；`next` 能算出下一步 | ¥0 | ❌ job_id |
| `P1-T05` | 架构图 H 区接 `tasks.json`（完成度 + 当前任务 + 阻塞） | T04 | H 区数据随 `tasks.json` 变 | ¥0 | 本轮附加要求 |
| `P1-T06` | workflow 入库（B1）：拉 SVD-XT 链式 JSON + sha256 | T01 | 文件入库、hash 可复算 | ¥0（用现成镜像或 0.15/h 无卡） | ⚠️ 前置 |
| `P1-T07` | 校验层：ffprobe 解码/几何/帧数/体积 + 黑帧 + 参数回读 | T03,T06 | TC-06/07/08 可判定 | ¥0 | ⚠️ 输出文件大小校验、❌ 解码+参数匹配 |
| `P1-T08` | 持久化→释放串行流程（G1 落地 + 8903 重试） | T07 | TC-04/11；产物必先落盘再销毁 | ¥0 | ❌ 持久化后释放 |
| `P1-T09` | **首次真实单任务闭环**（SVD-XT，4090，1 条） | T08 | manifest PASS + 实例已销毁 + 成本≤¥3 | ¥1–2 | ⚠️ SVD 可用（闭环化） |
| `P1-T10` | Wan2.1 黑帧复验（换 ComfyUI 0.36 镜像） | T09 | 非黑帧或定位到镜像级原因 | ¥1–2 | ⚠️ Wan 黑帧 |
| `P1-T11` | LTXV 2B 首跑 | T09 | 出片 + 过校验 | ¥0.5–1 | ⚠️ LTXV 待验证 |
| `P1-T12` | 图片存储服务（artifacts 落点定案 + 缩略图） | T09 | 图片类 job 产物可取回 | ¥0 | ⚠️ 图片存储服务 |
| `P1-T13` | 异常专项 12 条用例（TC-01…TC-12） | T08 | 12 条有结论（过/不过/条件） | ¥0.5 | ❌ 异常专项测试 |
| `P1-T14` | 收尾：报告 + 架构图定稿 + 合并评审（main 是否合入另议） | T13 | 报告可复核、main 仍干净 | ¥0 | — |

**依赖图**（无环，`jobctl next` 按此算）：

```
T00 ─┐
T01 ─┼─► T02 ─► T03 ─► T04 ─► T05
     │                 └─► T07 ─► T08 ─► T09 ─┬─► T10
     └─► T06 ─────────────►┘                  ├─► T11
                                               ├─► T12
                          T08 ─► T13 ─► T14 ───┘
```

**分轮次交付**（每轮结束都回你「进展/阻塞/用例/成本/下一步」）：
- 第 1 轮：T02、T03、T04（底座，0 元）
- 第 2 轮：T01、T05、T06（台账 + 架构图 + workflow）
- 第 3 轮：T07、T08（校验 + 串行释放）
- 第 4 轮：T09（第一次花钱）
- 第 5 轮：T10–T12（模型线）
- 第 6 轮：T13、T14（异常 + 收尾）

---

## 6. 任务与作业如何挂接（成本不串账）

```
Task (P1-T09)  ──执行──►  Job (p1-20261027T1310-7f3a2c)  ──写──►  ledger.jsonl 的 job 字段
      ▲                          │
      └── job.json.task_ref ─────┘
```

- `job.json` 增 `task_ref: "P1-T09"`，回溯「这条 job 是哪个开发任务跑出来的」
- `jobctl show <job_id>` 顺带汇总该 job 的 `ledger` 花费（`event in {create, stop, terminate}`）
- 一个 Task 可以有多个 Job（重试/换模型），一个 Job 只属于一个 Task

---

## 7. 重跑的语义（必须现在定，否则状态机会含糊）

| 情形 | job_id | params_hash | attempt |
|---|---|---|---|
| 参数不变、机器挂了/网络断 | **不变** | 不变 | +1 |
| 参数变了（换卡、改步数、换模型） | **新 job_id** | 新值 | 重新从 01 开始 |
| 上次 `FAILED_VERIFY` 后想再试一次同参数 | 不变 | 不变 | +1 |

一句话：**改参数 = 新作业；不改参数 = 同一作业的下一次尝试。**
这样「重跑不重复烧钱」与「改参数可追溯」同时成立。

---

## 8. 工具与文件落点

```
tools/p1/store.py      原子写(tmp→fsync→rename)、单任务锁 flock、目录约定
tools/p1/ids.py        ID 生成与正则校验（new_job_id / new_task_id / valid_*）
tools/p1/schema.py     job / manifest / tasks 三个校验器（纯标准库）
tools/p1/state.py      状态枚举 + 转移白名单 + 守卫 + CAS
tools/p1/tasks.py      tasks.json 读写、依赖拓扑、next 计算、md 渲染
tools/p1/status.py     汇总 reports/p1/status.json（喂架构图 H 区）
tools/p1/jobctl.py     唯一 CLI 入口
reports/p1/tasks.json  任务清单（权威）
reports/p1/tasks.md    渲染版（平板看）
reports/p1/status.json 汇总（H 区数据源，已存在）
reports/p1/jobs/<job_id>/{job.json,events.jsonl,manifest.json,artifacts/}
```

`jobctl` 子命令：

| 命令 | 作用 | 示例 |
|---|---|---|
| `tasks` | 列清单（可按 status 过滤） | `jobctl tasks --status doing` |
| `next` | 依赖图算出「现在该做什么」 | `jobctl next` → `P1-T02` |
| `new` | 建 job（校验参数、算 hash、查重、落 job.json） | `jobctl new --task P1-T09 --model svd_xt --params p.json` |
| `show` | 状态链 + events + 花费 | `jobctl show p1-20261027T1310-7f3a2c` |
| `status` | 刷新 `status.json`（H 区随动） | `jobctl status` |
| `validate` | 校验 job/manifest/tasks（可 `--deep` 查 sha256） | `jobctl validate --deep` |

---

## 9. 校验器要查什么（validate 的检查单）

- `job_id` 命中正则，且**等于目录名**
- `params_hash` = 用当前 `model+params+expect` 重算的值（防手改漏改）
- `state` ∈ 枚举；`state_seq ≥ 1`；`events.jsonl` 最后一条的 `to` == `state` 且 `seq` == `state_seq`
- `manifest.job_id` / `manifest.params_hash` 与 job 一致
- `manifest.artifacts[].bytes` 与文件实际大小一致；`--deep` 时再核 `sha256`
- `tasks.json`：ID 唯一、依赖存在、无环、`blocked` 必须带 blockers
- 时间字段可被 `datetime.fromisoformat` 解析

---

## 10. 与历史数据的兼容（不动旧账）

- `.compshare/ledger.jsonl` 里 `csctl up` 写的 `job` 字段是 `uuid4().hex[:10]`（旧格式）。**不改历史行**，`cost`/`jobs` 报表标注为 `legacy`。
- 新体系写入的 `job` 字段一律 `p1-…` 格式；两种格式可共存，靠前缀区分，`validate` 只校验新格式。
- 已存在的 `reports/p1/status.json` 继续作为 H 区数据源，本轮只扩充 `tasks` 汇总字段，不改已有键。

---

## 11. 明确不做的（避免过度设计）

| 不做 | 原因 |
|---|---|
| SQLite / 任何 DB | 单机单任务，文件即真相，便于 rsync 到平板与 diff 审阅 |
| 全局自增计数器 | 需要锁或中心点；时间前缀 + mkdir 已足够 |
| 并发/队列 | 本轮口径就是单任务串行（`up.lock` 的并发问题另开分支，见 U1） |
| 把卡型/模型/分支名编进 ID | 参数会变，ID 要稳定、要能跨分支合并 |
| 重写历史 ledger | 旧记录只读，标注 legacy |
| Git 提交里强制带 ID | 建议 `T03: …` 前缀，但不设为硬约束（本轮也不提交） |

---

## 12. 本方案的验收（批准后第一轮就验，离线 0 元）

- V1 `jobctl tasks` 列出 15 条，依赖无环
- V2 `jobctl next` 输出 `P1-T02`（依赖最靠前的 todo）
- V3 `jobctl validate` 能拒绝：坏 job_id、目录名不一致、params_hash 对不上、有环依赖
- V4 同 `params_hash` 二次 `new` 被拒（除非 `--force` 生成新 job_id）
- V5 架构图 H 区显示 `tasks 完成度 x/15`、当前任务、阻塞数

---

## 13. 需要你拍板的 3 点（已给默认值，不回复即按默认）

| # | 决策 | 默认 | 备选 |
|---|---|---|---|
| D1 | 任务粒度 | **15 条**（如上表，一条一交付物，便于单独验收） | 合并成 8 条（T02+T03 合一、T10–T12 合一），轮次更少但颗粒粗、卡住时不好定位 |
| D2 | `job_id` 是否带分支/阶段前缀 | **只带 `p1-`**（阶段前缀，不带分支名） | 带完整分支名（`p1-stl-…`），可读但合并到 main 后语义失真 |
| D3 | 首次真实闭环的卡型 | **4090 按量 ¥2.15/h，约 26 分钟 ≈ ¥0.95** | V100S ¥1.41/h（更便宜，但 SVD 实测基线都在 4090 上，复现性差） |

---

> 批准方式：回「通过」即按默认 D1/D2/D3 开工第 1 轮（T02→T03→T04，0 元）；
> 要改哪条直接说编号（例：「D1 选合并 8 条」「T12 先不做」）。
