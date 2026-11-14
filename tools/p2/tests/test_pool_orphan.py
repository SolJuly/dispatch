#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预热池的过渡期重试与孤儿兜底测试。零网络、零费用。

覆盖 2026-11-14 真机踩到的费用泄漏：
  release() 撞 226609（"实例正在进行关机相关操作"）没有重试，直接把实例
  标成 dead —— 但云端仍在 Running，¥1.41/h 一直扣，池子再也不管它。

被测的三件事：
  1. _is_transient 认得 226609 / 8903 / "is in the task"，不误判别的东西
  2. _stop_with_retry / _terminate_with_retry 会重试可恢复错误、放弃不可恢复错误
  3. release 失败后标 orphan（而不是 dead），reconcile 见到 orphan 必须 terminate

用假 CS 对象，不碰真实 API。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import pool as P  # noqa: E402

# 测试里所有重试都不真睡 —— 12 个用例累计能省一分钟。
# 真实等待逻辑由 test_succeeds_after_transient 之类的「调用次数」断言覆盖，
# 不依赖墙上时间。
P.RETRY_TRANSIENT_WAIT = 0


class FakeCS:
    """最小 CS 替身：按预设脚本抛错/成功，并记录调用次数。"""

    def __init__(self, stop_script=None, term_script=None, instances=None):
        self.stop_calls = 0
        self.term_calls = 0
        self._stop = list(stop_script or [])
        self._term = list(term_script or [])
        self._instances = instances or []

    def stop(self, uid):
        self.stop_calls += 1
        if self._stop:
            act = self._stop.pop(0)
            if isinstance(act, Exception):
                raise act
        return None

    def terminate(self, uid):
        self.term_calls += 1
        if self._term:
            act = self._term.pop(0)
            if isinstance(act, Exception):
                raise act
        return None

    def wait_state(self, uid, states, timeout=0):
        return {"State": "Stopped"}

    def get(self, uid):
        return {"State": "Running"}

    def instances(self):
        return self._instances


class TransientDetectionTest(unittest.TestCase):
    def test_226609_is_transient(self):
        self.assertTrue(P._is_transient(
            Exception("StopCompShareInstance - 226609: 实例正在进行关机相关操作，请稍后再试。")))

    def test_8903_is_transient(self):
        self.assertTrue(P._is_transient(
            Exception("8903: UHost is in the task, please try again later")))

    def test_permanent_not_transient(self):
        for msg in ("Permission denied", "余额不足", "InvalidParameter", "NotFound"):
            self.assertFalse(P._is_transient(Exception(msg)), msg)

    def test_case_insensitive(self):
        self.assertTrue(P._is_transient(Exception("UHOST IS IN THE TASK")))


class StopRetryTest(unittest.TestCase):
    def test_succeeds_after_transient(self):
        err = Exception("226609: 实例正在进行关机相关操作")
        cs = FakeCS(stop_script=[err, err])          # 前两次失败，第三次成功
        P.RETRY_TRANSIENT_WAIT = 0
        P._stop_with_retry(cs, "u1", tries=3, wait_s=0)
        self.assertEqual(cs.stop_calls, 3)

    def test_gives_up_after_tries(self):
        err = Exception("226609: 实例正在进行关机相关操作")
        cs = FakeCS(stop_script=[err, err, err])
        with self.assertRaises(Exception):
            P._stop_with_retry(cs, "u1", tries=3, wait_s=0)
        self.assertEqual(cs.stop_calls, 3)

    def test_permanent_error_not_retried(self):
        """不可恢复的错误立即抛出 —— 重试只是浪费时间。"""
        cs = FakeCS(stop_script=[Exception("Permission denied")])
        with self.assertRaises(Exception):
            P._stop_with_retry(cs, "u1", tries=3, wait_s=0)
        self.assertEqual(cs.stop_calls, 1)            # 只调了一次

    def test_first_success_no_retry(self):
        cs = FakeCS()
        P._stop_with_retry(cs, "u1", tries=3, wait_s=0)
        self.assertEqual(cs.stop_calls, 1)


class TerminateRetryTest(unittest.TestCase):
    def test_succeeds_after_transient(self):
        err = Exception("8903: UHost is in the task")
        cs = FakeCS(term_script=[err])
        P._terminate_with_retry(cs, "u1", tries=3, wait_s=0)
        self.assertEqual(cs.term_calls, 2)

    def test_permanent_not_retried(self):
        cs = FakeCS(term_script=[Exception("余额不足")])
        with self.assertRaises(Exception):
            P._terminate_with_retry(cs, "u1", tries=3, wait_s=0)
        self.assertEqual(cs.term_calls, 1)


class ReleaseMarksOrphanTest(unittest.TestCase):
    """这是本次修复的核心：归还失败不能再标 dead。"""

    def setUp(self):
        self._orig_load = P.load
        self._orig_save = P.save
        self.saved = {}

        def fake_load():
            return {"schema": "p2.pool/v1", "gpu": "V100S", "size": 1,
                    "image_id": "", "max_total": 4,
                    "instances": [{"uid": "u1", "state": "busy", "job_id": "j1",
                                   "created_at": 0.0, "last_used": 0.0,
                                   "hourly": 0.0, "cloud_state": "running"}]}

        def fake_save(d):
            self.saved = d

        P.load = fake_load
        P.save = fake_save

    def tearDown(self):
        P.load = self._orig_load
        P.save = self._orig_save

    def test_release_failure_marks_orphan_not_dead(self):
        err = Exception("226609: 实例正在进行关机相关操作，请稍后再试。")
        cs = FakeCS(stop_script=[err, err, err])
        orig = (P.CS, P.RETRY_TRANSIENT_WAIT)
        P.CS = lambda *a, **k: cs
        P.RETRY_TRANSIENT_WAIT = 0        # 模块级：release() 内部读的就是它
        try:
            P.release("u1", keep=True)
        finally:
            P.CS, P.RETRY_TRANSIENT_WAIT = orig

        it = self.saved["instances"][0]
        self.assertEqual(it["state"], P.STATE_ORPHAN,
                         "归还失败必须标 orphan（云端可能还在烧钱），不能标 dead")
        self.assertIsNotNone(it.get("orphan_since"))
        self.assertIn("226609", it.get("orphan_reason", ""))

    def test_release_success_returns_to_idle(self):
        cs = FakeCS()
        orig_cs = P.CS
        P.CS = lambda *a, **k: cs
        try:
            P.release("u1", keep=True)
        finally:
            P.CS = orig_cs
        it = self.saved["instances"][0]
        self.assertEqual(it["state"], "idle")
        self.assertIsNone(it["job_id"])

    def test_release_with_transient_then_ok(self):
        """一次 226609 之后成功 —— 不该留下 orphan。"""
        err = Exception("226609: 实例正在进行关机相关操作，请稍后再试。")
        cs = FakeCS(stop_script=[err])
        orig_cs, orig_wait = P.CS, P.RETRY_TRANSIENT_WAIT
        P.CS = lambda *a, **k: cs
        P.RETRY_TRANSIENT_WAIT = 0
        try:
            P.release("u1", keep=True)
        finally:
            P.CS, P.RETRY_TRANSIENT_WAIT = orig_cs, orig_wait
        it = self.saved["instances"][0]
        self.assertEqual(it["state"], "idle")
        self.assertEqual(cs.stop_calls, 2)


class ReconcileOrphanTest(unittest.TestCase):
    """reconcile 见到 orphan 必须去 terminate，而不是只删记录。"""

    def setUp(self):
        self._orig = (P.load, P.save, P.CS)
        self.saved = {}

        def fake_load():
            return {"schema": "p2.pool/v1", "gpu": "V100S", "size": 1,
                    "image_id": "", "max_total": 4,
                    "instances": [{"uid": "u1", "state": P.STATE_ORPHAN,
                                   "job_id": None, "created_at": 0.0,
                                   "last_used": 0.0, "hourly": 0.0,
                                   "cloud_state": "running",
                                   "orphan_since": 0.0, "orphan_reason": "226609"}]}

        P.load = fake_load
        P.save = lambda d: self.saved.update(d)

    def tearDown(self):
        P.load, P.save, P.CS = self._orig

    def test_orphan_is_terminated(self):
        cs = FakeCS()
        P.CS = lambda *a, **k: cs
        out = P.reconcile()
        self.assertEqual(cs.term_calls, 1, "orphan 必须被 terminate")
        self.assertEqual(self.saved.get("instances"), [])
        self.assertTrue(any("orphan" in c for c in out["changed"]))

    def test_orphan_terminate_failure_keeps_record(self):
        """terminate 也失败时记录必须留着，下一轮再试 —— 删了就没人管了。"""
        err = Exception("8903: UHost is in the task")
        cs = FakeCS(term_script=[err, err, err])
        P.CS = lambda *a, **k: cs
        out = P.reconcile()
        self.assertEqual(len(self.saved.get("instances") or []), 1,
                         "销毁失败时不能删记录")
        self.assertEqual(self.saved["instances"][0]["state"], P.STATE_ORPHAN)
        self.assertTrue(any("retry-later" in c for c in out["changed"]))


class CloudOrphanScanTest(unittest.TestCase):
    """池记录完全丢失时（进程被杀），靠扫云端前缀找回孤儿。"""

    def setUp(self):
        self._orig = (P.load, P.save, P.CS)
        self.saved = {}
        P.load = lambda: {"schema": "p2.pool/v1", "gpu": "V100S", "size": 1,
                          "image_id": "", "max_total": 4, "instances": []}
        P.save = lambda d: self.saved.update(d)

    def tearDown(self):
        P.load, P.save, P.CS = self._orig

    def test_cloud_orphan_by_prefix_is_terminated(self):
        cs = FakeCS(instances=[
            {"UHostId": "ghost-1", "Name": f"{P.NAME_PREFIX}-1", "State": "Running"},
            {"UHostId": "other-1", "Name": "别人的机器", "State": "Running"},
        ])
        P.CS = lambda *a, **k: cs
        out = P.reconcile()
        self.assertEqual(cs.term_calls, 1, "只该销毁带池前缀的那台")
        self.assertTrue(any("cloud-orphan" in c for c in out["changed"]))

    def test_terminated_ones_skipped(self):
        cs = FakeCS(instances=[
            {"UHostId": "ghost-2", "Name": f"{P.NAME_PREFIX}-2", "State": "Terminated"},
        ])
        P.CS = lambda *a, **k: cs
        P.reconcile()
        self.assertEqual(cs.term_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
