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

    def test_explicit_gpu_flag_wins(self):
        """`--gpu` 是人工强制覆盖，不该被下限改写。

        理由：下限是**自动判断**的产物，人工指定是**人的判断**。
        人明确要 V100S 调试，就该给他 V100S —— 只是日志里会警告。
        """
        self.assertEqual(run._gpu_candidates(_job(floor=48), _opts(gpu="4090")), ["4090"])

    def test_garbage_floor_ignored(self):
        j = _job()
        j["instance"]["oom_escalation"] = {"floor_vram": "abc"}
        self.assertEqual(run._gpu_candidates(j, _opts()), ["4090", "4090_48G"])


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
