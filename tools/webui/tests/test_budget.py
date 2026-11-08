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
