#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 拼接单测（¥0，ffmpeg 现造样片，不落仓库）。"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import concat  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _vid(dst: Path, dur=1.0, size="320x240", fps=24, src="testsrc2"):
    # lavfi：filter 名与选项用 `=` 分隔；src 已带 `key=val`（如 color=c=red）时改用 `:`
    expr = f"{src}:s={size}:r={fps}:d={dur}" if "=" in src else f"{src}=s={size}:r={fps}:d={dur}"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", expr,
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(dst)],
                   check=True)


def _img(dst: Path, color="red", size="64x48"):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s={size}",
                    "-frames:v", "1", str(dst)], check=True)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg 不可用")
class ConcatTest(unittest.TestCase):
    def test_same_params_copy(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            ins = []
            for i in range(3):
                p = td / f"a{i}.mp4"
                _vid(p, 1.0)
                ins.append(p)
            out = td / "long.mp4"
            res = concat.concat_videos(ins, out, mode="auto")
            self.assertTrue(res["ok"], res.get("reason"))
            self.assertEqual(res["mode"], "copy")          # 同参走无损
            self.assertAlmostEqual(res["duration"], 3.0, delta=0.2)

    def test_mixed_params_reencode(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            a = td / "a.mp4"; _vid(a, 1.0, fps=24)
            b = td / "b.mp4"; _vid(b, 1.0, fps=16)
            out = td / "mix.mp4"
            res = concat.concat_videos([a, b], out, mode="auto", fps=24, size="320x240")
            self.assertTrue(res["ok"], res.get("reason"))
            self.assertEqual(res["mode"], "encode")
            self.assertEqual(res["fps"], 24)
            self.assertAlmostEqual(res["duration"], 2.0, delta=0.25)

    def test_image_grid(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            imgs = []
            for i, c in enumerate(["red", "green", "blue", "yellow"]):
                p = td / f"{i}.png"; _img(p, c)
                imgs.append(p)
            out = td / "sheet.png"
            res = concat.concat_images(imgs, out, cols=2, gap=8)
            self.assertTrue(res["ok"], res.get("reason"))
            self.assertEqual(res["size"], [2 * 64 + 8, 2 * 48 + 8])   # 136x104

    def test_broken_input(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            good = td / "a.mp4"; _vid(good, 1.0)
            res = concat.concat_videos([good, td / "missing.mp4"], td / "o.mp4")
            self.assertFalse(res["ok"])
            self.assertIn("不存在", res.get("reason", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
