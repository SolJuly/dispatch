#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作业状态对账的判据测试。

这个模块写错过两次，两次都是「把不成功的作业判成成功」，所以每条规则都配了
负向对照：

  ① 只看 `all(steps.values(), done)` → 跑了一半的作业（只有 4 步）被判成"走完"
  ② 只看步骤不看产物 → 产物被删的作业被判成 DONE（伪造成功）

用法：python3 -m unittest tools.p1.tests.test_jobs_reconcile
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import jobs_reconcile as R  # noqa: E402


def _job(root: Path, jid: str, state: str, *, steps: list[str] = (),
         uhost: str = "", verdict: str | None = None, arts: int = 0,
         idle_h: float = 100) -> Path:
    """造一个作业目录。`steps` 给已完成的步骤名，`arts` 给产物个数。"""
    d = root / jid
    d.mkdir(parents=True, exist_ok=True)
    job = {
        "schema": "p1.job/v1", "job_id": jid, "state": state, "state_seq": 1,
        "created_at": "2026-01-01T00:00:00+08:00", "kind": "image",
        "model": {"key": "m", "workflow": "w", "checkpoint": "c",
                  "workflow_sha256": "a" * 64},
        "params": {"prompt": "x"}, "expect": {}, "steps": {},
        "instance": {"uhost_id": uhost} if uhost else {},
    }
    for name in steps:
        job["steps"][name] = {"done": True,
                              "at": time.strftime("%Y-%m-%dT%H:%M:%S+08:00")}
    (d / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    if verdict is not None:
        (d / "manifest.json").write_text(
            json.dumps({"verdict": verdict}, ensure_ascii=False), encoding="utf-8")
    if arts:
        ad = d / "artifacts"
        ad.mkdir(exist_ok=True)
        for i in range(arts):
            (ad / f"00-primary_{i}.png").write_bytes(b"x")
    # 把 events 时间戳做旧，绕过 STALE_MIN
    ev = d / "events.jsonl"
    ts = time.time() - idle_h * 3600
    ev.write_text(json.dumps({"seq": 1, "ts": ts, "to": state}) + "\n", encoding="utf-8")
    return d


ALL_STEPS = ["provision", "setup", "ready", "submit", "wait",
             "fetch", "verify", "persist", "release"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def judge(self, d: Path, *, procs=(), insts=None, stale_min=1) -> R.Verdict:
        if insts is None:
            insts = {}
        return R.assess_one(d, set(procs), insts, stale_min=stale_min)


class TestKeepCases(Base):
    """该保的必须保住 —— 误判成失败会丢掉正在跑的作业。"""

    def test_process_alive_always_kept(self):
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING",
                 steps=["provision"], uhost="u1", idle_h=999)
        r = self.judge(d, procs=["p1-20260101T0000-aaaaaa"], insts={})
        self.assertEqual(r.action, "keep")
        self.assertIn("进程还在", r.reason)

    def test_recently_active_kept(self):
        """静默没到阈值就保留 —— 慢作业中间可能很久不写日志。"""
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING",
                 steps=["provision"], uhost="u1", idle_h=0.05)
        r = self.judge(d, insts={}, stale_min=60)
        self.assertEqual(r.action, "keep")

    def test_cloud_unreadable_keeps_conservative(self):
        """云端实例列表读不到（insts=None）时必须保守，不能把所有作业判死。

        这是最容易写错的一处：把"读不到"当成"实例全没了"，
        一次网络抖动就能把整个作业表标成失败。
        """
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING",
                 steps=["provision"], uhost="u1", idle_h=999)
        r = R.assess_one(d, set(), None, stale_min=1)
        self.assertEqual(r.action, "keep")
        self.assertIn("证据不足", r.reason)

    def test_terminal_untouched(self):
        for st in ("DONE", "FAILED", "ABORTED"):
            with self.subTest(state=st):
                d = _job(self.root, f"p1-2026010{len(st)}T0000-aaaaaa", st,
                         steps=ALL_STEPS, uhost="u1")
                self.assertIsNone(self.judge(d), f"{st} 不该被处理")


class TestFailCases(Base):
    """该判失败的必须判失败 —— 伪造成功比留个僵尸更糟。"""

    def test_no_artifacts_is_fail_even_if_all_steps_done(self):
        """**踩过的坑**：九步全 done、fetch 记录过文件名，但 artifacts/ 是空的。

        按 `all_steps_done` 判 done 会把它写成 DONE —— 等于把一个拿不出
        任何东西的作业标成成功。产物文件才是事实。
        """
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING",
                 steps=ALL_STEPS, uhost="u1", verdict="PASS", arts=0)
        r = self.judge(d, insts={})
        self.assertEqual(r.action, "fail", r.reason)
        self.assertIn("artifacts/ 为空", r.reason)

    def test_partial_steps_not_treated_as_complete(self):
        """**踩过的第二个坑**：只跑了 4 步的作业，`all(steps.values())` 也是 True。

        必须对着 STEP_ORDER 全表查，否则"跑了一半"被判成"走完了"。
        """
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING",
                 steps=["provision", "setup", "ready", "submit"],
                 uhost="u1", arts=0)
        r = self.judge(d, insts={})
        self.assertEqual(r.action, "fail")
        self.assertIn("步骤未走完", r.reason)
        self.assertNotIn("九步全 done", r.reason)

    def test_artifacts_without_manifest_is_fail_verify(self):
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "FAILED_VERIFY",
                 steps=ALL_STEPS[:8], uhost="u1", arts=2)
        r = self.judge(d, insts={})
        self.assertEqual(r.action, "fail_verify")
        self.assertIn("没有 manifest", r.reason)

    def test_manifest_not_pass_is_fail_verify(self):
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING",
                 steps=ALL_STEPS, uhost="u1", verdict="FAIL", arts=1)
        r = self.judge(d, insts={})
        self.assertEqual(r.action, "fail_verify")

    def test_no_uhost_no_artifacts_is_fail(self):
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "CREATED",
                 steps=[], arts=0)
        r = self.judge(d, insts={})
        self.assertEqual(r.action, "fail")


class TestSuccessCases(Base):
    """确实成功的要能认出来，否则每次对账都在"清理完了但状态还是错的"。"""

    def test_pass_with_artifacts_is_done(self):
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING",
                 steps=ALL_STEPS, uhost="u1", verdict="PASS", arts=1)
        r = self.judge(d, insts={})
        self.assertEqual(r.action, "done")
        self.assertIn("manifest PASS", r.reason)


class TestInstanceAlive(Base):
    """进程死了但实例还在 —— 不能直接判终态，实例要留着排查。"""

    def test_instance_alive_is_fail_run_not_fail(self):
        d = _job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING",
                 steps=["provision"], uhost="u1", idle_h=999)
        r = self.judge(d, insts={"u1": "Running"})
        self.assertEqual(r.action, "fail_run")
        self.assertTrue(r.inst_alive)
        self.assertIn("保留实例", r.reason)


class TestProcDetection(unittest.TestCase):
    """进程检测不能把自己算进去。"""

    def test_parses_job_id_from_ps(self):
        fake = ("root 1 /usr/bin/python3 tools/p1/run.py "
                "p1-20260101T0000-aaaaaa --batch\n"
                "root 2 /usr/bin/python3 tools/p1/jobs_reconcile.py\n")
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout=fake)):
            got = R._proc_owners()
        self.assertIn("p1-20260101T0000-aaaaaa", got)
        self.assertEqual(len(got), 1, "不该把别的进程算进来")

    def test_ps_failure_returns_empty_not_crash(self):
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            self.assertEqual(R._proc_owners(), set())


if __name__ == "__main__":
    unittest.main()
