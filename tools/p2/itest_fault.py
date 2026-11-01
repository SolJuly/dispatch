#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 联调（真机）：故障注入。会花钱，需显式 --yes。

    python3 tools/p2/itest_fault.py --scene heartbeat --yes   # kill worker → 心跳超时 → 重试 → 完成
    python3 tools/p2/itest_fault.py --scene cancel    --yes   # 运行中取消 → worker 被杀 + 实例释放
    python3 tools/p2/itest_fault.py --scene failrun   --yes   # 推理失败 → 重试 3 次 → terminal_failed
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import store, state as jobstate  # noqa: E402
from tools.p2 import batchctl, jobqueue, scheduler  # noqa: E402

TERMINAL = {"done", "failed", "terminal_failed", "cancelled", "reclaimed", "skipped"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [itest] {msg}", flush=True)


def job_state_of(jid: str) -> str:
    return (store.read_json(store.job_file(jid)) or {}).get("state") or "?"


def instance_of(jid: str) -> str | None:
    j = store.read_json(store.job_file(jid)) or {}
    return ((j.get("instance") or {}).get("uhost_id")) or None


def wait_for(fn, timeout: float, step: float = 5.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if fn():
            return True
        time.sleep(step)
    return False


def submit_one(spec: str) -> str:
    p = subprocess.run([sys.executable, str(_ROOT / "tools" / "p2" / "batchctl.py"),
                        "submit", "--name", "itest", "--specs", spec, "--slots", "1"],
                       capture_output=True, text=True, cwd=str(_ROOT))
    out = p.stdout + p.stderr
    print(out.strip())
    for tok in out.split():
        if tok.startswith("p1-") and len(tok) > 16:
            return tok.strip("（）()，,。")
    raise RuntimeError("没拿到 job_id")


def run_scene(scene: str, spec: str, kill_delay: int = 95, kill_state: str = "RUNNING") -> int:
    jid = submit_one(spec)
    log(f"作业 {jid} 已入队")

    s = scheduler.Scheduler(slots=1, poll=5, quota=5, daily_budget=8, stagger=0,
                            gpu=getattr(_ARGS, "gpu", "4090"))
    th = threading.Thread(target=s.run, daemon=True)
    th.start()

    if scene == "heartbeat":
        log(f"等作业进入 {kill_state}（状态触发，最多 300s）…")
        hit = wait_for(lambda: job_state_of(jid) in ({kill_state} | TERMINAL), 300, step=2)
        cur = job_state_of(jid)
        if cur in TERMINAL:
            log(f"✗ 作业已到终态 {cur}，没赶上注入（本轮作废，不计入联调）")
            s.stop = True
            return 3
        if not hit:
            log("✗ 等状态超时，放弃")
            s.stop = True
            return 1
        uid = instance_of(jid)
        log(f"作业已到 {job_state_of(jid)}，实例 {uid}；kill -9 worker")
        child = s.children.get(jid)
        if not child:
            log("✗ 找不到子进程（可能已结束）")
            s.stop = True
            return 1
        log(f"kill -9 worker pid={child.pid}")
        child.kill()
        # 心跳超时 60s + 重试；给足时间
        done = wait_for(lambda: ({i["job_id"]: i for i in jobqueue.items()}[jid]["state"] in TERMINAL),
                        600, step=10)
        s.stop = True
        row = {i["job_id"]: i for i in jobqueue.items()}[jid]
        log(f"终态：state={row['state']} attempts={row['attempts']} "
            f"exit={row['exit_code']} cost={row['cost']}")
        audit = jobqueue.audit_rows(jid)
        chain = " → ".join(f"{r['from_state'] or '-'}→{r['to_state']}" for r in reversed(audit))
        log(f"审计链：{chain}")
        return 0 if done else 1

    if scene == "cancel":
        log(f"等作业进入 {kill_state}（最多 300s）…")
        wait_for(lambda: job_state_of(jid) in ({kill_state} | TERMINAL), 300, step=2)
        if job_state_of(jid) in TERMINAL:
            log(f"✗ 作业已终态 {job_state_of(jid)}，没赶上取消")
            s.stop = True
            return 3
        log(f"作业已到 {job_state_of(jid)}，执行取消")
        rows = jobqueue.items(batch_id=None)
        bid = [r["batch_id"] for r in rows if r["job_id"] == jid][0]
        log(f"取消批次 {bid}（--purge）")
        p = subprocess.run([sys.executable, str(_ROOT / "tools" / "p2" / "batchctl.py"),
                            "cancel", "--batch", bid, "--purge"],
                           capture_output=True, text=True, cwd=str(_ROOT))
        print(p.stdout.strip())
        # 调度器下一轮会杀 worker
        wait_for(lambda: s.children.get(jid) is None, 120, step=5)
        s.stop = True
        row = {i["job_id"]: i for i in jobqueue.items()}[jid]
        log(f"终态：state={row['state']} exit={row['exit_code']}")
        audit = jobqueue.audit_rows(jid)
        log("审计链：" + " → ".join(f"{r['from_state'] or '-'}→{r['to_state']}" for r in reversed(audit)))
        return 0 if row["state"] == "cancelled" else 1

    log(f"未知场景 {scene}")
    return 1


_ARGS = None


def main(argv=None) -> int:
    global _ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", required=True, choices=["heartbeat", "cancel", "failrun"])
    ap.add_argument("--spec", default="reports/p2/specs/ltxv-a.json")
    ap.add_argument("--kill-delay", type=int, default=95, help="（兼容）固定延迟，已改为状态触发")
    ap.add_argument("--kill-state", default="RUNNING", help="作业进入该状态时注入故障")
    ap.add_argument("--gpu", default="4090", help="卡型（4090 无货时可换 V100S）")
    ap.add_argument("--yes", action="store_true", help="确认真机执行（会花钱）")
    a = ap.parse_args(argv)
    _ARGS = a
    if not a.yes:
        print("这是真机联调，会花钱。确认加 --yes")
        return 2
    return run_scene(a.scene, a.spec, a.kill_delay, a.kill_state)


if __name__ == "__main__":
    raise SystemExit(main())
