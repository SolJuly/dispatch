# -*- coding: utf-8 -*-
"""OOM 升卡在 run.py 里的接线：三条最容易接错的地方。

`test_oom_escalate.py` 测的是决策函数（纯逻辑）。这里测的是**接线**，
因为决策对但接线错，结果一样是白花钱：

  ① `_gpu_candidates` 必须遵守显存下限
     不守的话，升卡决定写进 job.json 了，下一轮建机还是选 24G 卡 ——
     等于没升。

  ② `step_release` 的 `force_release` 必须在所有"保留"分支**之前**
     顺序错了就变成：决定升卡 → 长任务空转窗口生效 → 旧机器留着
     → 新机器也建了 → 两条线同时计费。

  ③ `--no-escalate` 必须真的关掉整条路径
     这是唯一的"我不信这个判断"的出口，坏了就等于没出口。
"""
from __future__ import annotations

import argparse
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import run  # noqa: E402


def _opts(**kw):
    base = {"gpu": None, "creds": None, "batch": False, "no_escalate": False}
    base.update(kw)
    return argparse.Namespace(**base)


def _job(gpu_list=("4090", "4090_48G"), floor=0, hourly=2.15):
    inst = {"required_gpu": list(gpu_list), "gpu": "4090", "hourly": hourly}
    if floor:
        inst["oom_escalation"] = {"floor_vram": floor, "attempts": []}
    return {"job_id": "p1-t-wire", "instance": inst,
            "budget": {"max_cost": 6.0, "max_minutes": 45}, "steps": {}}


class TestGpuCandidatesHonoursFloor(unittest.TestCase):
    def test_no_floor_keeps_declared_order(self):
        """没升过卡 → 行为与原版**完全一致**（不能顺手改动默认路径）。"""
        self.assertEqual(run._gpu_candidates(_job(), _opts()), ["4090", "4090_48G"])

    def test_floor_narrows_declared_list(self):
        self.assertEqual(run._gpu_candidates(_job(floor=48), _opts()), ["4090_48G"])

    def test_floor_fills_when_all_too_small(self):
        """声明的卡全都太小 → 从阶梯表补够大的，按价格升序。"""
        got = run._gpu_candidates(_job(gpu_list=("4090",), floor=48), _opts())
        self.assertEqual(got[0], "4090_48G", "48G 够用就不该直接跳 80G")
        self.assertNotIn("V100S", got, "V100S 是 Volta，量化内核跑不动")

    def test_explicit_gpu_larger_than_floor_is_respected(self):
        """人工指定且**够大**的卡要尊重 —— 下限只排除太小的，不是否决权。"""
        self.assertEqual(run._gpu_candidates(_job(floor=48), _opts(gpu="4090_48G")),
                         ["4090_48G"])

    def test_explicit_gpu_below_floor_is_overridden(self):
        """真机 bug 回归（2026-11-12）：`--gpu 4090` 不能让升卡失效。

        现场证据：Wan 14B 在 24G 卡上 OOM 三次，每轮日志都写「计划 4090 ×1」，
        `job.json` 里 `floor_vram=48` 明明记着，而创建出来的三台全是 24G
        （`oom_escalation.attempts` 记着 `gpu: "4090", vram: 24` 三遍）。

        根因：`_gpu_candidates()` 第一版把 `--gpu` 放在最前面短路返回，
        而下限检查在它后面 —— 永远走不到。而 `p20ctl new --run` 和
        `batchctl run` **都会自动补 `--gpu`**（前者取 `required_gpu[0]`，
        后者 default="4090"），所以生产路径上几乎总是带着它。

        测试没抓到是因为它们直接构造 `opts.gpu = None`；生产传的是具体值。
        **这个 bug 只有真机能发现。**
        """
        self.assertEqual(run._gpu_candidates(_job(floor=48), _opts(gpu="4090")),
                         ["4090_48G"])

    def test_garbage_floor_ignored(self):
        j = _job()
        j["instance"]["oom_escalation"] = {"floor_vram": "abc"}
        self.assertEqual(run._gpu_candidates(j, _opts()), ["4090", "4090_48G"])


class TestPolicyWiring(unittest.TestCase):
    """用户选的 oom_policy 必须真的改变行为 —— 只加字段不改逻辑等于没做。"""

    def _err(self, cls="certain"):
        return run.RunError("boom: CUDA out of memory", "FAILED_RUN",
                            oom_cls=cls, oom_text="CUDA out of memory", gpu="4090")

    def _with_job(self, policy, fn):
        """_try_escalate 会读 job.json → 给它一个临时作业。"""
        import json, tempfile
        from tools.p1 import store
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "jobs" / "p1-x"
            d.mkdir(parents=True)
            (d / "job.json").write_text(json.dumps(
                {"job_id": "p1-x", "state": "FAILED_RUN", "state_seq": 5,
                 "model": {"key": "m"}, "params": {}, "expect": {},
                 "params_hash": "sha256:" + "a" * 64,
                 "budget": {"max_cost": 99.0, "max_minutes": 40},
                 "oom_policy": policy,
                 "instance": {"gpu": "4090", "hourly": 2.15,
                              "required_gpu": ["4090"]}}), encoding="utf-8")
            with mock.patch.object(store, "JOBS_DIR", Path(t) / "jobs"):
                return fn()

    def test_stop_policy_blocks_escalation(self):
        """用户选 stop → 不升卡，即使证据确凿、预算够、有货。"""
        from tools.p1 import oom_escalate as O
        with mock.patch.object(O, "plan", wraps=O.plan) as m:
            got = self._with_job("stop", lambda: run._try_escalate(
                "p1-x", {"oom_policy": "stop"}, _opts(),
                {"uhost_id": "u1"}, self._err()))
        self.assertFalse(got, "stop 策略必须拦下升卡")
        self.assertEqual(m.call_count, 0, "拦在策略这一关，plan() 不该被调到")

    def test_auto_policy_allows_escalation_to_plan(self):
        """auto → 进入 plan()（plan 自己还有三道闸，这里只验策略放行）。"""
        from tools.p1 import oom_escalate as O
        with mock.patch.object(O, "plan",
                               return_value={"ok": False, "reason": "x", "targets": []}) as m:
            self._with_job("auto", lambda: run._try_escalate(
                "p1-x", {"oom_policy": "auto"}, _opts(),
                {"uhost_id": "u1"}, self._err()))
        self.assertEqual(m.call_count, 1, "auto 策略必须把决定交给 plan()")

    def test_destroy_path_in_run_inner(self):
        """destroy 策略 → OOM 后走强制销毁，不留停机实例。

        用源码断言：行为测试要 mock 整个九步链，成本高且测不到接线。
        这条守的是"destroy 分支存在且在 _keep_instance 之前"。
        """
        import inspect
        src = inspect.getsource(run._run_inner)
        i_destroy = src.find("policy_of(job, opts) == _oom.POLICY_DESTROY")
        i_keep = src.find("_keep_instance(job_id, ctx, opts)")
        self.assertGreater(i_destroy, 0, "run.py 里没有 destroy 策略分支")
        self.assertLess(i_destroy, i_keep,
                        "destroy 分支必须在 _keep_instance 之前 —— "
                        "否则会先停机保留，与用户选的「直接销毁」矛盾")

    def test_policy_note_no_recursion(self):
        """第一版 policy_note 用 .get(k, self()) 写法爆栈 —— 锁住别再犯。"""
        from tools.p1 import oom_escalate as O
        for p in O.POLICIES:
            self.assertTrue(O.policy_note(p))
        self.assertEqual(O.policy_note("garbage"), O.policy_note("auto"))


class TestGrabFallbackRespectsFloor(unittest.TestCase):
    """抢卡顺延不能退回低于下限的卡。

    真机现场（2026-11-12）：floor=48，`_gpu_candidates()` 正确返回
    `["4090_48G"]`，但 `step_provision` 随后把首选的 `gpu`（4090/24G）
    插回列表最前面 → 候选变 `["4090", "4090_48G"]` → 抢卡在那两者间轮转，
    下一轮就退回 24G。那正是升卡要避免的卡。

    这条用源码断言守，因为真跑要"抢不到卡"这个外部条件。
    """

    def test_candidates_filtered_after_floor_reinsert(self):
        import inspect
        src = inspect.getsource(run.step_provision)
        i_reinsert = src.find("if gpu not in cands:")
        i_filter = src.find("filter_by_floor(cands, floor_now)")
        self.assertGreater(i_reinsert, 0)
        self.assertGreater(i_filter, 0, "抢卡候选没有按下限过滤")
        self.assertLess(i_reinsert, i_filter,
                        "过滤必须在把首选 gpu 插回列表**之后**，否则漏掉那一项")

    def test_empty_after_filter_fails_instead_of_retreating(self):
        import inspect
        src = inspect.getsource(run.step_provision)
        seg = src[src.find("filter_by_floor(cands, floor_now)"):]
        seg = seg[:seg.find("for attempt in range")]
        self.assertIn("if not cands:", seg,
                      "过滤后为空必须显式失败，不能退回到小卡")
        self.assertIn("不退回小卡", seg)


class TestEscalationBudget(unittest.TestCase):
    def test_off_means_zero_extra_rounds(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"P1_OOM_ESCALATE": "0"}):
            self.assertEqual(run._escalation_budget(), 0)

    def test_default_two(self):
        import os
        from unittest import mock
        env = {k: v for k, v in os.environ.items() if k != "P1_OOM_ESCALATE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(run._escalation_budget(), 2)


class TestOomEvidence(unittest.TestCase):
    """日志判定：不能把"进程没了"直接当成"显存不够"。"""

    def test_certain_from_log(self):
        from unittest import mock
        out = ("=== /root/comfy_run.log\n"
               "torch.OutOfMemoryError: CUDA out of memory. "
               "Tried to allocate 2.00 GiB (GPU 0; 23.65 GiB total capacity)\n")
        with mock.patch.object(run, "_ssh_run", return_value=(0, out)):
            cls, ev = run._oom_evidence("h", "23", "p")
        self.assertEqual(cls, "certain")
        self.assertIn("out of memory", ev.lower())

    def test_likely_when_log_clean(self):
        from unittest import mock
        out = "=== /root/comfy_run.log\n[INFO] got prompt\n[INFO] Prompt executed\n"
        with mock.patch.object(run, "_ssh_run", return_value=(0, out)):
            cls, ev = run._oom_evidence("h", "23", "p")
        self.assertEqual(cls, "likely")
        self.assertEqual(ev, "")

    def test_system_oom_killer_counts(self):
        """dmesg 里的 `Killed process` = 系统内存不足被杀，也算显存/内存吃紧。"""
        from unittest import mock
        out = "[12345.6] Out of memory: Killed process 4242 (python3)\n"
        with mock.patch.object(run, "_ssh_run", return_value=(0, out)):
            cls, ev = run._oom_evidence("h", "23", "p")
        self.assertEqual(cls, "certain")
        self.assertIn("dmesg", ev)

    def test_no_log_at_all_is_likely(self):
        from unittest import mock
        with mock.patch.object(run, "_ssh_run", return_value=(255, "")):
            cls, _ev = run._oom_evidence("h", "23", "p")
        self.assertEqual(cls, "likely")


class TestRunErrorCarriesEvidence(unittest.TestCase):
    """RunError 必须把证据带出来 —— 否则 `_try_escalate` 无从判断。"""

    def test_defaults_are_neutral(self):
        e = run.RunError("boom", "FAILED_RUN")
        self.assertEqual(e.oom_cls, "no")
        self.assertEqual(e.oom_text, "")
        self.assertEqual(e.gpu, "")

    def test_carries_fields(self):
        e = run.RunError("oom", "FAILED_RUN", oom_cls="certain",
                         oom_text="CUDA out of memory", gpu="4090")
        self.assertEqual(e.oom_cls, "certain")
        self.assertEqual(e.gpu, "4090")


class TestForceReleaseOrder(unittest.TestCase):
    """`force_release` 必须在 step_release 的"保留"判断之前生效。

    用源码顺序验证而不是跑真机：这条规则的失效方式是"顺序被后来的改动
    挪走了"，而那种改动不会让任何行为测试变红（它需要一个长任务作业
    才会显现），所以只能直接钉住位置。
    """

    def test_force_release_branch_precedes_idle_window(self):
        import inspect
        src = inspect.getsource(run.step_release)
        i_force = src.find("force_release")
        i_idle = src.find("_idle_after_done")
        i_borrowed = src.find("borrowed")
        self.assertGreater(i_force, 0, "step_release 里没有 force_release 分支")
        self.assertGreater(i_idle, 0, "_idle_after_done 不见了？")
        self.assertLess(i_force, i_idle,
                        "force_release 必须在长任务空转窗口之前 —— 否则旧机器会被留下来继续计费")
        self.assertLess(i_force, i_borrowed,
                        "force_release 必须在「借来的不销毁」分支之前 —— "
                        "否则下一单还会复用这台已证伪的机器")

    def test_force_release_terminates_not_stops(self):
        """必须 terminate（不可逆），不能 stop。

        停机是可逆的，但它仍然占着显存配额槽位，而且下一单会被
        `find_compatible` 选回来 —— 只要 `min_vram` 没生效就又 OOM 一次。
        terminate 是唯一"这台机器不再参与"的语义。

        实现放在 `_terminate_with_retry()` 里（见该函数注释：不能把带
        `cs.terminate` 的代码直接插进 step_release 前半段，那会破坏
        "所有保留分支排在唯一销毁分支之前"这条不变量）。
        所以这里检查的是：force 分支**调用了**那个 helper，且 helper 里是 terminate。
        """
        import inspect
        src = inspect.getsource(run.step_release)
        blk = src[src.find("force_release"):src.find("_idle_after_done")]
        self.assertIn("_terminate_with_retry", blk,
                      "force 分支必须走 _terminate_with_retry，不能裸调 stop()")
        helper = inspect.getsource(run._terminate_with_retry)
        self.assertIn("terminate", helper)
        self.assertNotIn("cs.stop(", helper, "停机不等于这台机器不再参与")

    def test_terminate_helper_is_the_only_terminate_in_release(self):
        """step_release 体内只应有一处 terminate（在负责销毁的收尾分支）。

        这条和 p2 的两个既有测试（test_lifecycle / test_reaper_and_slots）
        守的是同一个不变量，但方向相反：它们查"第一个 terminate 在保留分支
        之后"，这里查"terminate 没被复制到别处"。两边都绿才说明位置对。
        """
        import inspect
        src = inspect.getsource(run.step_release)
        self.assertEqual(src.count("cs.terminate("), 1,
                         "step_release 里出现多处 terminate —— 保留分支可能被绕过")


if __name__ == "__main__":
    unittest.main()
