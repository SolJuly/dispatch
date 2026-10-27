# 未知项登记台账

> 建立时间：2026-10-27 09:47（Asia/Shanghai）
> 来源：Phase 0 改进项 2 — API 边界压力测试（`tests/` 下 3 个脚本真实执行）
> 规则：**只登记，不在本轮修复**。每条给出现象、原文证据、影响面、等级、临时规避与下一步验证方式。

---

## 索引

| ID | 标题 | 等级 | 状态 |
|---|---|---|---|
| U1 | `csctl up` 的 `up.lock` 排他锁把并发创建串行化 | 需规避 | 未修复 |
| U2 | 过渡期（`Initializing`）调 `stop` / `rm` 返回 `8903 UHost is in the task` | 需规避 | 未修复 |
| U3 | `Initializing` 未出现在 `STATE_CN` 枚举，`csctl ls` 显示原始英文 | 仅记录 | 未修复 |
| U4 | `stop` 无重试、`rm` 有重试，两个命令的重试策略不对称 | 需规避 | 未修复 |
| U5 | `reports/test_cost_summary.md` 会被测试运行覆盖为简版，与完整版渲染冲突 | 仅记录 | 未修复 |

等级口径：**阻塞** = 会直接导致测试/生产失败；**需规避** = 会失败但可绕过；**仅记录** = 不影响正确性，属体验或一致性问题。

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
| **状态** | 未修复（本轮不修改 `csctl.py`） |

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
| **状态** | 未修复（本轮不修改 `csctl.py` 的 stop/rm 重试策略） |

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
| **状态** | 未修复（本轮不修改 `compshare/cs.py`） |

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
| **状态** | 未修复（本轮按"只记录"处理，`tests/test_rapid_destroy.py` 不改） |

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
| **状态** | 未修复（本轮不修改 `tests/_common.py`） |

---

## 处理原则

1. **本轮全部不修复**，只登记。任何修复都需要先评估对现有命令行为的影响，单独开一轮。
2. 修复优先级建议：**U2 > U1 > U4 > U5 > U3**（按"是否会导致实例空烧/费用损失"排序）。
3. 新增未知项一律追加到本文件，不新建第二份台账。
