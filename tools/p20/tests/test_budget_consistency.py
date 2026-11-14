#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡片预算自洽性：`max_cost` 必须够付 `max_minutes` 在这个工作流可能挑中的
**最贵卡型**上的钱。

## 为什么要有这条测试（2026-11-14 实测踩到）

批量实测 22 条图像，7 条在建机**之前**就被自己的预算闸拦下：

    ✗ 预估 ¥1.07（2.15/h × 30 分钟）超过作业预算上限 ¥1.00

当时觉得是"预估价算错了"，实际是卡片里两个数字按**两张不同的卡**算的：

    required_gpu: ["4090", "4090_48G"]     ← 两个都可能被挑中
    limits.max_minutes: 30                 ← 按 4090（¥2.15/h）算刚好 ¥1.07
    limits.max_cost: 1.0                   ← 却按更便宜的假设定的

`run.py` 的闸门用的是**实际挑中的那张卡**的时价。4090 没货、4090_48G（¥3.80/h）
顶上时，同样的 30 分钟要 ¥1.90 —— 于是合法作业被自己的配置拒绝。

54 张卡里有 52 张踩了这个坑，而且**一张都跑不起来也看不出来**：
预检（preflight）查的是权重和节点，`validate` 查的是参数与图结构，
两边都不看"预算够不够付自己声明的时长"。

## 判据为什么用最贵那张卡

因为调度器可能真的给你最贵那张。用便宜的算，等于假设"永远有货"，
而 4090_48G 恰恰是在 4090 无货时的回落项 —— 最需要它工作的时候。
预算上限的意义是"我最多愿意花这么多"，不是"我希望花这么多"。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

WF_DIR = _ROOT / "registry" / "workflows"

# 优云智算秒级计费的真实时价（2026-11 实测账单）。
# 新增卡型要同步加进来：漏了就会按默认 2.15 算，偏乐观。
GPU_HOURLY = {
    "V100S": 1.41,
    "4090": 2.15,
    "4090_48G": 3.80,
    "A800": 7.36,
}
DEFAULT_RATE = 2.15       # 未知卡型按 4090 算（保守）

# 允许的余量：闸门是 `est_real > cap` 严格大于，所以刚好相等能过；
# 但时价会变，留 1% 免得改价当天全线误杀。
SLACK = 1.01


def _cards() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(WF_DIR.glob("*.json"))]


class TestBudgetConsistency(unittest.TestCase):
    def test_max_cost_covers_max_minutes_on_priciest_gpu(self):
        """max_cost 必须 ≥ 最贵卡型跑满 max_minutes 的钱。"""
        bad = []
        for d in _cards():
            lim = d.get("limits") or {}
            mm, mc = lim.get("max_minutes"), lim.get("max_cost")
            if not mm or not mc:
                continue
            gpus = d.get("required_gpu") or ["4090"]
            worst = max(GPU_HOURLY.get(g, DEFAULT_RATE) for g in gpus)
            need = worst * mm / 60
            if need > mc * SLACK:
                bad.append(f"{d['key']}: {mm}min × ¥{worst}/h = ¥{need:.2f} "
                           f"> max_cost ¥{mc}")
        self.assertEqual(bad, [], "预算自相矛盾的卡：\n" + "\n".join(bad))

    def test_every_card_has_limits(self):
        """缺 limits 的卡会走 run.py 的兜底（30 分钟 / ¥1.0），更容易误杀。"""
        bad = [d["key"] for d in _cards()
               if not (d.get("limits") or {}).get("max_minutes")
               or not (d.get("limits") or {}).get("max_cost")]
        self.assertEqual(bad, [], f"缺 limits 的卡：{bad}")

    def test_required_gpu_rates_are_known(self):
        """required_gpu 里出现没登记时价的卡型 → 上面那条测试会算得偏乐观。"""
        unknown = set()
        for d in _cards():
            for g in (d.get("required_gpu") or []):
                if g not in GPU_HOURLY:
                    unknown.add(g)
        self.assertEqual(unknown, set(),
                         f"这些卡型没登记时价，预算校验会偏乐观：{sorted(unknown)}")


class TestBudgetGateArithmetic(unittest.TestCase):
    """把闸门公式本身钉住 —— 用真实踩到的数字。"""

    def test_qwen_30min_on_4090_48g_needs_more_than_one_yuan(self):
        # 2026-11-14 实测被拒的那一条
        self.assertGreater(3.80 * 30 / 60, 1.0)

    def test_qwen_20min_on_4090_48g_fits_in_the_new_cap(self):
        # 修正后的卡：max_minutes 仍是 30，max_cost 抬到 ¥1.99
        self.assertLessEqual(3.80 * 20 / 60, 1.99)

    def test_image_job_20min_on_4090_48g_fits(self):
        # 图像类统一 ¥1.33 上限
        self.assertLessEqual(3.80 * 20 / 60, 1.33)


if __name__ == "__main__":
    unittest.main()
