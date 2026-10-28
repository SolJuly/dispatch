#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 任务清单：tasks.json 读写、依赖拓扑、下一步计算、Markdown 渲染。

清单是**人工规划的账本**，不是执行记录：
  - 15 条任务，状态 todo/doing/blocked/done/dropped
  - depends_on 必须无环（校验在 schema.validate_tasks）
  - next() 只认「依赖全部 done 且自身 todo」的任务，避免人工拍脑袋排序
"""
from __future__ import annotations

import datetime as dt

from . import store
from .schema import TASKS_SCHEMA

STATUS_ICON = {"done": "✅", "doing": "🔧", "blocked": "⛔", "todo": "⬜", "dropped": "🚫"}
STATUS_CN = {"done": "完成", "doing": "进行中", "blocked": "阻塞", "todo": "待做", "dropped": "已放弃"}


def load() -> dict:
    d = store.read_json(store.TASKS_JSON)
    if d is None:
        raise FileNotFoundError(f"任务清单不存在：{store.rel(store.TASKS_JSON)}")
    return d


def save(d: dict) -> None:
    d["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    store.atomic_write_json(store.TASKS_JSON, d)


def by_id(d: dict, tid: str) -> dict:
    for t in d.get("tasks", []):
        if t.get("id") == tid:
            return t
    raise KeyError(f"任务不存在：{tid}")


def update(d: dict, tid: str, **fields) -> dict:
    t = by_id(d, tid)
    t.update(fields)
    t.setdefault("rounds", [])
    return t


def add_round(d: dict, tid: str, note: str, *, round_no: int | None = None,
              cost: float = 0.0) -> None:
    t = by_id(d, tid)
    t.setdefault("rounds", []).append({
        "round": round_no if round_no is not None else len(t["rounds"]) + 1,
        "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": note,
        "cost": cost,
    })


def next_task(d: dict) -> dict | None:
    """依赖已全部完成、自身仍是 todo 的最小 phase_step 任务。"""
    done = {t["id"] for t in d.get("tasks", []) if t.get("status") == "done"}
    dropped = {t["id"] for t in d.get("tasks", []) if t.get("status") == "dropped"}
    cands = [
        t for t in d.get("tasks", [])
        if t.get("status") == "todo"
        and all(dep in done or dep in dropped for dep in (t.get("depends_on") or []))
    ]
    if not cands:
        return None
    return sorted(cands, key=lambda t: (t.get("phase_step", 9), t.get("id", "")))[0]


def progress(d: dict) -> dict:
    tasks = d.get("tasks", [])
    cnt = {k: 0 for k in STATUS_CN}
    for t in tasks:
        cnt[t.get("status", "todo")] = cnt.get(t.get("status", "todo"), 0) + 1
    tot = len(tasks)
    return {
        "total": tot,
        "done": cnt.get("done", 0),
        "doing": cnt.get("doing", 0),
        "blocked": cnt.get("blocked", 0),
        "todo": cnt.get("todo", 0),
        "dropped": cnt.get("dropped", 0),
        "pct": round(100.0 * cnt.get("done", 0) / tot, 1) if tot else 0.0,
    }


def steps_view(d: dict) -> list[dict]:
    """按 phase_step 聚合成架构图 H 区要的 4 步视图。"""
    meta = {
        1: ("底座：job 定义 + 状态机", "schema / 转移表 / 守卫"),
        2: ("校验 & 持久化流程", "ffprobe / 参数回读 / 串行释放"),
        3: ("模型问题（Wan / LTXV / 图片）", "黑帧复验 / 首跑 / 存储"),
        4: ("异常专项测试", "TC-01…TC-12"),
    }
    out = []
    for step in (1, 2, 3, 4):
        ts = [t for t in d.get("tasks", []) if t.get("phase_step") == step]
        done = [t for t in ts if t.get("status") == "done"]
        doing = [t for t in ts if t.get("status") == "doing"]
        blocked = [t for t in ts if t.get("status") == "blocked"]
        pct = round(100.0 * len(done) / len(ts)) if ts else 0
        if blocked:
            st = "fail"
        elif doing:
            st = "doing"
        elif ts and len(done) == len(ts):
            st = "done"
        else:
            st = "todo"
        title, note = meta[step]
        cur = (blocked or doing or [None])[0]
        out.append({
            "id": str(step),
            "title": title,
            "st": st,
            "pct": pct,
            "note": f"{len(done)}/{len(ts)} 条" + (f" · {cur['id']}" if cur else f" · {note}"),
        })
    return out


def blocked_items(d: dict) -> list[str]:
    """汇总阻塞：任务自带的 blockers + 全局 blockers。"""
    out: list[str] = []
    for t in d.get("tasks", []):
        if t.get("status") == "blocked":
            for b in t.get("blockers") or []:
                out.append(f"{t['id']} {b}")
    for b in d.get("global_blockers") or []:
        out.append(b)
    return out


def render_md(d: dict) -> str:
    p = progress(d)
    lines = [
        "# P1 任务清单（由 tools/p1/tasks.py 渲染，勿手改）",
        "",
        f"> 阶段 {d.get('phase')} · 更新 {d.get('updated_at')} · "
        f"完成 {p['done']}/{p['total']}（{p['pct']}%）· "
        f"进行中 {p['doing']} · 阻塞 {p['blocked']}",
        "",
        "| ID | 任务 | 状态 | 步骤 | 依赖 | 预算 | 验收 |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in d.get("tasks", []):
        deps = ", ".join(t.get("depends_on") or []) or "—"
        acc = "；".join(t.get("acceptance") or []) or "—"
        lines.append(
            f"| `{t['id']}` | {t.get('title', '')} | {STATUS_ICON.get(t.get('status'), '')}"
            f" {STATUS_CN.get(t.get('status'), t.get('status'))} | {t.get('phase_step')} | {deps} "
            f"| ¥{t.get('budget_cny', 0):g} | {acc} |"
        )

    nxt = next_task(d)
    lines += ["", f"**下一步**：`{nxt['id']} {nxt['title']}`" if nxt else "**下一步**：无（全部完成或均被阻塞）", ""]

    bl = blocked_items(d)
    if bl:
        lines += ["## 阻塞", ""] + [f"- {b}" for b in bl] + [""]

    rounds = [(t["id"], r) for t in d.get("tasks", []) for r in (t.get("rounds") or [])]
    if rounds:
        lines += ["## 轮次记录", "", "| 任务 | 轮 | 时间 | 备注 | 成本 |", "|---|---|---|---|---|"]
        for tid, r in rounds[-12:]:
            lines.append(f"| {tid} | {r.get('round')} | {r.get('at', '')} | {r.get('note', '')} "
                         f"| ¥{r.get('cost', 0):g} |")
        lines.append("")
    return "\n".join(lines)


def write_md(d: dict) -> None:
    store.TASKS_MD.parent.mkdir(parents=True, exist_ok=True)
    store.TASKS_MD.write_text(render_md(d), encoding="utf-8")
