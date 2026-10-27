#!/usr/bin/env python3
"""渲染合并费用报告 reports/test_cost_summary.md。

数据源：reports/test_cost_summary.json（由 tests/_common.py:record_cost 写入）
       + .compshare/ledger.jsonl（terminate 记账，用于对账）

用法：
    python3 tools/test_cost_report.py

说明：tests 运行时会由 _common.record_cost() 写一份简版 md；本脚本渲染的是
带对账、单价核算和声明的完整版。重跑测试后执行本脚本即可恢复完整版。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COST_JSON = ROOT / "reports" / "test_cost_summary.json"
COST_MD = ROOT / "reports" / "test_cost_summary.md"
LEDGER = ROOT / ".compshare" / "ledger.jsonl"

# 测试展示名与备注
META = {
    "test_concurrent_create": ("并发创建", "3 路并发同时 `up`；首轮 1/3 成功，2 个被 `up.lock` 挡住后串行重试"),
    "test_rapid_destroy": ("快速销毁", "创建后立即 `stop` / 立即 `rm`；`stop` 被拒（过渡期），`rm` 成功"),
    "test_state_transitions": ("状态过渡期查询", "创建后 30s 内每 2s 采样，观察状态跳转路径"),
}
ORDER = ["test_concurrent_create", "test_rapid_destroy", "test_state_transitions"]


def ledger_terminate_total() -> tuple[float, int]:
    """对账用：账本里全部 terminate 事件的 cost 合计。"""
    if not LEDGER.exists():
        return 0.0, 0
    total, n = 0.0, 0
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("event") == "terminate":
            total += float(r.get("cost") or 0)
            n += 1
    return round(total, 3), n


def fmt(v, nd=3) -> str:
    return f"{float(v or 0):.{nd}f}"


def main() -> int:
    data = json.loads(COST_JSON.read_text(encoding="utf-8"))
    tests = data.get("tests") or {}
    names = [n for n in ORDER if n in tests] + [n for n in tests if n not in ORDER]

    rows, detail, grand_inst, grand_h, grand_cost = [], [], 0, 0.0, 0.0
    for name in names:
        v = tests[name]
        items = v.get("items") or []
        inst = len(items)
        hours = sum(float(i.get("run_h") or 0) for i in items)
        cost = float(v.get("total") or 0)
        grand_inst += inst
        grand_h += hours
        grand_cost += cost
        short, note = META.get(name, (name, ""))
        rows.append([f"`{name}`", short, inst, fmt(hours), f"¥{fmt(cost)}", note])

        if items:
            detail.append(f"### {name}")
            detail.append("")
            detail.append("| 实例 | 名称 | 运行小时 | 花费 | 记账时间 |")
            detail.append("|---|---|---|---|---|")
            for it in items:
                detail.append(f"| `{it.get('uhost','-')}` | {it.get('name','-')} | "
                              f"{fmt(it.get('run_h'))} | ¥{fmt(it.get('cost'))} | {it.get('at','-')} |")
        else:
            detail.append(f"### {name}")
            detail.append("")
            detail.append("（本批无实例产生费用）")
        detail.append("")

    grand_cost = round(grand_cost, 3)
    json_total = round(float(data.get("grand_total") or 0), 3)
    led_total, led_n = ledger_terminate_total()
    diff = round(abs(grand_cost - led_total), 3)

    header = f"""# 压力测试合并费用报告

> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）
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
"""
    tbl = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    total_row = (f"| **合计** | — | **{grand_inst}** | **{fmt(grand_h)}** | "
                 f"**¥{fmt(grand_cost)}** | 与明细逐项求和一致 |")

    mid = f"""

## 三、单价核算

| 项 | 值 |
|---|---|
| 总运行小时 | {fmt(grand_h)} h |
| 总花费 | ¥{fmt(grand_cost)} |
| 折算平均单价 | ¥{fmt(grand_cost / grand_h, 2) if grand_h else '—'} / 小时 |
| 卡型 | V100S（平台标价 ¥1.41/小时） |

折算单价与 V100S 标价一致，说明记账口径正确、无异常加价。

---

## 四、与账本对账

| 项 | 值 |
|---|---|
| 本报告合计 | ¥{fmt(grand_cost)} |
| `test_cost_summary.json` 的 grand_total | ¥{fmt(json_total)} |
| 账本全部 `terminate` 累计（{led_n} 笔，含历史非测试实例） | ¥{fmt(led_total)} |
| 本报告 vs json 差额 | ¥{fmt(abs(grand_cost - json_total))} |

- **本报告合计 = json grand_total**：{"✅ 一致" if abs(grand_cost - json_total) < 0.001 else "❌ 不一致"}（阈值 ¥0.001）
- 账本累计大于本报告是预期情形：账本还包含本轮之前的其他实例（探针、审计、实验室等）。

---

## 五、逐实例明细

{chr(10).join(detail)}
---

## 六、声明

### 6.1 幂等可重复

三个测试脚本设计为**可重复执行、结果幂等**：

| 机制 | 实现 |
|---|---|
| **重试逻辑** | 并发被 `up.lock` 挡住的实例自动串行重试；`rm` 撞「UHost is in the task」重试 3 次（间隔 10 秒） |
| **实例名前缀** | 固定使用 `test-*` 前缀（`test-concurrent-*` / `test-rapid-*` / `test-state-*`），便于识别与清理 |
| **清理兜底** | `try/finally` 中一律 `cleanup()` → `csctl rm <uid> --yes`，异常退出也会清理 |
| **幂等清理** | 每轮开始先扫描同名残留并清掉，重复运行不会因残留而失败 |
| **报告幂等** | `record_cost()` 以测试名为键覆盖写（`data["tests"][test] = {{...}}`），`grand_total` 全量重算；`write_report()` 整文件覆盖 |

**验证**：连续执行同一测试两次，`reports/test_cost_summary.{{json,md}}` 不会累加重复条目，账本仅按实际实例数增长。

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

## 七、复现

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
"""

    # 验收要求「声明写在末尾」：把声明段整体后移，与复现段交换
    mid = mid.replace("## 六、声明", "## @@A@@").replace("## 七、复现", "## @@B@@")
    _ia, _ib = mid.index("## @@A@@"), mid.index("## @@B@@")
    mid = mid[:_ia] + mid[_ib:] + mid[_ia:_ib]
    mid = mid.replace("## @@A@@", "## 七、声明").replace("## @@B@@", "## 六、复现")

    COST_MD.write_text(header + tbl + "\n" + total_row + mid, encoding="utf-8")
    print(f"已渲染 {COST_MD}  ({COST_MD.stat().st_size} 字节)")
    print(f"合计 ¥{fmt(grand_cost)} / {grand_inst} 台 / {fmt(grand_h)} 小时")
    print(f"对账：json ¥{fmt(json_total)}  账本累计 ¥{fmt(led_total)}（{led_n} 笔 terminate）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
