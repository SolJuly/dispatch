# -*- coding: utf-8 -*-
"""同一任务重复提交的防护：去重键、提交锁竞态、幂等复用、DONE 拒绝、force 重跑。"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import jobctl, state, store  # noqa: E402


def _spec(tmp: Path, prompt: str = "a red apple") -> Path:
    wf = ROOT / "workflows/zimage_turbo_t2i.api.json"
    spec = {
        "kind": "image",
        "model": {
            "key": "zimage_turbo",
            "workflow": "workflows/zimage_turbo_t2i.api.json",
            "checkpoint": "z_image_turbo_bf16.safetensors",
            "registry": {"workflow_key": "zimage_turbo_t2i", "workflow_version": 1,
                         "model_key": "zimage_turbo", "model_version": 1,
                         "compat": "verified"},
        },
        "params": {"prompt": prompt, "seed": 42, "width": 512, "height": 512},
        "expect": {"ext": "png", "min_bytes": 20000},
        "budget": {"max_minutes": 20, "max_cost": 0.8},
        "instance": {"required_gpu": ["4090"]},
    }
    p = tmp / f"spec-{abs(hash(prompt)) % 100000}.json"
    p.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    assert wf.exists(), "测试依赖的 workflow 文件不存在"
    return p


def _args(spec: Path, force: bool = False):
    return argparse.Namespace(spec=str(spec), force=force, branch=None, task=None)


class _Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        self._orig_jobs = store.JOBS_DIR
        self._orig_lock = store.SUBMIT_LOCK_FILE
        store.JOBS_DIR = self.jobs
        store.SUBMIT_LOCK_FILE = self.root / ".submit.lock"

    def tearDown(self):
        store.JOBS_DIR = self._orig_jobs
        store.SUBMIT_LOCK_FILE = self._orig_lock
        self.tmp.cleanup()

    def _job_dirs(self):
        return sorted(p.name for p in self.jobs.iterdir() if p.is_dir() and (p / "job.json").exists())


class TestDedupKey(_Isolated):
    def test_stable_and_ignores_expect(self):
        m = {"key": "k", "workflow": "workflows/x.api.json",
             "checkpoint": "c", "registry": {"workflow_key": "wf"}}
        p = {"prompt": "hi", "seed": 1}
        k = store.task_dedup_key(m, p)
        self.assertEqual(k, store.task_dedup_key(m, p))
        # 换个 expect 不影响去重键（但会改 params_hash）
        self.assertEqual(k, store.task_dedup_key(m, {"prompt": "hi", "seed": 1}))
        self.assertNotEqual(k, store.task_dedup_key(m, {"prompt": "BYE", "seed": 1}))
        self.assertNotEqual(k, store.task_dedup_key({**m, "key": "other"}, p))

    def test_legacy_jobs_recomputed(self):
        """老作业没存 task_dedup_key，find_same_task 要能现算出来。"""
        spec = _spec(self.root)
        self.assertEqual(jobctl.cmd_new(_args(spec)), 0)
        jid = self._job_dirs()[0]
        jf = self.jobs / jid / "job.json"
        j = json.loads(jf.read_text(encoding="utf-8"))
        self.assertTrue(j.get("task_dedup_key"))
        j.pop("task_dedup_key")            # 模拟老作业
        jf.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        found = store.find_same_task(j["model"], j["params"])
        self.assertIn(jid, [x[0] for x in found])


class TestSequential(_Isolated):
    def test_inflight_reuses_existing(self):
        spec = _spec(self.root)
        self.assertEqual(jobctl.cmd_new(_args(spec)), 0)
        first = self._job_dirs()[0]
        # 第二次同参数、作业还在 CREATED：复用，不新建目录
        with mock.patch("sys.stdout", new=__import__("io").StringIO()) as buf:
            rc = jobctl.cmd_new(_args(spec))
        self.assertEqual(rc, 0)
        self.assertIn(store.DEDUP_REUSE_MARK, buf.getvalue())
        self.assertEqual(self._job_dirs(), [first], "绝不能建出第二个作业目录")

    def test_done_is_refused(self):
        spec = _spec(self.root)
        jobctl.cmd_new(_args(spec))
        jid = self._job_dirs()[0]
        jf = self.jobs / jid / "job.json"
        j = json.loads(jf.read_text(encoding="utf-8"))
        j["state"] = state.DONE
        jf.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(SystemExit):
            jobctl.cmd_new(_args(spec))
        self.assertEqual(len(self._job_dirs()), 1)

    def test_force_creates_new(self):
        spec = _spec(self.root)
        jobctl.cmd_new(_args(spec))
        jid = self._job_dirs()[0]
        jf = self.jobs / jid / "job.json"
        j = json.loads(jf.read_text(encoding="utf-8"))
        j["state"] = state.DONE
        jf.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(jobctl.cmd_new(_args(spec, force=True)), 0)
        self.assertEqual(len(self._job_dirs()), 2, "--force 应允许显式重跑")

    def test_different_params_are_distinct(self):
        jobctl.cmd_new(_args(_spec(self.root, "cat")))
        jobctl.cmd_new(_args(_spec(self.root, "dog")))
        self.assertEqual(len(self._job_dirs()), 2)

    def test_failed_jobs_are_not_reused(self):
        """失败/中止的旧作业不能被当成「进行中」复用（2026-11-08 审计发现的真 bug）。

        旧实现用 `state not in TERMINAL` 判进行中，而 FAILED_* 不在 TERMINAL，
        于是用户重提一个失败任务会拿到那个死掉的 job_id、什么都不执行。
        """
        spec = _spec(self.root)
        jobctl.cmd_new(_args(spec))
        jid = self._job_dirs()[0]
        jf = self.jobs / jid / "job.json"
        for dead_state in (state.FAILED_RUN, state.FAILED_VERIFY, state.FAILED_PROVISION,
                           state.FAILED, state.ABORTED):
            j = json.loads(jf.read_text(encoding="utf-8"))
            j["state"] = dead_state
            jf.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
            before = set(self._job_dirs())
            with mock.patch("sys.stdout", new=__import__("io").StringIO()) as buf:
                self.assertEqual(jobctl.cmd_new(_args(spec)), 0)
            self.assertNotIn(store.DEDUP_REUSE_MARK, buf.getvalue(),
                             f"{dead_state} 被错误复用了")
            new = set(self._job_dirs()) - before
            self.assertEqual(len(new), 1, f"{dead_state} 后应新建一个作业")
            # 只保留最新那个继续下一轮
            for p in before:
                __import__("shutil").rmtree(self.jobs / p, ignore_errors=True)
            jid = new.pop()
            jf = self.jobs / jid / "job.json"

    def test_aborted_is_not_reused(self):
        spec = _spec(self.root, "aborted-case")
        jobctl.cmd_new(_args(spec))
        jid = self._job_dirs()[0]
        jf = self.jobs / jid / "job.json"
        j = json.loads(jf.read_text(encoding="utf-8"))
        j["state"] = state.ABORTED
        jf.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
        with mock.patch("sys.stdout", new=__import__("io").StringIO()) as buf:
            jobctl.cmd_new(_args(spec))
        self.assertNotIn(store.DEDUP_REUSE_MARK, buf.getvalue())
        self.assertEqual(len(self._job_dirs()), 2)


class TestConcurrent(_Isolated):
    def test_double_click_creates_only_one(self):
        """4 个并发提交（模拟用户双击/多标签）：提交锁把查重+占位原子化，只建一个。"""
        specs = [_spec(self.root) for _ in range(4)]
        # 每个线程用自己独立的 spec 文件，内容完全相同
        barrier = threading.Barrier(4)

        def submit(p):
            barrier.wait()
            return jobctl.cmd_new(_args(p))

        with ThreadPoolExecutor(max_workers=4) as ex:
            rcs = list(ex.map(submit, specs))
        self.assertEqual(rcs, [0, 0, 0, 0])
        dirs = self._job_dirs()
        self.assertEqual(len(dirs), 1, f"并发重复提交建了 {len(dirs)} 个作业：{dirs}")


class TestWebuiReuse(unittest.TestCase):
    """create_job 识别 DEDUP_REUSE_MARK：reused=True、绝不 spawn 第二个 run.py。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        self.specs = self.root / "specs"
        self.fake = self.root / "fake_jobctl.py"
        self.fake.write_text(
            "import sys\n"
            "print('DEDUP_REUSE p1-20260101T0000-abc123')\n"
            "print('✓ 同参数作业已在 CREATED，复用：p1-20260101T0000-abc123')\n",
            encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_reuse_skips_spawn(self):
        from webui import api
        spawned = []
        r = api.create_job("qwen_image_t2i", {"prompt": "x", "seed": 1},
                           run=True, jobs_dir=self.jobs, spec_dir=self.specs,
                           jobctl=self.fake, spawn=lambda *a, **k: spawned.append(a))
        self.assertTrue(r["reused"])
        self.assertFalse(r["run"])
        self.assertEqual(r["job_id"], "p1-20260101T0000-abc123")
        self.assertEqual(spawned, [], "复用时不能再拉起一个 run.py")


if __name__ == "__main__":
    unittest.main()


class TestRmDeletableStates(unittest.TestCase):
    """jobctl rm 的可删状态判定。

    回归背景：FAILED_PROVISION 等失败分支不在 TERMINAL（它们会被折叠成 FAILED
    再进终态），但流程若崩在折叠之前，作业就永远卡在这个状态。它不是在跑，
    却被 rm 拒绝 → 界面上清不掉（实测踩到：p1-20261103T1245-a47eee）。
    """

    def test_fail_branch_is_deletable(self):
        from tools.p1 import state
        # 可删 = 终态 ∪ 失败分支
        for st in ("DONE", "FAILED", "ABORTED"):
            self.assertIn(st, state.TERMINAL, f"{st} 应是终态")
        for st in ("FAILED_PROVISION", "FAILED_RUN", "FAILED_FETCH", "FAILED_VERIFY"):
            self.assertIn(st, state.FAIL_BRANCH, f"{st} 应在失败分支里")

    def test_live_states_are_not_deletable(self):
        """主链上还可能继续的状态必须拒绝（保护真在跑的作业）。"""
        from tools.p1 import state
        deletable = set(state.TERMINAL) | set(state.FAIL_BRANCH)
        for st in ("CREATED", "PROVISIONING", "READY", "SUBMITTED", "RUNNING",
                   "FETCHING", "VERIFYING", "PERSISTED", "RELEASING", "RESUMING"):
            self.assertNotIn(st, deletable, f"{st} 不该被视为可删")

    def test_jobctl_rm_consults_fail_branch(self):
        """源码层面确认 cmd_rm 用了 FAIL_BRANCH，而不只判 TERMINAL。"""
        import inspect
        from tools.p1 import jobctl
        src = inspect.getsource(jobctl.cmd_rm)
        self.assertIn("FAIL_BRANCH", src,
                      "cmd_rm 必须把失败分支也视为可删，否则僵尸作业清不掉")
