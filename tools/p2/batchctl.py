#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 批量入口 batchctl：submit / list / show / run / doctor / cancel。

    python3 tools/p2/batchctl.py submit --name h3-sweep --specs 'specs/*.json' --slots 2 --budget 3
    python3 tools/p2/batchctl.py list --batch b-xxxx
    python3 tools/p2/batchctl.py run --slots 2 --daily-budget 8
    python3 tools/p2/batchctl.py doctor
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import store, state  # noqa: E402
from tools.p2 import jobqueue as queue, scheduler  # noqa: E402

JOBCTL = _ROOT / "tools" / "p1" / "jobctl.py"
JOB_ID_RE = re.compile(r"p1-\d{8}T\d{4}-[0-9a-f]{6}")


def _new_job(spec: Path) -> tuple[str | None, str]:
    """调 jobctl new 建作业（复用其校验/查重/原子占位），返回 (job_id, 错误)。"""
    p = subprocess.run([sys.executable, str(JOBCTL), "new", "--spec", str(spec)],
                       capture_output=True, text=True, cwd=str(_ROOT))
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        return None, out.strip().splitlines()[-1] if out.strip() else f"rc={p.returncode}"
    m = JOB_ID_RE.search(out)
    return (m.group(0) if m else None), ""


def cmd_submit(a) -> int:
    batch_id = a.batch or f"b-{time.strftime('%Y%m%dT%H%M')}-{int(time.time()) % 1000:03d}"
    specs = sorted(Path(x) for x in glob.glob(a.specs, recursive=True))
    # 允许 --specs 直接给多个文件（逗号分隔）
    specs += [Path(x) for x in a.specs.split(",") if x and Path(x).is_file()]
    specs = sorted({s.resolve() for s in specs if s.is_file()})
    if not specs:
        print(f"✗ 没有匹配的 spec：{a.specs}")
        return 1
    queue.create_batch(batch_id, a.name or batch_id, a.slots, a.budget)
    print(f"批次 {batch_id} · {len(specs)} 个 spec")
    ok_n = 0
    for s in specs:
        jid, err = _new_job(s)
        if not jid:
            print(f"  ✗ {s.name}: {err}")
            continue
        j = store.read_json(store.job_file(jid)) or {}
        ok, why = queue.submit(
            batch_id, jid, j.get("params_hash", ""),
            priority=a.priority, model_key=(j.get("model") or {}).get("key", ""),
            required_gpu=(j.get("instance") or {}).get("required_gpu") or [],
            est_cost=float((j.get("budget") or {}).get("max_cost") or 0),
            max_attempts=a.max_attempts)
        if ok:
            print(f"  ✓ {s.name} → {jid} queued")
            ok_n += 1
        else:
            print(f"  ✗ {s.name} → {jid} 入队被拒：{why}（回收空作业）")
            try:
                import shutil
                shutil.rmtree(store.job_dir(jid), ignore_errors=True)
            except Exception:
                pass
    print(f"\n入队 {ok_n}/{len(specs)}")
    print(f"下一步：python3 tools/p2/batchctl.py run --slots {a.slots}")
    return 0 if ok_n else 1


def cmd_list(a) -> int:
    rows = queue.items(batch_id=a.batch, state=a.state)
    if not rows:
        print("队列为空")
        return 0
    print(f"{'job_id':<24} {'state':<9} {'att':<4} {'model':<12} {'cost':<8} batch")
    print("-" * 96)
    for r in rows:
        print(f"{r['job_id']:<24} {r['state']:<9} {r['attempts']:<4} "
              f"{(r['model_key'] or '-')[:11]:<12} {r['cost'] if r['cost'] is not None else '-':<8} "
              f"{r['batch_id']}")
    s = queue.stats()
    print(f"\n总计 {s['total']} · " + " · ".join(f"{k}={v['n']}" for k, v in sorted(s["by_state"].items())))
    return 0


def cmd_show(a) -> int:
    rows = queue.items(batch_id=a.batch) if a.batch else queue.items()
    if not rows:
        print("没有条目")
        return 0
    agg: dict = {}
    for r in rows:
        agg.setdefault(r["state"], {"n": 0, "cost": 0.0})
        agg[r["state"]]["n"] += 1
        agg[r["state"]]["cost"] += float(r["cost"] or 0)
    print(json.dumps({"batch": a.batch, "agg": agg,
                      "failed_reasons": sorted({r["last_error"] for r in rows if r["state"] == "failed" and r["last_error"]})},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_run(a) -> int:
    import signal

    s = scheduler.Scheduler(slots=a.slots, gpu=a.gpu, hours=a.hours,
                            daily_budget=a.daily_budget, poll=a.poll,
                            stagger=a.stagger, dry_run=getattr(a, "dry_run", False),
                            quota=getattr(a, "quota", 5))

    def _stop(signum, frame):
        print("\n⏹ 收到中断：停止领取，等在跑的收尾（再按一次强退）…", flush=True)
        s.stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    return s.run()


def cmd_audit(a) -> int:
    rows = queue.audit_rows(job_id=a.job, limit=a.limit)
    if not rows:
        print("审计流水为空")
        return 0
    print(f"{'ts':<11} {'job_id':<24} {'from':<9} {'to':<16} {'exit':<5} {'gpu_min':<8} instance")
    print("-" * 110)
    for r in rows:
        print(f"{time.strftime('%H:%M:%S', time.localtime(r['ts'])):<11} {r['job_id']:<24} "
              f"{(r['from_state'] or '-'):<9} {r['to_state']:<16} "
              f"{r['exit_code'] if r['exit_code'] is not None else '-':<5} "
              f"{r['gpu_minutes'] if r['gpu_minutes'] is not None else '-':<8} "
              f"{r['instance_id'] or '-'}")
    return 0


def cmd_watchdog(a) -> int:
    """P1-5：扫描孤儿实例并回收（--once 适合 cron）。"""
    killed = scheduler.reclaim_orphans(dry_run=a.dry_run)
    stale = queue.reset_stale(scheduler.TIMEOUT_S)
    print(json.dumps({"reclaimed": killed, "heartbeat_timeout": stale}, ensure_ascii=False, indent=2))
    return 0


def cmd_doctor(a) -> int:
    d = scheduler.doctor()
    if a.reap:
        d["reaped"] = scheduler.reap_instances(a.ttl)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


def cmd_cancel(a) -> int:
    rows = queue.items(batch_id=a.batch)
    n = 0
    for r in rows:
        if r["state"] in ("queued", "claimed", "running"):
            queue.cancel(r["job_id"], "batch cancel")
            n += 1
    print(f"已取消 {n} 条")
    if getattr(a, "purge", False):
        killed = scheduler.reclaim_orphans(dry_run=False)
        print(f"purge：释放孤儿实例 {killed or '无'}")
    else:
        print("提示：在跑的 worker 会在调度器下一轮（≤30s）被终止；实例由 watchdog 释放。"
              "要立刻释放加 --purge")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="batchctl", description="P2 批量作业")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("submit", help="提交一批 spec")
    sp.add_argument("--specs", required=True, help="glob 或逗号分隔路径")
    sp.add_argument("--name")
    sp.add_argument("--batch")
    sp.add_argument("--slots", type=int, default=2)
    sp.add_argument("--budget", type=float, default=0.0)
    sp.add_argument("--priority", type=int, default=0)
    sp.add_argument("--max-attempts", type=int, default=3, help="最大尝试次数（默认 3）")
    sp.set_defaults(fn=cmd_submit)

    sp = sub.add_parser("list", help="列队列表")
    sp.add_argument("--batch")
    sp.add_argument("--state")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="批次汇总")
    sp.add_argument("--batch")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("run", help="跑调度器")
    sp.add_argument("--slots", type=int, default=2)
    sp.add_argument("--gpu", default="4090")
    sp.add_argument("--hours", type=float, default=1.0)
    sp.add_argument("--daily-budget", type=float, default=8.0)
    sp.add_argument("--poll", type=float, default=10.0)
    sp.add_argument("--stagger", type=float, default=10.0, help="错峰下单间隔秒")
    sp.add_argument("--quota", type=int, default=5, help="账户实例上限（Level1=5 / Level2=20）")
    sp.add_argument("--dry-run", action="store_true", help="只回收/对账，不真正建机")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("doctor", help="对账 + 可选回收")
    sp.add_argument("--reap", action="store_true")
    sp.add_argument("--ttl", type=int, default=45)
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("audit", help="审计流水（状态变更/exit_code/GPU 分钟）")
    sp.add_argument("--job")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_audit)

    sp = sub.add_parser("watchdog", help="孤儿实例回收（P1-5）")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_watchdog)

    sp = sub.add_parser("cancel", help="取消批次未完成项")
    sp.add_argument("--batch", required=True)
    sp.add_argument("--purge", action="store_true", help="同时释放实例（立刻止血）")
    sp.set_defaults(fn=cmd_cancel)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
