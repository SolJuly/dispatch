#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 freeze 单测：freezedetect 解析、尾段收口、门禁、降级。全部 ¥0（合成样片，不落仓库）。"""
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

from tools.p2 import freeze as fz  # noqa: E402
from tools.p1 import verify as v  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None


def _make(src: str, dst: Path, dur: float = 2.0, size: str = "320x240", fps: int = 24):
    # lavfi 语法：filter 名与选项用 `=` 分隔，选项之间用 `:`。
    # src 可能已带 `c=0x3355aa`（color），也可能只是裸名（testsrc2），这里统一拼接。
    opts = f"s={size}:r={fps}:d={dur}"
    expr = f"{src}:{opts}" if "=" in src else f"{src}={opts}"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi",
                    "-i", expr, "-pix_fmt", "yuv420p", str(dst)], check=True)


def _clip(tmp: Path, name: str, spec: str, dur: float = 2.0) -> Path:
    p = tmp / name
    _make(spec, p, dur)
    return p


def _freeze_then_move(tmp: Path) -> Path:
    p = tmp / "fin.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "color=c=0x3355aa:s=320x240:r=24:d=1",
        "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


class FreezeStatsTest(unittest.TestCase):
    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg 不可用")
    def test_static_tail_no_end(self):
        with tempfile.TemporaryDirectory() as td:
            p = _clip(Path(td), "static.mp4", "color=c=0x3355aa")
            s = fz.freeze_stats(p)
            self.assertTrue(s["ok"], s.get("error"))
            self.assertAlmostEqual(s["ratio"], 1.0, places=2)
            self.assertAlmostEqual(s["longest_s"], 2.0, places=1)
            self.assertEqual(len(s["events"]), 1)

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg 不可用")
    def test_moving_no_false_positive(self):
        with tempfile.TemporaryDirectory() as td:
            p = _clip(Path(td), "moving.mp4", "testsrc2")
            s = fz.freeze_stats(p)
            self.assertTrue(s["ok"], s.get("error"))
            self.assertEqual(s["ratio"], 0.0)
            self.assertGreater(s["ydif_max"], 0.0)   # 内容在动

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg 不可用")
    def test_freeze_then_move_pairing(self):
        with tempfile.TemporaryDirectory() as td:
            p = _freeze_then_move(Path(td))
            s = fz.freeze_stats(p)
            self.assertTrue(s["ok"], s.get("error"))
            self.assertAlmostEqual(s["ratio"], 0.5, places=2)
            self.assertEqual(len(s["events"]), 1)
            self.assertAlmostEqual(s["longest_s"], 1.0, places=2)


class GateTest(unittest.TestCase):
    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg 不可用")
    def test_gate_moving_pass_static_fail(self):
        job = {"params": {}}
        expect = {"freeze": {"ratio_max": 0.2, "longest_max_s": 1.0}}
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mv = _clip(td, "moving.mp4", "testsrc2")
            r_mv = v.verify_file(mv, expect, job, None, "video")
            self.assertIs(r_mv["checks"]["freeze"], True)
            self.assertEqual(r_mv["verdict"], "PASS")
            st = _clip(td, "static.mp4", "color=c=0x3355aa")
            r_st = v.verify_file(st, expect, job, None, "video")
            self.assertIs(r_st["checks"]["freeze"], False)
            self.assertEqual(r_st["verdict"], "FAIL")
            self.assertIn("freeze", r_st["failed"])

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg 不可用")
    def test_legacy_no_expect_freeze_warns_only(self):
        job = {"params": {}}
        expect = {}   # legacy：无 freeze 门禁
        with tempfile.TemporaryDirectory() as td:
            p = _clip(Path(td), "static.mp4", "color=c=0x3355aa")
            r = v.verify_file(p, expect, job, None, "video")
            self.assertIsNone(r["checks"].get("freeze"))
            # 冻结告警会出现（ratio=1.0 > 0.20），但不改 verdict 语义
            self.assertTrue(any("冻结" in w for w in r["warnings"]))


class DegradeTest(unittest.TestCase):
    def test_no_ffmpeg_strict_fail(self):
        orig = fz._run
        fz._run = lambda *a, **k: None          # 模拟 ffmpeg 不可用
        try:
            s = fz.freeze_stats(Path("/nonexistent.mp4"))
            self.assertFalse(s["ok"])
            self.assertIsNotNone(s.get("error"))
            ok, detail = fz.verdict(s, 0.2, 1.0)
            self.assertFalse(ok)
            self.assertTrue(detail.get("strict"))
        finally:
            fz._run = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
