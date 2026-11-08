#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 冻结/运动检测：用 ffmpeg freezedetect 做内容级静止判定，替代 P1 的亮度均值差。

背景（reports/p2/P2-任务执行计划.md §3）：
  P1 用 signalstats.YAVG 的窗口最大差判「几乎静止」，但 YAVG 是平均亮度，
  与画面内容无关 —— 画面里持续运动而曝光稳定时，YAVG 差接近 0，误报静止。
  freezedetect 是逐像素帧差（tolerance n + 最短时长 d），不吃这个亏。

一次解码同时拿到两类指标（freezedetect + signalstats 链在同一 -vf 里）：
  freeze_ratio   冻结时长占比（0~1）
  freeze_longest 单次冻结最长秒数
  freeze_events  每段冻结的 start/end/duration
  ydif_max       窗口内 signalstats.YDIF 最大值（内容变化强度，比 YAVG 差贴近内容）

坑（已处理）：
  - 冻结到片尾时 ffmpeg 只输出 freeze_start、不输出 freeze_end/duration，
    必须用 总时长 - start 收口，否则「整片静止」会被算成 0 秒冻结。
  - ffmpeg 版本差异：用宽松正则，不依赖键名外的东西。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# freezedetect 参数：n = 帧差容忍度（噪声容差），d = 判定为冻结的最短秒数
FREEZE_NOISE = "0.003"
FREEZE_MIN_DUR = 0.5
LIMIT_FRAMES = 400          # 超长片只取前 N 帧，避免单文件解码拖死

_RE_FREEZE = re.compile(r"lavfi\.freezedetect\.(freeze_start|freeze_end|freeze_duration)=([\d.]+)")
_RE_YDIF = re.compile(r"lavfi\.signalstats\.YDIF=([\d.]+)")


def _run(cmd, timeout=240):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def ffprobe_duration(path: Path) -> float | None:
    """读总时长（秒），用于尾段收口与 ratio 归一。"""
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", str(path)], timeout=60)
    if not r or r.returncode != 0:
        return None
    try:
        return round(float((r.stdout or "").strip().splitlines()[-1]), 3)
    except (ValueError, IndexError):
        return None


def _parse_events(text: str, clip_dur: float | None) -> tuple[list[dict], float | None, float | None]:
    """把 freezedetect 输出解析成冻结段列表。

    事件按 freeze_start/freeze_end/freeze_duration 顺序出现在时间线上：
      freeze_start=X  开始一段冻结
      freeze_end=Y    结束一段（可能紧跟 freeze_duration=Z）
    未闭合的 freeze_start 用 clip_dur 收口（尾段冻结）。
    返回 (events, freeze_ratio, freeze_longest)。
    """
    events: list[dict] = []
    open_start: float | None = None

    for m in _RE_FREEZE.finditer(text):
        kind, val_s = m.group(1), m.group(2)
        try:
            val = float(val_s)
        except ValueError:
            continue
        if kind == "freeze_start":
            open_start = val
        elif kind == "freeze_end":
            if open_start is not None:
                events.append({"start": open_start, "end": val, "duration": round(val - open_start, 3)})
                open_start = None

    # 尾段收口：有 freeze_start 无 freeze_end
    if open_start is not None and clip_dur is not None and clip_dur > open_start:
        events.append({"start": open_start, "end": clip_dur, "duration": round(clip_dur - open_start, 3)})

    if not events:
        return [], 0.0, 0.0
    longest = max(e["duration"] for e in events)
    total = sum(e["duration"] for e in events)
    ratio = round(min(total / clip_dur, 1.0), 4) if clip_dur and clip_dur > 0 else None
    return events, ratio, round(longest, 3)


def freeze_stats(path: Path, limit_frames: int = LIMIT_FRAMES) -> dict:
    """一次解码返回冻结 + YDIF 指标。失败时 ok=False 且不抛异常。"""
    out: dict = {
        "ok": False, "ratio": None, "longest_s": None, "events": [],
        "ydif_max": None, "duration": None, "partial": False, "error": None,
    }
    clip_dur = ffprobe_duration(path)
    out["duration"] = clip_dur

    r = _run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", str(limit_frames),
              "-vf", f"freezedetect=n={FREEZE_NOISE}:d={FREEZE_MIN_DUR},signalstats,metadata=print:file=-",
              "-f", "null", "-"], timeout=240)
    if r is None:
        out["error"] = "ffmpeg 不可用或超时"
        return out
    text = r.stdout or ""
    if r.returncode != 0 and not text:
        out["error"] = (r.stderr or "").strip()[:200] or f"ffmpeg rc={r.returncode}"
        return out

    events, ratio, longest = _parse_events(text, clip_dur)
    out["events"] = events
    out["ratio"] = ratio
    out["longest_s"] = longest

    ydifs = [float(m.group(1)) for m in _RE_YDIF.finditer(text)]
    if ydifs:
        out["ydif_max"] = round(max(ydifs), 3)

    out["ok"] = True
    return out


def is_full_freeze(stats: dict) -> bool | None:
    """是否「整片冻结/近乎静止」——素材驱动类唯一要硬拦的情况。

    依据标定报告（reports/p2/freeze-calibration.md）：坏样本 shot1/shot2 是
    ratio=1.0 / longest≈5.06s 的全片冻结，必须拦；而静态素材产出的「部分静止」
    （如 Animate2 站姿源图 ratio=0.716）是合法结果，不该判 FAIL。

    判据：ratio >= 0.95 且最长冻结段 >= 片长的 80%。测量失败返回 None（不臆断）。
    """
    if not stats.get("ok"):
        return None
    ratio, longest, dur = stats.get("ratio"), stats.get("longest_s"), stats.get("duration")
    if ratio is None or longest is None:
        return None
    if ratio >= 0.95:
        return True
    if dur and dur > 0 and longest / dur >= 0.8:
        return True
    return False


def verdict(stats: dict, ratio_max: float, longest_max_s: float,
            mode: str | None = None) -> tuple[bool | None, dict]:
    """门禁判定：ratio 与 longest 都达标才 PASS。

    - 无 expect.freeze → 返回 (None, ...) 表示不参与 verdict（legacy 作业）。
    - 有阈值但测量失败（ok=False）→ 严格口径视为 FAIL（测不了就不能放行）。
    - mode == "full_freeze_only"（素材驱动类）→ 只拦 is_full_freeze，
      中度静止降级为 details/warnings，不参与 PASS/FAIL。
      见 render.expect_for 的分档与 freeze-calibration.md。
    """
    if not stats.get("ok"):
        return False, {"error": stats.get("error") or "freeze 指标不可用", "strict": True}
    ratio = stats.get("ratio")
    longest = stats.get("longest_s")
    if ratio is None or longest is None:
        return False, {"error": "freeze 指标缺 ratio/longest", "strict": True}
    if mode == "full_freeze_only":
        full = is_full_freeze(stats)
        if full is None:
            return False, {"error": "全片冻结判定不可用", "strict": True}
        return (not full), {
            "mode": mode, "full_freeze": full,
            "ratio": ratio, "longest_s": longest,
            "events": len(stats.get("events") or []),
            "ydif_max": stats.get("ydif_max"),
            "downgraded": True,
        }
    ok_ratio = ratio <= float(ratio_max)
    ok_long = longest <= float(longest_max_s)
    return bool(ok_ratio and ok_long), {
        "ratio": ratio, "ratio_max": float(ratio_max),
        "longest_s": longest, "longest_max_s": float(longest_max_s),
        "events": len(stats.get("events") or []),
        "ydif_max": stats.get("ydif_max"),
    }


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        s = freeze_stats(Path(p))
        print(f"{p}\n  ok={s['ok']} ratio={s['ratio']} longest={s['longest_s']}s "
              f"ydif_max={s['ydif_max']} events={len(s['events'])} dur={s['duration']}")
        if s["error"]:
            print(f"  error={s['error']}")
