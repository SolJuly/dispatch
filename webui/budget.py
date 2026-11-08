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
    "pack_video_wan2_2_14b_i2v": (462, 80, 640, 640),
    "pack_video_wan2_2_14b_flf2v": (443, 80, 640, 640),
    "pack_video_wan2_2_14b_s2v": (142, 154, 832, 480),
    "h3_t2v_121f": (516, 241, 768, 512),
    "h3_i2v_121f": (292, 121, 768, 512),
    "ltxv_2b_t2v": (278, 251, 1280, 768),
    "wan21_i2v_480p": (282, 49, 832, 480),
    "pack_video_fastvideo_fasth3_i2v": (164, 121, 768, 512),   # 2026-11-07 真机 164s
    "pack_video_minimax_h3_r2v": (277, 121, 768, 512),
    "pack_video_minimax_h3_multiframe_reference": (267, 121, 768, 512),
    "pack_video_wan_animate2": (172, 64, 768, 512),            # 2026-11-07 真机 172s
    "pack_video_wan_animate2_distilled": (300, 64, 768, 512),
    "seedvr2_3b_upscale_video": (484, 243, 768, 512),
}
DEFAULT_REF = (300, 81, 640, 640)

# 没有真机基线的视频工作流，按「同族已实测档位」取锚点（不许拿图片档冒充视频）
FAMILY_REF = (
    ("ltx2_3", (600, 121, 1280, 720)),
    ("ltx2_5", (600, 121, 1280, 720)),
    ("ltx", (300, 73, 768, 512)),
    ("scail", (450, 81, 832, 480)),
    ("vace", (420, 81, 832, 480)),
    ("animate", (192, 77, 768, 512)),
    ("minimax_h3", (277, 121, 768, 512)),
    ("fasth3", (164, 121, 768, 512)),
)


def _family_ref(key: str) -> tuple[int, int, int, int] | None:
    for needle, ref in FAMILY_REF:
        if needle in key:
            return ref
    return None


def _probe_frames(path: str) -> int | None:
    """放大类作业没有 frames 参数，帧数得从源视频里数（否则估价会低好几倍）。"""
    try:
        p = Path(path)
        if not p.is_absolute():
            cand = [ROOT / path, ROOT / "reports/p2/p21/inputs" / path,
                    ROOT / "share/inputs" / path]
            p = next((c for c in cand if c.is_file()), ROOT / path)
        if not p.is_file():
            return None
        # 先只读容器元数据（毫秒级）；没有 nb_frames 才退化成全量数帧（长片很贵）
        for args, key in ((["-show_entries", "stream=nb_frames"], "nb_frames"),
                          (["-count_frames", "-show_entries", "stream=nb_read_frames"],
                           "nb_read_frames")):
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", *args,
                                "-of", "json", str(p)],
                               capture_output=True, text=True, timeout=60)
            st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
            n = int(st.get(key) or 0)
            if n:
                return n
        return None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _video_keys(workflow: dict | None) -> set[str]:
    """注册表里 type=video 的参数名（驱动视频可能叫 image_2 / image）。"""
    return {k for k, v in ((workflow or {}).get("params") or {}).items()
            if isinstance(v, dict) and v.get("type") == "video"}


def _scale(params: dict, ref_frames: int, ref_w: int, ref_h: int,
           video_keys: set[str] | None = None) -> float:
    # 驱动视频的真实帧数优先：模板自带的 frames 默认值（如 77）只是单轮帧数，
    # 不能代表「按驱动视频循环 N 轮」的真实工作量。
    frames = None
    for k in sorted(video_keys or ()):
        v = params.get(k)
        if isinstance(v, str) and v:
            frames = _probe_frames(v)
            if frames:
                # trim_seconds 是省钱开关：动作迁移按驱动视频帧数分轮跑，
                # 截到 4 秒（16fps→64 帧）只跑 1 轮，不截 256 帧要跑 4 轮。
                # 估价必须跟着缩，否则用户截短了却看到按整条报的价，
                # 会以为这个开关没用（实测过 trim=4 与 trim=0 报同一个价）。
                try:
                    _secs = float(params.get("trim_seconds") or 0)
                except (TypeError, ValueError):
                    _secs = 0.0
                if _secs > 0:
                    _trimmed = int(round(_secs * 16))   # _trim_video 固定重采样 16fps
                    if _trimmed:
                        frames = min(frames, _trimmed)
                break
    if not frames:
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
    return max(0.3, min(8.0, ratio))   # 长驱动视频（数百帧）真的会跑到 20+ 分钟


def estimate(workflow: dict, params: dict) -> dict:
    """返回 {seconds, cost, budget_hint, measured}。"""
    key = (workflow or {}).get("key") or ""
    basis = "measured"
    if key in MEASURED:
        secs, rf, rw, rh = MEASURED[key]
    elif (fam := _family_ref(key)):
        secs, rf, rw, rh = fam
        basis = "family"
    else:
        secs, rf, rw, rh = DEFAULT_REF
        basis = "default"
    measured = basis == "measured"
    seconds = secs * _scale(params or {}, rf, rw, rh, video_keys=_video_keys(workflow))
    cost = seconds / 3600.0 * PRICE_PER_HOUR + BOOT_COST
    return {
        "seconds": round(seconds, 1),
        "cost": round(cost, 2),
        "measured": measured,
        "basis": basis,
        "hint": (f"≈{seconds/60:.1f} 分钟 · ≈¥{cost:.2f}"
                 + ("" if measured else
                    ("（未实测，按同族基线估算）" if basis == "family"
                     else "（未实测，按默认基线估算）"))),
    }


def check_budget(workflow: dict, params: dict, budget: float | None) -> dict:
    """超预算抛 BudgetError；否则返回预估。"""
    est = estimate(workflow, params)
    limits = (workflow or {}).get("limits") or {}
    max_min = limits.get("max_minutes")
    if isinstance(max_min, (int, float)) and max_min > 0 and est["seconds"] > max_min * 60:
        raise BudgetError(
            f"预估 {est['seconds'] / 60:.1f} 分钟超过该工作流运行上限 {max_min:g} 分钟"
            f"（跑到上限会被判超时，实例时间照扣）。请调小帧数/分辨率，或先截短驱动视频。",
            est)
    if budget is not None and budget > 0 and est["cost"] > float(budget):
        raise BudgetError(
            f"预估 ¥{est['cost']:.2f} 超过预算上限 ¥{float(budget):.2f}（{est['hint']}）", est)
    return est


class BudgetError(Exception):
    def __init__(self, msg: str, est: dict):
        super().__init__(msg)
        self.est = est
