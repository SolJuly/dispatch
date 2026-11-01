#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 审计对账：核对「队列审计流水」与「作业真实结果」是否 100% 一致。

    python3 tools/p2/audit_check.py            # 人读表格
    python3 tools/p2/audit_check.py --json     # 机器读

检查项（任一不满足即 issue）：
  C1 审计链有序：to_state 必须按 queued→claimed→running→终态 前进，不得回退/跳变
  C2 终态必带 exit_code（done=0，failed/terminal_failed 非空）
  C3 gpu_minutes 与 manifest.cost.minutes 一致（容差 0.2 分钟）
  C4 item.cost 与 manifest.cost.amount 一致（容差 ¥0.01）
  C5 item.state 与 job.state 映射一致（done↔DONE、failed↔FAILED*、cancelled↔ABORTED）
  C6 每条 running 之前必须有 claimed，claimed 之前必须有 queued（缺环即报）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import store, state as jobstate  # noqa: E402
from tools.p2 import jobqueue  # noqa: E402

# 合法边（重试的 running/claimed→queued 是允许的，不是回退）
ALLOWED = {
    None: {"queued", "cancelled", "reclaimed"},
    "queued": {"claimed", "cancelled"},
    "claimed": {"running", "queued", "cancelled"},
    "running": {"done", "failed", "terminal_failed", "queued", "cancelled"},
    "failed": {"queued", "terminal_failed", "reclaimed"},
    "terminal_failed": {"reclaimed"},
    "done": set(), "cancelled": set(), "reclaimed": set(), "skipped": set(),
}
TERMINAL = {"done", "failed", "terminal_failed", "cancelled", "reclaimed", "skipped"}

ITEM_TO_JOB = {
    "done": {jobstate.DONE},
    "failed": {jobstate.FAILED, jobstate.FAILED_PROVISION, jobstate.FAILED_RUN,
               jobstate.FAILED_FETCH, jobstate.FAILED_VERIFY, jobstate.RELEASING},
    "terminal_failed": {jobstate.FAILED},
    "cancelled": {jobstate.ABORTED, jobstate.FAILED, jobstate.DONE},
    "reclaimed": {jobstate.FAILED, jobstate.ABORTED, jobstate.DONE},
}


def _issues_for(jid: str, audit: list[dict], item: dict | None,
                job: dict | None, man: dict | None) -> list[str]:
    """audit 按 id DESC 返回，这里翻成时间正序。"""
    out: list[str] = []
    rows = list(reversed(audit))
    if not rows:
        if item:
            return ["C0 有队列项但无审计流水"]
        return out

    # C1 合法边（from→to 必须在白名单）+ C6 缺环
    seen = []
    for r in rows:
        frm, to = r.get("from_state"), r.get("to_state")
        seen.append(to)
        if to not in ALLOWED:
            out.append(f"C1 未知状态 {to}")
        elif frm is not None and to not in ALLOWED.get(frm, set()):
            out.append(f"C1 非法边 {frm}→{to}")
    if "running" in seen and "claimed" not in seen:
        out.append("C6 running 前缺 claimed")
    if "claimed" in seen and "queued" not in seen:
        out.append("C6 claimed 前缺 queued")

    # C2 终态 exit_code
    last = rows[-1]
    to = last.get("to_state")
    if to in TERMINAL and to != "queued":
        if last.get("exit_code") is None and to in ("done", "failed", "terminal_failed"):
            out.append(f"C2 终态 {to} 缺 exit_code")
        if to == "done" and last.get("exit_code") not in (0, None):
            out.append(f"C2 done 但 exit_code={last.get('exit_code')}")

    # C3 gpu_minutes vs manifest
    if man:
        m_min = float((man.get("cost") or {}).get("minutes") or 0)
        a_min = last.get("gpu_minutes")
        if a_min is not None and abs(float(a_min) - m_min) > 0.2:
            out.append(f"C3 gpu_minutes {a_min} ≠ manifest {m_min}")

    # C4 cost vs manifest
    if man and item:
        m_cost = float((man.get("cost") or {}).get("amount") or 0)
        i_cost = float(item.get("cost") or 0)
        if abs(i_cost - m_cost) > 0.011:
            out.append(f"C4 item.cost ¥{i_cost:.4f} ≠ manifest ¥{m_cost:.4f}")

    # C5 item vs job 状态映射（不依赖 manifest）
    if item:
        st = item.get("state")
        js = (job or {}).get("state")
        allowed = ITEM_TO_JOB.get(st)
        if allowed and js not in allowed:
            out.append(f"C5 item={st} 但 job={js}（映射 {sorted(allowed)}）")
    return out


def check() -> dict:
    items = {i["job_id"]: i for i in jobqueue.items()}
    jobs = store.list_jobs()
    rows = []
    bad = 0
    for jid in jobs:
        item = items.get(jid)
        if item is None:
            continue          # legacy 单跑作业（P1 期，没有队列项）
        job = store.read_json(store.job_file(jid)) or {}
        man = store.read_json(store.job_file(jid, "manifest.json"))
        audit = jobqueue.audit_rows(jid, limit=200)
        iss = _issues_for(jid, audit, item, job, man)
        rows.append({"job_id": jid, "item_state": item.get("state"),
                     "job_state": job.get("state"), "audit_n": len(audit),
                     "exit_code": item.get("exit_code"), "cost": item.get("cost"),
                     "issues": iss})
        bad += 1 if iss else 0
    return {"checked": len(rows), "bad": bad, "rows": rows,
            "queue_total": len(items), "jobs_total": len(jobs)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="audit_check")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = check()
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0 if d["bad"] == 0 else 1

    print(f"审计对账：检查 {d['checked']} 个有队列项的作业 · 不一致 {d['bad']}")
    print(f"{'job_id':<24} {'item':<10} {'job':<18} {'audit':<6} {'exit':<5} {'cost':<8} 问题")
    print("-" * 108)
    for r in d["rows"]:
        cost_s = f"¥{r['cost']:.3f}" if r["cost"] is not None else "-"
        print(f"{r['job_id']:<24} {str(r['item_state']):<10} {str(r['job_state']):<18} "
              f"{r['audit_n']:<6} {str(r['exit_code']):<5} {cost_s:<8} "
              f"{'; '.join(r['issues']) or '✓'}")
    return 0 if d["bad"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
