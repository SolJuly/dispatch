#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 状态汇总：把 tasks.json + 最新 job + 账本聚合成 reports/p1/status.json。

架构图 H 区（tools/arch.py 的 p1_section）只读这一个文件，
所以「进展是否实时可见」取决于这里写得多诚实。
"""
from __future__ import annotations

import datetime as dt
import json

from . import store, tasks as tasks_mod
from .state import MAIN_CHAIN, TERMINAL, DONE, read_events

FAILED_STATES = {"FAILED", "ABORTED", "FAILED_PROVISION", "FAILED_RUN",
                 "FAILED_FETCH", "FAILED_VERIFY"}

# 键名必须与 verify.py 的 checks 一一对应：
#   decode / geometry / frames / size / black / params
CHECKS = [
    ("ffprobe 解码", "decode"),
    ("尺寸匹配", "geometry"),
    ("帧数 / 时长", "frames"),
    ("产物体积", "size"),
    ("黑帧占比", "black"),
    ("参数回读匹配", "params"),
]


P1_START = "2026-10-27 21:00"   # P1 首次建机时间，之前的花费属 P0


def _p1_total_from_ledger() -> float:
    """P1 期间的实际花费：读账本按起始时间过滤。

    不用 rounds 累加——那里有估算和跨轮重复（同一批建机被多次记录）。
    """
    total = 0.0
    try:
        with open(store.LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(r.get("at", "")) >= P1_START and r.get("cost"):
                    total += float(r["cost"])
    except FileNotFoundError:
        return 0.0
    return round(total, 2)


def _manifest_cost() -> float:
    """所有作业的 manifest 成本之和——比账本更可靠（账本只在成功路径写）。"""
    total = 0.0
    for jid in store.list_jobs():
        m = store.read_json(store.job_file(jid, "manifest.json"))
        if m:
            total += float((m.get("cost") or {}).get("amount") or 0)
    return round(total, 3)


def _ledger_cost(job_id: str | None) -> float:
    """从账本里累计该 job 的钱。"""
    if not job_id:
        return 0.0
    total = 0.0
    try:
        with open(store.LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    r = __import__("json").loads(line)
                except Exception:
                    continue
                if r.get("job") == job_id and r.get("cost"):
                    total += float(r["cost"])
    except FileNotFoundError:
        return 0.0
    return round(total, 3)


def _current_job() -> dict | None:
    """最近一个有 job.json 的作业（按字典序≈时间序取最后一个）。"""
    jobs = store.list_jobs()
    if not jobs:
        return None
    jid = jobs[-1]
    j = store.read_json(store.job_file(jid))
    if not j:
        return None
    j["_jid"] = jid
    return j


def _states_view(job: dict | None, events: list[dict]) -> list[dict]:
    reached = [e.get("to") for e in events if e.get("to")]
    cur = (job or {}).get("state")
    out = []
    for s in MAIN_CHAIN:
        if s in reached and s != cur:
            st = "done"
        elif s == cur:
            st = "done" if s in TERMINAL else "doing"
        elif s in reached:
            st = "done"
        else:
            st = "todo"
        out.append({"k": s, "label": _label(s), "st": st,
                    "at": _first_at(events, s)})
    return out


_LABELS = {
    "CREATED": "job落盘", "PROVISIONING": "建机", "READY": "就绪探活",
    "SUBMITTED": "提交任务", "RUNNING": "推理中", "FETCHING": "拉产物",
    "VERIFYING": "校验", "PERSISTED": "落盘", "RELEASING": "销毁", "DONE": "完成",
}


def _label(s: str) -> str:
    return _LABELS.get(s, s)


def _first_at(events: list[dict], s: str) -> str | None:
    for e in events:
        if e.get("to") == s:
            return e.get("at")
    return None


def _checks_view(job: dict | None) -> list[dict]:
    if not job:
        return [{"name": n, "st": "todo"} for n, _ in CHECKS]
    # 有 manifest 才谈得上复验结论
    man = store.read_json(store.job_file(job["_jid"], "manifest.json")) if job.get("state") else None
    out = []
    for name, key in CHECKS:
        if man and isinstance(man.get("checks"), dict) and key in man["checks"]:
            out.append({"name": name, "st": "done" if man["checks"][key] else "fail"})
        else:
            out.append({"name": name, "st": "todo"})
    return out


def build() -> dict:
    t = store.read_json(store.TASKS_JSON) or {"phase": "P1", "tasks": []}
    p = tasks_mod.progress(t)
    nxt = tasks_mod.next_task(t)
    job = _current_job()
    events = read_events(job["_jid"]) if job else []
    jid = (job or {}).get("_jid")
    state = (job or {}).get("state", "IDLE")

    cost_spent = _manifest_cost()          # 当前作业成本
    p1_total = _p1_total_from_ledger()
    budget = sum(float(x.get("budget_cny") or 0) for x in t.get("tasks", []))

    all_cases: set[str] = set()
    for x in t.get("tasks", []):
        all_cases.update(x.get("cases") or [])
    cr = t.get("cases_result") or {}
    cases = {"total": int(cr.get("total") or len(all_cases)),
             "defined": len(all_cases),
             "passed": int(cr.get("passed") or 0),
             "skipped": int(cr.get("skipped") or 0),
             "failed": int(cr.get("failed") or 0)}
    tasks_now = [x for x in t.get("tasks", []) if x.get("status") == "doing"]

    return {
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "branch": _branch(),
        "mode": "single-task",
        "scope_note": "只做单任务最小闭环，不做并发批量",
        "job": {
            "job_id": jid or "—",
            "state": state,
            "instance": ((job or {}).get("instance") or {}).get("uhost_id"),
            "gpu": (((job or {}).get("instance") or {}).get("gpu")
                    or ((((job or {}).get("instance") or {}).get("required_gpu") or [None])[0])),
        },
        "tasks": {
            **p,
            "current": (tasks_now[0]["id"] if tasks_now else (nxt["id"] if nxt else None)),
            "next": nxt["id"] if nxt else None,
        },
        "states": _states_view(job, events),
        "steps": tasks_mod.steps_view(t),
        "checks": _checks_view(job),
        "blockers": tasks_mod.blocked_items(t)[:6],
        "cases": cases,
        "cost": {"spent": cost_spent, "p1_total": p1_total, "budget": round(budget, 2)},
    }


def _branch() -> str:
    head = store.ROOT / ".git" / "HEAD"
    try:
        h = head.read_text(encoding="utf-8").strip()
        if h.startswith("ref:"):
            return h.split("/")[-1]
        return h[:8]
    except OSError:
        return "unknown"


def write() -> dict:
    d = build()
    store.atomic_write_json(store.STATUS_JSON, d)
    return d
