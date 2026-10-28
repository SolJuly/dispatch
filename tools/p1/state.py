#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 状态机：15 态、转移白名单、守卫 G1–G5、CAS 落盘、events 追加。

设计见 reports/p1/PLAN-第一步-底座.md §5。

三条不能破的：
  G1  非 PERSISTED 不得进入 RELEASING（除 FAILED_* 已归档）——产物不随实例消失
  G2  DONE 必须 manifest.verdict == PASS
  G3  所有转移 CAS：读 state_seq → 校验 → +1 写回
"""
from __future__ import annotations

import datetime as dt
import json

from . import store

# ---- 状态枚举 -------------------------------------------------------------
CREATED = "CREATED"
PROVISIONING = "PROVISIONING"
READY = "READY"
SUBMITTED = "SUBMITTED"
RUNNING = "RUNNING"
FETCHING = "FETCHING"
VERIFYING = "VERIFYING"
PERSISTED = "PERSISTED"
RELEASING = "RELEASING"
DONE = "DONE"

FAILED_PROVISION = "FAILED_PROVISION"
FAILED_RUN = "FAILED_RUN"
FAILED_FETCH = "FAILED_FETCH"
FAILED_VERIFY = "FAILED_VERIFY"
FAILED = "FAILED"
ABORTED = "ABORTED"

MAIN_CHAIN = [CREATED, PROVISIONING, READY, SUBMITTED, RUNNING,
              FETCHING, VERIFYING, PERSISTED, RELEASING, DONE]
FAIL_BRANCH = [FAILED_PROVISION, FAILED_RUN, FAILED_FETCH, FAILED_VERIFY]
STATES = MAIN_CHAIN + FAIL_BRANCH + [FAILED, ABORTED]

TERMINAL = {DONE, FAILED, ABORTED}

# ---- 转移白名单 -----------------------------------------------------------
TRANSITIONS: dict[str, set[str]] = {
    CREATED: {PROVISIONING, ABORTED},
    PROVISIONING: {READY, FAILED_PROVISION},
    READY: {SUBMITTED, FAILED_PROVISION},
    SUBMITTED: {RUNNING, FAILED_RUN},
    RUNNING: {FETCHING, FAILED_RUN},
    FETCHING: {VERIFYING, FAILED_FETCH},
    VERIFYING: {PERSISTED, FAILED_VERIFY},
    PERSISTED: {RELEASING},
    RELEASING: {DONE, FAILED},
    DONE: set(),
    FAILED: set(),
    ABORTED: set(),
}
for _f in FAIL_BRANCH:
    TRANSITIONS[_f] = {RELEASING}


class IllegalTransition(Exception):
    """非法转移：越级、跳步、终态再动。"""


class GuardViolation(Exception):
    """合法边但守卫不通过（G1/G2）。"""


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def check_transition(src: str, dst: str, job: dict, manifest: dict | None = None) -> None:
    """转移前的全部检查（白名单 + G1 + G2）。不通过则抛异常。"""
    if src not in STATES:
        raise IllegalTransition(f"未知状态 {src!r}")
    if dst not in STATES:
        raise IllegalTransition(f"未知目标状态 {dst!r}")
    if not can_transition(src, dst):
        raise IllegalTransition(f"非法转移 {src} → {dst}（允许：{sorted(TRANSITIONS.get(src, [])) or '无，终态'}）")

    # G1：没落盘不许销毁
    if dst == RELEASING and src != PERSISTED and src not in FAIL_BRANCH:
        raise GuardViolation(
            f"G1 拦截：{src} → RELEASING 不被允许。产物必须先 PERSISTED，"
            f"失败也必须走 FAILED_* 并留档。"
        )

    # G2：完成必须校验通过
    if dst == DONE:
        if not manifest:
            raise GuardViolation("G2 拦截：DONE 需要 manifest.json，当前缺失。")
        if manifest.get("job_id") != job.get("job_id"):
            raise GuardViolation("G2 拦截：manifest.job_id 与 job.job_id 不一致。")
        if manifest.get("verdict") != "PASS":
            raise GuardViolation(f"G2 拦截：manifest.verdict={manifest.get('verdict')!r}，非 PASS。")


# ---- 落盘与事件 -----------------------------------------------------------
def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _append_event(jdir, seq: int, src: str | None, dst: str, reason: str, evidence) -> None:
    rec = {"seq": seq, "at": _now(), "from": src, "to": dst, "reason": reason}
    if evidence is not None:
        rec["evidence"] = evidence
    with open(jdir / "events.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


def init_job(job: dict, *, reason: str = "作业创建") -> dict:
    """写 job.json 与第一条事件（from=None → CREATED）。"""
    jid = job["job_id"]
    jdir = store.job_dir(jid)
    job["state"] = CREATED
    job["state_seq"] = 1
    store.atomic_write_json(jdir / "job.json", job)
    _append_event(jdir, 1, None, CREATED, reason, None)
    return job


def load_job(job_id: str) -> dict:
    d = store.read_json(store.job_file(job_id))
    if d is None:
        raise FileNotFoundError(f"作业不存在：{job_id}")
    return d


def load_manifest(job_id: str) -> dict | None:
    return store.read_json(store.job_file(job_id, "manifest.json"))


def transition(job_id: str, dst: str, *, reason: str = "", evidence=None) -> dict:
    """执行一次状态转移（CAS）。

    返回转移后的 job。非法转移抛 IllegalTransition，守卫不过抛 GuardViolation。
    """
    jdir = store.job_dir(job_id)
    job = load_job(job_id)
    src = job.get("state")
    manifest = load_manifest(job_id) if dst == DONE else None
    check_transition(src, dst, job, manifest)

    # G3：CAS —— 读到的 seq 必须仍是文件里的 seq
    expected = job.get("state_seq")
    fresh = load_job(job_id)
    if fresh.get("state_seq") != expected:
        raise IllegalTransition(
            f"CAS 冲突：读取时 seq={expected}，落盘时已变为 {fresh.get('state_seq')}（并发或重入）"
        )

    job["state"] = dst
    job["state_seq"] = int(expected) + 1
    store.atomic_write_json(jdir / "job.json", job)
    _append_event(jdir, job["state_seq"], src, dst, reason, evidence)
    return job


def read_events(job_id: str) -> list[dict]:
    p = store.job_file(job_id, "events.jsonl")
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"seq": None, "raw": line, "error": "坏行"})
    except FileNotFoundError:
        return []
    return out


def state_badge(s: str) -> str:
    """给 CLI/文档用的中文短标签。"""
    return {
        CREATED: "已建", PROVISIONING: "建机中", READY: "就绪", SUBMITTED: "已提交",
        RUNNING: "推理中", FETCHING: "拉产物", VERIFYING: "校验中", PERSISTED: "已落盘",
        RELEASING: "销毁中", DONE: "完成", FAILED_PROVISION: "建机失败",
        FAILED_RUN: "推理失败", FAILED_FETCH: "传输失败", FAILED_VERIFY: "校验失败",
        FAILED: "失败", ABORTED: "已中止",
    }.get(s, s)
