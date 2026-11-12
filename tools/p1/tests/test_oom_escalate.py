#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OOM 自动升卡：三道闸必须各自独立生效。

## 为什么这些测试比平常更重要

自动升卡的失败模式**不是"不生效"，而是"生效太多次"**。少一道闸的结果是：
一条必然 OOM 的作业在 24G/48G/80G/96G 之间循环，每条都建机、每轮都扣钱，
而且它看起来"在工作"（日志一直在滚，状态一直在动）。

所以这里每个测试守的都是**一个字面上的坎**：

  ① 判定闸   进程崩了但没有 OOM 证据 → 不升卡（升卡是赌博，赌注 ¥3.80/h）
  ② 次数闸   到上限就停（不能靠"下次可能成功"自我说服）
  ③ 预算闸   已花 + 将花 一起算（只算单次会漏掉前面烧掉的钱）

还有一条容易写错的：**升卡只升不降**。降级等于把已经证伪的配置再跑一遍。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import gpu_ladder as ladder  # noqa: E402
from tools.p1 import oom_escalate as O      # noqa: E402

# 2026-11-12 实测库存快照：5090 长期 0，4090_48G 有 2
STOCK = {"2080": 0, "3080Ti": 0, "3090": 0, "4090": 54, "4090_48G": 2,
         "5090": 0, "A100": 1, "A800": 0, "H20": 2, "P40": 0, "V100S": 1}


def _job(gpu="4090", *, hourly=2.15, attempts=None, floor=0,
         max_cost=3.0, max_minutes=45, up_at=None):
    inst = {"gpu": gpu, "hourly": hourly}
    if up_at:
        inst["up_at"] = up_at
    rec = {"attempts": attempts or [], "floor_vram": floor, "schema": O.SCHEMA}
    inst["oom_escalation"] = rec
    return {"job_id": "p1-test-oom", "instance": inst,
            "budget": {"max_cost": max_cost, "max_minutes": max_minutes},
            "steps": {}}


def _attempt(gpu="4090", vram=24, hourly=2.15, minutes=10.0, cost=None):
    return {"gpu": gpu, "vram": vram, "hourly": hourly, "minutes": minutes,
            "cost": round(cost if cost is not None else hourly * minutes / 60.0, 4)}


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict("os.environ", {}, clear=False)
        self.env.start()
        for k in ("P1_OOM_ESCALATE", "P1_OOM_ESCALATE_ON_CRASH",
                  "P1_OOM_MAX_ESCALATIONS"):
            import os
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()

    def plan(self, job, *, gpu="4090", stock=None, cls="certain", text="CUDA out of memory"):
        return O.plan(job, current_gpu=gpu, oom_text=text,
                      stock=STOCK if stock is None else stock, cls=cls)


# --------------------------------------------------------------- 判定闸
class TestJudgment(Base):
    """判定闸：只有证据确凿的 OOM 才升卡。"""

    def test_certain_escalates(self):
        p = self.plan(_job())
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual(p["target"][0], "4090_48G")

    def test_crash_without_evidence_does_not(self):
        """进程没了但没有 OOM 字样 → 不升卡。

        这是四条里最重要的一条：段错误、代码 bug、ImportError 都会让进程消失，
        升到 96G 卡只会让同样的失败贵 4 倍，还多花一次建机费。
        """
        p = self.plan(_job(), cls="likely")
        self.assertFalse(p["ok"])
        self.assertIn("证据不足", p["reason"])

    def test_crash_can_be_opted_in(self):
        """显式开启后才允许"崩了就升" —— 这是赌博，必须用户自己承担。"""
        import os
        os.environ["P1_OOM_ESCALATE_ON_CRASH"] = "1"
        p = self.plan(_job(), cls="likely")
        self.assertTrue(p["ok"], p["reason"])

    def test_missing_model_is_not_oom(self):
        """`use_missing_models` 里可能带 "out of memory" 字样，不能算 OOM。"""
        self.assertEqual(ladder.classify_oom(
            "use_missing_models: out of memory checkpoint.safetensors"), "no")
        p = self.plan(_job(), cls="no", text="use_missing_models")
        self.assertFalse(p["ok"])

    def test_kernel_image_mismatch_is_not_oom(self):
        """架构不匹配（cu130 编的库跑在 cu128 上）换大卡没用。"""
        self.assertEqual(ladder.classify_oom(
            "CUDA error: no kernel image is available for execution on the device"), "no")

    def test_global_switch_off(self):
        import os
        os.environ["P1_OOM_ESCALATE"] = "0"
        p = self.plan(_job())
        self.assertFalse(p["ok"])
        self.assertIn("关闭", p["reason"])


# --------------------------------------------------------------- 次数闸
class TestAttempts(Base):
    def test_zero_attempts_first_escalation(self):
        self.assertTrue(self.plan(_job())["ok"])

    def test_at_limit_stops(self):
        job = _job(attempts=[_attempt("4090"), _attempt("4090_48G", 48, 3.8)])
        p = self.plan(job, gpu="4090_48G")
        self.assertFalse(p["ok"])
        self.assertIn("上限", p["reason"])

    def test_limit_configurable_but_capped(self):
        import os
        os.environ["P1_OOM_MAX_ESCALATIONS"] = "99"
        # 封顶 5：再多也没有 5 个档位可选，设更大只让日志变得可疑
        self.assertEqual(O.max_escalations(), 5)

    def test_bad_env_falls_back(self):
        import os
        os.environ["P1_OOM_MAX_ESCALATIONS"] = "abc"
        self.assertEqual(O.max_escalations(), O.MAX_ESCALATIONS)

    def test_default_is_two(self):
        self.assertEqual(O.max_escalations(), 2)


# --------------------------------------------------------------- 预算闸
class TestBudget(Base):
    def test_already_spent_counts(self):
        """已花 ¥2.5 + 48G 一档 ¥2.85 → 合计 ¥5.35 超预算 ¥3 → 拒绝。

        早期设计只看"这一轮预估 ¥2.85 < ¥3"，会放行，然后这一单实际花 ¥5.35。
        前面烧掉的钱必须一起算 —— 那正是"这条作业有没有超支"的定义。
        """
        job = _job(attempts=[_attempt("4090", minutes=70, hourly=2.15, cost=2.5)])
        p = self.plan(job)
        self.assertFalse(p["ok"])
        self.assertIn("预算", p["reason"])

    def test_cheap_escalation_allowed(self):
        job = _job(max_cost=6.0, attempts=[_attempt("4090", minutes=10, cost=0.36)])
        p = self.plan(job)
        self.assertTrue(p["ok"], p["reason"])
        self.assertEqual(p["target"][0], "4090_48G")

    def test_zero_budget_means_unlimited(self):
        """max_cost=0 = 不限制（schema 里允许），不能理解成"一分钱都不许花"。"""
        job = _job(max_cost=0.0)
        self.assertTrue(self.plan(job)["ok"])

    def test_hourly_from_job_not_static_table(self):
        """已花用作业记录的真实单价算，不用静态价格表。

        静态表过期会让"已花"偏低 → 预算闸在已经超支时放行。
        """
        job = _job(hourly=9.99, up_at=(dt.datetime.now().astimezone()
                                       - dt.timedelta(minutes=30)).isoformat())
        job["instance"]["gpu"] = "4090"          # 静态表里是 2.15
        self.assertGreater(O.spent_so_far(job), 4.0)   # 9.99 × 0.5h ≈ 5.0


# --------------------------------------------------------------- 选择闸
class TestTargets(Base):
    def test_no_downgrade(self):
        """只返回显存严格更大的卡。"""
        for g, v, _h in ladder.next_targets("4090", STOCK):
            self.assertGreater(v, 24)

    def test_minimal_sufficient_tier(self):
        """最小够用档：48G 能解决就不直接跳到 96G（¥7.50/h 是 48G 的两倍）。"""
        got = ladder.next_targets("4090", STOCK)
        self.assertEqual([g for g, _v, _h in got], ["4090_48G"])

    def test_skips_out_of_stock(self):
        """5090（32G）显存比 4090_48G 小但库存 0，不该出现；A800 也无货。"""
        got = {g for g, _v, _h in ladder.next_targets("4090", STOCK)}
        self.assertNotIn("5090", got)
        self.assertNotIn("A800", got)

    def test_no_stock_at_all(self):
        p = self.plan(_job(), stock={g: 0 for g in ladder.VRAM_GB})
        self.assertFalse(p["ok"])
        self.assertIn("没有", p["reason"])

    def test_unknown_gpu_refuses(self):
        """显存未知时不猜（猜错方向就是白花钱）。"""
        p = self.plan(_job(), gpu="神秘卡")
        self.assertFalse(p["ok"])
        self.assertIn("未知", p["reason"])

    def test_from_48g_goes_to_min_80(self):
        """48G 之后是 80G 一档（A100/A800），A800 无货 → A100。"""
        got = ladder.next_targets("4090_48G", STOCK)
        self.assertEqual([g for g, _v, _h in got], ["A100"])

    def test_floor_filter_keeps_order(self):
        self.assertEqual(ladder.filter_by_floor(["4090", "4090_48G", "A100"], 48),
                         ["4090_48G", "A100"])

    def test_floor_zero_keeps_everything(self):
        """floor=0 是"没要求"，不是"要求 0G" —— 必须原样返回。"""
        self.assertEqual(ladder.filter_by_floor(["4090", "4090_48G"], 0),
                         ["4090", "4090_48G"])


# --------------------------------------------------------------- 记账
class TestAccounting(Base):
    def test_spent_accumulates_attempts(self):
        job = _job(attempts=[_attempt(minutes=30, cost=1.075),
                             _attempt("4090_48G", 48, 3.8, 30, 1.9)])
        self.assertAlmostEqual(O.spent_so_far(job), 2.975, places=2)

    def test_live_round_included(self):
        up = (dt.datetime.now().astimezone() - dt.timedelta(minutes=60)).isoformat()
        job = _job(up_at=up)                       # 2.15 × 1h = 2.15
        self.assertAlmostEqual(O.spent_so_far(job), 2.15, delta=0.05)

    def test_no_up_at_is_zero(self):
        self.assertEqual(O.spent_so_far(_job()), 0.0)

    def test_describe_is_readable(self):
        job = _job(attempts=[_attempt(minutes=10)], floor=48)
        s = O.describe(job)
        self.assertIn("升卡 1 次", s)
        self.assertIn("≥48G", s)

    def test_describe_when_never_happened(self):
        self.assertEqual(O.describe({'instance': {}}), "未发生升卡")


# --------------------------------------------------------------- 落盘
class TestApply(Base):
    """apply() 只写盘、不建机、不碰平台。"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.jobs = Path(self.tmp.name) / "jobs"
        self.jid = "p1-test-apply"
        (self.jobs / self.jid).mkdir(parents=True)
        job = _job(up_at=(dt.datetime.now().astimezone()
                          - dt.timedelta(minutes=20)).isoformat())
        job["job_id"] = self.jid
        job["state"] = "FAILED_RUN"
        job["state_seq"] = 3
        (self.jobs / self.jid / "job.json").write_text(
            json.dumps(job, ensure_ascii=False), encoding="utf-8")
        self.p_jobs = mock.patch.object(O, "_ROOT", Path(self.tmp.name))
        self.p_jobs.start()
        import tools.p1.store as store_mod
        self.p_store = mock.patch.object(store_mod, "JOBS_DIR", self.jobs)
        self.p_store.start()

    def tearDown(self):
        self.p_store.stop()
        self.p_jobs.stop()
        self.tmp.cleanup()
        super().tearDown()

    def test_apply_records_floor_and_clears_steps(self):
        O.apply(self.jid, current_gpu="4090", target=("4090_48G", 48, 3.8),
                oom_text="CUDA out of memory")
        j = json.loads((self.jobs / self.jid / "job.json").read_text(encoding="utf-8"))
        rec = j["instance"]["oom_escalation"]
        self.assertEqual(rec["floor_vram"], 48, "必须抬显存下限，否则 resume 会回到小卡")
        self.assertEqual(len(rec["attempts"]), 1)
        self.assertEqual(j["steps"], {}, "断点必须清空，否则会从 wait 而不是 provision 重跑")
        self.assertIsNone(j["instance"]["up_at"], "旧计费起点必须作废")

    def test_floor_survives_reload(self):
        """下限必须能从磁盘读回来 —— 它是跨进程生效的那个字段。"""
        O.apply(self.jid, current_gpu="4090", target=("4090_48G", 48, 3.8), oom_text="oom")
        j = json.loads((self.jobs / self.jid / "job.json").read_text(encoding="utf-8"))
        self.assertEqual(O.floor_vram(j), 48)

    def test_reset_clears(self):
        O.apply(self.jid, current_gpu="4090", target=("4090_48G", 48, 3.8), oom_text="oom")
        O.reset(self.jid)
        j = json.loads((self.jobs / self.jid / "job.json").read_text(encoding="utf-8"))
        self.assertNotIn("oom_escalation", j.get("instance") or {})
        self.assertEqual(O.floor_vram(j), 0)


if __name__ == "__main__":
    unittest.main()
