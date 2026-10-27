# Phase 0 改进项 2 · 验收闭环任务报告

| 项 | 内容 |
|---|---|
| **任务** | API 边界压力测试验收补齐（4 个子任务） |
| **执行日期** | 2026-10-27 09:39 – 09:45（Asia/Shanghai） |
| **执行时长** | 约 6 分钟 |
| **直接费用** | **¥0.00**（全程未创建任何实例） |
| **实例残留** | **0 台** |
| **提交** | `41f01b4`（12 files, +2027 行） |
| **结论** | **4/4 子任务完成，5/5 验收标准通过** |

---

## 一、任务背景

Phase 0 改进项 2 的 API 边界压力测试此前已真实执行完成：`tests/` 下三个测试脚本跑通、实例已清理、三份报告已生成。但对照验收标准仍有 4 项未闭环：

| # | 缺口 | 性质 |
|---|---|---|
| 1 | `--dry-run` 从未验证 | 安全性未证明 |
| 2 | 合并费用报告未达验收级 | 交付物不完整 |
| 3 | 幂等性与「未修改现有命令」无声明 | 缺少证据 |
| 4 | 测试中的新未知项未登记 | 知识未沉淀 |

本次任务即针对这 4 项做定向补齐，**范围严格限定为「新增测试与报告」，不修改任何现有命令行为**。

---

## 二、执行摘要

| 子任务 | 内容 | 结果 | 关键证据 |
|---|---|---|---|
| **1** | `--dry-run` 验证与补齐 | ✅ 完成 | 三脚本 rc=0；断网下输出字节级一致；三点不变式全成立 |
| **2** | 合并费用报告 | ✅ 完成 | ¥0.159 / 6 台 / 0.113h，与明细、账本、标价三方吻合 |
| **3** | 声明 + 未知项登记 | ✅ 完成 | 声明写入报告末尾；台账登记 **5** 条（指定 3 + 新增 2） |
| **4** | git 入库 | ✅ 完成 | `41f01b4`，12 files，暂存前已做敏感信息扫描 |

---

## 三、执行时间线

| 时刻 | 动作 |
|---|---|
| 09:39 | 接收任务，确认 4 项范围 |
| 09:41 | 只读审计：核对 dry-run 实现、费用数据源、STATE_CN、git 状态（确认非仓库） |
| 09:42:48 | 三个 `--dry-run` 联网执行，全部 rc=0 |
| 09:42:52 | `unshare -n` 断网重跑，输出与联网版字节级一致 |
| 09:43:05 | 三份报告追加「验收记录」小节 |
| 09:43:51 | 费用报告渲染完成（`tools/test_cost_report.py`） |
| 09:44:07 | `reports/unknowns.md` 建立 |
| 09:44:29 | git commit `41f01b4` |
| 09:45 | 验收自检 + 同步文件窗口 |

---

## 四、子任务 1：`--dry-run` 验证

### 4.1 实现检查（静态审计）

| 检查项 | 结果 | 依据 |
|---|---|---|
| `--dry-run` 参数注册 | ✅ | `tests/_common.py` 的 `parse_args()` 统一注册 |
| `main()` 提前返回 | ✅ | `if args.dry_run: return plan(...)`，位于所有 csctl 调用之前 |
| `plan()` 是否纯打印 | ✅ | 逐行审查：只 `print`，不构造 CS 对象、不读 API |
| `csctl(dry_run=True)` | ✅ | 本地短路返回 `Result(skipped=True)`，不起子进程 |
| `popen_csctl()` 兜底 | ⚠️ 无 | 但靠「提前 return」不会走到（已登记为 U4 相关观察） |

**结论：三个脚本均已实现，无需补写。**

### 4.2 执行结果

| 脚本 | 退出码 | 输出内容 |
|---|---|---|
| `test_concurrent_create.py --dry-run` | **0** | 3 条 `up` 命令、锁行为预期、等待与清理步骤、费用写入位置 |
| `test_rapid_destroy.py --dry-run` | **0** | 场景 A/B 各两条命令、预期被拒情形、重试策略、兜底清理 |
| `test_state_transitions.py --dry-run` | **0** | 创建命令、采样参数（30s / 2s / 约 15 点）、分析项、清理步骤 |

三个输出均以 `--dry-run：未调用任何云端接口，未创建实例。` 结尾。

### 4.3 零触网证明（加强验证）

除「`csctl ls` 无新实例」外，另用 `unshare -n` 剥离全部网络（无网卡、无 DNS、无路由）后重跑：

```
$ unshare -n python3 tests/test_concurrent_create.py --dry-run   rc=0
$ unshare -n python3 tests/test_rapid_destroy.py   --dry-run     rc=0
$ unshare -n python3 tests/test_state_transitions.py --dry-run   rc=0

diff <联网输出> <断网输出>  →  三个全部无差异（字节级完全一致）
```

**断网仍能跑通且输出一致 ⇒ 该路径不存在任何网络调用。**

### 4.4 零副作用（三点不变式）

| 指标 | 运行前 | 运行后 | 结论 |
|---|---|---|---|
| `.compshare/ledger.jsonl` 行数 | 985 | 985 | ✅ 无新增记账 |
| 实例数（`csctl ls`） | 0 | 0 | ✅ 无新实例 |
| 账户余额 | ¥50.82 | ¥50.82 | ✅ 无扣费 |

### 4.5 产物

三份报告各自新增末尾小节「**验收记录：--dry-run 验证**」：

| 报告 | 字节 | 新增内容 |
|---|---|---|
| `reports/test_concurrent_create.md` | 3579 | 实现检查表 + 执行记录 + 断网证明 + 三点不变式 |
| `reports/test_rapid_destroy.md` | 3268 | 同上 |
| `reports/test_state_transitions.md` | 3318 | 同上 |

---

## 五、子任务 2：合并费用报告

### 5.1 交付物

`reports/test_cost_summary.md`（5411 字节，7 个章节），配套渲染器 `tools/test_cost_report.py`（222 行）。

### 5.2 费用总表

| 测试名 | 用例 | 实例数 | 运行小时合计 | 花费 | 备注 |
|---|---|---|---|---|---|
| `test_concurrent_create` | 并发创建 | 3 | 0.101 | ¥0.142 | 3 路并发同时 `up`；首轮 1/3 成功，2 个被 `up.lock` 挡住后串行重试 |
| `test_rapid_destroy` | 快速销毁 | 2 | 0.012 | ¥0.017 | 创建后立即 `stop` / 立即 `rm`；`stop` 被拒，`rm` 成功 |
| `test_state_transitions` | 状态过渡期查询 | 1 | 0.000 | ¥0.000 | 30s 内每 2s 采样，观察状态跳转 |
| **合计** | — | **6** | **0.113** | **¥0.159** | 与明细逐项求和一致 |

### 5.3 费用口径

- 以 `.compshare/ledger.jsonl` 的 `terminate` 记账为准（实例销毁时平台回算的 `run_h × 单价`）
- **实际扣费可能延迟数分钟**到账，与余额变化存在分钟级时间差
- 仅统计实例侧费用（卡时 + 磁盘），不含镜像存储、共享库等固定项

### 5.4 三重对账

| 核对项 | 值 | 结论 |
|---|---|---|
| 三测试小计求和 | 0.142 + 0.017 + 0.000 = **0.159** | ✅ 与合计一致 |
| `test_cost_summary.json` 的 `grand_total` | **0.159** | ✅ 差额 ¥0.000 |
| 折算单价 | 0.159 ÷ 0.113h = **¥1.41/h** | ✅ 与 V100S 标价一致，无异常加价 |

> 账本全部 `terminate` 累计为 ¥1.632（15 笔），高于本报告是预期情形——账本还包含本轮之前的探针、审计、实验室实例。

### 5.5 说明

该文件此前已存在一份简版（974 字节，9:11 由 `_common.record_cost()` 自动生成）。本次为**升级覆写**，补齐口径说明、单价核算、对账与小节结构。

---

## 六、子任务 3：声明与未知项

### 6.1 声明（写入 `test_cost_summary.md` 末尾第七节）

**幂等可重复**：

| 机制 | 实现 |
|---|---|
| 重试逻辑 | 并发被 `up.lock` 挡住的自动串行重试；`rm` 撞「UHost is in the task」重试 3 次（间隔 10s） |
| 实例名前缀 | 固定 `test-*`（`test-concurrent-*` / `test-rapid-*` / `test-state-*`） |
| 清理兜底 | `try/finally` 中一律 `cleanup()` → `csctl rm <uid> --yes`，异常退出也会清理 |
| 幂等清理 | 每轮开始先扫描同名残留并清掉 |
| 报告幂等 | `record_cost()` 以测试名为键覆盖写；`write_report()` 整文件覆盖 |

**未修改现有命令**：本轮只新增 `tests/` 脚本与 `reports/` 报告；`csctl.py`、`compshare/*.py`、`tools/checkpoint.py` 均未改；测试只调用公开子命令（`up`/`stop`/`rm`/`ls`），未 import 内部函数、未打补丁。

### 6.2 未知项台账

`reports/unknowns.md`（8995 字节），登记 **5 条**：

| ID | 未知项 | 等级 | 状态 |
|---|---|---|---|
| U1 | `csctl up` 的 `up.lock` 排他锁把并发创建串行化（首轮 2/3 被挡） | 需规避 | 未修复 |
| U2 | 过渡期（`Initializing`）调 `stop`/`rm` 返回 `8903 UHost is in the task` | 需规避 | 未修复 |
| U3 | `Initializing` 未出现在 `STATE_CN` 枚举，`csctl ls` 显示原始英文 | 仅记录 | 未修复 |
| U4 🆕 | `stop` 无重试、`rm` 有重试，两个命令的重试策略不对称 | 需规避 | 未修复 |
| U5 🆕 | `test_cost_summary.md` 有两个写入方，会互相覆盖 | 仅记录 | 未修复 |

每条含：现象、**原文证据**（引用具体错误码与输出片段）、机理、影响面、等级、临时规避、待验证方向。

**修复优先级建议**：U2 > U1 > U4 > U5 > U3（按「是否导致实例空烧/费用损失」排序）。

---

## 七、子任务 4：git 入库

### 7.1 前置条件

该目录**此前不是 git 仓库**（`fatal: not a git repository`），已执行 `git init -b main`，配置本地身份 `ops@local`（不涉及远端，未 push）。

### 7.2 提交内容

```
41f01b4 test(api-boundary): 补齐 dry-run 验证、合并费用报告、未知项台账

 .gitignore                        |  18 ++
 reports/test_concurrent_create.md | 142 ++++++++++++++
 reports/test_cost_summary.json    |  65 +++++++
 reports/test_cost_summary.md      | 131 +++++++++++++
 reports/test_rapid_destroy.md     | 136 +++++++++++++
 reports/test_state_transitions.md | 145 ++++++++++++++
 reports/unknowns.md               | 103 ++++++++++
 tests/_common.py                  | 396 ++++++++++++++++++++++++++++++++++++++
 tests/test_concurrent_create.py   | 237 +++++++++++++++++++++++++++
 tests/test_rapid_destroy.py       | 239 +++++++++++++++++++++++++++
 tests/test_state_transitions.py   | 193 +++++++++++++++++++++++++++
 tools/test_cost_report.py         | 222 ++++++++++++++++++++++++++++++++++++++
 12 files changed, 2027 insertions(+)
```

### 7.3 安全措施

入库前扫描 `tests/*.py` 的 `accesskey` / `secretkey` / `password` / `sshpass -p` 等关键字，**无命中**。

`.gitignore` 排除：`compshare/credentials.env`、`.compshare/`、`share/`、`logs/`、`*.log`、`__pycache__/`、`.share_token`、`.htpasswd`、`reports/archive/`。

`git diff --cached --name-only` 复核：**无凭据文件、无日志文件**。

---

## 八、验收标准逐条核对

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 三个 `--dry-run` 打印操作计划、rc=0、`csctl ls` 无新实例 | ✅ **通过** | 三次 rc=0；断网下输出字节级一致；ledger 985→985；余额 ¥50.82 未变；实例 0→0 |
| 2 | `reports/test_cost_summary.md` 存在，合计 ¥0.159 与明细一致 | ✅ **通过** | 5411 字节；0.142+0.017+0.000=0.159；单价 ¥1.41 与 V100S 标价吻合 |
| 3 | `reports/unknowns.md` 存在，含 3 个未知项 | ✅ **通过** | 8995 字节，实含 **5** 条（指定 3 条全部在内 + 新增 2 条） |
| 4 | `git log` 可见本轮提交 | ✅ **通过** | `41f01b4`（2026-10-27 09:44:29） |
| 5 | `csctl ls` 中 test-* 实例 0 台，无残留 | ✅ **通过** | `csctl ls \| grep -c "test-"` → **0** |

---

## 九、产出清单

| 文件 | 动作 | 字节 |
|---|---|---|
| `reports/test_cost_summary.md` | 升级覆写 | 5411 |
| `reports/unknowns.md` | 新建 | 8995 |
| `reports/test_concurrent_create.md` | 追加验收记录 | 3579 |
| `reports/test_rapid_destroy.md` | 追加验收记录 | 3268 |
| `reports/test_state_transitions.md` | 追加验收记录 | 3318 |
| `tools/test_cost_report.py` | 新建（报告渲染器） | 222 行 |
| `.gitignore` | 新建（凭据/日志/缓存排除） | 18 行 |
| `logs/dryrun/*.out`、`*.err` | 新建（dry-run 原始输出留证） | 12 个文件 |

---

## 十、偏离说明

| # | 偏离 | 原因 |
|---|---|---|
| 1 | 执行 `git init` | 目录原本不是仓库，为满足「git log 可见提交」只能新建；已配本地身份，未涉及远端 |
| 2 | 额外新增 `tools/test_cost_report.py` | 该渲染器是方案中承诺项。没有它，`record_cost()` 写出的简版会覆盖完整报告（即台账 U5） |
| 3 | 额外新增 `.gitignore` | 防止 `credentials.env`、`.compshare/`、`__pycache__` 被误入库 |
| 4 | 未修改任何现有命令 | 这是任务约束本身，非偏离；已作为声明写入报告并在台账中标注「未修复」 |

---

## 十一、遗留事项

> 2026-10-27 10:37 更新：以下事项**全部暂缓（Deferred）**，修复顺序已定，等后续其他方向跑通后再统一处理。详见 `reports/unknowns.md` 的「修复优先级队列」。

| 顺位 | ID | 事项 | 暂缓原因 |
|---|---|---|---|
| **P1** | U2 | 过渡期 `8903 UHost is in the task` 重试（`stop`/`rm`） | 唯一会造成实例空烧的项，优先修但需独立一轮 |
| **P2** | U1 | `up.lock` 把并发创建串行化 | 不造成费用损失，失败即退出 |
| **P3** | U4 | `stop` 无重试、`rm` 有重试的不对称 | 与 P1 同源，随 P1 一起消解 |
| **P4** | U6 | `--dry-run` 覆盖缺口（`csctl up --dry-run` 未验证、`popen_csctl` 无兜底） | 只影响验证完备度，不影响正确性与费用 |
| 随 P1 | U3 | `Initializing` 未入 `STATE_CN` 映射 | 与 P1 改同一处状态机，拆开做要动两遍 |
| 未排期 | U5 | 费用报告双写入方冲突 | 已有稳定临时规避（跑完测试重渲染） |

**本轮不改代码**：上述 6 条全部只登记、不修复。`--dry-run` 的覆盖缺口即台账 U6，已随本次更新归入 P4。

| 其他 | 说明 | 动作 |
|---|---|---|
| 本报告未入库 | 生成于 `41f01b4` 提交之后 | 如需入库：`git add reports/phase0-item2-task-report-20261027.md reports/unknowns.md && git commit` |

---

## 附录：复现命令

```bash
# 1) dry-run 验证（零费用）
python3 tests/test_concurrent_create.py --dry-run
python3 tests/test_rapid_destroy.py --dry-run
python3 tests/test_state_transitions.py --dry-run

# 断网加强验证（需 root）
unshare -n python3 tests/test_concurrent_create.py --dry-run

# 2) 重渲染合并费用报告
python3 tools/test_cost_report.py

# 3) 重新真实执行（会产生费用，合计约 ¥0.16）
python3 tests/test_concurrent_create.py
python3 tests/test_rapid_destroy.py
python3 tests/test_state_transitions.py

# 4) 验收自查
python3 csctl.py ls | grep -c "test-"     # 期望 0
git log --oneline -1
```

---

> 报告生成：2026-10-27 09:47（Asia/Shanghai）
> 数据来源：`logs/dryrun/*.out`、`reports/test_cost_summary.json`、`.compshare/ledger.jsonl`、`git log`
