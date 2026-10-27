# 未知项登记台账

> 建立时间：2026-10-27 09:47（Asia/Shanghai）
> 来源：Phase 0 改进项 2 — API 边界压力测试（`tests/` 下 3 个脚本真实执行）
> 规则：**只登记，不在本轮修复**。每条给出现象、原文证据、影响面、等级、临时规避与下一步验证方式。
> 当前处置：**全部暂缓（Deferred）**。修复优先级已排定，等后续其他方向跑通后再统一处理，见「修复优先级队列」。

---

## 索引

| ID | 标题 | 等级 | 优先级 | 状态 |
|---|---|---|---|---|
| U2 | 过渡期（`Initializing`）调 `stop` / `rm` 返回 `8903 UHost is in the task` | 需规避 | **P1** | 暂缓 |
| U1 | `csctl up` 的 `up.lock` 排他锁把并发创建串行化 | 需规避 | **P2** | 暂缓 |
| U4 | `stop` 无重试、`rm` 有重试，两个命令的重试策略不对称 | 需规避 | **P3** | 暂缓 |
| U6 | `--dry-run` 覆盖缺口（`csctl up --dry-run` 未验证、`popen_csctl` 无兜底） | 仅记录 | **P4** | 暂缓 |
| U3 | `Initializing` 未出现在 `STATE_CN` 枚举，`csctl ls` 显示原始英文 | 仅记录 | 随 P1 同批 | 暂缓 |
| U5 | `reports/test_cost_summary.md` 会被测试运行覆盖为简版，与完整版渲染冲突 | 仅记录 | 未排期 | 暂缓 |

等级口径：**阻塞** = 会直接导致测试/生产失败；**需规避** = 会失败但可绕过；**仅记录** = 不影响正确性，属体验或一致性问题。

---

## 修复优先级队列

> 定于 2026-10-27 10:37。**全部暂缓**，等后续其他方向跑通后再回头处理。
> 排序原则：先修「会导致实例空烧／费用损失」的，再修「影响可观测性」的，最后修「只影响验证完备度」的。

| 顺位 | ID | 事项 | 为什么排这个位置 | 处理方式 |
|---|---|---|---|---|
| **P1** | U2 | 过渡期 `8903 UHost is in the task` 重试 | 唯一会直接导致「该关的机关不掉 → 实例继续计费」的问题；创建后立刻关机的省钱路径完全依赖它 | 给 `stop` / `rm` 加统一 8903 重试（含间隔与上限） |
| **P2** | U1 | `up.lock` 把并发创建串行化 | 会让任何并发脚本退化，但不造成费用损失（失败即退出，不产生实例） | 评估锁改「阻塞等待 + 超时」，或文档明确要求调用方自带重试 |
| **P3** | U4 | `stop` 无重试、`rm` 有重试的不对称 | 与 P1 同源，是 P1 的具体表现；P1 修好后此项自然消解 | 与 P1 合并处理，不单独开工 |
| **P4** | U6 | `--dry-run` 覆盖缺口 | 只影响「验证完备度」，不影响运行正确性与费用 | 补一份 `csctl up --dry-run` 的行为验证；给 `popen_csctl` 加 dry_run 兜底 |
| 随 P1 | U3 | `Initializing` 未入 `STATE_CN` | 与 P1 同属「过渡期状态」问题，改同一处状态机代码，拆开做反而要动两遍 | 与 P1 同批改，补映射 + 补文档 |
| 未排期 | U5 | 费用报告双写入方 | 已有稳定临时规避（跑完测试固定重渲染），不影响验收 | 待 U1–U6 处理完再评估是否改为「json 唯一数据源」 |

**执行前置条件**：其他方向（视频链路、成本优化等）跑通后，再按 P1 → P2 → P3 → P4 顺序推进；U3 跟随 P1，U5 最后。

---

## U1 — `csctl up` 的 `up.lock` 排他锁把并发创建串行化

| 项 | 内容 |
|---|---|
| **现象** | 同时并发发起 3 个 `csctl.py up`，只有 1 个成功创建，另外 2 个立即返回失败（rc=1），耗时 0.3s。串行重试后 3 台全部创建成功。 |
| **证据** | `reports/test_concurrent_create.md`「二、首轮并发明细」：`test-concurrent-a` rc=1 / 0.3s / 被 up.lock 挡住；`test-concurrent-b` rc=0 / 76.0s / `uhost-1wyuub32uq7g`。首轮成功数 **1/3**。 |
| **机理** | `csctl.py` 中的 `acquire_up_lock()` 用 `flock` 对 `.compshare/up.lock` 加排他锁，第二个 `up` 拿不到锁就直接退出。这是**客户端**行为，不是平台 API 的并发限制。 |
| **影响面** | ① 任何依赖"批量并发创建"的脚本或流程图必须自带重试；② 若无重试，会被误判为"平台不支持并发创建"；③ 并发数越多，被挡住的比例越高。 |
| **等级** | 需规避 |
| **临时规避** | 并发发起后，对被锁挡住的实例做**串行重试**（`test_concurrent_create.py` 已实现此逻辑，重试后 3/3 成功）；或改为串行创建。 |
| **待验证** | ① 平台 API 本身是否支持真正并发创建（绕过 csctl 直接调 `CreateCompShareInstance`）；② 锁的等待模式（现在是非阻塞立即失败，可否改为带超时的阻塞等待）。 |
| **状态** | **暂缓（Deferred）** · 优先级 P2 · 本轮不修改 `csctl.py` |

---

## U2 — 过渡期（`Initializing`）调 `stop` / `rm` 返回 `8903 UHost is in the task`

| 项 | 内容 |
|---|---|
| **现象** | 实例刚创建、状态还是 `Initializing` 时调 `stop` 或 `rm`，平台返回 8903 错误拒绝执行。`rm` 重试一次后成功；`stop` 未被重试，最终失败（实例仍走到 `Running`）。 |
| **证据（stop）** | `reports/test_rapid_destroy.md`「二、场景 A」：动手瞬间状态 `Initializing`，`stop rc=1`，耗时 3.7s，输出原文：<br>`StopCompShareInstance - 8903: UHost is in the task, please try again later (_client.py:87 ucloud)`<br>`错误: ... 8903: UHost is in the task, please try again later`<br>关机后状态：`Running`（即 stop 未生效） |
| **证据（rm）** | 同报告「三、场景 B」：第 1 次 `rm` rc=1 / 2.5s，输出 `TerminateCompShareInstance - 8903: uhost is in the task, please try again later`；第 2 次 rc=0 / 6.2s 成功，实例消失。 |
| **机理** | 平台在实例处于"任务进行中"（Install / Initializing 等过渡态）时，对 `Stop` / `Terminate` 请求返回 8903。属**服务端状态机保护**，不是网络或客户端问题。 |
| **影响面** | ① "创建后立刻关机省钱"这条路在过渡期不可用，必须重试；② 自动化脚本若无重试会留下运行中的实例（持续烧钱）；③ 兜底清理逻辑必须覆盖过渡期。 |
| **等级** | 需规避 |
| **临时规避** | 捕获 `8903` / `is in the task` 关键字后**间隔重试**。参考 `test_rapid_destroy.py` 对 `rm` 的 3 次重试（间隔 10s）。 |
| **待验证** | ① 8903 的可重试窗口有多长（从创建到可 stop 的最短时间）；② 是否所有过渡态（`Install`/`Initializing`/`Starting`）都返回 8903；③ 8903 是"稍后重试必然成功"还是"某些阶段永久拒绝"。 |
| **状态** | **暂缓（Deferred）** · 优先级 P1 · 本轮不修改 `csctl.py` 的 stop/rm 重试策略 |

---

## U3 — `Initializing` 未出现在 `STATE_CN` 枚举，`csctl ls` 显示原始英文

| 项 | 内容 |
|---|---|
| **现象** | 平台实际返回 `State = "Initializing"`，但 `compshare/cs.py` 的 `STATE_CN` 映射表里没有这个键，导致 `csctl ls` 的状态列直接显示英文 `Initializing`（取不到中文时回落为原始值）。 |
| **证据** | `reports/test_state_transitions.md`「四、状态文档化核对」：11 个采样点全部为 `Initializing`，标注「**未文档化**」；`STATE_CN` 现有 9 个键（`Running` / `Stopped` / `Starting` / `Stopping` / `Install` / `Rebooting` / `Interrupted` / `Terminating` / `Unknown`），**不含 `Initializing`**。 |
| **机理** | `cs.py:165`：`i["_state_cn"] = STATE_CN.get(i.get("State", "Unknown"), i.get("State", "?"))` —— 未命中时回落为原始英文。 |
| **影响面** | ① 界面中英文混杂，状态列出现 `Initializing`；② 任何按中文状态做判断的逻辑会漏掉这个状态；③ 文档与真实状态机不一致，容易误判实例"卡住"。 |
| **等级** | 仅记录（不影响执行正确性） |
| **临时规避** | 按英文 `State` 原值判断，不依赖中文映射；或在脚本侧自建扩展映射表。 |
| **待验证** | 平台完整状态枚举到底有多少个值（`Initializing` 之外是否还有未出现的），需要用更长的采样窗口或官方文档确认。 |
| **状态** | **暂缓（Deferred）** · 随 P1 同批 · 本轮不修改 `compshare/cs.py` |

---

## U4 — `stop` 无重试、`rm` 有重试，两个命令的重试策略不对称

| 项 | 内容 |
|---|---|
| **现象** | 面对同一个 8903 过渡期拒绝，`rm` 被脚本重试 3 次并最终成功；`stop` 只尝试 1 次就放弃，实例继续运行到 `Running` 并开始正常计费。 |
| **证据** | `reports/test_rapid_destroy.md`：场景 B `rm` 尝试 2 次（rc=1 → rc=0 成功）；场景 A `stop` rc=1 后无第二次尝试，结论表记为「被拒/失败」，关机后状态 `Running`。 |
| **机理** | 测试脚本对 `rm` 包了重试循环，对 `stop` 没有。属**脚本实现不一致**，非平台问题。 |
| **影响面** | ① "创建后立即关机省钱"这类场景实际没跑通；② 若把这个模式带到生产，过渡期 stop 失败会导致实例空烧；③ 失败与成功在同一份报告里并列，容易被误读为"stop 不支持"。 |
| **等级** | 需规避 |
| **临时规避** | 在调用侧自己加 `stop` 重试（捕获 8903 / `is in the task`），或先等状态离开过渡期再 stop。 |
| **待验证** | 给 `stop` 加上与 `rm` 等价的重试后，场景 A 能否成功关机（需再跑一次真实测试，约 ¥0.02）。 |
| **状态** | **暂缓（Deferred）** · 优先级 P3 · 本轮按"只记录"处理，`tests/test_rapid_destroy.py` 不改 |

---

## U5 — `reports/test_cost_summary.md` 会被测试运行覆盖为简版

| 项 | 内容 |
|---|---|
| **现象** | `reports/test_cost_summary.md` 有两个写入方：测试运行时由 `tests/_common.py:record_cost()` 写一份**简版**（仅累计+分测试小计+明细），`tools/test_cost_report.py` 渲染一份**完整版**（含口径、单价核算、对账、声明）。任一方写入都会覆盖对方。 |
| **证据** | `tests/_common.py:311` `record_cost()` 末尾 `COST_MD.write_text(...)` 直接覆盖；本报告由 `python3 tools/test_cost_report.py` 生成，5411 字节，含 7 个章节。 |
| **机理** | 两个渲染器写同一个路径，没有"完整版优先"或"分文件"约定。 |
| **影响面** | ① 重跑任一测试后，合并报告的完整版内容会退化为简版，验收材料丢失；② 幂等性只在单一方内部成立，跨方不成立。 |
| **等级** | 仅记录 |
| **临时规避** | 跑完测试后固定执行 `python3 tools/test_cost_report.py` 重新渲染完整版（已写入 `reports/test_cost_summary.md` 第六节复现命令）。 |
| **待验证** | 是否改为双文件（`test_cost_summary.json` 为唯一数据源，md 由渲染器独占生成），需评估对 `_common.record_cost()` 的改动面。 |
| **状态** | **暂缓（Deferred）** · 未排期 · 本轮不修改 `tests/_common.py` |

---

## U6 — `--dry-run` 覆盖缺口：`csctl up --dry-run` 未验证、`popen_csctl` 无兜底

| 项 | 内容 |
|---|---|
| **现象** | 本轮 dry-run 验证只覆盖了 `tests/` 三个脚本的 dry-run（本地短路、完全离线）。`csctl up --dry-run` 是**另一种语义**——它会先做只读探测（查库存、查价、查余额、找镜像），再在创建前返回，从未单独验证过它的「不创建、不计费」行为。另外 `tests/_common.popen_csctl()` **没有** `dry_run` 参数兜底。 |
| **证据** | `csctl.py` 的 `cmd_up`：dry-run 分支位于 `cs.stock()` / `cs.price()` / `cs.balance()` 调用**之后**，写法为 `if a.dry_run: print("--dry-run：不实际创建。"); return`；`tests/_common.py:114 popen_csctl(*args)` 签名为 `def popen_csctl(*args: str)`，无 dry_run 形参。 |
| **机理** | 两者概念不同：测试脚本的 dry-run 是「客户端完全离线演练」，`csctl up --dry-run` 是「真实只读探测 + 跳过写操作」。前者已证明零触网，后者**必然会触网（只读接口）**，但「不创建」这一点未做端到端验证。`popen_csctl` 目前只靠调用方在 `main()` 里提前 return 才不会真跑，属隐式契约。 |
| **影响面** | ① `csctl up --dry-run` 若因代码演进误把写操作提前，不会被现有测试发现；② 未来给测试加新场景时，漏传提前 return 就可能让 dry-run 真创建实例（烧钱风险）；③ 验收时「dry-run 已验证」的说法需要限定范围，否则语义被夸大。 |
| **等级** | 仅记录（不影响当前运行正确性） |
| **临时规避** | ① 涉及 `up --dry-run` 的场景，人工核对输出里是否以「--dry-run：不实际创建。」结尾且 `csctl ls` 无新增；② 测试脚本里新增场景时，`main()` 首行必须保留 `if args.dry_run: return plan(...)`。 |
| **待验证** | ① 给 `csctl up --dry-run` 补一份验证：前后 `csctl ls` 与 ledger 行数不变、余额不变；② 评估给 `popen_csctl` 加 `dry_run` 形参（返回假进程对象），把隐式契约变显式。 |
| **状态** | **暂缓（Deferred）** · 优先级 P4 · 本轮不修改 `csctl.py` 与 `tests/_common.py` |

---

## 处理原则

1. **本轮全部不修复，全部暂缓**。任何修复都要先评估对现有命令行为的影响，单独开一轮，且要等其他方向跑通后再动。
2. 修复顺序固定为：**U2（P1）→ U1（P2）→ U4（P3）→ U6（P4）**；U3 随 U2 同批处理；U5 未排期。该顺序由「是否导致实例空烧/费用损失」决定，不随讨论顺序变动。
3. 拒绝"顺手修"：这 6 条都涉及核心命令或测试框架，改动必须独立分支、独立验证，不与功能开发混在一起。
4. 新增未知项一律追加到本文件，不新建第二份台账；新条目需给出等级与初步顺位。
