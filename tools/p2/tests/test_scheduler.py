#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 调度器单测（¥0，临时 DB + monkeypatch，不建机、不联网）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import jobqueue as queue  # noqa: E402
from tools.p2 import scheduler as sch  # noqa: E402


def H(c):
    return "sha256:" + c * 64


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self._orig_db = queue.DB
        self._td = tempfile.TemporaryDirectory()
        queue.DB = Path(self._td.name) / "queue.db"
        queue.create_batch("b-ok", "ok", 2, 100.0)
        self._orig_quota = sch.Scheduler._quota_left
        self._orig_spawn = sch.Scheduler._spawn
        self._spawned: list[str] = []

        sch.Scheduler._quota_left = lambda self: 5
        sch.Scheduler._spawn = lambda self, item: self._spawned.append(item["job_id"])
        # 不碰平台
        sch.reclaim_orphans = lambda dry_run=False: []

    def tearDown(self):
        sch.Scheduler._quota_left = self._orig_quota
        sch.Scheduler._spawn = self._orig_spawn
        queue.DB = self._orig_db
        self._td.cleanup()

    def test_dry_run_does_not_claim(self):
        queue.submit("b-ok", "j1", H("a"))
        q = queue.items()
        s = sch.Scheduler(dry_run=True)
        n = s.once()
        self.assertEqual(n, 0)
        self.assertEqual(self._spawned, [])
        self.assertEqual({i["job_id"]: i for i in queue.items()}["j1"]["state"], "queued")

    def test_batch_budget_blocks_and_releases(self):
        queue.create_batch("b-tight", "tight", 2, 0.01)      # 批预算 1 分
        queue.submit("b-tight", "j1", H("b"), est_cost=1.0)  # 在途估算 ¥1 → 超预算
        s = sch.Scheduler(dry_run=False)
        n = s.once()
        self.assertEqual(n, 0)
        self.assertEqual(self._spawned, [])
        row = {i["job_id"]: i for i in queue.items()}["j1"]
        self.assertEqual(row["state"], "queued")             # 退回队列
        self.assertEqual(row["attempts"], 0)                 # 不消耗重试次数

    def test_finish_does_not_resurrect_cancelled(self):
        queue.submit("b-ok", "j1", H("c"))
        queue.claim("w1")
        queue.cancel("j1", "test")
        queue.finish("j1", True, 0.1, instance_id="uhost-x", exit_code=0)
        self.assertEqual({i["job_id"]: i for i in queue.items()}["j1"]["state"], "cancelled")

    def test_cancel_then_scheduler_kills_child(self):
        class FakeProc:
            def __init__(self):
                self.killed = False

            def kill(self):
                self.killed = True

        queue.submit("b-ok", "j1", H("d"))
        queue.claim("w1")
        p = FakeProc()
        s = sch.Scheduler(dry_run=False)
        s.children = {"j1": p}
        queue.cancel("j1", "test")
        s._kill_cancelled_children()
        self.assertTrue(p.killed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
