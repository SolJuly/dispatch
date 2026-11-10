#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 校验器：job.json / manifest.json / tasks.json。

环境没有 jsonschema（P0 审计已记录），这里手写纯标准库校验：
  - 只返回错误清单，不抛异常（调用方决定是打印还是中止）
  - 允许未知字段（前向兼容），只校验契约里声明过的键
  - params_hash 一律重算比对，防手改漏改
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

from . import store
from .state import STATES, PERSISTED, DONE

JOB_SCHEMA = "p1.job/v1"
MANIFEST_SCHEMA = "p1.manifest/v1"
TASKS_SCHEMA = "p1.tasks/v1"

HEX64 = re.compile(r"^[0-9a-f]{64}$")
KINDS = ("video", "image")
# 生命周期枚举。与 tools/p2/lifecycle.py 的 IDLE_TIERS 必须一致 ——
# 但这里**不 import** 它：schema 是纯标准库校验器，不该依赖 P2 模块。
TASK_CLASSES = ("short", "long")
TASK_CLASS_SOURCES = ("user", "estimated", "default")
IDLE_TIERS = (60, 90, 120)
VERDICTS = ("PASS", "FAIL")
TASK_STATUS = ("todo", "doing", "blocked", "done", "dropped")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _iso(v) -> bool:
    if not isinstance(v, str) or not v:
        return False
    try:
        dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------- job.json
def validate_job(d, *, job_id_expected: str | None = None) -> list[str]:
    e: list[str] = []
    if not isinstance(d, dict):
        return ["job.json 不是对象"]

    if d.get("schema") != JOB_SCHEMA:
        e.append(f"schema 应为 {JOB_SCHEMA}，实际 {d.get('schema')!r}")

    from .ids import valid_job_id
    jid = d.get("job_id")
    if not valid_job_id(jid):
        e.append(f"job_id 格式非法：{jid!r}（应为 p1-YYYYMMDDTHHMM-6hex）")
    elif job_id_expected and jid != job_id_expected:
        e.append(f"job_id 与目录名不一致：{jid!r} != {job_id_expected!r}")

    if not _iso(d.get("created_at")):
        e.append(f"created_at 不是合法 ISO 时间：{d.get('created_at')!r}")
    if not isinstance(d.get("branch"), str) or not d.get("branch"):
        e.append("branch 缺失")
    if d.get("kind") not in KINDS:
        e.append(f"kind 应为 {KINDS}，实际 {d.get('kind')!r}")

    model = d.get("model")
    if not isinstance(model, dict):
        e.append("model 缺失或不是对象")
        model = {}
    for k in ("key", "workflow", "checkpoint"):
        if not isinstance(model.get(k), str) or not model.get(k):
            e.append(f"model.{k} 缺失")
    if not HEX64.match(str(model.get("workflow_sha256") or "")):
        e.append("model.workflow_sha256 应为 64 位十六进制")

    params = d.get("params")
    if not isinstance(params, dict) or not params:
        e.append("params 缺失或为空")

    expect = d.get("expect")
    if not isinstance(expect, dict):
        e.append("expect 缺失或不是对象")
        expect = {}
    if not isinstance(expect.get("ext"), str) or not expect.get("ext"):
        e.append("expect.ext 缺失")
    if not _is_int(expect.get("min_bytes")) or expect.get("min_bytes", 0) <= 0:
        e.append("expect.min_bytes 应为正整数")
    dur = expect.get("duration_s")
    if dur is not None and not (isinstance(dur, list) and len(dur) == 2 and all(_is_num(x) for x in dur)):
        e.append("expect.duration_s 应为 [下限, 上限] 或省略")
    br = expect.get("black_ratio_max")
    if br is not None and not (_is_num(br) and 0 <= br <= 1):
        e.append("expect.black_ratio_max 应在 0–1 之间")
    fz = expect.get("freeze")
    if fz is not None:
        if not isinstance(fz, dict):
            e.append("expect.freeze 应为对象")
        else:
            if not _is_num(fz.get("ratio_max")) or not (0 <= fz.get("ratio_max", -1) <= 1):
                e.append("expect.freeze.ratio_max 应在 0–1 之间")
            if not _is_num(fz.get("longest_max_s")) or fz.get("longest_max_s", -1) <= 0:
                e.append("expect.freeze.longest_max_s 应为正数")
    # D3 严格管控：video 且 freeze_policy=strict 必须带 expect.freeze
    if d.get("kind") == "video" and d.get("freeze_policy") == "strict" and fz is None:
        e.append("freeze_policy=strict 但 expect.freeze 缺失（D3 冻结门禁）")

    budget = d.get("budget") or {}
    if not isinstance(budget, dict):
        e.append("budget 不是对象")
        budget = {}
    if not _is_num(budget.get("max_minutes")) or budget.get("max_minutes", 0) <= 0:
        e.append("budget.max_minutes 应为正数")
    if not _is_num(budget.get("max_cost")) or budget.get("max_cost", -1) < 0:
        e.append("budget.max_cost 应为非负数")

    inst = d.get("instance") or {}
    if not isinstance(inst, dict):
        e.append("instance 不是对象")
        inst = {}
    if not isinstance(inst.get("required_gpu"), list) or not inst["required_gpu"]:
        e.append("instance.required_gpu 应为非空列表")

    ph = d.get("params_hash")
    if not store.is_params_hash(ph):
        e.append("params_hash 格式应为 sha256:<64hex>")
    else:
        calc = store.params_hash(model, params, expect)
        if ph != calc:
            e.append(f"params_hash 与参数重算不一致（文件 {ph[:19]}… / 重算 {calc[:19]}…）")

    if d.get("state") not in STATES:
        e.append(f"state 不在枚举内：{d.get('state')!r}")
    if not _is_int(d.get("state_seq")) or d.get("state_seq", 0) < 1:
        e.append("state_seq 应为 ≥1 的整数")

    # ---- 生命周期字段（2026-11-10 定版）----
    # 这三个字段决定"实例跑完留多久"，写错了直接反映在账单上，所以必须校验。
    # 但它们**可以缺省**：定版之前的作业没有这些字段，那些作业按短任务处理
    # （`lifecycle.idle_for_job` 的兜底路径），不该判成非法。
    tc = d.get("task_class")
    if tc is not None and tc not in TASK_CLASSES:
        e.append(f"task_class 应为 {TASK_CLASSES} 之一，实际 {tc!r}")
    src = d.get("task_class_source")
    if src is not None and src not in TASK_CLASS_SOURCES:
        e.append(f"task_class_source 应为 {TASK_CLASS_SOURCES} 之一，实际 {src!r}")
    tier = d.get("idle_tier")
    if tier is not None and tier not in IDLE_TIERS:
        e.append(f"idle_tier 应为 {IDLE_TIERS} 之一，实际 {tier!r}")
    iad = d.get("idle_after_done_s")
    if iad is not None:
        if not _is_int(iad) or iad < 0:
            e.append(f"idle_after_done_s 应为非负整数，实际 {iad!r}")
        elif tc == "short" and iad != 0:
            # 短任务恒 0 —— 这是硬规则，不是"建议值"。短任务给 120s 空转
            # 等于任务成本的 53%~84%，写错了钱就白烧。
            e.append(f"短任务 idle_after_done_s 必须为 0，实际 {iad}")
        elif tc == "long" and iad not in IDLE_TIERS:
            e.append(f"长任务 idle_after_done_s 应为 {IDLE_TIERS} 之一，实际 {iad}")

    return e


# ------------------------------------------------------------ manifest.json
def validate_manifest(d, *, job_id_expected: str | None = None,
                      deep_dir: Path | None = None, deep: bool = False) -> list[str]:
    e: list[str] = []
    if not isinstance(d, dict):
        return ["manifest.json 不是对象"]

    if d.get("schema") != MANIFEST_SCHEMA:
        e.append(f"schema 应为 {MANIFEST_SCHEMA}，实际 {d.get('schema')!r}")
    jid = d.get("job_id")
    if job_id_expected and jid != job_id_expected:
        e.append(f"manifest.job_id 与作业不一致：{jid!r} != {job_id_expected!r}")
    if not store.is_params_hash(d.get("params_hash")):
        e.append("manifest.params_hash 格式非法")
    if not _iso(d.get("persisted_at")):
        e.append("persisted_at 不是合法 ISO 时间")

    inst = d.get("instance") or {}
    if not isinstance(inst, dict):
        e.append("instance 不是对象")
        inst = {}
    if not isinstance(inst.get("uhost_id"), str) or not inst.get("uhost_id"):
        e.append("instance.uhost_id 缺失")
    if not _is_num(inst.get("minutes")) or inst.get("minutes", -1) < 0:
        e.append("instance.minutes 应为非负数")

    arts = d.get("artifacts")
    if not isinstance(arts, list) or not arts:
        e.append("artifacts 应为非空列表")
        arts = []
    for i, a in enumerate(arts):
        tag = f"artifacts[{i}]"
        if not isinstance(a, dict):
            e.append(f"{tag} 不是对象")
            continue
        if not isinstance(a.get("rel"), str) or not a.get("rel"):
            e.append(f"{tag}.rel 缺失")
        if not _is_int(a.get("bytes")) or a.get("bytes", 0) <= 0:
            e.append(f"{tag}.bytes 应为正数")
        if not HEX64.match(str(a.get("sha256") or "")):
            e.append(f"{tag}.sha256 应为 64 位十六进制")
        for k in ("probe", "match"):
            if not isinstance(a.get(k), dict):
                e.append(f"{tag}.{k} 缺失或不是对象")
        if deep and deep_dir and isinstance(a.get("rel"), str):
            fp = deep_dir / a["rel"]
            if not fp.exists():
                e.append(f"{tag} 文件不存在：{store.rel(fp)}")
            else:
                size = fp.stat().st_size
                if _is_int(a.get("bytes")) and size != a["bytes"]:
                    e.append(f"{tag} 体积不符：manifest {a['bytes']} / 实际 {size}")
                if deep:
                    h = hashlib.sha256(fp.read_bytes()).hexdigest()
                    if HEX64.match(str(a.get("sha256") or "")) and h != a["sha256"]:
                        e.append(f"{tag} sha256 不符：manifest {a['sha256'][:12]}… / 实际 {h[:12]}…")

    checks = d.get("checks")
    if not isinstance(checks, dict) or not all(isinstance(v, bool) for v in checks.values()):
        e.append("checks 应为 {名称: bool} 对象")
        checks = {}
    verdict = d.get("verdict")
    if verdict not in VERDICTS:
        e.append(f"verdict 应为 {VERDICTS}，实际 {verdict!r}")
    if verdict == "PASS":
        if checks and not all(checks.values()):
            bad = [k for k, v in checks.items() if not v]
            e.append(f"verdict=PASS 但复验项未全通过：{bad}")
        hist = d.get("state_history")
        if not isinstance(hist, list) or PERSISTED not in hist:
            e.append("verdict=PASS 但 state_history 未包含 PERSISTED")

    cost = d.get("cost") or {}
    if not isinstance(cost, dict) or not isinstance(cost, dict):
        e.append("cost 不是对象")
    else:
        for k in ("hourly", "minutes", "amount"):
            if not _is_num(cost.get(k)):
                e.append(f"cost.{k} 应为数字")

    hist2 = d.get("state_history")
    if not isinstance(hist2, list) or not hist2 or not all(isinstance(x, str) and x in STATES for x in hist2):
        e.append("state_history 应为状态名列表")
    return e


# -------------------------------------------------------------- tasks.json
def find_dep_cycles(tasks: list[dict]) -> list[str]:
    """返回参与成环的 task id（无环返回空）。"""
    graph = {t.get("id"): [d for d in (t.get("depends_on") or []) if isinstance(d, str)]
             for t in tasks if isinstance(t, dict)}
    color: dict[str, int] = {}
    bad: list[str] = []

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = 1
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            if color.get(dep, 0) == 1:
                cycle = stack[stack.index(dep):] + [dep] if dep in stack else [node, dep]
                bad.append(" → ".join(cycle))
            elif color.get(dep, 0) == 0:
                dfs(dep, stack + [dep])
        color[node] = 2

    for n in graph:
        if color.get(n, 0) == 0:
            dfs(n, [n])
    return bad


def validate_tasks(d) -> list[str]:
    e: list[str] = []
    if not isinstance(d, dict):
        return ["tasks.json 不是对象"]
    if d.get("schema") != TASKS_SCHEMA:
        e.append(f"schema 应为 {TASKS_SCHEMA}，实际 {d.get('schema')!r}")
    if not isinstance(d.get("phase"), str) or not d.get("phase"):
        e.append("phase 缺失")
    if not _iso(d.get("updated_at")):
        e.append("updated_at 不是合法 ISO 时间")

    tasks = d.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return e + ["tasks 应为非空列表"]

    from .ids import valid_task_id
    ids: list[str] = []
    for i, t in enumerate(tasks):
        tag = f"tasks[{i}]"
        if not isinstance(t, dict):
            e.append(f"{tag} 不是对象")
            continue
        tid = t.get("id")
        if not valid_task_id(tid):
            e.append(f"{tag}.id 非法：{tid!r}")
        else:
            ids.append(tid)
        if not isinstance(t.get("title"), str) or not t.get("title"):
            e.append(f"{tag}.title 缺失")
        st = t.get("status")
        if st not in TASK_STATUS:
            e.append(f"{tag}.status 非法：{st!r}（应为 {TASK_STATUS}）")
        if not _is_int(t.get("phase_step")) or not (1 <= t.get("phase_step", 0) <= 4):
            e.append(f"{tag}.phase_step 应为 1–4")
        deps = t.get("depends_on")
        if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
            e.append(f"{tag}.depends_on 应为字符串列表")
        for k in ("deliverables", "acceptance", "blockers", "cases", "rounds", "evidence"):
            if not isinstance(t.get(k), list):
                e.append(f"{tag}.{k} 应为列表")
        if st == "blocked" and not (t.get("blockers") or []):
            e.append(f"{tag} 状态 blocked 但 blockers 为空（卡住了必须写清卡在哪）")
        if not _is_num(t.get("budget_cny")) or t.get("budget_cny", -1) < 0:
            e.append(f"{tag}.budget_cny 应为非负数")

    dup = {x for x in ids if ids.count(x) > 1}
    if dup:
        e.append(f"任务 id 重复：{sorted(dup)}")
    known = set(ids)
    for t in tasks:
        for dep in (t.get("depends_on") or []):
            if dep not in known:
                e.append(f"{t.get('id')} 依赖了不存在的任务：{dep}")
    for cyc in find_dep_cycles(tasks):
        e.append(f"依赖成环：{cyc}")
    return e


def validate_all(*, deep: bool = False, job_id: str | None = None) -> dict[str, list[str]]:
    """一次性校验 tasks + 全部（或指定）job。返回 {对象: 错误列表}。"""
    out: dict[str, list[str]] = {}

    t = store.read_json(store.TASKS_JSON)
    out["tasks.json"] = validate_tasks(t) if t is not None else ["文件不存在或无法解析"]

    jobs = [job_id] if job_id else store.list_jobs()
    for jid in jobs:
        jdir = store.job_dir(jid)
        j = store.read_json(jdir / "job.json")
        errs = validate_job(j, job_id_expected=jid) if j else ["job.json 不存在或无法解析"]

        # events 与 state_seq 对齐
        if j:
            evs = []
            try:
                with open(jdir / "events.jsonl", encoding="utf-8") as f:
                    evs = [ln for ln in f.read().splitlines() if ln.strip()]
            except FileNotFoundError:
                pass
            if not evs:
                errs.append("events.jsonl 缺失或为空")
            else:
                import json as _json
                last = _json.loads(evs[-1])
                if last.get("seq") != j.get("state_seq"):
                    errs.append(f"events 最后 seq={last.get('seq')} 与 state_seq={j.get('state_seq')} 不一致")
                if last.get("to") != j.get("state"):
                    errs.append(f"events 最后 to={last.get('to')!r} 与 state={j.get('state')!r} 不一致")
                if len(evs) != j.get("state_seq"):
                    errs.append(f"events 行数 {len(evs)} 与 state_seq={j.get('state_seq')} 不匹配")

        m = store.read_json(jdir / "manifest.json")
        if m is not None:
            errs += validate_manifest(m, job_id_expected=jid, deep_dir=jdir, deep=deep)
        elif j and j.get("state") in (PERSISTED, DONE):
            errs.append(f"state={j.get('state')} 但 manifest.json 缺失")
        out[jid] = errs
    return out
