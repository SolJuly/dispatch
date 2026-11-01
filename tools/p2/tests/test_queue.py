#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 队列单测（¥0，临时 SQLite，不碰真实 queue.db）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import jobqueue as queue  # noqa: E402


class QueueTest(unittest.TestCase):
    def setUp(self):
        self._orig_db = queue.DB
        self._td = tempfile.TemporaryDirectory()
        queue.DB = Path(self._td.name) / "queue.db"
        queue.create_batch("b-1", "test", 2, 1.0)

    def tearDown(self):
        queue.DB = self._orig_db
        self._td.cleanup()

    def test_submit_and_dedupe(self):
        ok, _ = queue.submit("b-1", "j1", "sha256:" + "a" * 64)
        self.assertTrue(ok)
        ok2, why = queue.submit("b-1", "j2", "sha256:" + "a" * 64)   # 同参数
        self.assertFalse(ok2)
        self.assertIn("同参数", why)
        self.assertEqual(queue.stats()["total"], 1)

    def test_claim_atomic(self):
        queue.submit("b-1", "j1", "sha256:" + "b" * 64)
        a = queue.claim("w1")
        b = queue.claim("w2")
        self.assertIsNotNone(a)
        self.assertIsNone(b)          # 同一条不会被领两次
        self.assertEqual(a["state"], "claimed")

    def test_finish_and_stats(self):
        queue.submit("b-1", "j1", "sha256:" + "c" * 64)
        queue.claim("w1")
        queue.mark_running("j1")
        queue.finish("j1", True, 0.12)
        s = queue.stats()
        self.assertEqual(s["by_state"]["done"]["n"], 1)
        self.assertAlmostEqual(s["by_state"]["done"]["cost"], 0.12, places=3)

    def test_requeue_until_attempts(self):
        queue.submit("b-1", "j1", "sha256:" + "d" * 64, max_attempts=2)
        self.assertIsNotNone(queue.claim("w1"))     # attempts=1
        self.assertTrue(queue.requeue("j1", "нет stock"))   # 回 queued
        self.assertIsNotNone(queue.claim("w1"))     # attempts=2
        self.assertFalse(queue.requeue("j1", "still"))      # 到上限 → failed
        self.assertEqual({i["job_id"]: i["state"] for i in queue.items()}["j1"], "failed")

    def test_reset_stale(self):
        queue.submit("b-1", "j1", "sha256:" + "e" * 64)
        queue.claim("w1", lease=-5)                 # 租约已过期
        out = queue.reset_stale()
        self.assertIn("j1:requeued", out)
        self.assertEqual({i["job_id"]: i["state"] for i in queue.items()}["j1"], "queued")


if __name__ == "__main__":
    unittest.main(verbosity=2)
