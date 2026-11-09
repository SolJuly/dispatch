#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例清理 CLI。

    python3 tools/p2/reaper_cli.py status          # 每台实例的判定与理由（不动手）
    python3 tools/p2/reaper_cli.py run --dry-run   # 只打印会关哪些
    python3 tools/p2/reaper_cli.py run             # 真关机
    python3 tools/p2/reaper_cli.py run --terminate # 真销毁（确认不要了才用）
    python3 tools/p2/reaper_cli.py run --idle-min 20

systemd 里跑的是 `run`（见 deploy/systemd/p2-reaper.service）。

为什么单独一个入口而不是挂在 batchctl 下面：清理要在调度器停摆时照样能跑，
不该被"调度器相关"的命令树连坐。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import reaper  # noqa: E402


def cmd_status(a) -> int:
    rows = reaper.assess()
    if a.json:
        print(json.dumps([{"uid": s.uid, "name": s.name, "state": s.cloud_state,
                           "busy": s.busy, "idle_min": s.idle_min, "reason": s.reason,
                           "jobs": s.jobs} for s in rows], ensure_ascii=False, indent=2))
        return 0
    limit = a.idle_min or reaper.idle_minutes()
    if not rows:
        print("云平台上没有实例（或读不到凭证）")
        return 0
    print(f"阈值：空闲 ≥ {limit} 分钟且无作业 → 可清理\n")
    for s in rows:
        flag = "可清理" if (not s.busy and s.idle_min >= limit) else "保留  "
        print(f"  [{flag}] {s.uid}  {s.name[:20]:20s}  {s.cloud_state:10s}  "
              f"空闲 {s.idle_min:6.1f}m  {s.reason}")
    return 0


def cmd_run(a) -> int:
    action = "terminate" if a.terminate else "stop"
    done = reaper.reap(idle_min=a.idle_min, dry_run=a.dry_run, action=action)
    if not done:
        print("没有需要清理的实例")
        return 0
    print(f"\n{'（演练）' if a.dry_run else ''}共 {len(done)} 台："
          f"{', '.join(s.uid for s in done)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reaper", description="实例清理（按作业状态判空闲）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="看判定结果（不动手）")
    sp.add_argument("--idle-min", type=int, default=0)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("run", help="执行清理")
    sp.add_argument("--idle-min", type=int, default=0)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--terminate", action="store_true",
                    help="销毁而不是关机（默认关机，可 start 复用）")
    sp.set_defaults(fn=cmd_run)

    a = ap.parse_args(argv)
    # 0 表示"用默认"，转成 None 让 reaper 自己取 P2_IDLE_REAP_MIN
    if not getattr(a, "idle_min", 0):
        a.idle_min = None
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
