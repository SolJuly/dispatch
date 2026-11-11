#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""花费闸：日预算 + 余额安全线 + 余额差校正。

## 为什么必须测

原来**只有调度器**有预算熔断，而调度器是死代码。真跑作业的 `autostart`
补位路径一道闸都没有：能连续拉起作业，每个建机 ¥2.15/h，一天 ¥200+ 无人拦。

余额 ¥17.92 时四台并发满负荷 = ¥206/天 → 4 小时烧光。这不是理论风险。

## 三个必须守住的点

  ① **读不到余额时放行**，不能把它当成 0（那会把所有作业堵死），
     也不能当成无穷（那就等于没这道闸）。
  ② **账本会低估**。实测 2026-11-12 01:00：账本最后一条 balance 是 ¥17.92，
     实时已经是 ¥17.37 —— 差 ¥0.55，账本里一条记录都没有。只信账本
     会烧到 ¥12 才熔断。所以要用余额差校正，取两者较大值。
  ③ **"没有基线"和"已花 0"是两回事**。当天还没采样过 balance 时，
     不能拿"当前余额"当基线（那会算出已花 0，永远不熔断）。
"""
from __future__ import annotations

import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import money_guard as G  # noqa: E402


def _ledger(tmp: Path, rows: list[dict]) -> Path:
    p = tmp / "ledger.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                 encoding="utf-8")
    return p


def _today() -> str:
    return datetime.date.today().isoformat()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, ledger=None, *, bal=None, cap=None, floor=None):
        """bal=None 表示读不到余额。"""
        patches = [
            mock.patch.object(G, "balance", return_value=bal),
        ]
        if cap is not None:
            patches.append(mock.patch.dict("os.environ", {"P1_DAILY_CAP": str(cap)}))
        if floor is not None:
            patches.append(mock.patch.dict("os.environ", {"P1_BALANCE_FLOOR": str(floor)}))
        for p in patches:
            p.start()
        try:
            return G.check(Path(ledger) if ledger else (self.root / "none.jsonl"))
        finally:
            for p in reversed(patches):
                p.stop()


class TestDailyCap(Base):
    def test_under_cap_allows(self):
        today = _today()
        led = _ledger(self.root, [
            {"at": f"{today} 00:01:00", "event": "balance", "balance": 100.0},
            {"at": f"{today} 00:30:00", "event": "job", "cost": 2.0},
        ])
        ok, why = self.check(led, bal=100.0, cap=8.0)
        self.assertTrue(ok, why)

    def test_over_cap_blocks(self):
        today = _today()
        led = _ledger(self.root, [
            {"at": f"{today} 00:01:00", "event": "balance", "balance": 100.0},
            {"at": f"{today} 00:30:00", "event": "job", "cost": 9.0},
        ])
        ok, why = self.check(led, bal=100.0, cap=8.0)
        self.assertFalse(ok)
        self.assertIn("日预算上限", why)
        self.assertIn("9.00", why)

    def test_cap_zero_means_unlimited(self):
        today = _today()
        led = _ledger(self.root, [
            {"at": f"{today} 00:01:00", "event": "balance", "balance": 100.0},
            {"at": f"{today} 00:30:00", "event": "job", "cost": 999.0},
        ])
        ok, _ = self.check(led, bal=100.0, cap=0, floor=0)
        self.assertTrue(ok, "P1_DAILY_CAP=0 表示显式关掉限制")


class TestBalanceFloor(Base):
    def test_below_floor_blocks(self):
        ok, why = self.check(bal=3.0, floor=5.0, cap=0)
        self.assertFalse(ok)
        self.assertIn("安全线", why)
        self.assertIn("3.00", why)

    def test_at_floor_allows(self):
        ok, _ = self.check(bal=5.0, floor=5.0, cap=0)
        self.assertTrue(ok, "等于安全线不算低于")

    def test_unreadable_balance_allows_but_says_so(self):
        """读不到余额要**放行**。

        这是最后兜底闸，不是主闸。网络抖一下把所有作业堵死，
        代价比偶尔多花几毛钱大得多。但必须说出来 ——
        静默放行会让人以为查过了。
        """
        ok, why = self.check(bal=None, floor=5.0, cap=0)
        self.assertTrue(ok)
        self.assertIn("读不到余额", why)


class TestReconciliation(Base):
    """账本低估必须被余额差纠正 —— 这是本模块最核心的一条。"""

    def test_balance_delta_beats_ledger(self):
        """账本只记 ¥0.20，余额实际少了 ¥5.00 → 必须按 ¥5.00 算。"""
        today = _today()
        led = _ledger(self.root, [
            {"at": f"{today} 00:01:00", "event": "balance", "balance": 100.0},
            {"at": f"{today} 00:30:00", "event": "job", "cost": 0.20},
        ])
        with mock.patch.object(G, "balance", return_value=95.0):
            spent, note = G.reconciled_spent(led)
        self.assertAlmostEqual(spent, 5.0, places=2)
        self.assertIn("余额差", note)

    def test_ledger_wins_when_larger(self):
        """账本比余额差大时用账本（余额可能有充值、退款等干扰）。"""
        today = _today()
        led = _ledger(self.root, [
            {"at": f"{today} 00:01:00", "event": "balance", "balance": 100.0},
            {"at": f"{today} 00:30:00", "event": "job", "cost": 3.0},
        ])
        with mock.patch.object(G, "balance", return_value=99.0):   # 只少 1.0
            spent, note = G.reconciled_spent(led)
        self.assertAlmostEqual(spent, 3.0, places=2)
        self.assertIn("账本", note)

    def test_no_baseline_falls_back_to_ledger(self):
        """当天没有 balance 采样时，不能拿当前余额当基线。

        否则 spent = 当前余额 − 当前余额 = 0，永远不熔断。
        """
        today = _today()
        led = _ledger(self.root, [
            {"at": f"{today} 00:30:00", "event": "job", "cost": 2.5},
        ])
        with mock.patch.object(G, "balance", return_value=50.0):
            spent, note = G.reconciled_spent(led)
        self.assertAlmostEqual(spent, 2.5, places=2)
        self.assertIn("无当日基线", note)

    def test_baseline_uses_first_sample_of_today(self):
        """基线取**当天第一条** balance，不是最后一条，也不是昨天的末值。

        跨零点时余额可能刚被 settle 扣过；用昨天末值当基线会把那笔算进今天。
        """
        today = _today()
        y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        led = _ledger(self.root, [
            {"at": f"{y} 23:50:00", "event": "balance", "balance": 200.0},
            {"at": f"{today} 00:01:00", "event": "balance", "balance": 100.0},
            {"at": f"{today} 06:00:00", "event": "balance", "balance": 98.0},
        ])
        self.assertEqual(G._opening_balance(led), 100.0)

    def test_balance_rising_does_not_go_negative(self):
        """充值导致余额上升时，余额差是负数 → 不能算成负花费。"""
        today = _today()
        led = _ledger(self.root, [
            {"at": f"{today} 00:01:00", "event": "balance", "balance": 10.0},
            {"at": f"{today} 00:30:00", "event": "job", "cost": 0.5},
        ])
        with mock.patch.object(G, "balance", return_value=100.0):   # 充值了
            spent, _ = G.reconciled_spent(led)
        self.assertGreaterEqual(spent, 0.0)
        self.assertAlmostEqual(spent, 0.5, places=2)


class TestThresholds(Base):
    def test_env_override(self):
        with mock.patch.dict("os.environ", {"P1_DAILY_CAP": "12.5"}):
            self.assertAlmostEqual(G.daily_cap(), 12.5)
        with mock.patch.dict("os.environ", {"P1_BALANCE_FLOOR": "20"}):
            self.assertAlmostEqual(G.balance_floor(), 20.0)

    def test_illegal_env_falls_back(self):
        for bad in ("abc", "", "  "):
            with self.subTest(bad=bad):
                with mock.patch.dict("os.environ", {"P1_DAILY_CAP": bad}):
                    self.assertAlmostEqual(G.daily_cap(), G.DEFAULT_DAILY_CAP)

    def test_negative_clamped_to_zero(self):
        with mock.patch.dict("os.environ", {"P1_DAILY_CAP": "-5"}):
            self.assertEqual(G.daily_cap(), 0.0)


class TestAutostartIntegration(unittest.TestCase):
    """补位必须真的走这道闸 —— 不接上等于没写。"""

    def test_autostart_calls_money_guard(self):
        src = (_ROOT / "tools" / "p1" / "autostart.py").read_text(encoding="utf-8")
        self.assertIn("money_guard", src, "autostart 必须调花费闸")
        self.assertIn("money_guard.check", src)
        # 必须在 spawn 之前判 —— 建机之后再拦就没意义了
        idx_check = src.index("money_guard.check")
        idx_spawn = src.index("spawn([")
        self.assertLess(idx_check, idx_spawn,
                        "花费闸必须在 spawn 之前，否则机器已经建了")

    def test_blocked_guard_starts_nothing(self):
        import tempfile
        from tools.p1 import autostart as A
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        d = root / "p1-x-000001"
        d.mkdir(parents=True)
        (d / "job.json").write_text(json.dumps({
            "job_id": "p1-x-000001", "state": "CREATED", "auto_run": True,
            "created_at": "2026-01-01T00:00:00+08:00",
            "model": {"key": "m1"}, "params": {}, "budget": {}}), encoding="utf-8")
        spawned = []
        said = []
        with mock.patch.object(G, "check", return_value=(False, "今日已花 ¥99")), \
             mock.patch("tools.p2.concurrency.max_parallel", return_value=2), \
             mock.patch("tools.p2.concurrency.slots_used", return_value=0):
            out = A.start_pending(root, spawn=lambda c, **k: spawned.append(c),
                                  say=lambda m: said.append(m))
        self.assertEqual(out, [], "闸拦住了就不能启动")
        self.assertEqual(spawned, [], "闸拦住时绝不该 spawn")
        self.assertTrue(any("停止补位" in s for s in said))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
