#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归档历史作业：把超出保留策略的作业移进 reports/p1/jobs/.trash/。

为什么需要：作业目录只增不减，79 个历史作业占 130MB，界面任务页和 /ui 历史页
会被半年前的调试记录淹没。但「删」是危险的——产物可能是唯一副本，状态机残留
（僵尸 RUNNING）还可能说明流程有 bug。所以设计成**移动而非删除**。

安全设计（按顺序）：
  1. 默认 dry-run，只打印计划；--apply 才动手
  2. 永不归档非终态的作业（RUNNING/CREATED/PROVISIONING 等），除非 --force-zombie
     且该作业确实超过 zombie-days 天——僵尸是状态机崩溃留下的，不是真的在跑
  3. 有产物的作业必须显式 --with-artifacts，否则自动跳过（产物可能是唯一副本）
  4. 移动到 .trash/ 而不是删除；.trash 只往下沉一层，list_jobs 不会收录
  5. 每次移动写一行 manifest，可原路恢复

用法：
  python3 scripts/archive_jobs.py                      # 预览：按默认策略会归档什么
  python3 scripts/archive_jobs.py --days 14             # 预览：保留 14 天
  python3 scripts/archive_jobs.py --apply               # 执行
  python3 scripts/archive_jobs.py --apply --with-artifacts
  python3 scripts/archive_jobs.py --apply --force-zombie --zombie-days 3
  python3 scripts/archive_jobs.py --restore p1-2026...  # 从 .trash 恢复
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
JOBS = PROJ / "reports" / "p1" / "jobs"
TRASH = JOBS / ".trash"
MANIFEST = TRASH / "manifest.jsonl"

# 这些状态表示作业还在流程里，归档会把状态机搞乱
LIVE_STATES = {
    "CREATED", "PROVISIONING", "READY", "SUBMITTED", "RUNNING",
    "FETCHING", "VERIFYING", "PERSISTED", "RELEASING", "RESUMING",
}
TERMINAL = {"DONE", "FAILED", "ABORTED"}


def load_jobs():
    """列出作业目录下的一层子目录（.trash 里的不会出现，因为它们的 job.json 在两层深）。"""
    out = []
    if not JOBS.is_dir():
        return out
    for d in sorted(JOBS.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        f = d / "job.json"
        job = {}
        if f.exists():
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                job = {}
        art = d / "artifacts"
        files = [p for p in art.iterdir() if p.is_file()] if art.is_dir() else []
        size = sum(p.stat().st_size for p in files)
        mtime = job.get("created_at") or ""
        if not mtime:
            # 没有 job.json 或没写时间：退回目录 mtime
            mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(d.stat().st_mtime))
        out.append({
            "dir": d, "job_id": d.name, "job": job,
            "state": job.get("state") or "NO_JOB_JSON",
            "created_at": str(mtime)[:19],
            "n_art": len(files), "bytes": size,
        })
    return out


def age_days(created_at: str) -> float:
    try:
        t = time.mktime(time.strptime(created_at.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0
    return (time.time() - t) / 86400.0


def plan(jobs, *, days: float, with_artifacts: bool,
         force_zombie: bool, zombie_days: float):
    """返回 (待归档, 跳过原因统计)。"""
    take, skip = [], Counter()
    for j in jobs:
        st = j["state"]
        age = age_days(j["created_at"])

        if st in LIVE_STATES:
            if st == "RUNNING" and force_zombie and age >= zombie_days:
                # 僵尸：声称在跑，但早就超时。仍要求有产物保护
                if j["n_art"] and not with_artifacts:
                    skip["僵尸但有产物(需 --with-artifacts)"] += 1
                    continue
                take.append({**j, "reason": f"僵尸 {st}（{age:.0f} 天）"})
                continue
            skip[f"非终态 {st}（流程在跑，不动）"] += 1
            continue

        if age < days:
            skip[f"未满 {days:g} 天"] += 1
            continue

        if j["n_art"] and not with_artifacts:
            skip["有产物（需 --with-artifacts）"] += 1
            continue

        take.append({**j, "reason": f"{st} · {age:.0f} 天前 · {j['n_art']} 产物"})
    return take, skip


def do_apply(take, *, dry: bool):
    if not take:
        print("没有需要归档的作业。")
        return 0
    if dry:
        print(f"\n将归档 {len(take)} 个作业（dry-run，加 --apply 执行）：")
        for j in take:
            print(f"  {j['job_id']:32s} {j['state']:16s} {j['created_at']}  "
                  f"{j['n_art']} 产物 {j['bytes'] / 1024:.0f}KB  ← {j['reason']}")
        total = sum(j["bytes"] for j in take)
        print(f"\n合计：{len(take)} 个作业，{total / 1048576:.1f} MB")
        return 0

    TRASH.mkdir(parents=True, exist_ok=True)
    moved = 0
    with MANIFEST.open("a", encoding="utf-8") as mf:
        for j in take:
            src, dst = j["dir"], TRASH / j["job_id"]
            if dst.exists():
                # 同名冲突（极少见）：加时间戳后缀，绝不覆盖已有归档
                dst = TRASH / f"{j['job_id']}.{int(time.time())}"
            try:
                shutil.move(str(src), str(dst))
            except OSError as e:
                print(f"  ✗ {j['job_id']}: {e}", file=sys.stderr)
                continue
            mf.write(json.dumps({
                "job_id": j["job_id"], "state": j["state"],
                "created_at": j["created_at"], "n_art": j["n_art"],
                "bytes": j["bytes"], "reason": j["reason"],
                "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "restore_to": str(src),
            }, ensure_ascii=False) + "\n")
            moved += 1
            print(f"  ✓ {j['job_id']}  ({j['n_art']} 产物)")
    print(f"\n已归档 {moved} 个 → {TRASH}")
    print(f"清单：{MANIFEST}")
    return 0


def do_restore(job_id: str):
    src = TRASH / job_id
    if not src.is_dir():
        print(f"✗ .trash 里没有 {job_id}", file=sys.stderr)
        return 1
    dst = JOBS / job_id
    if dst.exists():
        print(f"✗ 目标已存在：{dst}", file=sys.stderr)
        return 1
    shutil.move(str(src), str(dst))
    print(f"✓ 已恢复到 {dst}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7.0, help="保留天数（默认 7）")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    ap.add_argument("--with-artifacts", action="store_true",
                    help="连有产物的作业一起归档（默认跳过，产物可能是唯一副本）")
    ap.add_argument("--force-zombie", action="store_true",
                    help="归档声称 RUNNING 但早已超时的僵尸作业")
    ap.add_argument("--zombie-days", type=float, default=3.0,
                    help="僵尸判定阈值（默认 3 天）")
    ap.add_argument("--restore", metavar="JOB_ID", help="从 .trash 恢复一个作业")
    args = ap.parse_args()

    if args.restore:
        return do_restore(args.restore)

    jobs = load_jobs()
    print(f"作业目录：{JOBS}")
    print(f"共 {len(jobs)} 个 · {sum(j['bytes'] for j in jobs) / 1048576:.1f} MB")
    print("状态分布：" + ", ".join(f"{k}={v}" for k, v in
                                 Counter(j["state"] for j in jobs).most_common()))

    take, skip = plan(jobs, days=args.days, with_artifacts=args.with_artifacts,
                      force_zombie=args.force_zombie, zombie_days=args.zombie_days)
    if skip:
        print("\n跳过：")
        for k, v in skip.most_common():
            print(f"  {v:3d}  {k}")
    return do_apply(take, dry=not args.apply)


if __name__ == "__main__":
    sys.exit(main())
