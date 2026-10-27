# 压力测试合并费用报告

> 生成时间：2026-10-27 09:43:51（Asia/Shanghai）
> 数据源：`reports/test_cost_summary.json`（由 `tests/_common.py:record_cost()` 写入）  
> 渲染器：`python3 tools/test_cost_report.py`  
> 覆盖范围：Phase 0 改进项 2 — API 边界压力测试，共 3 个测试脚本

---

## 一、费用口径

**以 `.compshare/ledger.jsonl` 中 `terminate` 事件的记账为准**，即实例销毁时平台回算的实际计费金额（`run_h` × 单价）。

需要注意两点：

1. **实际扣费可能延迟数分钟**到账，账本记录的是销毁时刻的回算值，与账户余额变化可能存在分钟级时间差；
2. 本报告只统计**实例侧费用**（卡时 + 磁盘），不含镜像存储、共享库等固定项。

---

## 二、总计

| 测试名 | 用例 | 实例数 | 运行小时合计 | 花费 | 备注 |
|---|---|---|---|---|---|
| `test_concurrent_create` | 并发创建 | 3 | 0.101 | ¥0.142 | 3 路并发同时 `up`；首轮 1/3 成功，2 个被 `up.lock` 挡住后串行重试 |
| `test_rapid_destroy` | 快速销毁 | 2 | 0.012 | ¥0.017 | 创建后立即 `stop` / 立即 `rm`；`stop` 被拒（过渡期），`rm` 成功 |
| `test_state_transitions` | 状态过渡期查询 | 1 | 0.000 | ¥0.000 | 创建后 30s 内每 2s 采样，观察状态跳转路径 |
| **合计** | — | **6** | **0.113** | **¥0.159** | 与明细逐项求和一致 |

## 三、单价核算

| 项 | 值 |
|---|---|
| 总运行小时 | 0.113 h |
| 总花费 | ¥0.159 |
| 折算平均单价 | ¥1.41 / 小时 |
| 卡型 | V100S（平台标价 ¥1.41/小时） |

折算单价与 V100S 标价一致，说明记账口径正确、无异常加价。

---

## 四、与账本对账

| 项 | 值 |
|---|---|
| 本报告合计 | ¥0.159 |
| `test_cost_summary.json` 的 grand_total | ¥0.159 |
| 账本全部 `terminate` 累计（15 笔，含历史非测试实例） | ¥1.632 |
| 本报告 vs json 差额 | ¥0.000 |

- **本报告合计 = json grand_total**：✅ 一致（阈值 ¥0.001）
- 账本累计大于本报告是预期情形：账本还包含本轮之前的其他实例（探针、审计、实验室等）。

---

## 五、逐实例明细

### test_concurrent_create

| 实例 | 名称 | 运行小时 | 花费 | 记账时间 |
|---|---|---|---|---|
| `uhost-1wyuvwjurbqk` | test-concurrent-a | 0.027 | ¥0.038 | 2026-10-27 09:11:43 |
| `uhost-1wyuub32uq7g` | test-concurrent-b | 0.067 | ¥0.094 | 2026-10-27 09:11:50 |
| `uhost-1wyuz1l5c31r` | test-concurrent-c | 0.007 | ¥0.010 | 2026-10-27 09:11:55 |

### test_rapid_destroy

| 实例 | 名称 | 运行小时 | 花费 | 记账时间 |
|---|---|---|---|---|
| `uhost-1wyutc7jvptb` | test-rapid-rm | 0.000 | ¥0.000 | 2026-10-27 09:06:15 |
| `uhost-1wyurjqu7srz` | test-rapid-stop | 0.012 | ¥0.017 | 2026-10-27 09:06:22 |

### test_state_transitions

| 实例 | 名称 | 运行小时 | 花费 | 记账时间 |
|---|---|---|---|---|
| `uhost-1wyuqafmju4o` | test-state-tx | 0.000 | ¥0.000 | 2026-10-27 09:04:15 |

---

## 六、复现

```bash
# 真实执行（会创建实例并产生费用，约 ¥0.16 总计）
python3 tests/test_concurrent_create.py
python3 tests/test_rapid_destroy.py
python3 tests/test_state_transitions.py

# 只验证流程、零费用（已验证不触网）
python3 tests/test_concurrent_create.py --dry-run
python3 tests/test_rapid_destroy.py --dry-run
python3 tests/test_state_transitions.py --dry-run

# 重新渲染本报告
python3 tools/test_cost_report.py
```
## 七、声明

### 6.1 幂等可重复

三个测试脚本设计为**可重复执行、结果幂等**：

| 机制 | 实现 |
|---|---|
| **重试逻辑** | 并发被 `up.lock` 挡住的实例自动串行重试；`rm` 撞「UHost is in the task」重试 3 次（间隔 10 秒） |
| **实例名前缀** | 固定使用 `test-*` 前缀（`test-concurrent-*` / `test-rapid-*` / `test-state-*`），便于识别与清理 |
| **清理兜底** | `try/finally` 中一律 `cleanup()` → `csctl rm <uid> --yes`，异常退出也会清理 |
| **幂等清理** | 每轮开始先扫描同名残留并清掉，重复运行不会因残留而失败 |
| **报告幂等** | `record_cost()` 以测试名为键覆盖写（`data["tests"][test] = {...}`），`grand_total` 全量重算；`write_report()` 整文件覆盖 |

**验证**：连续执行同一测试两次，`reports/test_cost_summary.{json,md}` 不会累加重复条目，账本仅按实际实例数增长。

### 6.2 未修改现有命令

**本轮只新增 `tests/` 下的测试脚本与 `reports/` 下的报告文件，未修改 `csctl.py`、`compshare/*.py`、`tools/checkpoint.py` 等任何现有命令的行为。**

具体说明：

| 项 | 状态 |
|---|---|
| `csctl.py` | 未修改（文件指纹与测试前一致） |
| `compshare/cs.py`、`compshare/credentials.env` | 未修改 |
| `tools/*.py`（含 `checkpoint.py`、`riskmon.py`） | 未修改 |
| 新增内容 | `tests/`（3 个测试 + `_common.py`）、`reports/*.md`、`tools/test_cost_report.py`（仅渲染报告，不参与任何命令执行路径） |
| 调用方式 | 测试只调用 csctl 的**公开子命令**（`up` / `stop` / `rm` / `ls`），未 import 内部函数、未打补丁、未改环境变量 |

> 注：本目录不是 git 仓库的历史基线，故以文件指纹 + 命令面快照作为「未修改」的证据；证据明细见验收记录。

---

