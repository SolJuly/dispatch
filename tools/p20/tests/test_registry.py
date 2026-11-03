# -*- coding: utf-8 -*-
"""登记库单测：导入 / 兼容性 / 解析 / 版本快照 / 结构推导。"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.p20 import config_schema as cs
from tools.p20 import registry

ROOT = Path(__file__).resolve().parents[3]
WF_DIR = ROOT / "registry" / "workflows"
MODEL_DIR = ROOT / "registry" / "models"
COMPAT = ROOT / "registry" / "compat.json"


class RegistryBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        cls.compat = cls.base / "compat.json"
        shutil.copy(COMPAT, cls.compat)
        cls.db = cls.base / "registry.db"
        registry.import_all(cls.db, workflow_dir=WF_DIR, model_dir=MODEL_DIR,
                            compat_path=cls.compat, quiet=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestImport(RegistryBase):
    def test_counts(self):
        s = registry.stats(self.db)
        self.assertEqual(s["workflows"], 9)
        self.assertEqual(s["models"], 5)
        self.assertEqual(s["compat"], {"verified": 6, "equivalent": 2, "experimental": 1})

    def test_idempotent(self):
        before = registry.stats(self.db)
        registry.import_all(self.db, workflow_dir=WF_DIR, model_dir=MODEL_DIR,
                            compat_path=self.compat, quiet=True)
        after = registry.stats(self.db)
        self.assertEqual(before["workflows"], after["workflows"])
        self.assertEqual(before["compat"], after["compat"])

    def test_list_and_show(self):
        wfs = {w["key"]: w for w in registry.list_workflows(self.db)}
        self.assertEqual(len(wfs), 9)
        self.assertEqual(wfs["qwen_image_t2i"]["compat"]["status"], "verified")
        self.assertEqual(wfs["h3_i2v_121f"]["compat"]["status"], "experimental")
        w = registry.get_workflow("qwen_image_t2i", db_path=self.db)
        self.assertEqual(w["model"]["key"], "qwen_image")
        self.assertEqual(len(w["graph_obj"]), 9)
        m = registry.get_model("qwen_image", db_path=self.db)
        self.assertIn("unet", m["files"])

    def test_version_snapshots(self):
        cur = registry.get_workflow("qwen_image_t2i", db_path=self.db)["version"]
        w = registry.get_workflow("qwen_image_t2i", version=cur, db_path=self.db)
        self.assertTrue(w["snapshot"])
        self.assertEqual(len(w["graph_obj"]), 9)
        m = registry.get_model("qwen_image", version=1, db_path=self.db)
        self.assertTrue(m["snapshot"])
        with self.assertRaises(registry.RegistryError):
            registry.get_workflow("qwen_image_t2i", version=99, db_path=self.db)

    def test_missing_key(self):
        with self.assertRaises(registry.RegistryError):
            registry.get_workflow("nope", db_path=self.db)


class TestResolve(RegistryBase):
    def test_defaults_applied(self):
        r = registry.resolve("qwen_image_t2i", params={"prompt": "hi", "seed": 3},
                             db_path=self.db)
        self.assertEqual(r["params"]["width"], 1024)
        self.assertEqual(r["params"]["steps"], 20)
        self.assertEqual(r["params"]["cfg"], 2.5)
        self.assertEqual(r["params"]["neg_prompt"], "")
        self.assertIn("width", r["defaults_applied"])

    def test_missing_required(self):
        with self.assertRaises(registry.RegistryError) as cm:
            registry.resolve("qwen_image_t2i", params={"seed": 3}, db_path=self.db)
        self.assertIn("prompt 必填", str(cm.exception))

    def test_unknown_param(self):
        with self.assertRaises(registry.RegistryError) as cm:
            registry.resolve("qwen_image_t2i", params={"prompt": "hi", "seed": 3,
                                                       "banana": 1}, db_path=self.db)
        self.assertIn("banana", str(cm.exception))

    def test_range_and_multiple(self):
        with self.assertRaises(registry.RegistryError) as cm:
            registry.resolve("qwen_image_t2i", params={"prompt": "hi", "seed": 3,
                                                       "width": 1000}, db_path=self.db)
        self.assertIn("不是 16 的倍数", str(cm.exception))
        with self.assertRaises(registry.RegistryError) as cm:
            registry.resolve("qwen_image_t2i", params={"prompt": "hi", "seed": 3,
                                                       "steps": 999}, db_path=self.db)
        self.assertIn("上限", str(cm.exception))

    def test_experimental_gate(self):
        with self.assertRaises(registry.RegistryError) as cm:
            registry.resolve("h3_i2v_121f", params={"prompt": "hi", "seed": 1,
                                                    "image": "f.png"}, db_path=self.db)
        self.assertIn("兼容性未验证", str(cm.exception))
        r = registry.resolve("h3_i2v_121f", params={"prompt": "hi", "seed": 1,
                                                    "image": "f.png"},
                             allow_experimental=True, db_path=self.db)
        self.assertEqual(r["compat"]["status"], "experimental")

    def test_int_type(self):
        with self.assertRaises(registry.RegistryError) as cm:
            registry.resolve("qwen_image_t2i", params={"prompt": "hi", "seed": "abc"},
                             db_path=self.db)
        self.assertIn("类型应为 int", str(cm.exception))


class TestCompat(RegistryBase):
    def test_incompatible_blocks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        d = json.loads(self.compat.read_text(encoding="utf-8"))
        for en in d["entries"]:
            if en["workflow"] == "h3_t2v_121f" and en["model"] == "h3":
                en["status"] = "incompatible"
        p = base / "compat.json"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        db = base / "r.db"
        registry.import_all(db, workflow_dir=WF_DIR, model_dir=MODEL_DIR,
                            compat_path=p, quiet=True)
        with self.assertRaises(registry.RegistryError) as cm:
            registry.resolve("h3_t2v_121f", params={"prompt": "x", "seed": 1},
                             db_path=db)
        self.assertIn("兼容性拒绝", str(cm.exception))

    def test_equivalent_by_structure(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        wfdir = base / "workflows"
        shutil.copytree(WF_DIR, wfdir)
        clone = json.loads((WF_DIR / "qwen_image_t2i.json").read_text(encoding="utf-8"))
        clone["key"] = "qwen_image_t2i_portrait_test"
        clone["name"] = "clone"
        clone["params"]["width"]["default"] = 768
        clone["params"]["height"]["default"] = 1024
        (wfdir / "clone.json").write_text(json.dumps(clone, ensure_ascii=False), encoding="utf-8")
        db = base / "r.db"
        registry.import_all(db, workflow_dir=wfdir, model_dir=MODEL_DIR,
                            compat_path=self.compat, quiet=True)
        c = registry.compat_for("qwen_image_t2i_portrait_test", "qwen_image", db_path=db)
        self.assertEqual(c["status"], "equivalent")
        self.assertIn("structure-equal:qwen_image_t2i", c["basis"])
        self.assertTrue(c["evidence"])

    def test_set_compat_writes_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        compat = base / "compat.json"
        shutil.copy(COMPAT, compat)
        db = base / "r.db"
        registry.import_all(db, workflow_dir=WF_DIR, model_dir=MODEL_DIR,
                            compat_path=compat, quiet=True)
        r = registry.set_compat("h3_i2v_121f", "h3", "verified",
                                evidence=["p1-20261028T2145-85e4d5"], notes="test",
                                compat_path=compat, db_path=db)
        self.assertEqual(r["status"], "verified")
        d = json.loads(compat.read_text(encoding="utf-8"))
        self.assertTrue(any(e["workflow"] == "h3_i2v_121f" for e in d["entries"]))


    def test_import_rejects_bad_binding(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        wfdir = base / "workflows"
        shutil.copytree(WF_DIR, wfdir)
        p = wfdir / "qwen_image_t2i.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["params"]["prompt"]["bindings"][0]["node"] = "nope"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        db = base / "r.db"
        with self.assertRaises(cs.ConfigError) as cm:
            registry.import_all(db, workflow_dir=wfdir, model_dir=MODEL_DIR,
                                compat_path=self.compat, quiet=True)
        self.assertIn("不存在的节点", str(cm.exception))
        self.assertFalse(db.exists())

    def test_fingerprint_changes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        wfdir = base / "workflows"
        shutil.copytree(WF_DIR, wfdir)
        fp1 = registry.source_fingerprint(wfdir, MODEL_DIR, self.compat)
        p = wfdir / "qwen_image_t2i.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        d["name"] += " (edited)"
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        fp2 = registry.source_fingerprint(wfdir, MODEL_DIR, self.compat)
        self.assertNotEqual(fp1, fp2)


if __name__ == "__main__":
    unittest.main()
