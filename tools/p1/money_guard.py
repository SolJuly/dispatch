#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""花费闸：三道钱上的限制，防止"一觉醒来余额没了"。

## 为什么需要它

系统里原来**只有调度器**有预算熔断（`scheduler.py` 的 `daily_budget`），
而调度器是死代码（从 queue.db 领活，没人往里放活）。真正跑作业的
`autostart` 补位路径**一道闸都没有**：

    补位能连续拉起作业 → 每个都建机 ¥2.15/h → 一天 ¥200+ 无人拦

实测 2026-11-12 凌晨：余额 ¥17.92。四台并发满负荷跑一天 = ¥206.40。
余额只够 4 小时。这不是理论风险 —— 一次参数填错导致的连环重试就够了。

## 三道闸，管的事各不相同

    ① 日预算   今天已经花了多少 → 超了就停补位（不是停已经在跑的）
    ② 余额线   账户还剩多少 → 低于安全线就不建新机
    ③ 单作业   这一个作业预估多少 → 超了就拒（这条已存在于 run.py）

三道必须都有，因为它们防的是不同的东西：

    · 只有日预算 → 余额剩 ¥3 时照样建机，跑到一半没钱，留下孤儿实例
    · 只有余额线 → 余额 ¥17 时能被四台并发在 4 小时内烧光
    · 只有单作业闸 → 拦不住"十个作业各花 ¥1"

## 阈值怎么定的

    DEFAULT_DAILY_CAP = 8.0    ￥8/天。比历史单日最高花费略高，不误伤正常使用。
                               改自调度器里的同一个数（那里也是 8.0）。
    BALANCE_FLOOR     = 5.0    ￥5。低于这条线时，一次失败作业的钱都可能不够，
                               而且 run.py 自己也有 `bal < 5` 的下限。

两个都可以用环境变量覆盖：`P1_DAILY_CAP` / `P1_BALANCE_FLOOR`。

## 为什么是"停补位"而不是"杀在跑的"

已经在跑的作业杀掉 = 白烧那部分钱（GPU 时间不退）。补位是**入口**，
在入口拦住的成本是零。所以熔断只关入口，让在跑的跑完。

顺带：`run.py` 自己的建机闸（`check_provision` + 余额闸）仍在，双保险。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

LEDGER = _ROOT / ".compshare" / "ledger.jsonl"

DEFAULT_DAILY_CAP = 8.0
BALANCE_FLOOR = 5.0


def daily_cap() -> float:
    """每日花费上限。0 或负数 = 不限制（显式关掉，不是默认）。"""
    raw = os.environ.get("P1_DAILY_CAP")
    if raw is None or raw.strip() == "":
        return DEFAULT_DAILY_CAP
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_DAILY_CAP


def balance_floor() -> float:
    raw = os.environ.get("P1_BALANCE_FLOOR")
    if raw is None or raw.strip() == "":
        return BALANCE_FLOOR
    try:
        return max(0.0, float(raw))
    except ValueError:
        return BALANCE_FLOOR


def today_spent(ledger: Path | None = None) -> float:
    """今天花了多少。口径统一在 spend.py（含 settle 真实扣费）。"""
    try:
        from tools.p2 import spend
        return float(spend.today_cost(Path(ledger) if ledger else LEDGER))
    except Exception:                      # noqa: BLE001
        return 0.0


def reconciled_spent(ledger: Path | None = None, *, creds=None) -> tuple[float, str]:
    """今日花费，**用余额差校正**。返回 (金额, 说明)。

    为什么不能只用账本数：实测 2026-11-12 01:00，账本最后一条 balance 记
    ¥17.92，而平台实时余额已经是 ¥17.37 —— 差 ¥0.55，且这 ¥0.55 在账本里
    **一条记录都没有**（账本是本地采样，settle 靠 `csctl settle` 按周期回填，
    中间那段就是黑洞）。

    如果熔断只读账本，它看到的是 ¥1.20 而真实是 ¥1.75。差异会累积 ——
    采样越稀疏，低估越多，最后"上限 ¥8"实际烧到 ¥12 才熔断。

    校正办法：用「当日首次采样余额 − 当前余额」算真实花了多少，与账本取 max。
      · 两者都在涨，取大的那个（宁可早熔断）
      · 读不到余额或没有当日基线时，退回账本数（有总比没有强）
    """
    spent = today_spent(ledger)
    bal = balance(creds)
    if bal is None:
        return spent, f"账本 ¥{spent:.2f}（读不到余额，未校正）"
    base = _opening_balance(ledger)
    if base is None:
        return spent, f"账本 ¥{spent:.2f}（无当日基线，未校正）"
    by_balance = max(0.0, base - bal)
    if by_balance > spent + 0.01:
        return by_balance, f"余额差 ¥{by_balance:.2f}（账本只记到 ¥{spent:.2f}）"
    return spent, f"账本 ¥{spent:.2f}"


def _opening_balance(ledger: Path | None = None) -> float | None:
    """当日第一条 balance 采样值 —— 用它当"今天开始时有多少钱"。

    取**当天第一条**而不是昨天的最后一条：跨零点时余额可能刚被 settle 扣过，
    昨天的末值已经不含那笔，用它做基线会把这笔算进今天。
    """
    p = Path(ledger) if ledger else LEDGER
    if not p.exists():
        return None
    today = datetime.date.today().isoformat()
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(r, dict) or r.get("event") != "balance":
                continue
            at = str(r.get("at") or "")
            if at[:10] != today:
                continue
            v = r.get("balance")
            if v is not None:
                return float(v)
    except OSError:
        return None
    return None


def balance(creds=None) -> float | None:
    """账户可用余额。读不到返回 None —— **不要当成 0 也不要当成无穷**。"""
    try:
        from compshare.cs import CS
        b = CS(creds).balance() if creds else CS().balance()
        v = b.get("AmountAvailable")
        if v is None:
            v = b.get("Amount")
        return float(v) if v is not None else None
    except Exception:                      # noqa: BLE001
        return None


def check(ledger: Path | None = None, *, creds=None,
          balance_cache: float | None = None) -> tuple[bool, str]:
    """还能不能开新作业。返回 (ok, 人话原因)。

    `balance_cache`：调用方已经查过余额就传进来，省一次 API 调用
    （补位一次可能判多个作业，逐个查余额既慢又可能触发限流）。
    """
    cap = daily_cap()
    if cap > 0:
        # 用余额差校正过的数值 —— 账本采样有黑洞，单看它会低估
        spent, _why = reconciled_spent(ledger, creds=creds)
        if spent >= cap:
            return False, (f"今日已花 ¥{spent:.2f}，达到日预算上限 ¥{cap:.2f}。"
                           f"等明天，或调大 P1_DAILY_CAP 后再跑。")

    floor = balance_floor()
    if floor > 0:
        bal = balance_cache if balance_cache is not None else balance(creds)
        if bal is None:
            # 读不到余额：**放行**。这是最后兜底闸，不是主闸；
            # 网络抖一下就把所有作业堵死，代价比偶尔多花几毛钱大得多。
            # 但要说出来，别让人以为查过了。
            return True, "读不到余额（按放行处理）"
        if bal < floor:
            return False, (f"余额 ¥{bal:.2f} 低于安全线 ¥{floor:.2f}，不建新机。"
                           f"充值后即可继续；已在跑的作业不受影响。")
    return True, ""


def describe(ledger: Path | None = None, *, creds=None) -> str:
    """给人看的一行状态，`autostart --dry-run` 和排障用。"""
    cap = daily_cap()
    spent, spent_note = reconciled_spent(ledger, creds=creds)
    bal = balance(creds)
    ok, why = check(ledger, creds=creds, balance_cache=bal)
    parts = [f"今日已花 ¥{spent:.2f}"
             + (f" / 上限 ¥{cap:.2f}" if cap > 0 else "（未设上限）"),
             f"[{spent_note}]",
             f"余额 " + (f"¥{bal:.2f}" if bal is not None else "读不到")]
    if not ok:
        parts.append(f"⛔ {why}")
    return " · ".join(parts)


if __name__ == "__main__":
    print(describe())
    ok, why = check()
    print(("✅ 可以开新作业" if ok else f"⛔ {why}"))
    raise SystemExit(0 if ok else 1)
