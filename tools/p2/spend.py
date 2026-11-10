#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成本口径：把「今天到底花了多少钱」收敛成一个答案。

## 为什么需要单独一个模块

修之前有**三份实现**，口径全不一样：

    tools/p2/scheduler.py:today_cost()    累加 `r.get("cost")`（不筛 event）
    csctl.py:ledger_today_cost()          只累加 `event == "cost"`
    tools/riskmon.py:_today_cost()        转调 csctl 那份，兜底再抄一份

更关键的是**三份都看不见真实扣费**。账本里的记录长这样（2026-11-09 实测）：

    {"event": "job",    "job": "p1-...", "cost": 0.2222}      ← 作业自报
    {"event": "settle", "amount": 0.23, "from": 8.38,
     "to": 8.15, "prev_at": "...", "gap_s": 601}              ← 平台真扣

`settle` 记的是 `amount`，不是 `cost`。三份实现都只读 `cost`，所以：

    today_cost() = ¥0.00      ← 熔断器看到的
    真实扣费     = ¥0.86      ← 账本 settle 加起来的

熔断阈值 ¥8.00，看到的永远是 0，于是**永远不会熔断**。这一轮 P4 要真机跑
24 条工作流，熔断器失明意味着没有任何东西拦得住连续烧钱 —— 这是必须先修的。

## 口径怎么定

**一次花费只记一次。** 两条来源会重叠：

  · `job` 记录是 run.py 按 manifest 自报的（hourly × minutes / 60）
  · `settle` 记录是 csctl 用平台余额差算出来的

两者不可能完全相等（自报按整分钟四舍五入，平台按秒结算）。直接相加会重复计数。

所以按**优先级取一份**，不求和：

    ① 有 settle → 用 settle 的 amount（平台是唯一事实来源）
    ② 没 settle 但有 job → 用 job 的 cost（作业刚结束、还没到结算点时的近似）

判断粒度按"作业"而不是按天：某个作业既有 job 记录又有对应的 settle 时，
以 settle 为准 —— 但 settle 记录里没有 job_id（它是按实例/时间窗口结算的），
所以做不到精确配对。

**实用的折中**（也是这里采用的）：`settle` 是账户级的、覆盖性的事实，
`job` 是作业级的、逐条的估算。两者共存时：

    total = max(Σ settle.amount, Σ job.cost)

取 max 而不是相加，是因为 settle 通常**已经包含**了那些 job 的花费。
取 max 保证：① 不会因为只看到 job 而低估（job 只在作业结束时写，进行中看不到）；
② 不会因为 settle 和 job 都算而高估一倍。

熔断器要的是"别让它烧穿"的下界，宁可略微高估也不要低估 —— 所以 max 是对的。
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / ".compshare" / "ledger.jsonl"

# 这几种事件带"钱"，其余（balance / create / ssh_exec / stop / …）不计
SPEND_EVENTS_SETTLE = ("settle",)
SPEND_EVENTS_JOB = ("job", "cost")


def _rows(ledger: Path | None = None) -> list[dict]:
    p = Path(ledger) if ledger else LEDGER
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict):
            out.append(r)
    return out


def _day_of(r: dict) -> str:
    """记录属于哪一天。`at` 是 "YYYY-MM-DD HH:MM:SS"；没有就按 ts 换算。"""
    at = str(r.get("at") or "")
    if len(at) >= 10:
        return at[:10]
    ts = r.get("ts")
    if ts:
        try:
            return dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            pass
    return ""


def spend_on(day: str | None = None, *, ledger: Path | None = None) -> dict:
    """某一天的花费明细。`day` 缺省为今天。

    返回 {"day", "settle", "job", "total", "settle_n", "job_n"}。
    `total` 是熔断该用的那个数（max 口径，理由见模块 docstring）。
    """
    day = day or dt.date.today().isoformat()
    settle = job = 0.0
    settle_n = job_n = 0
    for r in _rows(ledger):
        if _day_of(r) != day:
            continue
        ev = r.get("event")
        if ev in SPEND_EVENTS_SETTLE:
            try:
                settle += float(r.get("amount") or 0)
                settle_n += 1
            except (TypeError, ValueError):
                pass
        elif ev in SPEND_EVENTS_JOB:
            try:
                job += float(r.get("cost") or 0)
                job_n += 1
            except (TypeError, ValueError):
                pass
    return {"day": day, "settle": round(settle, 4), "job": round(job, 4),
            "total": round(max(settle, job), 4),
            "settle_n": settle_n, "job_n": job_n}


def today_cost(ledger: Path | None = None) -> float:
    """今天花了多少（熔断器用）。含 settle 真实扣费，不再是 0。"""
    return spend_on(None, ledger=ledger)["total"]
