#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从真实作业日志里回归预算基线，打印可直接粘进 webui/budget.py 的 MEASURED 条目。

用法：
    python3 scripts/harvest_budget_baselines.py            # 只看
    python3 scripts/harvest_budget_baselines.py --write    # 直接改写 budget.py 里已有的键

口径：取每个工作流「推理完成 耗时 Ns」的最小值（同参数下最接近干净的机器状态），
帧数/尺寸取 job.json 里的 params；缺 frames 的用 duration×fps 或视频帧数。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / "reports/p1/jobs"
BUDGET = ROOT / "webui/budget.py"


def _params_frames(params: dict, fps: int) -> int | None:
    for key in ("frames", "length"):
        v = params.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    v = params.get("duration")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(round(float(v) * fps))
    return None


def harvest() -> dict[str, tuple[int, int, int, int]]:
    best: dict[str, tuple[int, int, int, int]] = {}
    for jf in JOBS.glob("*/job.json"):
        log = jf.with_name("run.log")
        if not log.is_file():
            continue
        text = log.read_text(encoding="utf-8", errors="ignore")
        m = re.findall(r"推理完成.*?耗时 (\d+)s", text)
        if not m:
            continue
        secs = int(m[-1])
        try:
            job = json.loads(jf.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        reg = ((job.get("model") or {}).get("registry") or {})
        key = reg.get("workflow_key")
        if not key or job.get("state") != "DONE":
            continue
        params = job.get("params") or {}
        fps = 16
        frames = _params_frames(params, fps)
        w, h = params.get("width"), params.get("height")
        if not frames or not w or not h:
            continue
        old = best.get(key)
        if old is None or secs < old[0]:
            best[key] = (secs, int(frames), int(w), int(h))
    return best


def main() -> int:
    best = harvest()
    if not best:
        print("没有可用的真机日志（需要 state=DONE 且日志里有「推理完成 … 耗时 Ns」）")
        return 1
    print("key, (seconds, ref_frames, ref_w, ref_h)")
    for k, v in sorted(best.items()):
        print(f'    "{k}": {v},')
    if "--write" in sys.argv:
        src = BUDGET.read_text(encoding="utf-8")
        for k, v in best.items():
            pat = re.compile(rf'("{re.escape(k)}": \()[^)]*(\))')
            src = pat.sub(lambda m: f"{m.group(1)}{v[0]}, {v[1]}, {v[2]}, {v[3]}{m.group(2)}", src)
        BUDGET.write_text(src, encoding="utf-8")
        print(f"已写回 {BUDGET.relative_to(ROOT)}（只改已存在的键）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
