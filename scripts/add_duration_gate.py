#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给缺时长闸门的视频工作流补一条保守的 duration_s 区间。

背景：27 个视频工作流里只有 6 个有 `expect_defaults.duration_s`。
其余 21 个只验「体积 > min_bytes + 能解码」，所以**模型故障时产出 1 秒垃圾
照样 PASS**——七项复验全过，用户看到「完成」，片子是废的。

为什么不精确推导：这些工作流的帧率/帧数来自图里的运行时节点
（GetVideoComponents / GetImageSize / 素材驱动），静态拿不到常数。
2026-11-07 的审计已逐个核查过，确认「数值不唯一，主动放弃」。
本次不做精确化，只加**兜底下限**：
  · 下限 = 1.0 秒 —— 低于这个必然是坏片（正常出片最短也有 2.9 秒）
  · 上限 = 120 秒 —— 远大于任何单轮产出的时长，只用来拦「时间戳错乱」
    导致的异常长片，不参与正常判定

这样做的取舍：**宁可放过可疑的中等长度片子，也不能把好片判死。**
精确化的前提是先实测出每个工作流的真实帧率，那是后续按需做的活。

用法：
  python3 scripts/add_duration_gate.py            # 预览
  python3 scripts/add_duration_gate.py --apply     # 写回
  python3 scripts/add_duration_gate.py --apply --import
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
WFDIR = PROJ / "registry" / "workflows"

# 兜底区间。保守：只拦显而易见的坏片，不误杀好片。
MIN_S = 1.0
MAX_S = 120.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--import", dest="do_import", action="store_true")
    args = ap.parse_args()

    files = sorted(WFDIR.glob("*.json"))
    plan, skipped, already = [], 0, 0
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("kind") != "video":
            skipped += 1
            continue
        ed = d.get("expect_defaults") or {}
        if "duration_s" in ed:
            already += 1
            continue
        old = ed.get("min_bytes")
        ed["duration_s"] = [MIN_S, MAX_S]
        d["expect_defaults"] = ed
        plan.append((d.get("key"), old))
        if args.apply:
            f.write_text(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")

    print(f"扫描 {len(files)} 个 json（非 video 跳过 {skipped}）")
    print(f"已有 duration_s：{already}")
    print(f"本次补充：{len(plan)}")
    for k, mb in plan:
        print(f"  + {k[:52]:52s} min_bytes={mb}")
    if not args.apply:
        print("\n加 --apply 写入")
        return 0

    print("✓ 已写回")
    if args.do_import:
        r = subprocess.run([sys.executable, "-m", "tools.p20.p20ctl", "import"],
                           cwd=str(PROJ), capture_output=True, text=True)
        print((r.stdout or "") + (r.stderr or ""))
    else:
        print("⚠ 记得跑：python3 -m tools.p20.p20ctl import")
    return 0


if __name__ == "__main__":
    sys.exit(main())
