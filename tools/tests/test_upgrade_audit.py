#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜像升级审计：判据必须分档，不能混成一个数字。

## 为什么这些测试重要

这个工具的失效方式不是"报错"，是**给出一个听起来严重的数字**：

    第一版："破坏性变更 5 处，6 条工作流受影响"
    真相：  那 5 处全是输出纯追加（[STRING] → [STRING, STRING]），
            6 条工作流全部真机跑通，全部只用 idx 0。

如果按第一版的结论决策，会**因为假警报放弃一次本来可用的升级**。
反过来，把 `LOST` 判成 `ADDED` 也危险 —— 那是真的会死，但没人知道。

所以每个测试都在守一条具体的分档判据：
  输出变短 = 坏；输出同长但错位 = 坏；输出**纯追加** = 好。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]   # tools/tests/x.py → 仓库根
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools import upgrade_audit as U  # noqa: E402


def _node(inputs: dict | None = None, output: list | None = None,
          optional: dict | None = None) -> dict:
    return {"input": {"required": inputs or {}, "optional": optional or {}},
            "output": output or []}


class TestDiffKinds(unittest.TestCase):
    def test_lost_class_is_breaking(self):
        d = U.diff_oi({"A": _node()}, {})
        self.assertEqual(d.lost, ["A"])
        self.assertEqual(d.by_kind("LOST")[0].severity, 3)

    def test_added_class_is_safe(self):
        d = U.diff_oi({}, {"A": _node()})
        self.assertEqual(d.safe[0].kind, "ADDED")
        self.assertFalse(d.breaking)

    def test_unchanged_is_silent(self):
        d = U.diff_oi({"A": _node({"x": ["INT", {}]}, ["IMAGE"])},
                      {"A": _node({"x": ["INT", {}]}, ["IMAGE"])})
        self.assertEqual(d.findings, [])

    # ---- 输出三态 ----
    def test_output_shrunk_is_breaking(self):
        d = U.diff_oi({"A": _node(output=["STRING", "STRING"])},
                      {"A": _node(output=["STRING"])})
        f = d.by_kind("OUTPUT")
        self.assertEqual(len(f), 1)
        self.assertIn("越界", f[0].detail)

    def test_output_pure_append_is_safe(self):
        """0.43.0 的 TextGenerate 就是这一类：[STRING] → [STRING, STRING]。

        真机上 6 条用到它的工作流全部跑通（都只用 idx 0）。
        判成破坏性变更 = 因为假警报放弃一次可用的升级。
        """
        d = U.diff_oi({"TextGenerate": _node(output=["STRING"])},
                      {"TextGenerate": _node(output=["STRING", "STRING"])})
        self.assertFalse(d.breaking, d.by_kind("OUTPUT"))
        self.assertEqual(d.by_kind("ADDED")[0].kind, "ADDED")
        self.assertIn("纯追加", d.by_kind("ADDED")[0].detail)

    def test_output_reorder_is_breaking(self):
        """数量相同但顺序变了 —— idx 1 从 STRING 变成 IMAGE，连线会错。"""
        d = U.diff_oi({"A": _node(output=["STRING", "IMAGE"])},
                      {"A": _node(output=["IMAGE", "STRING"])})
        f = d.by_kind("OUTPUT")
        self.assertEqual(len(f), 1)
        self.assertIn("错位", f[0].detail)

    def test_output_type_change_same_len_is_breaking(self):
        d = U.diff_oi({"A": _node(output=["IMAGE"])},
                      {"A": _node(output=["MASK"])})
        self.assertTrue(d.breaking)

    # ---- 输入 ----
    def test_new_required_without_default_is_breaking(self):
        d = U.diff_oi({"A": _node({"x": ["INT", {}]})},
                      {"A": _node({"x": ["INT", {}], "y": ["STRING", {}]})})
        f = d.by_kind("INPUT")
        self.assertEqual(len(f), 1)
        self.assertIn("y", f[0].detail)
        self.assertIn("无默认值", f[0].detail)

    def test_new_required_with_default_is_safe(self):
        """有默认值 = 服务端会补。

        2026-11-10 那一版没区分这一点，8 条工作流报了 53 个"缺输入"，
        大部分是误报 —— 照那个清单修会把能跑的工作流改坏。
        """
        d = U.diff_oi({"A": _node({"x": ["INT", {}]})},
                      {"A": _node({"x": ["INT", {}],
                                   "y": ["INT", {"default": 4}]})})
        self.assertFalse(d.breaking, d.by_kind("INPUT"))
        self.assertIn("带默认值", d.by_kind("ADDED")[0].detail)

    def test_removed_required_is_safe_but_noted(self):
        d = U.diff_oi({"A": _node({"x": ["INT", {}], "y": ["INT", {}]})},
                      {"A": _node({"x": ["INT", {}]})})
        self.assertFalse(d.breaking)
        self.assertIn("移除", d.by_kind("ADDED")[0].detail)

    def test_field_type_change_is_breaking(self):
        d = U.diff_oi({"A": _node({"x": ["INT", {}]})},
                      {"A": _node({"x": ["STRING", {}]})})
        f = d.by_kind("INPUT")
        self.assertEqual(len(f), 1)
        self.assertIn("INT → STRING", f[0].detail)

    def test_combo_type_normalized(self):
        """COMBO 的 spec[0] 是候选列表，不是类型名 —— 两次都要归一成 COMBO。

        不归一的话，候选值变了会被判成"类型变了"（全是误报）。
        """
        d = U.diff_oi({"A": _node({"x": [["a", "b"], {}]})},
                      {"A": _node({"x": [["a", "b", "c"], {}]})})
        self.assertFalse(d.breaking, d.findings)


class TestImpact(unittest.TestCase):
    """影响面要按**工作流**算，不能按节点数。"""

    WFS = {"wf_a": {"CheckpointLoaderSimple", "KSampler"},
           "wf_b": {"KSampler", "LoraLoader"},
           "wf_c": {"SaveImage"}}

    def test_lost_maps_to_workflows(self):
        d = U.diff_oi({"LoraLoader": _node()}, {})
        hit = U.attach_workflows(d, self.WFS)
        self.assertEqual(hit["LoraLoader"], ["wf_b"])
        self.assertIn("1/3", U.format_impact(d, self.WFS))

    def test_added_does_not_map(self):
        d = U.diff_oi({}, {"KSampler": _node()})
        hit = U.attach_workflows(d, self.WFS)
        self.assertEqual(hit, {}, "新增节点不该算作影响任何工作流")

    def test_shared_class_maps_to_all_owners(self):
        d = U.diff_oi({"KSampler": _node()}, {})
        hit = U.attach_workflows(d, self.WFS)
        self.assertEqual(hit["KSampler"], ["wf_a", "wf_b"])

    def test_no_impact_message(self):
        d = U.diff_oi({"KSampler": _node()}, {"KSampler": _node()})
        U.attach_workflows(d, self.WFS)
        self.assertIn("全部不受影响", U.format_impact(d, self.WFS))


class TestLoad(unittest.TestCase):
    def test_wrapped_snapshot(self):
        """`/object_info` 的返回被 `{"object_info": {...}}` 包一层也能读。"""
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "oi.json"
            p.write_text(json.dumps({"object_info": {"A": _node()}}), encoding="utf-8")
            self.assertEqual(list(U.load_oi(p)), ["A"])

    def test_bare_snapshot(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "oi.json"
            p.write_text(json.dumps({"A": _node()}), encoding="utf-8")
            self.assertEqual(list(U.load_oi(p)), ["A"])


class TestRealEvidence(unittest.TestCase):
    """拿仓库里真实的三份快照跑一遍 —— 防的是"判据只在小样例上对"。

    这三条断言就是 2026-11-12 那天的结论，写死在这里，
    下次升级时如果工具改坏了会立刻红。
    """

    def _audit(self, target: str):
        base = _ROOT / "reports/p2/p21/evidence/objectinfo-v4.json"
        tgt = _ROOT / target
        if not base.is_file() or not tgt.is_file():
            self.skipTest("证据快照不在仓库里")
        return U.diff_oi(U.load_oi(base), U.load_oi(tgt))

    def test_v4_to_core43_has_no_breaking_change(self):
        """方案 C（v4 只换核心到 0.43.0）零破坏性变更。

        真机已验证：节点 1757→1818 零丢失，SDXL/LTXV/SeedVR2 全部出片。
        这条一旦变红，说明判据被改得过于激进，或者新镜像真的坏了。
        """
        d = self._audit("reports/p20/evidence/oi-v4-core43.json")
        self.assertEqual(d.breaking, [], [f.detail for f in d.breaking])
        self.assertEqual(d.lost, [])
        self.assertGreater(d.target_n, d.base_n)

    def test_text_generate_pure_append_is_recorded_as_added(self):
        """0.43.0 里 TextGenerate 输出 [STRING]→[STRING,STRING]，必须归 ADDED。"""
        d = self._audit("reports/p20/evidence/oi-v4-core43.json")
        tg = [f for f in d.findings if f.class_type == "TextGenerate"]
        self.assertTrue(tg, "TextGenerate 应该有变化记录")
        self.assertEqual(tg[0].kind, "ADDED", tg[0].detail)

    def test_bare_0450_loses_custom_nodes(self):
        """裸 0.45.0 快照只有 1094 节点（无自定义节点包），必须报大量 LOST。

        这正是"不能拿裸镜像直接换"的证据：765 个节点类消失。
        """
        d = self._audit("reports/p20/evidence/oi-0450.json")
        self.assertGreater(len(d.lost), 500)
        self.assertLess(d.target_n, d.base_n)


if __name__ == "__main__":
    unittest.main()
