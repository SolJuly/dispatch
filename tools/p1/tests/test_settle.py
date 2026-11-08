#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：余额日结（settle）显性化 —— 不花钱，纯本地。

覆盖 2026-11-08 踩的坑：平台的镜像存储费不产生任何 API 事件，只在每天
01:10 做一次余额阶跃。只记 balance 的话它在账本里完全隐形，导致成本
分析漏算 ¥16.25（13 天）。

覆盖点：
  · 余额下降 → 产生 settle，字段齐全
  · 余额不变/上升 → 不产生 settle（充值不能记成负支出）
  · 浮点尾巴被 round 掉（8.459999999999999 → 8.46）
  · 有作业事件 → attribution=job；无活动 → idle
  · 回填幂等（跑两次不重复写）
  · _last_balance 能从大文件尾部正确读出（不整文件入内存）

用法：python3 tools/p1/tests/test_settle.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import cost_snapshot as cs  # noqa: E402
from scripts import backfill_settle as bf  # noqa: E402


def _bal(ts: int, at: str, balance: float) -> dict:
    return {"ts": ts, "at": at, "event": "balance", "balance": balance}


class TestBuildRecords(unittest.TestCase):
    def test_drop_creates_settle(self):
        prev = _bal(1794070802, "2026-11-08 01:00:02", 9.78)
        recs = cs.build_records(9.23, prev, now=1794071402, activity=[])
        self.assertEqual([r["event"] for r in recs], ["balance", "settle"])
        s = recs[1]
        self.assertAlmostEqual(s["amount"], 0.55, places=4)
        self.assertEqual(s["from"], 9.78)
        self.assertEqual(s["to"], 9.23)
        self.assertEqual(s["prev_at"], "2026-11-08 01:00:02")
        self.assertEqual(s["gap_s"], 600)
        self.assertEqual(s["attribution"], "idle")

    def test_no_settle_when_flat(self):
        prev = _bal(1000, "2026-11-08 01:00:02", 9.23)
        recs = cs.build_records(9.23, prev, now=1600, activity=[])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["event"], "balance")

    def test_no_settle_on_topup(self):
        """充值（余额上升）绝不能记成负支出。"""
        prev = _bal(1000, "2026-11-06 15:20:02", 7.99)
        recs = cs.build_records(21.99, prev, now=1600, activity=[])
        self.assertEqual(len(recs), 1)

    def test_float_tail_rounded(self):
        prev = _bal(1000, "2026-11-09 00:01:44", 9.01)
        recs = cs.build_records(9.01 - 0.55, prev, now=1600, activity=[])
        self.assertEqual(recs[0]["balance"], 8.46)
        self.assertEqual(recs[1]["to"], 8.46)
        self.assertAlmostEqual(recs[1]["amount"], 0.55, places=4)

    def test_noise_below_eps_ignored(self):
        prev = _bal(1000, "2026-11-08 01:00:02", 9.01)
        recs = cs.build_records(9.009, prev, now=1600, activity=[])
        self.assertEqual(len(recs), 1)

    def test_attribution_job_when_activity(self):
        prev = _bal(1000, "2026-11-08 13:50:02", 9.23)
        recs = cs.build_records(9.01, prev, now=1600, activity=["job", "terminate"])
        self.assertEqual(recs[1]["attribution"], "job")
        self.assertEqual(recs[1]["activity"], ["job", "terminate"])

    def test_gap_flag_when_long_silence(self):
        prev = _bal(1000, "2026-11-01 00:00:00", 50.0)
        recs = cs.build_records(40.0, prev, now=1000 + cs.MAX_GAP_S + 1, activity=[])
        self.assertTrue(recs[1].get("gap"))

    def test_no_prev(self):
        recs = cs.build_records(9.01, None, now=1600, activity=[])
        self.assertEqual(len(recs), 1)


class TestLastBalance(unittest.TestCase):
    def test_reads_tail_of_large_ledger(self):
        """账本上万行时不能整文件读进内存。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ledger.jsonl"
            with p.open("w", encoding="utf-8") as f:
                for i in range(5000):
                    f.write(json.dumps({"ts": i, "at": f"t{i}", "event": "job", "x": i}) + "\n")
                f.write(json.dumps(_bal(9000, "2026-11-08 01:10:02", 9.23)) + "\n")
            got = cs._last_balance(p)
            self.assertIsNotNone(got)
            self.assertAlmostEqual(got["balance"], 9.23, places=4)

    def test_missing_file(self):
        self.assertIsNone(cs._last_balance(Path("/nonexistent/x.jsonl")))


class TestBackfill(unittest.TestCase):
    def _rows(self):
        return [
            _bal(1000, "2026-11-08 01:00:02", 9.78),
            _bal(1600, "2026-11-08 01:10:02", 9.23),
            {"ts": 2500, "at": "2026-11-08 01:21:42", "event": "job", "cost": 0.1},
            _bal(2000, "2026-11-08 01:20:02", 9.23),   # 不变 → 无 settle
            _bal(3000, "2026-11-08 01:30:02", 9.00),   # 再降 0.23，窗口内有 job
        ]

    def test_build_settles(self):
        s = bf.build_settles(self._rows())
        self.assertEqual(len(s), 2)
        self.assertAlmostEqual(s[0]["amount"], 0.55, places=4)
        self.assertEqual(s[0]["attribution"], "idle")
        self.assertEqual(s[1]["attribution"], "job")   # 期间有 job 事件
        self.assertTrue(all(x.get("backfilled") for x in s))

    def test_summary(self):
        s = bf.build_settles(self._rows())
        st = bf.summarize(s)
        self.assertEqual(st["n"], 2)
        self.assertAlmostEqual(st["total"], 0.78, places=4)
        self.assertEqual(st["idle_n"], 1)

    def test_idempotent(self):
        rows = self._rows()
        s = bf.build_settles(rows)
        merged, added = bf.merge(rows, s)
        self.assertEqual(added, 2)
        # 再跑：已存在，不应新增
        merged2, added2 = bf.merge(merged, s)
        self.assertEqual(added2, 0)

    def test_settle_placed_after_its_balance(self):
        rows = self._rows()
        s = bf.build_settles(rows)
        merged, _ = bf.merge(rows, s)
        # settle 的 at 必须紧跟同 at 的 balance
        for i, r in enumerate(merged):
            if r.get("event") == "settle":
                self.assertEqual(merged[i - 1]["event"], "balance")
                self.assertEqual(merged[i - 1]["at"], r["at"])

    def test_end_to_end_write(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ledger.jsonl"
            with p.open("w", encoding="utf-8") as f:
                for r in self._rows():
                    f.write(json.dumps(r) + "\n")
            rc = bf.main(["--apply", "--ledger", str(p), "--no-backup"])
            self.assertEqual(rc, 0)
            rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
            settles = [r for r in rows if r["event"] == "settle"]
            self.assertEqual(len(settles), 2)
            # balance 一条不少
            self.assertEqual(len([r for r in rows if r["event"] == "balance"]), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
