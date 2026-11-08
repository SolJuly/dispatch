# -*- coding: utf-8 -*-
"""p20ctl 小工具函数：job_id 解析（jobctl 会先打印历史同参数作业）。"""
from __future__ import annotations

import unittest

from tools.p20 import p20ctl


class TestPickJobId(unittest.TestCase):
    def test_picks_last_when_warning_mentions_old_job(self):
        out = ("⚠ 同参数作业尚未完成：p1-20261103T1245-a47eee（算一次新尝试）\n"
               "✓ 作业已建：p1-20261103T1259-551e20\n")
        self.assertEqual(p20ctl._pick_job_id(out), "p1-20261103T1259-551e20")

    def test_single_id(self):
        self.assertEqual(p20ctl._pick_job_id("✓ 作业已建：p1-20261101T2121-c82708\n"),
                         "p1-20261101T2121-c82708")

    def test_none(self):
        self.assertEqual(p20ctl._pick_job_id("no id here"), "")

    def test_bad_format_ignored(self):
        self.assertEqual(p20ctl._pick_job_id("p1-2026-x 不是合法 id"), "")


if __name__ == "__main__":
    unittest.main()
