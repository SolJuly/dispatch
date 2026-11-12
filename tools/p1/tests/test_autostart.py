#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动补位：`auto_run` 作业在并发腾出来后自动启动。

## 为什么需要它

并发满时后端会**降级成"只建作业不执行"**，把作业留在 CREATED —— 这比 429
拒绝好（不丢用户填好的整套参数）。但降级之后没有任何东西会回头启动它：
没有常驻调度器能领（队列里根本没有这些作业）、用户也不会一直盯着页面。

于是一个勾了「立即执行」的作业能挂几个小时，直到对账脚本按超时判它失败。
2026-11-11 的 `p1-20261111T2012-8ba75f` 就是这么死的。

## 两条完全不同的语义

  · 勾了「立即执行」+ 并发满 → `auto_run: true` → 排队，前面跑完自动开始
  · 根本没勾                → 无此字段      → 一直等人点「执行」

混在一起是这次要避免的核心错误：把"用户想跑"的作业当"用户不想跑"处理，
它们就永远不跑；反过来把"用户只想先存着"的作业自动拉起，会花他没打算花的钱。

## 覆盖点

  · 只挑 auto_run 的，不碰其他人的
  · 按创建时间先来先跑
  · 按**模型分组**判并发，不按作业条数（同模型挤一台机，只占 1 槽）
  · 没槽时不启动，也不报错
  · 启动子进程失败不崩
  · 读不到并发状态时宁可不动（保守）
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

from tools.p1 import autostart as A  # noqa: E402


def _job(root: Path, jid: str, *, state: str = "CREATED", auto: bool = False,
         created: str = "2026-01-01T00:00:00+08:00", model: str = "m1") -> Path:
    d = root / jid
    d.mkdir(parents=True, exist_ok=True)
    j = {"schema": "p1.job/v1", "job_id": jid, "state": state,
         "created_at": created, "kind": "image",
         "model": {"key": model, "workflow": "w", "checkpoint": "c",
                   "workflow_sha256": "a" * 64},
         "params": {"prompt": "x"}, "expect": {}, "steps": {}, "instance": {},
         "budget": {"max_minutes": 45, "max_cost": 3.0}}
    if auto:
        j["auto_run"] = True
    (d / "job.json").write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
    return d


class FakeProc:
    def __init__(self, *a, **k):
        self.args = a


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.spawned: list[list[str]] = []
        self.said: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _spawn(self, cmd, **kw):
        self.spawned.append(list(cmd))
        return FakeProc()

    def say(self, m):
        self.said.append(str(m))

    def run_start(self, *, cap=2, used=0, **kw):
        with mock.patch("tools.p2.concurrency.max_parallel", return_value=cap), \
             mock.patch("tools.p2.concurrency.slots_used", return_value=used):
            return A.start_pending(self.root, spawn=self._spawn, say=self.say, **kw)


class TestLedgerIsolation(unittest.TestCase):
    """测试绝不能读生产账本。

    ## 为什么这条单独抽出来

    2026-11-12 真机验证时踩到的连锁故障：

        真机花了 ¥9.03 → 超过日上限 ¥8.00 → money_guard 拒绝补位
        → 9 条 autostart 测试全红

    而代码里一行没改。测试红的原因是**当天真的花了太多钱**。

    根因在 `_ledger_for()` 的"文件不存在就返回 None"分支：
    临时目录没有 ledger.jsonl → None → `money_guard.check(None)` 读**真实账本**。

    讽刺的是那个函数的 docstring 早就写"如果账本还读真实的，那种测试迟早会
    莫名其妙地红"。注释写对了，但那个 None 分支把临时路径换回了真实路径。

    这类测试的失效方式最难受：**它红的时候你会去查代码，而代码是对的。**
    """

    def test_temp_dir_gets_temp_ledger_even_if_missing(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "jobs"
            d.mkdir()
            got = A._ledger_for(d)
            self.assertIsNotNone(got, "临时目录不能返回 None（None = 读真实账本）")
            self.assertNotEqual(Path(got).resolve(), Path(A._money_guard_ledger()).resolve(),
                                "临时目录拿到了真实账本路径")

    def test_production_dir_gets_real_ledger(self):
        got = Path(A._ledger_for(A._REAL_JOBS)).resolve()
        self.assertEqual(got, Path(A._money_guard_ledger()).resolve())

    def test_temp_ledger_means_zero_spent(self):
        """临时账本不存在 = 今天没花钱，闸门必须放行。

        直接用 money_guard，因为问题出在"不存在的账本怎么算已花"。
        这也是那条连锁故障的最小复现：账本指向临时目录后，
        当天真花多少钱都不该影响这个判断。
        """
        from tools.p1 import money_guard
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "jobs"
            d.mkdir()
            ledger = A._ledger_for(d)
            self.assertEqual(money_guard.today_spent(ledger), 0.0)
            ok, why = money_guard.check(ledger, creds=None,
                                        balance_cache=99.0)   # 余额充足，只看日预算
        self.assertTrue(ok, why)


class TestSelection(Base):
    """挑谁、不挑谁 —— 这是整个功能的对错分界线。"""

    def test_only_auto_run_jobs(self):
        _job(self.root, "p1-20260101T0000-000001", auto=True)
        _job(self.root, "p1-20260101T0000-000002", auto=False)   # 没勾执行
        started = self.run_start()
        self.assertEqual(started, ["p1-20260101T0000-000001"],
                         f"只该启动 auto_run 的，实际 {started}")
        joined = " ".join(" ".join(c) for c in self.spawned)
        self.assertNotIn("000002", joined, "没勾执行的作业绝不能被自动拉起")

    def test_skips_non_created_states(self):
        _job(self.root, "p1-20260101T0000-000001", state="DONE", auto=True)
        _job(self.root, "p1-20260101T0000-000002", state="RUNNING", auto=True)
        _job(self.root, "p1-20260101T0000-000003", state="FAILED", auto=True)
        self.assertEqual(self.run_start(), [])

    def test_first_come_first_served(self):
        _job(self.root, "p1-20260101T0300-000003", auto=True,
             created="2026-01-01T03:00:00+08:00")
        _job(self.root, "p1-20260101T0100-000001", auto=True,
             created="2026-01-01T01:00:00+08:00")
        _job(self.root, "p1-20260101T0200-000002", auto=True,
             created="2026-01-01T02:00:00+08:00")
        started = self.run_start(cap=2)
        self.assertEqual(started, ["p1-20260101T0100-000001",
                                   "p1-20260101T0200-000002"],
                         "应按创建时间升序，先来先跑")

    def test_no_room_starts_nothing(self):
        _job(self.root, "p1-20260101T0000-000001", auto=True)
        self.assertEqual(self.run_start(cap=2, used=2), [])

    def test_respects_remaining_room(self):
        for i in range(5):
            _job(self.root, f"p1-20260101T000{i}-00000{i}", auto=True,
                 created=f"2026-01-01T00:0{i}:00+08:00")
        started = self.run_start(cap=3, used=1)
        self.assertEqual(len(started), 2, f"cap=3 used=1 → 只能启 2 个，实际 {started}")

    def test_limit_caps_starts(self):
        for i in range(4):
            _job(self.root, f"p1-20260101T000{i}-00000{i}", auto=True,
                 created=f"2026-01-01T00:0{i}:00+08:00")
        started = self.run_start(cap=4, limit=1)
        self.assertEqual(len(started), 1)

    def test_empty_dir_ok(self):
        self.assertEqual(self.run_start(), [])


class TestConcurrencySemantics(Base):
    """并发按**模型分组**算，不是按作业条数。

    同一模型的 5 个作业会挂在一台机上串行，只占 1 个槽。
    按条数判会把它们错当成"占满 2 槽"而拒绝启动 —— 那台机器明明还能接活。
    """

    def test_uses_slot_semantics_not_job_count(self):
        for i in range(4):
            _job(self.root, f"p1-20260101T000{i}-00000{i}", auto=True,
                 created=f"2026-01-01T00:0{i}:00+08:00", model="same_model")
        self.run_start(cap=2, used=1)
        self.assertTrue(self.spawned, "同模型多作业只占 1 槽，还有空位就该启动")

    def test_passes_slots_used_the_jobs_dir(self):
        """`slots_used` 必须收到我们的临时目录，否则会去读真实仓库的作业。"""
        _job(self.root, "p1-20260101T0000-000001", auto=True)
        seen = {}

        def fake_used(jobs_dir=None):
            seen["dir"] = jobs_dir
            return 0

        with mock.patch("tools.p2.concurrency.max_parallel", return_value=2), \
             mock.patch("tools.p2.concurrency.slots_used", fake_used):
            A.start_pending(self.root, spawn=self._spawn, say=self.say)
        self.assertEqual(seen.get("dir"), self.root)


class TestRobustness(Base):
    def test_spawn_failure_does_not_crash(self):
        _job(self.root, "p1-20260101T0000-000001", auto=True)
        _job(self.root, "p1-20260101T0000-000002", auto=True,
             created="2026-01-01T01:00:00+08:00")

        def boom(cmd, **kw):
            if "000001" in " ".join(cmd):
                raise OSError("spawn 失败")
            return FakeProc()

        with mock.patch("tools.p2.concurrency.max_parallel", return_value=2), \
             mock.patch("tools.p2.concurrency.slots_used", return_value=0):
            started = A.start_pending(self.root, spawn=boom, say=self.say)
        self.assertIn("p1-20260101T0000-000002", started,
                      "一个启动失败不该拖累后面的")
        self.assertTrue(any("启动失败" in s for s in self.said))

    def test_unreadable_concurrency_is_conservative(self):
        """读不到并发状态时宁可不动 —— 瞎启动会超建、按 ¥2.15/h 计费。"""
        _job(self.root, "p1-20260101T0000-000001", auto=True)
        with mock.patch("tools.p2.concurrency.max_parallel",
                        side_effect=RuntimeError("读不到")):
            started = A.start_pending(self.root, spawn=self._spawn, say=self.say)
        self.assertEqual(started, [])
        self.assertTrue(any("跳过补位" in s for s in self.said))

    def test_corrupt_job_json_skipped(self):
        d = self.root / "p1-20260101T0000-000001"
        d.mkdir(parents=True)
        (d / "job.json").write_text("{ 坏掉的 json", encoding="utf-8")
        _job(self.root, "p1-20260101T0000-000002", auto=True)
        started = self.run_start()
        self.assertEqual(started, ["p1-20260101T0000-000002"])

    def test_after_terminal_swallows_errors(self):
        """补位出错绝不能影响刚跑完的那个作业。"""
        with mock.patch.object(A, "start_pending", side_effect=RuntimeError("boom")):
            self.assertEqual(A.after_terminal("p1-x", self.root, say=self.say), [])
        self.assertTrue(any("不影响本作业" in s for s in self.said))


class TestWiring(unittest.TestCase):
    """源码接线：意图写入 + 终态触发。这两处断任何一处，功能就是死的。"""

    def _src(self, rel):
        return (_ROOT / rel).read_text(encoding="utf-8")

    def test_create_job_sets_auto_run_on_defer(self):
        src = self._src("webui/api.py")
        self.assertIn('spec["auto_run"] = True', src,
                      "并发满降级时必须把用户意图记下来")
        # 只在 defer 分支里设，不能在无条件路径上设
        idx = src.index('spec["auto_run"] = True')
        head = src[max(0, idx - 400):idx]
        self.assertIn("defer_reason", head, "auto_run 必须挂在 defer 分支上")

    def test_jobctl_persists_auto_run(self):
        src = self._src("tools/p1/jobctl.py")
        self.assertIn('job["auto_run"] = True', src,
                      "spec 里的 auto_run 必须落进 job.json，否则补位读不到")

    def test_schema_validates_auto_run(self):
        src = self._src("tools/p1/schema.py")
        self.assertIn("auto_run", src)
        self.assertIn("布尔", src, "要校验类型，否则 'true' 字符串会混进来")

    def test_run_triggers_autostart_on_terminal(self):
        src = self._src("tools/p1/run.py")
        self.assertIn("_with_autostart", src, "run() 必须挂补位")
        self.assertIn("autostart.after_terminal", src)
        # 必须判终态：失败但保留实例时槽位没让出来，急着补位会撞并发上限
        idx = src.index("def _with_autostart")
        blk = src[idx:idx + 900]
        self.assertIn("state.TERMINAL", blk,
                      "只有真正进入终态才该触发补位")

    def test_api_exposes_auto_run(self):
        src = self._src("webui/api.py")
        self.assertIn('"auto_run": bool(job.get("auto_run"))', src,
                      "列表接口要返回 auto_run，否则前端分不清排队和待执行")


if __name__ == "__main__":
    unittest.main()
