#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频质量客观指标：不靠肉眼，先把能算的算出来。

指标：
  frames/fps/duration  容器层是否与预期一致（查「时间轴被压缩」）
  black_ratio          YAVG<16 的帧占比（黑帧/纯黑）
  yavg_std             亮度序列标准差（闪烁/曝光抖动，越小越稳）
  frame_diff           相邻帧平均绝对差（运动强度代理，骤变=跳帧）
  color_drift          首末帧 YUV 均值差（链式拼接的色偏累积）
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
from pathlib import Path


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180)


def probe(p: Path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=nb_frames,r_frame_rate,duration,width,height,codec_name",
              "-of", "json", str(p)])
    try:
        st = json.loads(r.stdout)["streams"][0]
    except Exception:
        return {}
    fr = st.get("r_frame_rate", "0/1")
    try:
        num, den = fr.split("/")
        fps = round(float(num) / float(den), 3) if float(den) else 0.0
    except Exception:
        fps = 0.0
    return {"frames": int(st.get("nb_frames") or 0), "fps": fps,
            "duration": round(float(st.get("duration") or 0), 3),
            "w": st.get("width"), "h": st.get("height"), "codec": st.get("codec_name")}


def yavg(p: Path) -> list[float]:
    r = _run(["ffprobe", "-v", "error", "-f", "lavfi",
              "-i", f"movie={p},signalstats",
              "-show_entries", "frame_tags=lavfi.signalstats.YAVG",
              "-of", "csv=p=0"])
    out = []
    for line in r.stdout.splitlines():
        line = line.strip().strip(",")
        if not line:
            continue
        try:
            out.append(float(line.split(",")[-1]))
        except ValueError:
            pass
    return out


def frame_means(p: Path) -> list[tuple[float, float, float]]:
    """每帧的 Y/U/V 均值，用 signalstats。"""
    r = _run(["ffprobe", "-v", "error", "-f", "lavfi",
              "-i", f"movie={p},signalstats",
              "-show_entries", "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.UAVG,lavfi.signalstats.VAVG",
              "-of", "csv=p=0"])
    out = []
    for line in r.stdout.splitlines():
        parts = [x for x in line.strip().split(",") if x]
        if len(parts) >= 3:
            try:
                out.append((float(parts[-3]), float(parts[-2]), float(parts[-1])))
            except ValueError:
                pass
    return out


def analyze(p: Path) -> dict:
    pr = probe(p)
    ys = yavg(p)
    fm = frame_means(p)
    d = {"file": p.name, "bytes": p.stat().st_size, **pr}
    if ys:
        d["yavg_mean"] = round(statistics.fmean(ys), 2)
        d["yavg_std"] = round(statistics.pstdev(ys), 3) if len(ys) > 1 else 0.0
        d["black_ratio"] = round(sum(1 for y in ys if y < 16) / len(ys), 3)
        d["frame_diff"] = round(statistics.fmean(abs(a - b) for a, b in zip(ys, ys[1:])), 3) if len(ys) > 1 else 0.0
        d["yavg_range"] = round(max(ys) - min(ys), 2)
    if fm:
        first, last = fm[0], fm[-1]
        d["color_drift"] = round(sum(abs(a - b) for a, b in zip(first, last)), 2)
        d["first_yuv"] = [round(x, 1) for x in first]
        d["last_yuv"] = [round(x, 1) for x in last]
    return d


def main():
    paths = [Path(x) for x in sys.argv[1:]]
    rows = [analyze(p) for p in paths if p.exists()]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
