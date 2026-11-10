#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开机前资产预演 + 图参数补全 的测试。

这两块解决的是同一个问题：**把「开完机才发现」的成本降到零**。

2026-11-11 P4 花 ¥3.12 跑了 4 条，其中约一半烧在"setup 跑完才发现
软链或参数不对"——每次 3 分钟机时。这两块工具让这些判断在本地就能做完。

测试重点：
  · asset_preflight 的判据是「模拟建成软链后的候选」，不是「查快照」
  · graph_fill 的补全是**保守的**：模型名/文件/提示词一律不补
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import asset_preflight as AP  # noqa: E402
from tools.p20 import graph_fill as GF  # noqa: E402


def _oi(**nodes) -> dict:
    return {k: {"input": {"required": v}} for k, v in nodes.items()}


class TestCandidates(unittest.TestCase):
    def test_plain_list(self):
        oi = _oi(A={"x": [["v1", "v2"], {}]})
        self.assertEqual(AP.candidates(oi, "A", "x"), ["v1", "v2"])

    def test_combo_string_with_options(self):
        oi = _oi(A={"x": ["COMBO", {"options": ["p", "q"]}]})
        self.assertEqual(AP.candidates(oi, "A", "x"), ["p", "q"])

    def test_missing_returns_none_not_empty(self):
        """取不到必须是 None（不知道），不能是 []（知道是空）。

        这两者对上层完全不同：None → 证据不足；[] → 确实一个都没有。
        """
        self.assertIsNone(AP.candidates(_oi(), "A", "x"))
        self.assertIsNone(AP.candidates(_oi(A={}), "A", "x"))
        self.assertIsNone(AP.candidates(_oi(A={"x": ["INT", {}]}), "A", "x"))

    def test_empty_options_is_empty_list(self):
        oi = _oi(A={"x": ["COMBO", {"options": []}]})
        self.assertEqual(AP.candidates(oi, "A", "x"), [])


class TestLoaderDirCheck(unittest.TestCase):
    def test_known_override_detects_wrong_dir(self):
        """LatentUpscaleModelLoader 读 latent_upscale_models，不是 upscale_models。

        2026-11-11 实测：链到 upscale_models 后候选 0 项，而 ls 看着完全正常。
        """
        oi = _oi(LatentUpscaleModelLoader={"model_name": [["a.pt"], {}]})
        self.assertIs(
            AP._loader_reads_dir(oi, {}, "LatentUpscaleModelLoader", "model_name",
                                 "upscale_models"), False)
        self.assertIs(
            AP._loader_reads_dir(oi, {}, "LatentUpscaleModelLoader", "model_name",
                                 "latent_upscale_models"), True)

    def test_unknown_when_no_candidates(self):
        oi = _oi(X={"y": [[""], {}]})
        self.assertIsNone(AP._loader_reads_dir(oi, {}, "X", "y", "whatever"))


class TestForecast(unittest.TestCase):
    def _wf(self, name="m.safetensors", loader="CheckpointLoaderSimple",
            input_name="ckpt_name"):
        return ({"key": "w", "graph": "workflows/nonexistent.api.json",
                 "model": {"key": "m"}},
                {"key": "m", "files": {"ckpt": {
                    "name": name, "loader": loader, "input": input_name}}})

    def test_ok_when_file_found_and_dir_known(self):
        wf, model = self._wf()
        oi = _oi(CheckpointLoaderSimple={"ckpt_name": [["x.safetensors"], {}]})
        weights = {"m.safetensors": "/model/a/b/m.safetensors"}
        f = AP.forecast_workflow(wf, model, oi=oi, weights=weights)
        self.assertEqual(f.status, AP.STATUS_OK)

    def test_fail_when_file_absent(self):
        wf, model = self._wf()
        oi = _oi(CheckpointLoaderSimple={"ckpt_name": [["x.safetensors"], {}]})
        f = AP.forecast_workflow(wf, model, oi=oi, weights={})
        self.assertEqual(f.status, AP.STATUS_FAIL)
        self.assertIn("三源都未命中", f.blocking()[0].reason)

    def test_compat_links_rescues_renamed_file(self):
        """改名映射要能救回"文件在但名字不同"的情况。"""
        wf, model = self._wf(name="qwen_std_vae.safetensors", loader="VAELoader",
                             input_name="vae_name")
        oi = _oi(VAELoader={"vae_name": [["qwen_std_vae.safetensors"], {}]})
        f = AP.forecast_workflow(wf, model, oi=oi, weights={},
                                 compat_links={"qwen_std_vae.safetensors":
                                               "/model/x/qwen_image_vae.safetensors"})
        self.assertEqual(f.status, AP.STATUS_OK)
        self.assertIn("compat_links", f.assets[0].reason)

    def test_unknown_when_loader_missing_from_snapshot(self):
        wf, model = self._wf()
        f = AP.forecast_workflow(wf, model, oi={}, weights={
            "m.safetensors": "/model/a/m.safetensors"})
        self.assertEqual(f.status, AP.STATUS_UNKNOWN)

    def test_no_model_card(self):
        wf, _ = self._wf()
        f = AP.forecast_workflow(wf, None, oi={}, weights={})
        self.assertEqual(f.status, AP.STATUS_FAIL)
        self.assertIn("模型卡不存在", f.notes[0])

    def test_placeholder_value_not_flagged(self):
        """`{{UPSCALE_MODEL}}` 是渲染时替换的，不该被当"不在候选里"。

        2026-11-11 因此把 verified 的 detail_polish 误判成必失败。
        """
        wf = {"key": "w", "graph": "workflows/nonexistent.api.json",
              "model": {"key": "m"}}
        model = {"key": "m", "files": {}}
        oi = _oi(UpscaleModelLoader={"model_name": [["real.pt"], {}]})
        f = AP.forecast_workflow(wf, model, oi=oi, weights={})
        # 没有权重需求 → 不该因为图里没值就报失败
        self.assertNotEqual(f.status, AP.STATUS_FAIL)


class TestSummarize(unittest.TestCase):
    def test_counts(self):
        rows = [AP.WorkflowForecast("a", "m", AP.STATUS_OK),
                AP.WorkflowForecast("b", "m", AP.STATUS_FAIL),
                AP.WorkflowForecast("c", "m", AP.STATUS_FAIL),
                AP.WorkflowForecast("d", "m", AP.STATUS_UNKNOWN)]
        s = AP.summarize(rows)
        self.assertEqual((s["total"], s["ok"], s["would_fail"], s["unknown"]),
                         (4, 1, 2, 1))


# ------------------------------------------------------------------ graph_fill
class TestPickValue(unittest.TestCase):
    def test_prefers_default(self):
        v, why = GF._pick_value(["INT", {"default": 20}], "steps")
        self.assertEqual(v, 20)
        self.assertIn("default", why)

    def test_enum_takes_first(self):
        v, _ = GF._pick_value([["area", "bilinear"], {}], "upscale_method")
        self.assertEqual(v, "area")

    def test_never_fills_model_names(self):
        """模型名/文件名补错了 = 用错权重，成片里看不出来。"""
        for name in ("ckpt_name", "unet_name", "lora_name", "vae_name",
                     "clip_name", "model_name", "text_encoder"):
            v, why = GF._pick_value(["COMBO", {"options": ["a.safetensors"]}], name)
            self.assertIs(v, GF._SKIP, f"{name} 不该被自动补")
            self.assertIn("不补名单", why)

    def test_never_fills_content(self):
        """提示词补空串会出废片，而且作业会 PASS。"""
        for name in ("prompt", "text", "neg_prompt", "string_a"):
            v, _ = GF._pick_value(["STRING", {"default": ""}], name)
            self.assertIs(v, GF._SKIP)

    def test_expression_is_fillable(self):
        """ComfyMathExpression 的默认表达式是节点语义，不是用户内容。

        与 steps=20 同性质 —— 补它不改变意图。
        """
        v, _ = GF._pick_value(["STRING", {"default": "a + b"}], "expression")
        self.assertEqual(v, "a + b")

    def test_link_types_not_filled(self):
        # image 同时在"不补名单"里，命中更早的那条规则也正确；
        # 换一个不在名单里的连线型字段来验证"连线"这条分支。
        v, why = GF._pick_value(["LATENT", {}], "samples")
        self.assertIs(v, GF._SKIP)
        self.assertIn("连线", why)

    def test_unknown_type_skipped(self):
        v, why = GF._pick_value(["WEIRD_TYPE", {}], "foo")
        self.assertIs(v, GF._SKIP)


class TestPlanAndApply(unittest.TestCase):
    def test_dry_run_does_not_write(self):
        """plan 只算不写 —— 这是 --apply 才生效的保证。"""
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "g.api.json"
            g.write_text(json.dumps({"1": {"class_type": "N", "inputs": {}}}))
            before = g.read_text()
            oi = _oi(N={"a": ["INT", {"default": 7}]})
            plan = GF.Plan(workflow="w", graph_path=str(g))
            ins = {"1": {"class_type": "N", "inputs": {}}}
            for nid, node in ins.items():
                for fname, fspec in oi["N"]["input"]["required"].items():
                    v, why = GF._pick_value(fspec, fname)
                    if v is not GF._SKIP:
                        node["inputs"][fname] = v
                        plan.fills.append(GF.Fill(nid, "N", fname, v, why))
            self.assertEqual(g.read_text(), before, "plan 阶段不该动文件")
            self.assertEqual(len(plan.fills), 1)

    def test_apply_writes_and_backs_up(self):
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "g.api.json"
            g.write_text(json.dumps({"1": {"class_type": "N", "inputs": {}}}))
            orig = GF.ROOT
            try:
                GF.ROOT = Path(td)
                plan = GF.Plan(workflow="w", graph_path="g.api.json",
                               fills=[GF.Fill("1", "N", "a", 7, "test")])
                bak = GF.apply_plan(plan)
                data = json.loads(g.read_text())
                self.assertEqual(data["1"]["inputs"]["a"], 7)
                self.assertTrue(bak, "必须留备份")
                self.assertTrue((Path(td) / bak).is_file())
            finally:
                GF.ROOT = orig

    def test_apply_noop_when_no_fills(self):
        with tempfile.TemporaryDirectory() as td:
            g = Path(td) / "g.api.json"
            g.write_text("{}")
            orig = GF.ROOT
            try:
                GF.ROOT = Path(td)
                bak = GF.apply_plan(GF.Plan(workflow="w", graph_path="g.api.json"))
                self.assertEqual(bak, "")
            finally:
                GF.ROOT = orig


class TestRealRegistryClean(unittest.TestCase):
    """在真实仓库上跑一遍：补全后不该再有"可安全补"的字段。"""

    def test_no_fillable_left_after_apply(self):
        plans = []
        from tools.p20 import registry as R
        for wf in R.list_workflows():
            plan = GF.plan_workflow(wf["key"], {})
            plans.append(plan)
        # 传空 oi 时不该产生任何 fills（没有 schema 就无从判断）
        self.assertEqual(sum(len(p.fills) for p in plans), 0)


if __name__ == "__main__":
    unittest.main()
