#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 队列单测（P0 幂等/心跳 + P1 退避/审计）：¥0，临时 SQLite。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import jobqueue as queue  # noqa: E402


def P(c):
    return "sha256:" + c * 64


class QueueTest(unittest.TestCase):
    def setUp(self):
        self._orig_db = queue.DB
        self._td = tempfile.TemporaryDirectory()
        queue.DB = Path(self._td.name) / "queue.db"
        queue.create_batch("b-1", "test", 2, 1.0)

    def tearDown(self):
        queue.DB = self._orig_db
        self._td.cleanup()

    def _force_ready(self, jid):
        c = queue.connect()
        try:
            c.execute("UPDATE item SET next_retry_at=0 WHERE job_id=?", (jid,))
        finally:
            c.close()

    # ---------------- P0-1 幂等
    def test_duplicate_job_id_rejected(self):
        ok, _ = queue.submit("b-1", "j1", P("a"))
        self.assertTrue(ok)
        ok2, why = queue.submit("b-1", "j1", P("b"))      # 同 job_id
        self.assertFalse(ok2)
        self.assertIn("幂等", why)
        self.assertEqual(queue.stats()["total"], 1)

    def test_duplicate_params_active_rejected(self):
        queue.submit("b-1", "j1", P("a"))
        ok, why = queue.submit("b-1", "j2", P("a"))       # 同参数
        self.assertFalse(ok)
        self.assertIn("同参数", why)

    # ---------------- 领取
    def test_claim_atomic(self):
        queue.submit("b-1", "j1", P("b"))
        self.assertIsNotNone(queue.claim("w1"))
        self.assertIsNone(queue.claim("w2"))

    # ---------------- P1-4 退避 + 上限
    def test_backoff_ladder_and_terminal(self):
        queue.submit("b-1", "j1", P("c"), max_attempts=3)
        it = queue.claim("w1")
        self.assertEqual(it["attempts"], 1)
        self.assertTrue(queue.requeue("j1", "fail-1"))
        row = {i["job_id"]: i for i in queue.items()}["j1"]
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["backoff_s"], 1)
        self.assertIsNone(queue.claim("w1"))              # 退避未到期
        self._force_ready("j1")
        it = queue.claim("w1")
        self.assertEqual(it["attempts"], 2)
        self.assertTrue(queue.requeue("j1", "fail-2"))
        self.assertEqual({i["job_id"]: i for i in queue.items()}["j1"]["backoff_s"], 5)
        self._force_ready("j1")
        it = queue.claim("w1")
        self.assertEqual(it["attempts"], 3)
        self.assertFalse(queue.requeue("j1", "fail-3"))   # 到上限
        self.assertEqual({i["job_id"]: i for i in queue.items()}["j1"]["state"], "terminal_failed")

    # ---------------- 瞬时限流不消耗尝试（defer）
    def test_defer_does_not_consume_attempts(self):
        queue.submit("b-1", "j1", P("g"), max_attempts=3)
        queue.claim("w1")
        self.assertTrue(queue.defer("j1", 60, "无现货"))
        row = {i["job_id"]: i for i in queue.items()}["j1"]
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempts"], 0)          # 不消耗
        self.assertEqual(row["defers"], 1)
        self.assertIsNone(queue.claim("w1"))          # 延迟未到期

    # ---------------- terminal_failed 也要有 exit_code
    def test_terminal_failed_records_exit_code(self):
        queue.submit("b-1", "j1", P("h"), max_attempts=1)
        queue.claim("w1")
        self.assertFalse(queue.requeue("j1", "boom", exit_code=137))
        row = {i["job_id"]: i for i in queue.items()}["j1"]
        self.assertEqual(row["state"], "terminal_failed")
        self.assertEqual(row["exit_code"], 137)
        tf = next(r for r in queue.audit_rows("j1") if r["to_state"] == "terminal_failed")
        self.assertEqual(tf["exit_code"], 137)

    # ---------------- 重试也要累加成本（否则终态失败的成本永远是 0）
    def test_requeue_accumulates_cost(self):
        queue.submit("b-1", "j1", P("i"), max_attempts=2)
        queue.claim("w1")
        self.assertTrue(queue.requeue("j1", "boom-1", exit_code=1, gpu_minutes=2.0, cost=0.07))
        row = {i["job_id"]: i for i in queue.items()}["j1"]
        self.assertAlmostEqual(row["cost"], 0.07, places=3)
        self.assertAlmostEqual(row["gpu_minutes"], 2.0, places=2)
        self._force_ready("j1")
        queue.claim("w1")
        self.assertFalse(queue.requeue("j1", "boom-2", exit_code=1, gpu_minutes=2.5, cost=0.09))
        row = {i["job_id"]: i for i in queue.items()}["j1"]
        self.assertEqual(row["state"], "terminal_failed")
        self.assertAlmostEqual(row["cost"], 0.16, places=3)
        self.assertAlmostEqual(row["gpu_minutes"], 4.5, places=2)

    # ---------------- P0-2 心跳超时
    def test_heartbeat_timeout_requeues(self):
        queue.submit("b-1", "j1", P("d"))
        queue.claim("w1")
        c = queue.connect()
        try:
            c.execute("UPDATE item SET heartbeat_at=? WHERE job_id=?", (int(time.time()) - 120, "j1"))
        finally:
            c.close()
        out = queue.reset_stale(60)
        self.assertIn("j1:requeued", out)
        self.assertEqual({i["job_id"]: i for i in queue.items()}["j1"]["state"], "queued")

    def test_heartbeat_keeps_alive(self):
        queue.submit("b-1", "j1", P("e"))
        queue.claim("w1")
        queue.heartbeat("j1")
        self.assertEqual(queue.reset_stale(60), [])       # 刚打过点，不算超时

    # ---------------- P0-3 审计
    def test_audit_records_exit_and_gpu(self):
        queue.submit("b-1", "j1", P("f"))
        queue.claim("w1")
        queue.mark_running("j1")
        queue.finish("j1", True, 0.12, instance_id="uhost-x", exit_code=0, gpu_minutes=2.5)
        rows = queue.audit_rows("j1")
        to_states = [r["to_state"] for r in rows]
        self.assertIn("queued", to_states)
        self.assertIn("running", to_states)
        done = next(r for r in rows if r["to_state"] == "done")
        self.assertEqual(done["instance_id"], "uhost-x")
        self.assertEqual(done["exit_code"], 0)
        self.assertAlmostEqual(done["gpu_minutes"], 2.5, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
