#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：单任务锁排队 + 队列执行器（不花钱，纯本地）。

覆盖事故场景：控制台连点几个作业，第 2 个 run.py 拿不到 .job.lock 就直接退出，
作业永远停在 CREATED。现在：
  · JobLock(wait=N) 会排队等锁，超时才抛 JobLocked
  · queue.py 能把遗留的 CREATED 作业按顺序跑掉

用法：python3 tools/p1/tests/test_lock_queue.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import p1queue as queue_mod  # noqa: E402
from tools.p1 import store  # noqa: E402


class TestJobLockWait(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="p1-lock-"))
        self.path = self.dir / ".job.lock"

    def test_nonblocking_still_raises(self):
        with store.JobLock(self.path):
            with self.assertRaises(store.JobLocked):
                with store.JobLock(self.path):
                    pass

    def test_wait_gets_lock_after_release(self):
        holder = store.JobLock(self.path)
        holder.__enter__()
        got: list[str] = []

        def waiter():
            with store.JobLock(self.path, wait=10):
                got.append("in")

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(1.0)
        self.assertEqual(got, [], "锁还在别人手里，不该进")
        holder.__exit__()
        t.join(timeout=10)
        self.assertEqual(got, ["in"])

    def test_wait_times_out(self):
        with store.JobLock(self.path):
            t0 = time.monotonic()
            with self.assertRaises(store.JobLocked):
                with store.JobLock(self.path, wait=1):
                    pass
            self.assertGreaterEqual(time.monotonic() - t0, 0.9)


class TestQueuePending(unittest.TestCase):
    def test_pending_filters_state_and_since(self):
        orig_jobs = store.JOBS_DIR
        orig_read = store.read_json
        orig_list = store.list_jobs
        tmp = Path(tempfile.mkdtemp(prefix="p1-queue-"))
        jobs = {
            "p1-20261107T0408-aaaaaa": {"state": "CREATED", "created_at": "2026-11-07T04:08:16+08:00"},
            "p1-20261105T0937-bbbbbb": {"state": "CREATED", "created_at": "2026-11-05T09:37:56+08:00"},
            "p1-20261107T0406-cccccc": {"state": "DONE", "created_at": "2026-11-07T04:06:25+08:00"},
        }
        try:
            store.JOBS_DIR = tmp
            store.read_json = lambda p, default=None: jobs.get(Path(p).parent.name, default)  # type: ignore
            store.list_jobs = lambda: sorted(jobs)  # type: ignore
            self.assertEqual([j for j, _ in queue_mod.pending()],
                             ["p1-20261105T0937-bbbbbb", "p1-20261107T0408-aaaaaa"])
            self.assertEqual([j for j, _ in queue_mod.pending(since="2026-11-07")],
                             ["p1-20261107T0408-aaaaaa"])
        finally:
            store.JOBS_DIR = orig_jobs
            store.read_json = orig_read  # type: ignore
            store.list_jobs = orig_list  # type: ignore


if __name__ == "__main__":
    unittest.main(verbosity=2)
