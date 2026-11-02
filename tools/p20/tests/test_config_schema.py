# -*- coding: utf-8 -*-
"""配置契约单测：真实配置全过 + 各类坏配置必须被挡住。"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from tools.p20 import config_schema as cs

ROOT = Path(__file__).resolve().parents[3]
WF_DIR = ROOT / "registry" / "workflows"
MODEL_DIR = ROOT / "registry" / "models"


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


class TestRealConfigs(unittest.TestCase):
    def test_all_real_configs_valid(self):
        for p in sorted(MODEL_DIR.glob("*.json")):
            with self.subTest(model=p.name):
                self.assertEqual(cs.validate_model(_load(p)), [])
        for p in sorted(WF_DIR.glob("*.json")):
            with self.subTest(workflow=p.name):
                self.assertEqual(cs.validate_workflow(_load(p), root=ROOT), [])
        compat = _load(ROOT / "registry" / "compat.json")
        self.assertEqual(cs.validate_compat(compat), [])

    def test_keys_are_unique(self):
        wf = [json.loads(p.read_text())["key"] for p in WF_DIR.glob("*.json")]
        md = [json.loads(p.read_text())["key"] for p in MODEL_DIR.glob("*.json")]
        self.assertEqual(len(wf), len(set(wf)))
        self.assertEqual(len(md), len(set(md)))


class TestModelValidation(unittest.TestCase):
    def setUp(self):
        self.m = copy.deepcopy(_load(MODEL_DIR / "qwen_image.json"))

    def test_bad_key(self):
        self.m["key"] = "Qwen-Image"
        self.assertTrue(any("key 非法" in e for e in cs.validate_model(self.m)))

    def test_missing_files(self):
        self.m["files"] = {}
        errs = cs.validate_model(self.m)
        self.assertTrue(any("files 应为非空" in e for e in errs))

    def test_bad_asset_match(self):
        self.m["files"]["unet"]["match"] = {"equals": "a", "contains": "b"}
        errs = cs.validate_model(self.m)
        self.assertTrue(any("必须且只能有" in e for e in errs))

    def test_bad_constraint(self):
        self.m["constraints"]["width"] = {"min": 100, "max": 50}
        errs = cs.validate_model(self.m)
        self.assertTrue(any("min > max" in e for e in errs))

    def test_primary_missing_role(self):
        self.m["primary"] = "vae"
        self.assertEqual(cs.validate_model(self.m), [])

    def test_model_signature_changes_with_weight(self):
        a = cs.model_signature(self.m)
        self.m["files"]["unet"]["name"] = "qwen/other.safetensors"
        self.assertNotEqual(a, cs.model_signature(self.m))


class TestWorkflowValidation(unittest.TestCase):
    def setUp(self):
        self.w = copy.deepcopy(_load(WF_DIR / "qwen_image_t2i.json"))

    def test_bad_binding_node(self):
        self.w["params"]["prompt"]["bindings"][0]["node"] = "nope"
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("不存在的节点/输入" in e for e in errs), errs)

    def test_uncovered_placeholder(self):
        self.w["params"].pop("prompt")
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("没有任何绑定覆盖" in e for e in errs), errs)

    def test_bad_default_type(self):
        self.w["params"]["steps"]["default"] = "twenty"
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("类型应为 int" in e for e in errs), errs)

    def test_missing_graph(self):
        self.w["graph"] = "workflows/__missing__.json"
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("graph 文件不存在" in e for e in errs), errs)

    def test_bad_output_node(self):
        self.w["output"]["node"] = "nope"
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("output.node 不存在" in e for e in errs), errs)

    def test_structure_signature_ignores_constants_but_not_nodes(self):
        g = _load(ROOT / self.w["graph"])
        sig = cs.structure_signature(g)
        g2 = copy.deepcopy(g)
        g2["lat"]["inputs"]["width"] = 768
        self.assertEqual(sig, cs.structure_signature(g2))
        g3 = copy.deepcopy(g)
        g3["extra"] = {"class_type": "LoadImage", "inputs": {"image": "x.png"}}
        self.assertNotEqual(sig, cs.structure_signature(g3))

    def test_enum_values_default_must_be_in_values(self):
        self.w["params"]["sampler"]["default"] = "not_a_sampler"
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("不在 values 内" in e for e in errs), errs)

    def test_enum_requires_values(self):
        self.w["params"]["sampler"].pop("values")
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("values 应为非空字符串数组" in e for e in errs), errs)

    def test_values_only_allowed_on_enum(self):
        self.w["params"]["steps"]["values"] = ["a", "b"]
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("只有 type=enum 才允许" in e for e in errs), errs)

    def test_count_reference_must_exist(self):
        self.w["output"]["count"] = "{{NOPE}}"
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("output.count 引用的参数不存在" in e for e in errs), errs)

    def test_count_reference_must_be_int_param(self):
        self.w["output"]["count"] = "{{SAMPLER}}"
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("只能引用 int 参数" in e for e in errs), errs)

    def test_images_must_be_int_or_template(self):
        self.w["expect_defaults"]["images"] = "four"
        errs = cs.validate_workflow(self.w, root=ROOT)
        self.assertTrue(any("expect_defaults.images" in e for e in errs), errs)

    def test_placeholder_sites(self):
        g = _load(ROOT / self.w["graph"])
        sites = cs.placeholder_sites(g)
        self.assertIn(("pos", "text", "PROMPT"), sites)
        self.assertIn(("ks", "seed", "SEED"), sites)


class TestCompatValidation(unittest.TestCase):
    def test_verified_requires_evidence(self):
        d = {"schema": cs.COMPAT_SCHEMA,
             "entries": [{"workflow": "qwen_image_t2i", "model": "qwen_image",
                          "status": "verified", "evidence": []}]}
        self.assertTrue(any("必须给出 evidence" in e for e in cs.validate_compat(d)))

    def test_bad_status(self):
        d = {"schema": cs.COMPAT_SCHEMA,
             "entries": [{"workflow": "a", "model": "b", "status": "maybe", "evidence": ["x"]}]}
        self.assertTrue(any("只允许 verified/incompatible" in e for e in cs.validate_compat(d)))


if __name__ == "__main__":
    unittest.main()
