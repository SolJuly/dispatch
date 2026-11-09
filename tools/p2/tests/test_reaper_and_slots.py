#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例清理 + 槽位复用测试（P0 / P2 / P3）。

覆盖 2026-11-09 这次改造的三块：

  reaper          清理判据从「GPU 利用率」换成「作业状态 + 空闲时长」
  instance_slots  槽位表：同模型复用、缺节点拒绝、跨工作流兼容
  run.py          _try_borrow_slot 的接入（静态断言）

全部 ¥0：不建机、不联网、不碰真实槽位表。

用法：python3 -m unittest tools.p2.tests.test_reaper_and_slots
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

from tools.p2 import instance_slots as S  # noqa: E402
from tools.p2 import reaper as R  # noqa: E402

RUN_PY = _ROOT / "tools" / "p1" / "run.py"
SCHED = _ROOT / "tools" / "p2" / "scheduler.py"


def _mk_job(root: Path, jid: str, state: str, *, uhost: str = "", model: str = "m",
            workflow: str = "w") -> Path:
    d = root / jid
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps({
        "schema": "p1.job/v1", "job_id": jid, "state": state,
        "created_at": "2026-01-01T00:00:00+08:00", "kind": "image",
        "model": {"key": model, "registry": {"workflow_key": workflow,
                                             "model_key": model}},
        "params": {"prompt": "x"}, "expect": {}, "steps": {},
        "instance": {"uhost_id": uhost} if uhost else {},
    }, ensure_ascii=False), encoding="utf-8")
    return d


def _mk_inst(uid: str, *, state: str = "Running", age_min: float = 60) -> dict:
    return {"UHostId": uid, "Name": f"t-{uid[-4:]}", "State": state,
            "StartTime": time.time() - age_min * 60}


# ---------------------------------------------------------------- reaper
class TestReaperIdleWindow(unittest.TestCase):
    """规则 A：空闲 10 分钟（可配）且无作业 → 清理。"""

    def test_default_is_ten_minutes(self):
        """默认 10 分钟。20 分钟太多（多空转 ¥0.72），更短会打断刚热好的机器。"""
        old = R.os.environ.pop("P2_IDLE_REAP_MIN", None)
        try:
            self.assertEqual(R.idle_minutes(), 10)
        finally:
            if old is not None:
                R.os.environ["P2_IDLE_REAP_MIN"] = old

    def test_env_override(self):
        old = R.os.environ.get("P2_IDLE_REAP_MIN")
        try:
            R.os.environ["P2_IDLE_REAP_MIN"] = "3"
            self.assertEqual(R.idle_minutes(), 3)
        finally:
            if old is None:
                R.os.environ.pop("P2_IDLE_REAP_MIN", None)
            else:
                R.os.environ["P2_IDLE_REAP_MIN"] = old

    def test_bad_env_falls_back_not_zero(self):
        """非法值回落默认，绝不能变 0 —— 0 会让所有实例立刻被关。"""
        old = R.os.environ.get("P2_IDLE_REAP_MIN")
        try:
            for bad in ("abc", "0", "-5", ""):
                R.os.environ["P2_IDLE_REAP_MIN"] = bad
                with self.subTest(bad=bad):
                    self.assertEqual(R.idle_minutes(), R.DEFAULT_IDLE_MIN)
        finally:
            if old is None:
                R.os.environ.pop("P2_IDLE_REAP_MIN", None)
            else:
                R.os.environ["P2_IDLE_REAP_MIN"] = old


class TestReaperJudgement(unittest.TestCase):
    """判据用「作业状态」，不是 GPU 利用率。

    为什么：GPU 闲可能是正在装模型（那时 GPU 还是 0%）。按 GPU 判会把
    刚提交、正在装模型的作业机器关掉 —— 这是 csctl watch 的缺陷。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_busy_instance_with_running_job_kept(self):
        """有 RUNNING 作业的实例，空闲多久都不清。"""
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING", uhost="u-busy")
        rows = R.assess([_mk_inst("u-busy", age_min=999)], jobs_dir=self.root)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].busy)
        self.assertEqual(R.reapable([_mk_inst("u-busy", age_min=999)],
                                    jobs_dir=self.root), [])

    def test_all_non_terminal_states_count_as_busy(self):
        """非终态全算忙：CREATED/PROVISIONING/RUNNING/VERIFYING 都占着槽。"""
        for st in ("CREATED", "PROVISIONING", "READY", "SUBMITTED", "RUNNING",
                   "FETCHING", "VERIFYING", "PERSISTED"):
            with self.subTest(state=st):
                tmp = tempfile.TemporaryDirectory()
                root = Path(tmp.name)
                _mk_job(root, "p1-20260101T0000-aaaaaa", st, uhost="u-x")
                rows = R.assess([_mk_inst("u-x")], jobs_dir=root)
                self.assertTrue(rows[0].busy, f"{st} 应该算忙")
                tmp.cleanup()

    def test_idle_instance_past_window_reaped(self):
        """没有作业、空闲超阈值 → 可清理。"""
        rows = R.reapable([_mk_inst("u-idle", age_min=30)], idle_min=10,
                          jobs_dir=self.root)
        self.assertEqual([s.uid for s in rows], ["u-idle"])

    def test_idle_instance_within_window_kept(self):
        """刚开 3 分钟、没作业 —— 不清理（可能下一单马上来）。"""
        rows = R.reapable([_mk_inst("u-new", age_min=3)], idle_min=10,
                          jobs_dir=self.root)
        self.assertEqual(rows, [])

    def test_uses_job_end_time_not_create_time(self):
        """关键判据：空闲从「最后一个作业结束」算，不是「实例创建」算。

        `reap_instances` 的老 bug 就是用 CreateTime —— 一台跑了 8 小时
        20 个作业的机器，会在第 45 分钟被腰斩。
        """
        d = _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u-long")
        # 造一个"刚结束"的 events.jsonl
        (d / "events.jsonl").write_text(
            json.dumps({"seq": 9, "ts": time.time() - 60, "to": "DONE"}) + "\n",
            encoding="utf-8")
        # 实例开机很早（3 小时前），但作业 1 分钟前才结束
        rows = R.assess([_mk_inst("u-long", age_min=180)], jobs_dir=self.root)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].busy)
        self.assertLess(rows[0].idle_min, 5, "空闲时长应从作业结束算，不是开机时间")
        self.assertEqual(R.reapable([_mk_inst("u-long", age_min=180)],
                                    idle_min=10, jobs_dir=self.root), [])

    def test_queued_same_group_blocks_reap(self):
        """队列里还有同模型的待跑作业 → 不清（规则 B 让路）。

        否则「作业 A 跑完、作业 B 还没被领取」的瞬间会关掉机器，
        B 起来要重新建机 + 装模型，比多空转一会儿贵。
        """
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE",
                uhost="u-q", model="qwen_image")
        with mock.patch.object(R, "queued_groups", return_value={"qwen_image": 2}):
            rows = R.assess([_mk_inst("u-q", age_min=60)], jobs_dir=self.root)
        self.assertTrue(rows[0].busy, "队列有同模型待跑，不该清理")
        self.assertIn("规则 B 让路", rows[0].reason)

    def test_stopped_instance_ignored(self):
        """云端已 Stopped 的不动（watch 已经关过了）。"""
        rows = R.assess([_mk_inst("u-off", state="Stopped", age_min=999)],
                        jobs_dir=self.root)
        self.assertTrue(rows[0].busy)
        self.assertIn("无需处理", rows[0].reason)

    def test_default_action_is_stop_not_terminate(self):
        """默认关机而不是销毁 —— 销毁不可逆，关机可 start 复用（~12s）。"""
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u-s")
        insts = [_mk_inst("u-s", age_min=60)]
        fake_cs = mock.MagicMock()
        with mock.patch("compshare.cs.CS", return_value=fake_cs, create=True), \
             mock.patch.object(R, "_live_instances", return_value=insts):
            R.reap(insts, idle_min=10, jobs_dir=self.root, say=lambda *_: None)
        fake_cs.stop.assert_called_once_with("u-s")
        fake_cs.terminate.assert_not_called()

    def test_dry_run_does_not_touch_cloud(self):
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u-d")
        insts = [_mk_inst("u-d", age_min=60)]
        fake_cs = mock.MagicMock()
        with mock.patch("compshare.cs.CS", return_value=fake_cs, create=True):
            got = R.reap(insts, idle_min=10, jobs_dir=self.root, dry_run=True,
                         say=lambda *_: None)
        self.assertEqual([s.uid for s in got], ["u-d"])
        fake_cs.stop.assert_not_called()
        fake_cs.terminate.assert_not_called()


class TestReaperWiring(unittest.TestCase):
    """reap_instances 必须真的委托给 reaper —— 老实现的 bug 不能再回来。"""

    def setUp(self):
        self.src = SCHED.read_text(encoding="utf-8")

    def test_reap_instances_delegates(self):
        body = self.src.split("def reap_instances")[1].split("\nclass ")[0]
        self.assertIn("from tools.p2 import reaper", body)
        self.assertIn("reaper.reap(", body)

    def test_old_buggy_active_variable_gone(self):
        """老实现里 `active` 变量算完不用，判据退化成 CreateTime。

        判据看**代码行**不看全文 —— 注释里解释"以前用 CreateTime"是正常的。
        """
        body = self.src.split("def reap_instances")[1].split("\nclass ")[0]
        code = [ln for ln in body.splitlines() if not ln.strip().startswith("#")]
        # 去掉 docstring 之后的实义代码
        joined = "\n".join(code)
        after_doc = joined.split('"""')[-1] if '"""' in joined else joined
        self.assertNotIn("CreateTime", after_doc, "不能再用创建时间做判据")
        self.assertNotIn("age_min", after_doc, "不能再用「活了多久」做判据")

    def test_ttl_not_hardcoded_45(self):
        """TTL 不能再硬编码 45，要和 reaper 的默认值统一（否则两处漂移）。"""
        self.assertNotIn("TTL_MIN = 45", self.src)

    def test_unit_files_exist(self):
        for f in ("p2-reaper.service", "p2-reaper.timer"):
            p = _ROOT / "deploy" / "systemd" / f
            self.assertTrue(p.is_file(), f"缺 {f}")

    def test_timer_runs_every_5min(self):
        t = (_ROOT / "deploy" / "systemd" / "p2-reaper.timer").read_text(encoding="utf-8")
        self.assertIn("OnUnitActiveSec=5min", t)


# ---------------------------------------------------------------- 槽位表
class TestSlots(unittest.TestCase):
    """规则 B：同模型复用实例，不同模型分开。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = S.SLOTS_JSON
        S.SLOTS_JSON = Path(self.tmp.name) / "slots.json"

    def tearDown(self):
        S.SLOTS_JSON = self._orig
        self.tmp.cleanup()

    def test_five_same_workflow_reuse_one_instance(self):
        """核心诉求：同工作流连提交 5 份，只需要 1 台实例。"""
        n, a = S.workflow_needs("qwen_image_t2i", "qwen_image")
        S.upsert(S.Slot(uid="u1", model_key="qwen_image", state="running",
                        workflows_ok=["qwen_image_t2i"],
                        verified_nodes=sorted(n), verified_assets=sorted(a)))
        for i in range(5):
            got, why = S.find_compatible("qwen_image", "qwen_image_t2i")
            self.assertIsNotNone(got, f"第 {i+1} 条应该复用，实得：{why}")
            self.assertEqual(got.uid, "u1")

    def test_different_model_gets_no_slot(self):
        """不同模型不能用同一台机（权重不同）。"""
        S.upsert(S.Slot(uid="u-sdxl", model_key="sdxl_juggernaut", state="running"))
        got, why = S.find_compatible("qwen_image", "qwen_image_t2i")
        self.assertIsNone(got)
        self.assertIn("没有 qwen_image", why)

    def test_same_model_different_workflow_reuses(self):
        """同模型不同工作流也要复用 —— 实测 sdxl 那 5 个工作流共用同一份权重。"""
        n, a = S.workflow_needs("sdxl_t2i", "sdxl_juggernaut")
        S.upsert(S.Slot(uid="u-sdxl", model_key="sdxl_juggernaut", state="running",
                        workflows_ok=["sdxl_t2i"],
                        verified_nodes=sorted(n), verified_assets=sorted(a)))
        for wk in ("sdxl_t2i", "sdxl_img2img", "sdxl_inpaint", "detail_polish",
                   "sdxl_t2i_lora"):
            got, why = S.find_compatible("sdxl_juggernaut", wk)
            self.assertIsNotNone(got, f"{wk} 应该复用：{why}")

    def test_missing_image_node_rejected(self):
        """镜像里真缺节点的（Qwen 2.1 缺 QwenImage21Cache）必须拒绝。

        这类换了机器也没用 —— 是镜像问题，不是实例问题。
        """
        S.upsert(S.Slot(uid="u-q21", model_key="qwen_image_2_1_int8_convrot",
                        state="running"))
        got, why = S.find_compatible("qwen_image_2_1_int8_convrot",
                                     "pack_image_qwen_image_2_1_image_edit")
        self.assertIsNone(got)
        self.assertIn("镜像缺节点", why)

    def test_cold_start_slot_accepts(self):
        """冷启动（什么都没跑过）要放行，否则新实例永远接不了第一条。"""
        S.upsert(S.Slot(uid="u-cold", model_key="qwen_image", state="running"))
        got, why = S.find_compatible("qwen_image", "qwen_image_t2i")
        self.assertIsNotNone(got, f"冷启动应放行：{why}")

    def test_stopped_slot_not_selected(self):
        S.upsert(S.Slot(uid="u-off", model_key="qwen_image", state="draining"))
        got, _ = S.find_compatible("qwen_image", "qwen_image_t2i")
        self.assertIsNone(got, "draining 状态的槽位不该被选")

    def test_prefers_least_busy(self):
        S.upsert(S.Slot(uid="u-busy", model_key="qwen_image", state="running",
                        busy_jobs=["j1", "j2"]))
        S.upsert(S.Slot(uid="u-free", model_key="qwen_image", state="running"))
        got, _ = S.find_compatible("qwen_image", "qwen_image_t2i")
        self.assertEqual(got.uid, "u-free", "应优先选空闲的那个")

    def test_touch_updates_idle_since(self):
        S.upsert(S.Slot(uid="u1", model_key="m", state="running"))
        S.touch("u1", job_id="j1", busy=True)
        self.assertEqual(S.get("u1").idle_since, 0.0, "忙时 idle_since 归零")
        S.touch("u1", job_id="j1", busy=False)
        self.assertGreater(S.get("u1").idle_since, 0, "空闲后记录时间戳")

    def test_mark_workflow_ok_extends_verified(self):
        S.upsert(S.Slot(uid="u1", model_key="qwen_image", state="running"))
        S.mark_workflow_ok("u1", "qwen_image_t2i", nodes={"A", "B"}, assets={"x.safetensors"})
        s = S.get("u1")
        self.assertIn("qwen_image_t2i", s.workflows_ok)
        self.assertEqual(set(s.verified_nodes), {"A", "B"})
        self.assertEqual(set(s.verified_assets), {"x.safetensors"})

    def test_rebuild_from_jobs(self):
        """槽位表丢了能从历史作业重建（自愈）。"""
        root = Path(self.tmp.name) / "jobs"
        _mk_job(root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u-rebuild",
                model="qwen_image", workflow="qwen_image_t2i")
        _mk_job(root, "p1-20260101T0001-bbbbbb", "FAILED", uhost="u-rebuild",
                model="qwen_image", workflow="qwen_image_t2i")
        n = S.rebuild_from_jobs(root)
        self.assertEqual(n, 1, "只收 DONE 的作业")
        s = S.get("u-rebuild")
        self.assertIsNotNone(s)
        self.assertIn("qwen_image_t2i", s.workflows_ok)


# ---------------------------------------------------------------- run.py 接入
class TestRunBorrowWiring(unittest.TestCase):
    """run.py 的接入点（静态断言 —— 真跑要建机，不在单测范围）。"""

    def setUp(self):
        self.src = RUN_PY.read_text(encoding="utf-8")

    def test_try_borrow_slot_exists(self):
        self.assertIn("def _try_borrow_slot(", self.src)

    def test_provision_tries_borrow_first(self):
        body = self.src.split("def step_provision")[1].split("\ndef ")[0]
        self.assertIn("_try_borrow_slot(job, opts)", body)
        # 复用成功要提前 return，不能继续建机
        self.assertLess(body.index("_try_borrow_slot"),
                        body.index("cs.create("), "必须先试复用再建机")

    def test_release_does_not_destroy_borrowed(self):
        body = self.src.split("def step_release")[1].split("\ndef ")[0]
        self.assertIn('ctx.get("borrowed")', body)
        idx_borrow = body.index('ctx.get("borrowed")')
        idx_term = body.index("cs.terminate(")
        self.assertLess(idx_borrow, idx_term, "借用实例的 return 要在销毁之前")

    def test_borrowed_flag_set_on_return(self):
        body = self.src.split("def _try_borrow_slot")[1].split("\ndef ")[0]
        self.assertIn('"borrowed": True', body)

    def test_no_borrow_flag_available(self):
        self.assertIn("--no-borrow", self.src)
        self.assertIn("no_borrow", self.src)

    def test_slot_registered_on_done(self):
        body = self.src.split("def step_done")[1].split("\ndef ")[0]
        self.assertIn("mark_workflow_ok", body)

    def test_scheduler_counts_groups(self):
        src = SCHED.read_text(encoding="utf-8")
        self.assertIn("_active_groups", src)
        body = src.split("def _active_groups")[1].split("\n    def ")[0]
        self.assertIn("model_key", body, "按 model_key 分组")


if __name__ == "__main__":
    unittest.main()
