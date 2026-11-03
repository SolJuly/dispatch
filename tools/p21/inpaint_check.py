#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""局部重绘质量检查：蒙版外是否真的没动、蒙版内是否真的变了。

用 ffmpeg 解码成 raw RGB 再逐像素比，不依赖 PIL。

    python3 tools/p21/inpaint_check.py --input in.png --output out.png --mask mask.png

判定：
  · 蒙版外平均绝对差 outside_mean 越小越好（< 6/255 视为"基本没动"）
  · 蒙版内平均绝对差 inside_mean 应显著大于 outside（> 15/255 视为"确实重绘"）
  · 输出 JSON 供连测/验收引用
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def raw_rgb(path: Path) -> tuple[bytes, int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    info = json.loads(r.stdout or "{}")
    st = (info.get("streams") or [{}])[0]
    w, h = int(st.get("width")), int(st.get("height"))
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=180)
    if p.returncode != 0 or len(p.stdout) != w * h * 3:
        raise SystemExit(f"解码失败：{path}")
    return p.stdout, w, h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--mask", required=True)
    ap.add_argument("--threshold", type=float, default=6.0)
    a = ap.parse_args()

    a_px, w, h = raw_rgb(Path(a.input))
    b_px, w2, h2 = raw_rgb(Path(a.output))
    m_px, w3, h3 = raw_rgb(Path(a.mask))
    if not (w == w2 == w3 and h == h2 == h3):
        raise SystemExit(f"尺寸不一致：{w}x{h} / {w2}x{h2} / {w3}x{h3}")

    n = w * h
    in_sum = in_cnt = out_sum = out_cnt = 0
    max_out = 0
    for i in range(n):
        j = i * 3
        # 蒙版取红通道阈值 128（白=重绘）
        if m_px[j] >= 128:
            in_cnt += 1
            in_sum += (abs(a_px[j] - b_px[j]) + abs(a_px[j + 1] - b_px[j + 1])
                       + abs(a_px[j + 2] - b_px[j + 2])) / 3
        else:
            out_cnt += 1
            d = (abs(a_px[j] - b_px[j]) + abs(a_px[j + 1] - b_px[j + 1])
                 + abs(a_px[j + 2] - b_px[j + 2])) / 3
            out_sum += d
            max_out = max(max_out, d)

    inside = in_sum / in_cnt if in_cnt else 0.0
    outside = out_sum / out_cnt if out_cnt else 0.0
    res = {
        "size": [w, h],
        "mask_pixels": in_cnt,
        "inside_mean": round(inside, 2),
        "outside_mean": round(outside, 2),
        "outside_max": round(max_out, 1),
        "inside_changed": inside > 15,
        "outside_preserved": outside < a.threshold,
        "verdict": "PASS" if (inside > 15 and outside < a.threshold) else "FAIL",
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
