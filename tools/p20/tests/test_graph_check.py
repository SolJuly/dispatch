#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""graph_check 单测：把「哪些输入缺失会导致 400」这条判据钉死。

## 为什么值得单独测

这个判据错一次就要花真钱：2026-11-10 实测，`pack_image_qwen_image_edit_2511_int8`
缺 5 个输入，每条要建机 ¥0.10 才能发现一次。而且第一版检查器本身有 bug
（把 `values.a` 这种子键写成漏了），把已经配好的工作流报成"缺" ——
照那个清单修会把能跑的工作流改坏。

所以这里测的不是"函数能跑"，而是三条判据本身：

  ① required 里带 default    → **不算缺**（服务端补），漏判会让 20 多条工作流假红
  ② 动态类型 + 已有子键       → **不算缺**（`values.a` 就是 `values` 的子键）
  ③ 动态类型 + 没有子键       → 记为 dynamic（需实测），不是 missing（必须补）

用法：python3 -m unittest tools.p20.tests.test_graph_check
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p20 import graph_check as G  # noqa: E402


class TestKinds(unittest.TestCase):
    """分段判据：has_default > dynamic > missing，顺序不能换。"""

    def test_default_means_not_missing(self):
        """带 default 的 required 输入省略是合法的 —— ComfyUI 服务端会补。

        如果把这类算成"缺"，`EmptyFlux2LatentImage.batch_size` 这种
        （每个视频工作流都有）会让绝大多数工作流假红。
        """
        node = {"class_type": "X", "inputs": {}}
        oi = {"X": {"input": {"required": {
            "batch_size": ["INT", {"default": 1}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        self.assertEqual(r.missing, [])
        self.assertEqual(r.dynamic, [])

    def test_required_without_default_is_missing(self):
        node = {"class_type": "X", "inputs": {}}
        oi = {"X": {"input": {"required": {
            "strength": ["FLOAT", {"min": 0, "max": 10}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        self.assertEqual([m.field_name for m in r.missing], ["strength"])

    def test_dynamic_type_without_subkey_is_dynamic_not_missing(self):
        """AUTOGROW 没配 → 记为 dynamic（需实测），不能算 missing（必须补）。

        算成 missing 会让人去"改工作流"，而正确的下一步是"跑一次看看"。
        """
        node = {"class_type": "ComfyMathExpression",
                "inputs": {"expression": "a * 2"}}
        oi = {"ComfyMathExpression": {"input": {"required": {
            "expression": ["STRING", {"default": ""}],
            "values": ["COMFY_AUTOGROW_V3", {}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        self.assertEqual([m.field_name for m in r.dynamic], ["values"])
        self.assertEqual(r.missing, [])

    def test_dynamic_type_with_subkey_counts_as_present(self):
        """**踩过的 bug**：`values.a` 是 `values` 的子键写法，等价于已配。

        第一版只查 `fname in ins`，把这个判成"缺 values"，
        于是 fasth3 / minimax_h3 两条明明能跑的工作流被标红。
        """
        node = {"class_type": "ComfyMathExpression",
                "inputs": {"expression": "a * 2", "values.a": 5}}
        oi = {"ComfyMathExpression": {"input": {"required": {
            "expression": ["STRING", {"default": ""}],
            "values": ["COMFY_AUTOGROW_V3", {}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        self.assertEqual(r.dynamic, [], "有子键就不该报缺")
        self.assertEqual(r.missing, [])

    def test_dynamiccombo_with_subkey_counts_as_present(self):
        """DYNAMICCOMBO 同理：选了 key 之后子字段写成 `resize_type.width`。"""
        node = {"class_type": "ResizeImageMaskNode",
                "inputs": {"resize_type": "scale dimensions",
                           "resize_type.width": 512, "resize_type.height": 512}}
        oi = {"ResizeImageMaskNode": {"input": {"required": {
            "resize_type": ["COMFY_DYNAMICCOMBO_V3", {}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        self.assertEqual(r.dynamic, [])

    def test_dynamiccombo_needs_main_key_too(self):
        """DYNAMICCOMBO 必须主键 + 子键都给。

        踩过（2026-11-10）：7 处 ResizeImageMaskNode 只写了
        `resize_type.width/height` 没写 `resize_type`，ComfyUI 报
        "missing 1 required positional argument: 'resize_type'" ——
        它按主键分派实现，子键只是参数。
        """
        node = {"class_type": "ResizeImageMaskNode",
                "inputs": {"resize_type.width": 512, "resize_type.height": 512}}
        oi = {"ResizeImageMaskNode": {"input": {"required": {
            "resize_type": ["COMFY_DYNAMICCOMBO_V3", {}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        self.assertEqual(len(r.missing), 1)
        self.assertIn("缺主键", r.missing[0].field_name)

    def test_autogrow_does_not_need_main_key(self):
        """AUTOGROW 相反：天生用子键表达，没有"主键"概念。

        这条是上一条的负向对照 —— 把规则错误地套到 AUTOGROW 上，
        会把已经正确的 ComfyMathExpression 全判成缺（实测踩到）。
        """
        node = {"class_type": "ComfyMathExpression",
                "inputs": {"expression": "a * 2", "values.a": 5}}
        oi = {"ComfyMathExpression": {"input": {"required": {
            "expression": ["STRING", {"default": ""}],
            "values": ["COMFY_AUTOGROW_V3", {}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        self.assertEqual(r.missing, [], "AUTOGROW 不该要求主键")

    def test_combo_options_extracted(self):
        """COMBO 候选值要能取出来 —— 修图时要照着填合法值。"""
        node = {"class_type": "ImageScaleToTotalPixels", "inputs": {}}
        oi = {"ImageScaleToTotalPixels": {"input": {"required": {
            "upscale_method": ["COMBO", {"options": ["lanczos", "bilinear"]}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        m = r.missing[0]
        self.assertEqual(m.field_name, "upscale_method")
        self.assertEqual(m.options, ["lanczos", "bilinear"])

    def test_unconnected_link_is_reported_separately(self):
        """连接型输入没接线 → 图本身断了，报出来的类型要标「未连线」。

        这和"忘了填值"是两种毛病：前者从 objectinfo 里挑不出答案。
        """
        node = {"class_type": "VAEDecode", "inputs": {}}
        oi = {"VAEDecode": {"input": {"required": {
            "samples": ["LATENT", {}],
            "vae": ["VAE", {}],
        }}}}
        r = G._check_graph({"1": node}, oi, "t", "m")
        types = sorted(m.type_name for m in r.missing)
        self.assertTrue(all("未连线" in t for t in types), types)

    def test_unknown_class_reported(self):
        node = {"class_type": "NotInMirror", "inputs": {}}
        r = G._check_graph({"9": node}, {}, "t", "m")
        self.assertEqual(r.unknown_class, ["9:NotInMirror"])
        self.assertFalse(r.ok, "节点类不在镜像里，必须判为不可跑")


class TestTargetWorkflows(unittest.TestCase):
    """9 条实验工作流必须保持"无硬缺失"。

    这些是 2026-11-10 修过的：`qwen_image_edit_2511` 补了 5 个输入、
    `scail2` 两条各补 7 处、`ltx2_3/2_5` 补了音频 VAE 与文本编码器。
    任何一条退回缺失状态，都意味着有人改图时又漏了东西 —— 那会在真机上
    花 ¥0.10/条 才发现。
    """

    NINE = [
        "pack_image_flux2_klein_image_edit_4b_distilled",
        "pack_image_qwen_image_edit_2511_int8",
        "pack_image_qwen_image_layered",
        "pack_video_fasth3_ominiref_r2v",
        "pack_video_ltx2_3_flf2v",
        "pack_video_ltx2_5_flf2v",
        "pack_video_minimax_h3_r2v",
        "pack_video_wan21_scail2_character_replacement",
        "pack_video_wan21_scail2_character_replacement_int8",
    ]

    def setUp(self):
        self.oi = G.load_object_info()

    def test_no_hard_missing(self):
        for key in self.NINE:
            with self.subTest(workflow=key):
                r = G.check(key, self.oi)
                self.assertEqual(
                    [f"{m.node}:{m.class_type}.{m.field_name}" for m in r.missing],
                    [], f"{key} 又有硬缺失了")
                self.assertEqual(r.unknown_class, [],
                                 f"{key} 用了镜像里没有的节点")

    def test_checked_workflow_exists(self):
        """夹具前提：这 9 条都得在 registry 里，否则测的是空气。"""
        from tools.p20 import registry
        have = {w["key"] for w in registry.list_workflows()}
        for key in self.NINE:
            self.assertIn(key, have)


class TestGroups(unittest.TestCase):
    """同源工作流归并：修一条顶多条，这是降低工作量的关键。"""

    def test_same_missing_set_groups_together(self):
        r1 = G.Result("a", "m", 1, missing=[G.Missing("1", "X", "f", "missing")])
        r2 = G.Result("b", "m", 1, missing=[G.Missing("2", "X", "f", "missing")])
        g = G.groups([r1, r2])
        self.assertEqual(list(g.values()), [["a", "b"]])

    def test_clean_workflows_excluded(self):
        clean = G.Result("c", "m", 1)
        g = G.groups([clean])
        self.assertEqual(g, {})


if __name__ == "__main__":
    unittest.main()
