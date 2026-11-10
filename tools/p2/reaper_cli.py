#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例生命周期 CLI：看三态判定 / 执行停机与销毁。

    python3 tools/p2/reaper_cli.py status          # 每台实例的三态判定 + 倒计时（不动手）
    python3 tools/p2/reaper_cli.py run --dry-run   # 只打印会做什么
    python3 tools/p2/reaper_cli.py run             # 真执行（该停的停、该销毁的销毁）
    python3 tools/p2/reaper_cli.py run --stop-only # 只停机，不销毁
    python3 tools/p2/reaper_cli.py run --idle-min 20   # 覆盖窗口（排障用）

systemd 里跑的是 `run`（见 deploy/systemd/p2-reaper.service）。

为什么单独一个入口而不是挂在 batchctl 下面：清理要在调度器停摆时照样能跑，
不该被"调度器相关"的命令树连坐。

## status 输出要回答的问题

用户看一眼就要知道：

  ① 这台机器现在在干什么（有活儿 / 空转中 / 已停机）
  ② 还要等多久会变化（空转窗口剩几秒 / 停机保留期剩几分）
  ③ 为什么是这个判定（窗口多久、谁定的档）

所以三列缺一不可：**判定 + 倒计时 + 理由**。只给"可清理 / 保留"两个词
等于把三档设计藏起来了 —— 用户看不到"我这台是长任务 90 秒档"。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import lifecycle, reaper  # noqa: E402

ACTION_CN = {"keep": "保活", "stop": "停机", "terminate": "销毁"}
ACTION_ICON = {"keep": "·", "stop": "💤", "terminate": "🗑"}


def _countdown(s: "reaper.InstanceState") -> str:
    """把判定换算成「还要多久」—— 这是用户真正关心的那一列。"""
    cloud = (s.cloud_state or "").lower()
    if s.busy:
        return "有活儿，不动"
    if s.action == "terminate":
        return "已过期，可销毁"
    if cloud and cloud != "running":
        if s.stopped_remaining_min is None:
            return "停机时长未知"
        return f"剩 {int(s.stopped_remaining_min)} 分后销毁"
    if s.action == "stop":
        return "已到点，可停机"
    if s.idle_remaining_s > 0:
        return f"还剩 {int(s.idle_remaining_s)}s 进停机"
    return "—"


def _tier(s: "reaper.InstanceState") -> str:
    """这台实例的空转窗口是哪一档、谁定的。"""
    if s.task_class == "long":
        return lifecycle.tier_label(s.idle_window_s).split(" · ")[0]
    if s.task_class == "short":
        return "短任务 0s"
    if s.idle_window_s:
        return f"兜底 {s.idle_window_s}s"
    return "—"


def cmd_status(a) -> int:
    rows = reaper.assess()
    if a.json:
        print(json.dumps([{
            "uid": s.uid, "name": s.name, "state": s.cloud_state,
            "action": s.action, "reason": s.action_reason,
            "busy": s.busy, "jobs": s.jobs,
            "task_class": s.task_class, "idle_window_s": s.idle_window_s,
            "idle_elapsed_s": round(s.idle_elapsed_s, 1),
            "idle_remaining_s": s.idle_remaining_s,
            "countdown": _countdown(s),
            "stopped_remaining_min": s.stopped_remaining_min,
            "tier": _tier(s),
        } for s in rows], ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("云平台上没有实例（或读不到凭证）")
        return 0

    ttl_h = lifecycle.stopped_ttl_min() / 60
    print(f"生命周期：短任务跑完即停；长任务空转 "
          f"{'/'.join(str(t) for t in lifecycle.IDLE_TIERS)} 秒（默认 "
          f"{lifecycle.DEFAULT_LONG_IDLE_S}）；停机保留 {ttl_h:g} 小时后销毁\n")
    print(f"  {'判定':<6} {'实例':<22} {'云端':<10} {'档位':<10} {'还要多久':<20} 理由")
    print("  " + "-" * 108)
    for s in rows:
        print(f"  {ACTION_ICON.get(s.action, ' ')}{ACTION_CN.get(s.action, s.action):<5} "
              f"{s.uid:<22} {s.cloud_state:<10} {_tier(s):<10} "
              f"{_countdown(s):<20} {s.action_reason}")
    return 0


def cmd_run(a) -> int:
    action = "terminate" if a.terminate else ("stop" if a.stop_only else "auto")
    done = reaper.reap(idle_min=a.idle_min, dry_run=a.dry_run, action=action)
    if not done:
        print("没有需要处理的实例")
        return 0
    tail = "（演练，未真的动手）" if a.dry_run else ""
    stops = [s.uid for s in done if s.action == "stop"]
    terms = [s.uid for s in done if s.action == "terminate"]
    print(f"\n{tail}共 {len(done)} 台："
          + (f"停机 {', '.join(stops)} " if stops else "")
          + (f"销毁 {', '.join(terms)}" if terms else ""))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="reaper", description="实例生命周期（按作业状态三态判定：保活/停机/销毁）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="看三态判定与倒计时（不动手）")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("run", help="执行（该停的停、该销毁的销毁）")
    sp.add_argument("--idle-min", type=int, default=0,
                    help="覆盖空转窗口（分钟）。排障/演练用，会推翻三档设计")
    sp.add_argument("--dry-run", action="store_true")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--stop-only", action="store_true",
                   help="只停机，不销毁（即使停机超过保留期）")
    g.add_argument("--terminate", action="store_true",
                   help="把该停机的也一并销毁（确认不要了才用）")
    sp.set_defaults(fn=cmd_run)

    a = ap.parse_args(argv)
    # 0 表示"用默认"，转成 None 让 reaper 自己取生命周期策略
    if not getattr(a, "idle_min", 0):
        a.idle_min = None
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
