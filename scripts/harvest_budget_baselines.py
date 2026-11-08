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

# 从 scripts/ 直接跑时，sys.path[0] 是 scripts/，import webui 会 ModuleNotFoundError。
# 之前的异常被 except 静默吞掉 → 探测永远失败 → 参考帧退化成 params.frames，
# 动作迁移类的基线因此算低 27%（实测 300s 对应 64 帧，被记成 81 帧）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _params_frames(params: dict, fps: int) -> int | None:
    for key in ("frames", "length"):
        v = params.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
    v = params.get("duration")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(round(float(v) * fps))
    return None


def harvest(include_trash: bool = False) -> dict[str, tuple[int, int, int, int]]:
    """回溯真机日志。

    include_trash：本项目的作业按保留策略归档到 jobs/.trash/，归档里躺着的
    恰恰是最有参考价值的早期验证数据。默认不扫会让收割率接近 0。
    """
    best: dict[str, tuple[int, int, int, int]] = {}
    dirs = [JOBS] + ([JOBS / ".trash"] if include_trash else [])
    for jf in [p for d in dirs if d.is_dir() for p in d.glob("*/job.json")]:
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
        # 参考帧必须与「这次耗时」对应同一个工作量。
        # 动作迁移类真实工作量由**驱动视频的帧数**决定（模板按驱动视频分轮跑），
        # params.frames（如 81）只是单轮帧数，用它当参考会把基线算低 27%。
        # 实测：image_2=in-22a23224-t4-16fps.mp4（64 帧）跑 300s，
        #      若记成 (300, 81) 则 3.70 s/帧；实际是 4.69 s/帧。
        frames = None
        try:
            from webui import budget as _budget
            from tools.p20 import registry as _reg
            wf = _reg.get_workflow(key)
            for vk in sorted(_budget._video_keys(wf)):
                nm = params.get(vk)
                if isinstance(nm, str) and nm:
                    probed = _budget._probe_frames(nm)
                    if probed:
                        frames = probed
                        break
        except Exception as e:  # noqa: BLE001
            # 不要静默吞：探测失败会悄悄让参考帧退化，基线就悄悄算错。
            print(f"  ⚠ {key} 驱动视频探测失败（{type(e).__name__}: {e}），"
                  f"回退到 params.frames", file=sys.stderr)
            frames = None
        if not frames:
            frames = _params_frames(params, fps)
        if not frames:
            continue
        # width/height 缺了不跳过：多数工作流的尺寸在图里写死，params 里没有。
        # 用 0 表示未知，budget.py 的 MEASURED 只用 (secs, frames) 做缩放，
        # 分辨率只是辅助信息，缺了不影响按帧数估时。
        # 宽高缺了不能写 0：budget._scale 见 w/h 为 0 会直接返回 1.0，
        # 等于让缩放（含 trim_seconds 的省钱效果）完全失效。
        # 未知时回退到该 key 现有基线里的值，再退到 portfolio 默认 768×512。
        w = int(params.get("width") or 0)
        h = int(params.get("height") or 0)
        # 同一 key 可能有不同帧数的样本（h3_t2v 实测过 121/240/241 帧）。
        # 直接取「最小秒数」会把大帧数的样本误当成更优 —— 必须按 秒/帧 归一化比较，
        # 选单位开销最低的那次（最接近无干扰的机器状态），并保留它自己的帧数配对。
        rate = secs / max(1, int(frames))
        old = best.get(key)
        if old is None or rate < old[0] / max(1, old[1]):
            best[key] = (secs, int(frames), w, h)
    # 宽高未知的，用现有 MEASURED 的值兜底（读 budget.py 的当前内容）
    try:
        import importlib.util
        spec2 = importlib.util.spec_from_file_location("_b", BUDGET)
        mod = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(mod)          # type: ignore[union-attr]
        cur = getattr(mod, "MEASURED", {})
    except Exception:  # noqa: BLE001
        cur = {}
    for k, (secs, fr, w, h) in list(best.items()):
        if not w or not h:
            old_w, old_h = (cur.get(k) or (0, 0, 768, 512))[2:4]
            best[k] = (secs, fr, old_w or 768, old_h or 512)
    return best


def main() -> int:
    best = harvest(include_trash="--include-trash" in sys.argv)
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
