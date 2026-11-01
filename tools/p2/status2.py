#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 多作业视图：把所有 job + 队列 + 账本聚合成一份 status2.json（4d）。

    python3 tools/p2/status2.py            # 刷新 reports/p2/status2.json 并打印摘要
    python3 tools/p2/status2.py --json     # 只打印 JSON

与 P1 的 status.py 的分工：status.py 是「单作业 + 架构图 H 区」的旧视图（保持不变，
向后兼容）；status2 是批量时代的多作业视图，两者读同一批 job.json。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import store, state  # noqa: E402
from tools.p2 import jobqueue  # noqa: E402

OUT = _ROOT / "reports" / "p2" / "status2.json"
FAILED_STATES = {"FAILED", "ABORTED", "FAILED_PROVISION", "FAILED_RUN", "FAILED_FETCH", "FAILED_VERIFY"}


def collect() -> dict:
    jobs = []
    for jid in store.list_jobs():
        j = store.read_json(store.job_file(jid)) or {}
        man = store.read_json(store.job_file(jid, "manifest.json")) or {}
        jobs.append({
            "job_id": jid,
            "state": j.get("state"),
            "model": (j.get("model") or {}).get("key"),
            "created_at": j.get("created_at"),
            "verdict": man.get("verdict"),
            "cost": float((man.get("cost") or {}).get("amount") or 0),
            "artifacts": len(man.get("artifacts") or []),
            "checks": man.get("checks") or {},
        })
    done = sum(1 for j in jobs if j["state"] == state.DONE)
    failed = sum(1 for j in jobs if j["state"] in FAILED_STATES)
    q = jobqueue.stats()
    return {
        "schema": "p2.status/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "jobs": {"total": len(jobs), "done": done, "failed": failed,
                 "running": len(jobs) - done - failed},
        "queue": q,
        "cost_total": round(sum(j["cost"] for j in jobs), 4),
        "items": jobs,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="status2")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    d = collect()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    j, q = d["jobs"], d["queue"]
    print(f"作业 {j['total']} · 完成 {j['done']} · 失败 {j['failed']} · 进行 {j['running']}"
          f" · 产物成本 ¥{d['cost_total']:.3f}")
    print("队列：" + " · ".join(f"{k}={v['n']}(¥{v['cost']})" for k, v in sorted(q["by_state"].items())))
    for it in d["items"][-8:]:
        ck = "".join("✓" if v else "✗" for v in it["checks"].values())
        print(f"  {it['job_id']}  {it['state']:<16} {it['model'] or '-':<10} ¥{it['cost']:.3f} {ck}")
    print(f"\n→ {OUT.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
