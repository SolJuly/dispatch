#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例生命周期策略：短任务不空转 / 长任务三档空转 / 停机保留 2 小时。

## 一句话规则

    短任务（默认）  跑完立即回收          空转 0 秒
    长任务（UI 勾）  跑完空转 N 秒等下一单  N ∈ {60, 90, 120}，默认 90
    空转超时        停机（¥0，可唤醒）
    停机超 2 小时    销毁（腾配额）

## 为什么短任务恒 0，不是"小一点的窗口"

短任务本身只花 ¥0.085–0.139。给 120 秒窗口 = ¥0.0717 = **任务成本的 53%~84%**。
这不是"多一点"，是翻倍。所以短任务的窗口写死 0，连环境变量都不给 ——
给了就会出现 `P2_SHORT_IDLE_S=30` 这种配置，而 30 秒已经是短任务成本的 35%。

## 三档的经济含义（不是"随便选一个"）

    60s   空转 ¥0.0358  vs  复用净省 ¥0.0364   →  净 +¥0.0006  刚好打平
    90s   空转 ¥0.0537  vs  ¥0.0364            →  净 −¥0.0173   均衡（默认）
    120s  空转 ¥0.0717  vs  ¥0.0364            →  净 −¥0.0352   体验优先

复用净省 61 秒的来历（23 条实测）：建机 59s + setup 47s（烘焙快路径只花 5s）
− ComfyUI 重启 40s = 61s ≈ ¥0.0364。空转单价 ¥2.15/h = ¥0.000597/s。

**为什么写死元组、不开环境变量**：三档每一档都有明确的经济含义，
用户能理解"我在为什么付费"。开放任意值就会出现 `P2_LONG_IDLE_S=300`
——300 秒空转 = ¥0.179，占一条 600 秒长任务成本的 42%，而配置的人不会知道。

## 为什么不能按工作流判定长/短

实测同一个 `h3_t2v_121f` 跑过 273s / 516s / 641s；`video` 类里有 33–41 秒的作业，
`image` 类里有 88 秒的作业；单帧耗时跨度 28 倍。**工作流不决定时长，参数决定。**

判定优先级：

    ① 用户在 UI 勾「长任务」        → 显式意图，最高优先级
    ② 估算 > 10 分钟               → 需要模型 perf 数据，49 张模型卡目前零声明，暂时做不到
    ③ 都没有                       → 按短任务（省钱方向，判错代价小）

**默认短任务**的理由：短任务不空转是"省钱"方向，判错只是少留一会儿；
反过来默认长任务会让所有作业都空转，那是纯浪费。
"""
from __future__ import annotations

import os
from pathlib import Path

# 只允许这三个值。见模块注释"为什么写死元组、不开环境变量"。
IDLE_TIERS: tuple[int, ...] = (60, 90, 120)

# 默认档：90 秒。亏 ¥0.017 换"看一眼结果 → 改参数 → 提交"的从容。
DEFAULT_LONG_IDLE_S = 90

# 短任务空转窗口。恒 0，不可配。
SHORT_IDLE_S = 0

TASK_CLASSES = ("short", "long")
DEFAULT_TASK_CLASS = "short"

# 停机保留：2 小时。停机期间 ¥0（实测 11.5 小时 14 次采样全部 +0.00），
# 只占配额（账户 5 台上限）。2 小时够"看一眼结果 / 出去吃个饭再回来接着跑"。
DEFAULT_STOPPED_TTL_MIN = 120


def stopped_ttl_min() -> int:
    """停机保留多少分钟。非法值回落默认，不返回 0 —— 0 会让刚停机的实例立刻被销毁。"""
    raw = os.environ.get("P2_STOPPED_TTL_MIN")
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    return DEFAULT_STOPPED_TTL_MIN


def long_idle_s(task_class: str, chosen: int | None = None) -> int:
    """作业跑完后的空转秒数。

    短任务恒 0；长任务取 `chosen`（必须落在 `IDLE_TIERS` 里），否则回落默认 90。

    `chosen` 不在三档内就回落，**不做"任意值"支持**：三档每一个都有明确经济含义，
    任意值会让用户填出"300 秒"这种占长任务成本 42% 的数而不自知。
    静默回落而不是抛错，是因为这个值可能来自旧版 UI / 手改的 job.json ——
    为一个展示性参数让整条作业失败不值得。
    """
    if str(task_class or DEFAULT_TASK_CLASS).strip().lower() != "long":
        return SHORT_IDLE_S
    if chosen in IDLE_TIERS:
        return int(chosen)                     # type: ignore[arg-type]
    return DEFAULT_LONG_IDLE_S


def normalize_task_class(value) -> tuple[str, str]:
    """把外部输入归一成 (task_class, source)。

    · 用户明确给了 "short"/"long"          → ("...", "user")
    · 空 / 未知 / 非法                      → ("short", "default")

    返回 source 是为了以后估算上线时能对比"用户标的"和"估出来的"差多少 ——
    这个字段现在没用，但补数据要趁早，事后补不回来。
    """
    v = str(value or "").strip().lower()
    if v in TASK_CLASSES:
        return v, "user"
    return DEFAULT_TASK_CLASS, "default"


def normalize_tier(value, *, task_class: str = "long") -> int | None:
    """把外部输入归一成合法档位。短任务恒 None（没有档位可言）。"""
    if str(task_class or "").strip().lower() != "long":
        return None
    try:
        n = int(value)                          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n in IDLE_TIERS else None


def idle_for_job(job: dict) -> int:
    """从 job.json 算这条作业跑完该空转多久。

    优先用落盘时算好的 `idle_after_done_s`（审计用，能看到当时实算的值）；
    没有就按 `task_class` + `idle_tier` 现算（旧作业 / 手改过的 job.json 走这条）。
    """
    raw = job.get("idle_after_done_s")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        # 落盘的 0 只可能是短任务 —— 长任务不可能落 0（long_idle_s 最小 60）
        if raw == 0:
            return 0
        return raw if raw in IDLE_TIERS else DEFAULT_LONG_IDLE_S
    return long_idle_s(str(job.get("task_class") or DEFAULT_TASK_CLASS),
                       job.get("idle_tier"))


def tier_label(seconds: int | None) -> str:
    """档位的中文名，给 CLI / UI 用。"""
    if not seconds:
        return "不空转（短任务）"
    return {60: "60 秒 · 省钱最优",
            90: "90 秒 · 均衡（推荐，默认）",
            120: "120 秒 · 体验优先"}.get(int(seconds), f"{int(seconds)} 秒")


def tier_note(seconds: int | None) -> str:
    """每档的一行说明。UI 直接渲染，别在 JS 里再写一遍数字（会漂移）。"""
    if not seconds:
        return "短任务跑完立即回收，不为空转付费。"
    notes = {
        60: "空转约 ¥0.036，刚好等于「停机再唤醒」能省下的钱 —— 数学上打平。",
        90: "多花约 ¥0.018，换更从容的调整时间（默认档）。",
        120: "多花约 ¥0.035（占一次长任务的 8%），换「不用重新等 113 秒建机」。",
    }
    return notes.get(int(seconds), "")


def describe(job: dict) -> dict:
    """给 UI / CLI / 日志的完整描述，一处算清所有派生值。"""
    tc = str(job.get("task_class") or DEFAULT_TASK_CLASS)
    idle = idle_for_job(job)
    return {
        "task_class": tc,
        "task_class_source": str(job.get("task_class_source") or "default"),
        "idle_after_done_s": idle,
        "tier_label": tier_label(idle),
        "tier_note": tier_note(idle),
        "stopped_ttl_min": stopped_ttl_min(),
        "stopped_ttl_label": f"停机保留 {stopped_ttl_min() // 60} 小时",
    }


# ---------------------------------------------------------------- 停机计时器
# 「停机超过 2 小时就销毁」需要一个「什么时候停的」记录。云端 Describe 不返回停机
# 时间戳（实测该接口只有 StartTime，且停机后仍停在最后一次开机时间），所以必须
# 自己落盘。三个写入点：run.py 释放、reaper 关机、pool 归还。
#
# 为什么不从账本的 `stop` 事件推：账本只有 3 条 stop（csctl stop 才写），
# 而真正大量发生的是 run.py 的 terminate。账本补不齐，落盘文件才可靠。
STOPPED_FILE_NAME = "stopped_at.json"

# 模块级路径，测试可改（同 instance_slots.SLOTS_JSON 的做法），
# 免得单测往真实运行态文件里写东西。
STOPPED_JSON = (Path(__file__).resolve().parents[2]
                / "reports" / "p2" / STOPPED_FILE_NAME)

# 账本路径**独立**，不能从 STOPPED_JSON 推导：单测会把 STOPPED_JSON 指到临时目录，
# 那样账本就永远找不到，兜底逻辑在测试里静默失效（实测踩到）。
LEDGER_JSON = (Path(__file__).resolve().parents[2]
               / ".compshare" / "ledger.jsonl")

# 失败作业保留多少分钟：`run.py._keep_instance` 设的是同值定时关机。
# 两处必须一致 —— reaper 早于它关机器会让「可续跑」的窗口悄悄缩短。
FAILED_KEEP_MIN = 15


def _stopped_path():
    return STOPPED_JSON


def _load_stopped() -> dict:
    import json
    p = _stopped_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def record_stopped(uid: str, ts: float | None = None) -> None:
    """记下「这台实例是什么时候停的」。幂等：重复记录会刷新为最新一次。

    **必须刷新而不是保留第一次**：一台实例可以跑完 A 停机、被唤醒跑 B 又停机。
    保留第一次的话，B 跑完立刻就会因为"停机超过 2 小时"被销毁。
    这正是「停机计时从最后一次停机算」那条边界。
    """
    import json
    import time
    if not uid:
        return
    p = _stopped_path()
    d = _load_stopped()
    d[str(uid)] = float(ts if ts is not None else time.time())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def clear_stopped(uid: str) -> None:
    """实例被唤醒/销毁后清掉计时器（否则残留记录会误伤同名新实例）。"""
    import json
    if not uid:
        return
    p = _stopped_path()
    d = _load_stopped()
    if d.pop(str(uid), None) is None:
        return
    try:
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def stopped_since(uid: str, *, ledger_fallback: bool = True) -> float:
    """这台实例的停机起点（epoch 秒）。没有记录返回 0（= 不知道，别销毁）。

    `ledger_fallback`：本地没记录时回查账本的 `stop` / `terminate` 事件。

    为什么需要兜底（2026-11-10 实测踩到）：`stopped_at.json` 是这一版才有的文件，
    定版之前停掉的实例一条记录都没有。按"无记录就不销毁"处理，它们会**永远占着
    配额**（账户 5 台上限），新任务报 8903。实测就有一台停了 16.4 小时的。

    兜底取**最后一次** stop/terminate 的时间戳：同一台实例可能反复开关，
    保留期要按最近一次算。账本里有就足以判断，不需要更准。
    """
    if not uid:
        return 0.0
    v = _load_stopped().get(str(uid))
    try:
        ts = float(v or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts or not ledger_fallback:
        return ts
    return _ledger_stop_ts(uid)


def _ledger_stop_ts(uid: str) -> float:
    """从账本里找这台实例最后一次停机的时刻。找不到返回 0。"""
    import json
    try:
        lines = LEDGER_JSON.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0.0
    last = 0.0
    for line in lines:
        if uid not in line:                     # 快筛：绝大多数行不是这台机器
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("uhost") != uid:
            continue
        if e.get("event") in ("stop", "terminate"):
            try:
                last = max(last, float(e.get("ts") or 0))
            except (TypeError, ValueError):
                pass
    return last


def stopped_remaining_min(uid: str, now: float | None = None) -> float | None:
    """停机保留期还剩几分钟。

    · 正数  还在保留期内
    · ≤ 0   已过期（该销毁）
    · None  没有停机记录 → **不可判**，不能销毁

    以前这里用 -1 同时表示"不可判"和"已过期（恰好剩 -1 分钟）"，两个语义撞了：
    停机 121 分钟的实例算出来是 -1.0，被当成"没有记录"，于是永远不销毁，
    5 台配额被停机实例吃满、新任务报 8903。改成 None 才分得开。
    """
    import time
    since = stopped_since(uid)
    if not since:
        return None
    now = now if now is not None else time.time()
    return round((since + stopped_ttl_min() * 60 - now) / 60, 1)
