#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""失败后停机保留（¥0）+ 续跑唤醒 —— 老设计是"开机等 15 分钟"。

## 为什么改

老设计 `cs.stop_at(uid, now + 15min)` 是**定时**关机：那 15 分钟机器是
Running，照常计费，等人来 `--resume`。没人来就是纯烧（H20 ¥1.88）。
停机实例 ¥0（实测），所以失败应该**立刻停**，续跑再花 ~12s 唤醒。

## 这些测试守什么

改这条路径最怕三件事，每件都是一条测试：

  ① 停不下来时**必须回落到定时关机** —— 否则那台机器没有任何兜底地
     一直计费，比老设计还糟
  ② 停机会杀死 ComfyUI，**断点在 submit/wait 的必须作废那几个标记** ——
     否则续跑去轮询一个已不存在的 prompt_id，挂到超时
     （断点在 fetch 之后的不作废 —— 产物在磁盘上，作废等于逼着重跑推理）
  ③ 唤醒后**必须清停机登记** —— 否则 reaper 拿旧起点算"已停 2 小时"，
     唤醒后下一轮巡检就把它销毁（误销毁不可逆）
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import run as R          # noqa: E402
from tools.p1 import store             # noqa: E402


# ------------------------------------------------------------------ 假 CS
class FakeCS:
    """只实现本测试会用到的四个方法，别的调用立刻炸（防止静默走错路）。"""

    def __init__(self, state="Running", fail_stop=False, fail_start=False):
        self.state = state
        self.fail_stop = fail_stop
        self.fail_start = fail_start
        self.calls: list[str] = []

    def stop(self, uid):
        self.calls.append(f"stop:{uid}")
        if self.fail_stop:
            raise RuntimeError("8903 transitional")

    def wait_state(self, uid, states, timeout=600):
        self.calls.append(f"wait:{sorted(states)}")
        if self.fail_stop:
            raise RuntimeError("never reached stopped")
        return {"State": "Stopped"}

    def stop_at(self, uid, ts):
        self.calls.append(f"stop_at:{uid}")
        return {}

    def start(self, uid):
        self.calls.append(f"start:{uid}")
        if self.fail_start:
            raise RuntimeError("cannot start")

    def get(self, uid):
        self.calls.append(f"get:{uid}")
        return {"UHostId": uid, "State": self.state,
                "IPSet": [{"IP": "1.2.3.4", "Type": "BGP"}],
                "Password": "cGFzcw=="}

    def public_ip(self, info):
        return "1.2.3.4"

    def password_of(self, info):
        return "pass"


def _opts(**kw):
    base = {"creds": None, "abort": False, "no_keep": False, "batch": False,
            "keep_on_fail": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _job_dict(steps_done=()):
    steps = {}
    for name in ("provision", "setup", "ready", "submit", "wait"):
        if name in steps_done:
            steps[name] = {"done": True, "at": "2026-11-12T12:00:00"}
    return {"job_id": "p1-t-stop", "state": "FAILED_RUN", "state_seq": 9,
            "model": {"key": "m"},
            "params": {}, "expect": {},
            "params_hash": "sha256:" + "a" * 64,
            "budget": {"max_minutes": 20, "max_cost": 1.0},
            "instance": {"uhost_id": "uhost-1", "gpu": "4090"},
            "steps": steps}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = Path(self.tmp.name) / "jobs"
        self.jobs.mkdir(parents=True)
        self.jid = "p1-t-stop"
        (self.jobs / self.jid).mkdir()
        self.p_jobs = mock.patch.object(store, "JOBS_DIR", self.jobs)
        self.p_jobs.start()
        self.p_rec = mock.patch("tools.p2.lifecycle.record_stopped")
        self.mock_rec = self.p_rec.start()
        self.p_clr = mock.patch("tools.p2.lifecycle.clear_stopped")
        self.mock_clr = self.p_clr.start()

    def tearDown(self):
        self.p_clr.stop()
        self.p_rec.stop()
        self.p_jobs.stop()
        self.tmp.cleanup()

    def write_job(self, j):
        (self.jobs / self.jid / "job.json").write_text(
            json.dumps(j, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------- 立刻停机
class TestStopNow(Base):
    def test_keeps_by_stopping_not_by_timer(self):
        """主路径：立刻停机，不再走 15 分钟定时关机。"""
        self.write_job(_job_dict())
        cs = FakeCS()
        with mock.patch.object(R, "_cs", return_value=cs):
            kept = R._keep_instance(self.jid, {"uhost_id": "uhost-1"}, _opts())
        self.assertTrue(kept)
        self.assertIn("stop:uhost-1", cs.calls, "必须立刻 cs.stop，而不是 stop_at")
        self.assertNotIn("stop_at:uhost-1", cs.calls,
                         "立刻停机成功时不应再设定时器（那是老行为的兜底）")
        self.assertIn("wait:['stopped']", cs.calls, "必须等确认 Stopped")
        # 停机要登记，reaper 才知道 2 小时后销毁
        self.mock_rec.assert_called_once_with("uhost-1")

    def test_stop_failure_falls_back_to_timer(self):
        """停不下来 → 回落 stop_at。否则机器没有任何兜底地一直计费。"""
        self.write_job(_job_dict())
        cs = FakeCS(fail_stop=True)
        with mock.patch.object(R, "_cs", return_value=cs):
            kept = R._keep_instance(self.jid, {"uhost_id": "uhost-1"}, _opts())
        self.assertTrue(kept, "回落定时关机也算保留（能续跑）")
        self.assertIn("stop_at:uhost-1", cs.calls, "停机失败必须设兜底定时器")

    def test_batch_mode_still_releases(self):
        """批量模式仍然直接销毁：停机虽然 ¥0，但占着 5 台配额。"""
        self.write_job(_job_dict())
        cs = FakeCS()
        with mock.patch.object(R, "_cs", return_value=cs):
            kept = R._keep_instance(self.jid, {"uhost_id": "uhost-1"},
                                    _opts(batch=True))
        self.assertFalse(kept)
        self.assertEqual(cs.calls, [], "批量模式不应碰实例")

    def test_recorded_in_job_json(self):
        self.write_job(_job_dict())
        cs = FakeCS()
        with mock.patch.object(R, "_cs", return_value=cs):
            R._keep_instance(self.jid, {"uhost_id": "uhost-1"}, _opts())
        j = json.loads((self.jobs / self.jid / "job.json").read_text(encoding="utf-8"))
        self.assertTrue(j["instance"]["stopped_keep"],
                        "续跑要知道这台是停机保留的（决定要不要唤醒）")


# --------------------------------------------------------------- 断点作废
class TestInvalidateSteps(Base):
    def test_submit_breakpoint_invalidates_chain(self):
        """断点在 submit/wait → ComfyUI 内存态没了，必须重走推理链。"""
        # setup/ready 已完成，submit 未完成 → 断点 = submit
        self.write_job(_job_dict(steps_done=("provision", "setup", "ready")))
        R._invalidate_inmemory_steps(self.jid)
        j = json.loads((self.jobs / self.jid / "job.json").read_text(encoding="utf-8"))
        for k in ("setup", "ready"):
            self.assertFalse(j["steps"][k]["done"],
                             f"{k} 的结果只活在 ComfyUI 内存里，停机即失效")
        self.assertEqual(j["steps"]["setup"].get("invalidated_by"), "machine_stopped")

    def test_fetch_breakpoint_preserves_everything(self):
        """断点在 fetch 之后 → 产物在磁盘上，停机不丢，一个都不作废。

        作废等于逼着重跑推理，白花一遍 GPU 钱。
        """
        self.write_job(_job_dict(steps_done=("provision", "setup", "ready",
                                             "submit", "wait")))
        R._invalidate_inmemory_steps(self.jid)
        j = json.loads((self.jobs / self.jid / "job.json").read_text(encoding="utf-8"))
        for k in ("setup", "ready", "submit", "wait"):
            self.assertTrue(j["steps"][k]["done"], f"{k} 不该被作废")

    def test_noop_when_nothing_to_invalidate(self):
        """断点在 setup（setup 未完成）→ 本来就要重做，不该写任何东西。"""
        self.write_job(_job_dict(steps_done=("provision",)))
        R._invalidate_inmemory_steps(self.jid)
        j = json.loads((self.jobs / self.jid / "job.json").read_text(encoding="utf-8"))
        self.assertNotIn("invalidated_by", json.dumps(j))


# --------------------------------------------------------------- 唤醒
class TestWake(Base):
    def setUp(self):
        super().setUp()
        # sshd 探活失败要重试 20 次×3s —— 真 sleep 会拖 60 秒。
        # 这里 patch 掉：测的是"探不探、探几次"，不是"等多久"。
        self.p_sleep = mock.patch.object(R.time, "sleep", lambda *_: None)
        self.p_sleep.start()

    def tearDown(self):
        self.p_sleep.stop()
        super().tearDown()

    def _ctx_job(self):
        return _job_dict(steps_done=("provision", "setup", "ready", "submit"))

    def test_resume_wakes_stopped_instance(self):
        """--resume 路径：Stopped → start + sshd 探活 + 清停机登记。"""
        cs = FakeCS(state="Stopped")
        with mock.patch.object(R, "_cs", return_value=cs), \
             mock.patch.object(R, "_ssh_run", return_value=(0, "ssh-ready")):
            ctx = R._rebuild_ctx(self._ctx_job(), None, wake=True)
        self.assertIn("start:uhost-1", cs.calls, "停机实例必须先开机")
        self.assertTrue(ctx.get("uhost_id"))
        self.mock_clr.assert_called_once_with(
            "uhost-1")   # 不清登记，reaper 会按旧起点把唤醒后的机器销毁

    def test_running_instance_not_started(self):
        """本来就在跑的不再 start（幂等，白花 12 秒）。"""
        cs = FakeCS(state="Running")
        with mock.patch.object(R, "_cs", return_value=cs), \
             mock.patch.object(R, "_ssh_run", return_value=(0, "ssh-ready")):
            R._rebuild_ctx(self._ctx_job(), None, wake=True)
        self.assertNotIn("start:uhost-1", cs.calls)

    def test_abort_does_not_wake(self):
        """--abort 不唤醒：销毁对 Stopped 照样有效，先开机是纯浪费。"""
        cs = FakeCS(state="Stopped")
        with mock.patch.object(R, "_cs", return_value=cs):
            ctx = R._rebuild_ctx(self._ctx_job(), None)      # wake 默认 False
        self.assertNotIn("start:uhost-1", cs.calls, "abort 路径不该开机")
        self.assertTrue(ctx.get("uhost_id"))

    def test_wake_failure_returns_empty(self):
        """唤醒失败（实例被回收）→ 返回空 ctx，调用方提示重新建作业。"""
        cs = FakeCS(state="Stopped", fail_start=True)
        with mock.patch.object(R, "_cs", return_value=cs):
            ctx = R._rebuild_ctx(self._ctx_job(), None, wake=True)
        self.assertEqual(ctx, {})

    def test_unknown_state_refuses(self):
        """状态既不是 Running 也不是 Stopped（销毁中/回收中）→ 不猜。"""
        cs = FakeCS(state="Destroying")
        with mock.patch.object(R, "_cs", return_value=cs):
            ctx = R._rebuild_ctx(self._ctx_job(), None, wake=True)
        self.assertEqual(ctx, {}, "过渡态不该被当作可唤醒")

    def test_sshd_probe_required(self):
        """Running ≠ ssh 能连。不探活的话 _execute 第一个 ssh 步骤必失败。"""
        cs = FakeCS(state="Stopped")
        with mock.patch.object(R, "_cs", return_value=cs), \
             mock.patch.object(R, "_ssh_run", return_value=(255, "")) as m_ssh:
            ctx = R._rebuild_ctx(self._ctx_job(), None, wake=True)
        self.assertGreaterEqual(m_ssh.call_count, 1, "必须探活 sshd")
        self.assertEqual(ctx, {}, "sshd 未就绪不该往下走")


# --------------------------------------------------------------- 接线
class TestForceReleaseReason(unittest.TestCase):
    """同一个"强制销毁"有两个来由，日志不能都写"升卡"。

    真机踩到：oom_policy=destroy 的作业日志是
    「已销毁 uhost-xxx（升卡：这台机器已证明显存不足）」——
    用户选的是"直接销毁"，读到的却是"升卡"，自相矛盾。
    """

    def test_reason_comes_from_ctx(self):
        import inspect
        src = inspect.getsource(R.step_release)
        self.assertIn("force_release_reason", src,
                      "销毁原因必须由调用方给，不能写死")
        self.assertNotIn('why="升卡：', src,
                         "step_release 里不该出现写死的升卡原因")

    def test_escalation_sets_its_own_reason(self):
        import inspect
        src = inspect.getsource(R._try_escalate)
        self.assertIn('force_release_reason"] = f"升卡：', src)

    def test_destroy_sets_its_own_reason(self):
        import inspect
        src = inspect.getsource(R._run_inner)
        self.assertIn("oom_policy=destroy 销毁", src)


class TestWiring(unittest.TestCase):
    """源码级断言：接线错了行为测试看不出（它们 mock 的是同一层）。"""

    def test_resume_passes_wake(self):
        src = (R.ROOT / "tools/p1/run.py").read_text(encoding="utf-8")
        body = src.split("def _resume")[1].split("\ndef ")[0]
        self.assertIn("wake=True", body, "--resume 必须唤醒停机保留的实例")

    def test_abort_does_not_pass_wake(self):
        src = (R.ROOT / "tools/p1/run.py").read_text(encoding="utf-8")
        body = src.split("def _abort")[1].split("\ndef ")[0]
        self.assertNotIn("wake=True", body, "--abort 不该为销毁而开机")


if __name__ == "__main__":
    unittest.main()
