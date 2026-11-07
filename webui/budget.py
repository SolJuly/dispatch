# -*- coding: utf-8 -*-
"""视频/图片作业的成本预估与预算闸门。

数据来自真机实测（2026-11 的回归与真机作业），按帧数/分辨率线性缩放；
只用于"提交前提醒 + 预算拦截"，不做账单依据。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PRICE_PER_HOUR = 2.15          # 4090 后付费
BOOT_COST = 0.08               # 建机 + 装模型的一次性摊销
DEFAULT_BUDGET = 1.0           # 控制台默认上限（元）

# key -> (实测秒数, 参考帧数, 参考宽, 参考高)
MEASURED = {
    "pack_video_wan2_2_14b_i2v": (450, 81, 640, 640),
    "pack_video_wan2_2_14b_flf2v": (460, 81, 640, 640),
    "pack_video_wan2_2_14b_s2v": (142, 154, 832, 480),
    "h3_t2v_121f": (300, 121, 768, 512),
    "h3_i2v_121f": (292, 121, 768, 512),
    "ltxv_2b_t2v": (41, 73, 768, 512),
    "wan21_i2v_480p": (299, 49, 832, 480),
    "pack_video_fastvideo_fasth3_i2v": (152, 121, 768, 512),
    "pack_video_minimax_h3_r2v": (277, 121, 768, 512),
    "pack_video_minimax_h3_multiframe_reference": (267, 121, 768, 512),
    "pack_video_wan_animate2": (192, 77, 768, 512),
    "pack_video_wan_animate2_distilled": (277, 77, 768, 512),
    "seedvr2_3b_upscale_video": (164, 73, 768, 512),
}
DEFAULT_REF = (300, 81, 640, 640)


def _probe_frames(path: str) -> int | None:
    """放大类作业没有 frames 参数，帧数得从源视频里数（否则估价会低好几倍）。"""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / path
        if not p.is_file():
            return None
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-count_frames", "-show_entries", "stream=nb_read_frames",
                            "-of", "json", str(p)],
                           capture_output=True, text=True, timeout=60)
        st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
        n = int(st.get("nb_read_frames") or 0)
        return n or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _scale(params: dict, ref_frames: int, ref_w: int, ref_h: int) -> float:
    frames = params.get("frames") or params.get("length")
    if not frames and params.get("video"):
        frames = _probe_frames(str(params["video"]))
    if not frames and params.get("duration"):
        try:
            frames = int(params["duration"]) * 16 + 1
        except (TypeError, ValueError):
            frames = ref_frames
    frames = frames or ref_frames
    w = params.get("width") or ref_w
    h = params.get("height") or ref_h
    try:
        frames, w, h = int(frames), int(w), int(h)
    except (TypeError, ValueError):
        return 1.0
    if not frames or not w or not h:
        return 1.0
    ratio = (frames / ref_frames) * ((w * h) / (ref_w * ref_h))
    return max(0.3, min(3.0, ratio))


def estimate(workflow: dict, params: dict) -> dict:
    """返回 {seconds, cost, budget_hint, measured}。"""
    key = (workflow or {}).get("key") or ""
    secs, rf, rw, rh = MEASURED.get(key, DEFAULT_REF)
    measured = key in MEASURED
    seconds = secs * _scale(params or {}, rf, rw, rh)
    cost = seconds / 3600.0 * PRICE_PER_HOUR + BOOT_COST
    return {
        "seconds": round(seconds, 1),
        "cost": round(cost, 2),
        "measured": measured,
        "hint": f"≈{seconds/60:.1f} 分钟 · ≈¥{cost:.2f}",
    }


def check_budget(workflow: dict, params: dict, budget: float | None) -> dict:
    """超预算抛 BudgetError；否则返回预估。"""
    est = estimate(workflow, params)
    if budget is not None and budget > 0 and est["cost"] > float(budget):
        raise BudgetError(
            f"预估 ¥{est['cost']:.2f} 超过预算上限 ¥{float(budget):.2f}（{est['hint']}）", est)
    return est


class BudgetError(Exception):
    def __init__(self, msg: str, est: dict):
        super().__init__(msg)
        self.est = est
