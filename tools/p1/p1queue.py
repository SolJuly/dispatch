#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""p1queue —— 把 CREATED 状态的作业按时间顺序逐个跑完（单任务、串行、可续跑）。

  python3 tools/p1/p1queue.py                 # 清空队列：HEAD 里所有 CREATED 作业
  python3 tools/p1/p1queue.py --dry-run       # 只列出会跑哪些
  python3 tools/p1/p1queue.py --limit 1       # 只跑最早的一个
  python3 tools/p1/p1queue.py --include-aborted  # 连 ABORTED 一起（不常用）

注意：文件名不能叫 queue.py —— 脚本目录在 sys.path[0]，
会把标准库 queue 顶掉，子进程 import requests 直接崩。

为什么需要它：run.py 拿不到单任务锁就退出，控制台并发提交会留下停在 CREATED 的作业。
现在控制台提交带 --wait-lock 排队，这个脚本负责把历史遗留的 CREATED 一次性跑掉。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import state, store  # noqa: E402

RUN_PY = ROOT / "tools" / "p1" / "run.py"


def pending(include_aborted: bool = False,
            since: str | None = None) -> list[tuple[str, dict]]:
    """按 job_id（≈创建时间）返回待跑作业。since 为 YYYY-MM-DD，只要这天之后的。"""
    out = []
    want = {state.CREATED} | ({state.ABORTED} if include_aborted else set())
    for jid in store.list_jobs():
        j = store.read_json(store.job_file(jid))
        if not j or j.get("state") not in want:
            continue
        if since and str(j.get("created_at") or "")[:10] < since:
            continue
        out.append((jid, j))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="p1queue", description="串行跑完 CREATED 作业")
    ap.add_argument("--dry-run", action="store_true", help="只列出，不执行")
    ap.add_argument("--limit", type=int, default=0, help="最多跑几个（0=全部）")
    ap.add_argument("--include-aborted", action="store_true", help="连 ABORTED 一起跑")
    ap.add_argument("--wait-lock", type=int, default=0,
                    help="每个作业等锁的秒数（0=直接独占，队列内本应无锁冲突）")
    ap.add_argument("--since", help="只跑这天的作业（YYYY-MM-DD）")
    a = ap.parse_args(argv)

    items = pending(a.include_aborted, a.since)
    if a.limit and a.limit > 0:
        items = items[:a.limit]
    if not items:
        print("队列为空：没有 CREATED 状态的作业")
        return 0

    print(f"待跑 {len(items)} 个作业：")
    for jid, j in items:
        reg = ((j.get("model") or {}).get("registry") or {})
        print(f"  · {jid}  {reg.get('workflow_key') or j['model']['key']}  "
              f"预算 ¥{(j.get('budget') or {}).get('max_cost')}")
    if a.dry_run:
        print("（--dry-run：不执行）")
        return 0

    done, failed = [], []
    t0 = time.time()
    for i, (jid, _j) in enumerate(items, 1):
        print(f"\n=== [{i}/{len(items)}] {jid}", flush=True)
        cmd = [sys.executable, str(RUN_PY), jid, "--no-keep"]
        if a.wait_lock > 0:
            cmd += ["--wait-lock", str(a.wait_lock)]
        # 子进程输出同时进作业 run.log（控制台详情页读它）和本队列日志
        log_path = store.job_dir(jid) / "run.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rc = 1
        with log_path.open("ab") as fh:
            fh.write(f"\n--- queue 起跑 {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode())
            fh.flush()
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert proc.stdout is not None
            for line in proc.stdout:
                fh.write(line.encode("utf-8"))
                fh.flush()
                sys.stdout.write(line)
                sys.stdout.flush()
            rc = proc.wait()
        st = (store.read_json(store.job_file(jid)) or {}).get("state")
        print(f"=== [{i}/{len(items)}] {jid} → rc={rc} state={st}", flush=True)
        (done if rc == 0 else failed).append((jid, st))

    mins = (time.time() - t0) / 60
    print(f"\n队列结束：成功 {len(done)} · 失败 {len(failed)} · 用时 {mins:.1f} 分钟")
    for jid, st in done:
        print(f"  ✓ {jid} {st}")
    for jid, st in failed:
        print(f"  ✗ {jid} {st}（看 reports/p1/jobs/{jid}/run.log）")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
