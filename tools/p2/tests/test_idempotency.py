#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 幂等矩阵测试（¥0，临时 DB）：重复 job_id / 重复参数 / 并发双提。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import jobqueue as queue  # noqa: E402


def H(c):
    return "sha256:" + c * 64


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self._orig = queue.DB
        self._td = tempfile.TemporaryDirectory()
        queue.DB = Path(self._td.name) / "queue.db"
        queue.create_batch("b", "test", 2, 10.0)

    def tearDown(self):
        queue.DB = self._orig
        self._td.cleanup()

    def test_same_job_id_active_blocked(self):
        self.assertTrue(queue.submit("b", "j1", H("a"))[0])
        ok, why = queue.submit("b", "j1", H("b"))
        self.assertFalse(ok)
        self.assertIn("幂等", why)
        self.assertEqual(len(queue.items()), 1)

    def test_same_params_active_blocked(self):
        self.assertTrue(queue.submit("b", "j1", H("a"))[0])
        ok, why = queue.submit("b", "j2", H("a"))
        self.assertFalse(ok)
        self.assertIn("同参数", why)
        self.assertEqual(len(queue.items()), 1)

    def test_same_job_id_after_done_blocked(self):
        queue.submit("b", "j1", H("a"))
        queue.claim("w1")
        queue.finish("j1", True, 0.0)
        ok, why = queue.submit("b", "j1", H("a"))
        self.assertFalse(ok)
        self.assertIn("已存在", why)

    def test_concurrent_submit_same_params(self):
        """两个线程同时提同一参数：只能成功一个（唯一索引兜底）。"""
        res = []

        def go(i):
            res.append(queue.submit("b", f"j{i}", H("z"))[0])

        ts = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sum(1 for x in res if x), 1, f"应只有 1 个成功，实际 {res}")
        self.assertEqual(len(queue.items()), 1)

    def test_cancel_then_resubmit_same_params(self):
        """取消后同参数可以再提（不阻塞正常重试）。"""
        queue.submit("b", "j1", H("a"))
        queue.cancel("j1", "test")
        ok, why = queue.submit("b", "j2", H("a"))
        self.assertTrue(ok, why)


if __name__ == "__main__":
    unittest.main(verbosity=2)
