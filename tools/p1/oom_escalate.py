#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OOM 自动升卡重试：显存不够就换一张更大的卡重跑，而不是原地失败。

## 修的是哪个洞

2026-11-12 之前，一条作业撞上显存不足的路径是这样的：

    step_wait 探测到 ComfyUI 进程没了
      → raise RunError("疑似显存不足/崩溃", FAILED_RUN)
      → 保留实例 15 分钟等人工 --resume
      → 人来了，--resume，还是同一张 24G 卡
      → 同样的地方以同样的方式 OOM

**钱花了两遍，结论一遍都没多。** 因为「换卡」这件事在整条链上没有任何入口：
`required_gpu` 是提交时写死的列表，`_pick_gpu` 只按「有没有货」挑，
没有任何代码会读「上次失败是因为显存」这个事实。

## 三道闸，缺一个就会变成烧钱机器

自动重试最大的风险不是不生效，是**无限生效**。所以三样都必须有：

    ① 判定闸   只对**证据确凿**的 OOM 升卡
               `gpu_ladder.classify_oom()` 返回 `certain` 才算。
               进程崩了但日志里没有 OOM 字样 → 不升卡（可能是段错误、
               可能是代码 bug，升到 96G 也一样死，只是贵 4 倍）。

    ② 次数闸   最多升 2 次（24G → 48G → 80/96G，共 3 次尝试）
               96G 还 OOM 就说明问题不在显存 —— 继续升只会更贵地失败。

    ③ 预算闸   升卡后的预估花费必须仍在 `budget.max_cost` 之内，
               且**已花 + 将花** 一起算。只算单次预估会漏掉前面烧掉的钱：
               作业预算 ¥3，第一次烧 ¥1，48G 档要 ¥1.9 → 合计 ¥2.9 还能过；
               但如果第一次烧了 ¥2.2，就该停 —— 不停的话这一单会冲到 ¥4.1。

三道闸任何一道不过 → 当作普通失败处理（保留实例、提示 --resume），
行为和加这个功能之前完全一样。**不改变既有语义，只是多一条出路。**

## 状态记在哪

`job.json` 的 `instance.oom_escalation`，因为它是「这台作业的实例策略」，
不是「这一次运行」的临时变量 —— 升卡决策必须能跨进程（--resume）延续，
否则重跑一次就会回到 24G 卡上，重新踩一遍。

    "oom_escalation": {
      "attempts": [{"gpu": "4090", "vram": 24, "hourly": 2.15, "minutes": 12.3,
                    "cost": 0.44, "why": "..."}],
      "floor_vram": 48,          # 下一次必须 ≥48G；_gpu_candidates 读它
      "last_gpu": "4090_48G"
    }

`floor_vram` 是**跨次运行生效**的那个字段：只要它在，`--resume` 也不会
再选回小卡。要回到小卡必须人工删这一节（或者新建作业）。

## 为什么升卡要销毁旧实例而不是保留

`_keep_instance` 的设计是「留着等续跑」。但 OOM 的情形下那台机器
**已经证明跑不了这个作业**，留着它只有两个结果：占着并发槽位、
按 ¥2.15/h 空转。所以这里显式销毁，并把它记为「已确认无用」。
（用户仍可用 `--no-escalate` 关掉整条路径，那时走原来的保留语义。）
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import state  # noqa: E402
from tools.p1 import store  # noqa: E402
from tools.p2 import gpu_ladder as ladder  # noqa: E402

SCHEMA = "p1.oom_escalation/v1"

# 最多升几次。2 次 = 最多 3 次尝试（原卡 + 两档更大的卡）。
# 定 2 而不是 3：24G → 48G → 80G → 96G 的第四档要 ¥10.75/h，
# 而能落到那里的作业通常不是显存问题。
MAX_ESCALATIONS = 2

# 单次尝试的预估时长，用来算"这一轮大概要花多少"。与
# concurrency._PLAN_MINUTES 同源，但**不 import 它** —— 那边是并发闸的
# 粗估（含 SAFETY=2），这里是预算核算，两个口径混用会让预算莫名收紧一半。
_PLAN_MINUTES = 45



# ------------------------------------------------------------------ 开关
def enabled() -> bool:
    """`P1_OOM_ESCALATE=0` 关闭整条路径（回到"失败即停"的老行为）。"""
    raw = (os.environ.get("P1_OOM_ESCALATE") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def escalate_on_crash() -> bool:
    """「进程崩了但没有 OOM 证据」时要不要也升卡。

    默认 False：这是**赌博**，赌注是一整轮建机费。默认关闭意味着
    「不确定就按老路走」，符合本项目一贯的「不假装知道」。
    """
    raw = (os.environ.get("P1_OOM_ESCALATE_ON_CRASH") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def max_escalations() -> int:
    raw = (os.environ.get("P1_OOM_MAX_ESCALATIONS") or "").strip()
    if not raw:
        return MAX_ESCALATIONS
    try:
        # 上限 5：再多也没有 5 个档位可选，设更大只是让日志变得可疑
        return max(0, min(5, int(raw)))
    except ValueError:
        return MAX_ESCALATIONS


# ------------------------------------------------------------------ 记账
def record(job: dict) -> dict:
    """读出现有的升卡记录（缺省返回空骨架，不返回 None）。"""
    rec = ((job.get("instance") or {}).get("oom_escalation")) or {}
    if not isinstance(rec, dict):
        rec = {}
    rec.setdefault("attempts", [])
    rec.setdefault("floor_vram", 0)
    rec.setdefault("schema", SCHEMA)
    return rec


def floor_vram(job: dict) -> int:
    """下一轮建机必须满足的最小显存。0 = 无要求。"""
    try:
        return int(record(job).get("floor_vram") or 0)
    except (TypeError, ValueError):
        return 0


def attempt_count(job: dict) -> int:
    return len(record(job).get("attempts") or [])


def _hourly_of(job: dict) -> float:
    """本作业那台机器的真实 ¥/h，按可信度依次回落。

    顺序很重要：
      1. `instance.hourly`  —— 建机时 `cs.price()` 的实时返回（最可信）
      2. `steps.provision.hourly` —— `_mark_step` 记下的同一份值
      3. `gpu_ladder.HOURLY` —— 2026-11-12 的静态表（可能过期，只用来兜底）

    不能直接用静态表：智云调价后，按旧价算出的"已花"会偏低，
    预算闸就会在已经超支时放行 —— 而这道闸存在的唯一目的就是拦住那一刻。
    """
    inst = job.get("instance") or {}
    for v in (inst.get("hourly"), inst.get("_hourly"),
              ((job.get("steps") or {}).get("provision") or {}).get("hourly")):
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return ladder.hourly_of(str(inst.get("gpu") or ""))


def spent_so_far(job: dict) -> float:
    """本作业**已经**烧掉的钱（按每次尝试的 hourly × 分钟累计）。

    为什么不查余额：余额被多条作业共享，差额不一定是本作业花的。
    为什么不读 ledger：ledger 是结算后的账，正在跑的这轮还没进去。
    只有「自己记的」是能自证的那一份。
    """
    total = 0.0
    for a in (record(job).get("attempts") or []):
        try:
            total += float(a.get("cost") or 0)
        except (TypeError, ValueError):
            continue
    # 进行中那一轮（还没有 ended_at）：按 up_at 到现在折算
    cur = _live_cost(job)
    return round(total + cur, 4)


def _live_cost(job: dict) -> float:
    hourly = _hourly_of(job)
    if hourly <= 0:
        return 0.0
    return hourly * _live_minutes(job) / 60.0


def _live_minutes(job: dict) -> float:
    inst = job.get("instance") or {}
    up_at = inst.get("up_at")
    if not up_at:
        return 0.0
    try:
        up = dt.datetime.fromisoformat(str(up_at))
    except ValueError:
        return 0.0
    now = dt.datetime.now().astimezone()
    if up.tzinfo is None:
        up = up.replace(tzinfo=now.tzinfo)
    return max(0.0, (now - up).total_seconds() / 60.0)


def _stamp_attempt(job_id: str, *, gpu: str, why: str) -> None:
    """把「当前这一轮」封存成一条历史尝试（写进 job.json）。

    在决定升卡的那一刻调用：此时实例即将销毁，成本已经确定。
    """
    j = state.load_job(job_id)
    rec = record(j)
    inst = j.get("instance") or {}
    mins = round(_live_minutes(j), 1)
    hourly = _hourly_of(j)
    rec["attempts"].append({
        "gpu": gpu or str(inst.get("gpu") or ""),
        "vram": ladder.vram_of(gpu or str(inst.get("gpu") or "")),
        "hourly": round(hourly, 3),
        "minutes": mins,
        "cost": round(hourly * mins / 60.0, 4),
        "why": why[:300],
        "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    rec["last_gpu"] = gpu or str(inst.get("gpu") or "")
    j.setdefault("instance", {})["oom_escalation"] = rec
    store.atomic_write_json(store.job_file(job_id), j)


def _set_floor(job_id: str, floor: int) -> None:
    j = state.load_job(job_id)
    rec = record(j)
    rec["floor_vram"] = int(floor)
    j.setdefault("instance", {})["oom_escalation"] = rec
    store.atomic_write_json(store.job_file(job_id), j)


# ------------------------------------------------------------------ 决策
def plan(job: dict, *, current_gpu: str, oom_text: str, stock: dict[str, int] | None,
         cls: str = "certain") -> dict:
    """升不升、升到哪、为什么。**纯函数**：不建机、不写盘、不改 job。

    返回：
        {"ok": True,  "targets": [(gpu, vram, hourly), ...], "reason": "..."}
        {"ok": False, "targets": [], "reason": "拒绝升卡的理由"}
    """
    if not enabled():
        return {"ok": False, "targets": [], "reason": "升卡已关闭（P1_OOM_ESCALATE=0）"}
    if cls != "certain" and not (cls == "likely" and escalate_on_crash()):
        return {"ok": False, "targets": [],
                "reason": f"证据不足（{cls}）：日志里没有确诊的显存不足，不升卡"}

    n = attempt_count(job)
    cap = max_escalations()
    if n >= cap:
        return {"ok": False, "targets": [],
                "reason": f"已升卡 {n} 次（上限 {cap}），继续升只会更贵地失败"}

    cur = str(current_gpu or "").strip()
    if ladder.vram_of(cur) <= 0:
        return {"ok": False, "targets": [],
                "reason": f"当前卡型 {cur!r} 显存未知，无法判断该升到哪一档"}

    targets = ladder.next_targets(cur, stock)
    if not targets:
        avail = ladder.summarize_stock(stock or {})
        skip = ladder.skipped_note(cur)
        tail = f"；跳过 {skip}" if skip else ""
        return {"ok": False, "targets": [],
                "reason": (f"没有比 {cur}（{ladder.vram_of(cur)}G）更大且有货的卡型"
                           f"（{avail}{tail}）。等有货，或人工降低分辨率/帧数")}

    # 预算闸：已花 + 新一档的预估，必须还在 budget.max_cost 之内
    budget = job.get("budget") or {}
    max_min = float(budget.get("max_minutes") or _PLAN_MINUTES)
    plan_min = min(max_min, float(_PLAN_MINUTES))
    already = spent_so_far(job)
    cap_cost = float(budget.get("max_cost") or 0)

    affordable = []
    rejected = []
    for g, v, h in targets:
        est = h * (plan_min / 60.0)
        if cap_cost > 0 and already + est > cap_cost:
            rejected.append(f"{g}（{v}G ¥{h:.2f}/h 预计 ¥{est:.2f}，"
                            f"已花 ¥{already:.2f} 合计超预算 ¥{cap_cost:.2f}）")
            continue
        affordable.append((g, v, h))
    if not affordable:
        return {"ok": False, "targets": [],
                "reason": ("预算不够升卡：" + "；".join(rejected[:3])
                           + "。等下一单，或调高 budget.max_cost")}

    g, v, h = affordable[0]
    why = (f"显存不足，{ladder.vram_of(cur)}G → {v}G（{g} ¥{h:.2f}/h，"
           f"预计 ¥{h * plan_min / 60.0:.2f}，已花 ¥{already:.2f}）")
    if oom_text:
        why += f" · {oom_text}"
    return {"ok": True, "targets": affordable, "target": (g, v, h),
            "reason": why, "already_spent": already, "est_cost": h * plan_min / 60.0}


def apply(job_id: str, *, current_gpu: str, target: tuple[str, int, float],
          oom_text: str) -> dict:
    """落盘升卡决定：封存本轮、抬显存下限、清断点。返回更新后的 job。

    **不建机**。建机由调用方继续走 `_execute(start_at="provision")` 完成 ——
    这样"升卡"和"首次建机"走完全同一条代码路径，不会出现两套建机逻辑。
    """
    _stamp_attempt(job_id, gpu=current_gpu, why=oom_text)
    g, v, _h = target
    _set_floor(job_id, v)
    j = state.load_job(job_id)
    j["steps"] = {}                       # 断点清空，从 provision 重做
    j.setdefault("instance", {})["up_at"] = None   # 旧的计费起点作废
    store.atomic_write_json(store.job_file(job_id), j)
    return j


def describe(job: dict) -> str:
    """一行状态，给 `jobctl show` / 报告用。"""
    rec = record(job)
    n = len(rec.get("attempts") or [])
    if not n and not rec.get("floor_vram"):
        return "未发生升卡"
    parts = [f"已升卡 {n} 次"]
    for a in rec.get("attempts") or []:
        parts.append(f"{a.get('gpu')}({a.get('vram')}G) {a.get('minutes')}分钟 ¥{a.get('cost')}")
    if rec.get("floor_vram"):
        parts.append(f"下次要求 ≥{rec['floor_vram']}G")
    parts.append(f"累计 ¥{spent_so_far(job):.2f}")
    return " · ".join(parts)


def reset(job_id: str) -> None:
    """人工清除升卡状态（回到默认卡型）。给「我知道这次 OOM 是参数写错了」用。"""
    j = state.load_job(job_id)
    inst = j.get("instance") or {}
    inst.pop("oom_escalation", None)
    j["instance"] = inst
    store.atomic_write_json(store.job_file(job_id), j)

