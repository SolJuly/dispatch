# -*- coding: utf-8 -*-
"""视频预算：预估与超限拒绝。"""
from __future__ import annotations

import unittest

from webui import api, budget


class TestEstimate(unittest.TestCase):
    def test_measured_video(self):
        wf = {"key": "h3_t2v_121f"}
        est = budget.estimate(wf, {"frames": 121, "width": 768, "height": 512})
        self.assertTrue(est["measured"])
        self.assertGreater(est["cost"], 0.1)
        self.assertLess(est["cost"], 1.0)

    def test_unknown_falls_back(self):
        est = budget.estimate({"key": "no_such_wf"}, {})
        self.assertFalse(est["measured"])
        self.assertGreater(est["cost"], 0)

    def test_scaling_by_frames(self):
        a = budget.estimate({"key": "h3_t2v_121f"}, {"frames": 61, "width": 768, "height": 512})
        b = budget.estimate({"key": "h3_t2v_121f"}, {"frames": 121, "width": 768, "height": 512})
        self.assertLess(a["cost"], b["cost"])


class TestBudgetGate(unittest.TestCase):
    def test_reject_over_budget(self):
        with self.assertRaises(api.ApiError) as cm:
            api.create_job("h3_t2v_121f",
                           {"prompt": "x", "seed": 1, "width": 768, "height": 512,
                            "frames": 121, "steps": 20, "cfg": 1.0},
                           budget=0.01)
        self.assertEqual(cm.exception.code, 402)
        self.assertIn("预算", cm.exception.msg)


if __name__ == "__main__":
    unittest.main()


class TestTrimAffectsEstimate(unittest.TestCase):
    """trim_seconds 必须影响估价。

    回归：budget._scale 完全没读 trim_seconds，导致截到 4 秒的作业
    仍按整条视频报价（实测 trim=4 与 trim=0 报同一个价 ¥0.80）。
    用户会以为这个省钱开关没用，然后不设它 → 白烧 4 倍的钱。
    """

    def setUp(self):
        from tools.p20 import registry
        self.budget = __import__("webui.budget", fromlist=["x"])
        self.wf = registry.get_workflow("pack_video_wan_animate2_distilled")

    def test_trim_reduces_estimate(self):
        b, wf = self.budget, self.wf
        p0 = b.estimate(wf, {"frames": 81, "image_2": "in-22a23224.mp4", "trim_seconds": 0})
        p4 = b.estimate(wf, {"frames": 81, "image_2": "in-22a23224.mp4", "trim_seconds": 4})
        self.assertLess(p4["seconds"], p0["seconds"], "trim=4 应比 trim=0 便宜")

    def test_trim_monotonic(self):
        """trim 越大越贵（线性）。"""
        b, wf = self.budget, self.wf
        prev = 0
        for t in (1, 2, 4, 8):
            r = b.estimate(wf, {"frames": 81, "image_2": "in-22a23224.mp4", "trim_seconds": t})
            self.assertGreaterEqual(r["seconds"], prev, f"trim={t} 应 >= 前一个")
            prev = r["seconds"]

    def test_trim_capped_by_source_length(self):
        """trim 超过源视频长度时，按源视频算（不会凭空变长）。"""
        b, wf = self.budget, self.wf
        r = b.estimate(wf, {"frames": 81, "image_2": "in-22a23224.mp4", "trim_seconds": 999})
        # 源视频 256 帧 @16fps = 16 秒，trim=999 不应超过整条
        r0 = b.estimate(wf, {"frames": 81, "image_2": "in-22a23224.mp4", "trim_seconds": 0})
        self.assertLessEqual(r["seconds"], r0["seconds"])


class TestMeasuredBaselines(unittest.TestCase):
    """MEASURED 的参考帧数必须与耗时对应同一个工作量。

    回归：收割脚本 import webui 失败但异常被静默吞掉，
    参考帧退化成 params.frames（动作迁移的 81 只是单轮帧数），
    而实际驱动视频只有 64 帧 —— 基线被算低 27%。
    """

    def test_reference_frames_are_present(self):
        from webui import budget
        for k, v in budget.MEASURED.items():
            with self.subTest(key=k):
                secs, frames, w, h = v
                self.assertGreater(secs, 0, f"{k} 耗时应为正")
                self.assertGreater(frames, 0, f"{k} 参考帧应 > 0")
                self.assertGreater(w, 0, f"{k} 参考宽应 > 0（0 会让缩放恒为 1）")
                self.assertGreater(h, 0, f"{k} 参考高应 > 0")

    def test_animate2_distilled_uses_driver_frames(self):
        """动作迁移的参考帧应是驱动视频帧数（64），不是 params.frames（81）。"""
        from webui import budget
        v = budget.MEASURED.get("pack_video_wan_animate2_distilled")
        self.assertIsNotNone(v)
        self.assertEqual(v[1], 64, f"参考帧应为驱动视频的 64 帧，实际 {v[1]}")
