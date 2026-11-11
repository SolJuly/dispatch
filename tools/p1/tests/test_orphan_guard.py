#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：建机后的平台侧定时关机兜底。

## 为什么需要这层兜底

`_on_signal` 只在 SIGTERM / SIGINT 时执行。进程被 `kill -9`、被 OOM killer
干掉、或控制机重启时，**不走任何信号处理器**。那时 `_ORPHAN_UID` 只活在
内存里，实例仍在 Running 计费，而本地没有任何东西知道该关它。

实测（2026-11-11 23:40 riskmon R02）：
    运行中实例无定时关机 · 1 台 · p1-f76311
那台机器有作业在正常管，但**平台侧零兜底**。

## 覆盖点

  · 建机后立刻调 stop_at（不是等到失败/信号）
  · 时长 = max_minutes + 15 分钟余量
  · budget 缺失/非法 → 回落 45 分钟（与 _make_spec 默认一致）
  · stop_at 失败**不中断作业**，但要打日志说清楚（别让人以为有兜底）
  · 设的是平台侧 API，不是本地计时器
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import run as R  # noqa: E402


class FakeCS:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def stop_at(self, uid, ts):
        if self.fail:
            raise RuntimeError("API 不可用")
        self.calls.append((uid, ts))
        return True


class TestStopAtGuard(unittest.TestCase):

    def _job(self, max_minutes=None):
        j = {"job_id": "p1-test-000000"}
        if max_minutes is not None:
            j["budget"] = {"max_minutes": max_minutes}
        return j

    def test_sets_stop_at_after_provision(self):
        cs = FakeCS()
        with mock.patch.object(R, "_say", lambda *a, **k: None):
            R._set_stop_at_guard(cs, "uhost-abc", self._job(max_minutes=40))
        self.assertEqual(len(cs.calls), 1, "建机后必须设一次定时关机")
        uid, ts = cs.calls[0]
        self.assertEqual(uid, "uhost-abc")
        # 40 + 15 = 55 分钟
        import time
        delta_min = (ts - time.time()) / 60
        self.assertAlmostEqual(delta_min, 55, delta=1.5)

    def test_default_when_budget_missing(self):
        """budget 缺失时应回落 45 分钟 + 15 = 60，而不是不设或设成 0。"""
        cs = FakeCS()
        with mock.patch.object(R, "_say", lambda *a, **k: None):
            R._set_stop_at_guard(cs, "u1", self._job())
        import time
        _, ts = cs.calls[0]
        self.assertAlmostEqual((ts - time.time()) / 60, 60, delta=1.5)

    def test_illegal_budget_falls_back(self):
        for bad in ("abc", -5, 0, None, {}):
            with self.subTest(bad=bad):
                cs = FakeCS()
                j = {"job_id": "x", "budget": {"max_minutes": bad}}
                with mock.patch.object(R, "_say", lambda *a, **k: None):
                    R._set_stop_at_guard(cs, "u2", j)
                self.assertEqual(len(cs.calls), 1, f"budget={bad!r} 时仍必须设兜底")

    def test_failure_does_not_raise(self):
        """设不上定时关机**不能**中断作业 —— reaper 还在巡检兜底。

        但必须打日志。静默失败最糟：作业照跑，而人以为有平台兜底。
        """
        cs = FakeCS(fail=True)
        said = []
        with mock.patch.object(R, "_say", lambda *a, **k: said.append(" ".join(map(str, a)))):
            R._set_stop_at_guard(cs, "u3", self._job(max_minutes=30))   # 不该抛
        self.assertTrue(said, "失败必须打日志")
        joined = " ".join(said)
        self.assertIn("定时关机", joined)
        self.assertIn("reaper", joined, "要说明只剩 reaper 兜底了")

    def test_uses_platform_api_not_local_timer(self):
        """必须是平台侧 API —— 控制机挂掉时本地计时器一起消失，兜底就没了。"""
        cs = FakeCS()
        with mock.patch.object(R, "_say", lambda *a, **k: None):
            R._set_stop_at_guard(cs, "u4", self._job(max_minutes=40))
        self.assertTrue(hasattr(cs, "calls"), "应通过 cs.stop_at 走平台 API")
        self.assertEqual(len(cs.calls), 1)

    def test_idempotent_safe(self):
        """重复调用只是把时间往后推，不会报错（续跑场景）。"""
        cs = FakeCS()
        with mock.patch.object(R, "_say", lambda *a, **k: None):
            R._set_stop_at_guard(cs, "u5", self._job(max_minutes=40))
            R._set_stop_at_guard(cs, "u5", self._job(max_minutes=40))
        self.assertEqual(len(cs.calls), 2)


class TestProvisionGateExcludesSelf(unittest.TestCase):
    """续跑时必须排除自己那台实例，否则重启会自我否决。

    实测 p1-20261111T0058-8302f5：推理失败 restart，
    FAILED_RUN → RESUMING → PROVISIONING 只隔 3 秒，上一轮实例还在 Running，
    旧代码数到「云端 2 台（上限 2）」就把重启拒了 —— 其中一台是它自己。
    """

    def test_run_py_passes_own_uid(self):
        src = (_ROOT / "tools" / "p1" / "run.py").read_text(encoding="utf-8")
        self.assertIn("own_uid", src, "run.py 必须把本作业的 uhost_id 取出来")
        self.assertIn("exclude=", src, "必须把自身实例传给 check_provision 排除")
        # 确认取的是本作业自己的 instance.uhost_id
        idx = src.index("own_uid")
        self.assertIn("instance", src[idx:idx + 200])


if __name__ == "__main__":
    unittest.main()
