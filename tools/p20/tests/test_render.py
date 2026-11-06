# -*- coding: utf-8 -*-
"""参数映射单测：与 P1 legacy 渲染等价 + 绑定/模型注入/失败场景。"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from tools.p1 import run as p1run
from tools.p20 import config_schema as cs
from tools.p20 import registry, render

ROOT = Path(__file__).resolve().parents[3]

CASES = {
    "qwen_image_t2i": dict(prompt="hello", neg_prompt="bad", seed=7,
                           width=1024, height=1024, steps=20, cfg=2.5,
                           sampler="euler", scheduler="simple", batch_size=1),
    "h3_t2v_121f": dict(prompt="hello", neg_prompt="bad", seed=7,
                        width=768, height=512, frames=121, steps=20, cfg=1.0),
    "h3_i2v_121f": dict(prompt="hello", neg_prompt="bad", image="frame.png", seed=7,
                        width=768, height=512, frames=121, steps=20, cfg=1.0),
    "ltxv_2b_t2v": dict(prompt="hello", neg_prompt="bad", seed=7,
                        width=768, height=512, frames=73, steps=30, cfg=3.0),
    "wan21_i2v_480p": dict(prompt="hello", neg_prompt="bad", image="frame.png", seed=7,
                           width=832, height=480, frames=49, steps=20, cfg=3.5),
}


def _norm_numeric(graph: dict) -> dict:
    """legacy 渲染会把 noise_seed 留成字符串；registry 按类型转 int（修复）。"""
    g = copy.deepcopy(graph)
    for n in g.values():
        for k, v in (n.get("inputs") or {}).items():
            if k in ("seed", "noise_seed") and isinstance(v, str) and v.lstrip("-").isdigit():
                n["inputs"][k] = int(v)
    return g


def _diff(a: dict, b: dict) -> list[str]:
    out = []
    for nid in sorted(set(a) | set(b)):
        ca, cb = (a.get(nid) or {}).get("class_type"), (b.get(nid) or {}).get("class_type")
        if ca != cb:
            out.append(f"{nid}: class {ca} vs {cb}")
            continue
        ia = (a.get(nid) or {}).get("inputs") or {}
        ib = (b.get(nid) or {}).get("inputs") or {}
        for k in sorted(set(ia) | set(ib)):
            if ia.get(k) != ib.get(k):
                out.append(f"{nid}.{k}: {ia.get(k)!r} vs {ib.get(k)!r}")
    return out


class TestEquivalence(unittest.TestCase):
    def test_registry_render_equals_legacy_for_all_workflows(self):
        for key, params in CASES.items():
            if key == "qwen_image_t2i":
                # v2 图新增 SAMPLER/SCHEDULER/BATCH_SIZE 占位符，legacy 渲染器按设计不认；
                # v1 的等价性已在 P2.0 交付时逐字段比对过。
                continue
            with self.subTest(workflow=key):
                wf = registry.get_workflow(key)
                model = registry.get_model(wf["model"]["key"])
                g1, _ = render.render(wf, model, params, "JOB123")
                legacy = p1run._render_workflow(
                    json.loads(json.dumps(wf["graph_obj"])), params, "JOB123")
                self.assertEqual(_diff(_norm_numeric(g1), _norm_numeric(legacy)), [])

    def test_ltxv_noise_seed_is_int_not_string(self):
        wf = registry.get_workflow("ltxv_2b_t2v")
        model = registry.get_model("ltxv_2b")
        g, _ = render.render(wf, model, CASES["ltxv_2b_t2v"], "J")
        self.assertIsInstance(g["ks"]["inputs"]["noise_seed"], int)

    def test_no_placeholder_left(self):
        wf = registry.get_workflow("qwen_image_t2i")
        model = registry.get_model("qwen_image")
        g, meta = render.render(wf, model, CASES["qwen_image_t2i"], "J")
        text = json.dumps(g, ensure_ascii=False)
        self.assertNotIn("{{", text)
        self.assertEqual(meta["prefix"], "J")
        self.assertEqual(g["save"]["inputs"]["filename_prefix"], "J")


class TestModelInjection(unittest.TestCase):
    def test_model_file_overrides_graph(self):
        wf = registry.get_workflow("qwen_image_t2i")
        model = copy.deepcopy(registry.get_model("qwen_image"))
        model["files"]["unet"]["name"] = "qwen/another.safetensors"
        g, _ = render.render(wf, model, CASES["qwen_image_t2i"], "J")
        self.assertEqual(g["unet"]["inputs"]["unet_name"], "qwen/another.safetensors")

    def test_clip_extra_injected(self):
        wf = registry.get_workflow("qwen_image_t2i")
        model = registry.get_model("qwen_image")
        g, _ = render.render(wf, model, CASES["qwen_image_t2i"], "J")
        self.assertEqual(g["clip"]["inputs"]["type"], "qwen_image")

    def test_missing_slot_role(self):
        wf = registry.get_workflow("wan21_i2v_480p")
        model = copy.deepcopy(registry.get_model("wan21_i2v"))
        model["files"].pop("clip_vision")
        with self.assertRaises(render.RenderError):
            render.render(wf, model, CASES["wan21_i2v_480p"], "J")


class TestFailureScenarios(unittest.TestCase):
    def test_binding_points_to_missing_node(self):
        wf = copy.deepcopy(registry.get_workflow("qwen_image_t2i"))
        wf["params"]["prompt"]["bindings"][0]["node"] = "nope"
        with self.assertRaises(render.RenderError):
            render.render(wf, registry.get_model("qwen_image"), CASES["qwen_image_t2i"], "J")

    def test_leftover_placeholder_detected(self):
        wf = copy.deepcopy(registry.get_workflow("qwen_image_t2i"))
        params = dict(CASES["qwen_image_t2i"])
        params.pop("batch_size")          # 少给一个参数 → 图里的 {{BATCH_SIZE}} 没人填
        with self.assertRaises(render.RenderError) as cm:
            render.render(wf, registry.get_model("qwen_image"), params, "J")
        self.assertIn("未绑定占位符", str(cm.exception))


class TestReadBack(unittest.TestCase):
    def test_read_and_check_params(self):
        wf = registry.get_workflow("h3_t2v_121f")
        model = registry.get_model("h3")
        params = CASES["h3_t2v_121f"]
        g, _ = render.render(wf, model, params, "J")
        got, warns = render.read_params(g, wf)
        self.assertEqual(warns, [])
        self.assertEqual(got["width"], 768)
        self.assertEqual(got["frames"], 121)
        job = {"params": params, "expect": {}}
        ok, detail = render.check_params(job, g, wf)
        self.assertTrue(ok)
        self.assertEqual(detail["diffs"], [])

    def test_param_mismatch_detected(self):
        wf = registry.get_workflow("h3_t2v_121f")
        model = registry.get_model("h3")
        g, _ = render.render(wf, model, CASES["h3_t2v_121f"], "J")
        job = {"params": dict(CASES["h3_t2v_121f"], width=1024)}
        ok, detail = render.check_params(job, g, wf)
        self.assertFalse(ok)
        self.assertTrue(detail["diffs"])

    def test_multi_binding_disagree_warns(self):
        wf = registry.get_workflow("h3_t2v_121f")
        model = registry.get_model("h3")
        g, _ = render.render(wf, model, CASES["h3_t2v_121f"], "J")
        g["neg"]["inputs"]["width"] = 640
        got, warns = render.read_params(g, wf)
        self.assertEqual(got["width"], 768)
        self.assertTrue(any("不一致" in w for w in warns))


class TestExpect(unittest.TestCase):
    def test_image_geometry(self):
        wf = registry.get_workflow("qwen_image_t2i")
        exp = render.expect_for(wf, {"width": 1024, "height": 1024})
        self.assertEqual(exp["width"], [1024])
        self.assertEqual(exp["ext"], "png")

    def test_expect_scale_for_2x_upscale(self):
        # Z-Image HQ 先 1024 采样，再 2x 细化到 2048；校验期望要跟着放大
        wf = registry.get_workflow("zimage_turbo_t2i_hq")
        exp = render.expect_for(wf, {"width": 1024, "height": 1024})
        self.assertEqual(exp["width"], [2048])
        self.assertEqual(exp["height"], [2048])
        exp = render.expect_for(wf, {"width": 768, "height": 512})
        self.assertEqual(exp["width"], [1536])
        self.assertEqual(exp["height"], [1024])

    def test_video_freeze_gate_default(self):
        wf = registry.get_workflow("ltxv_2b_t2v")
        exp = render.expect_for(wf, {"width": 768, "height": 512})
        self.assertEqual(exp["freeze"], {"ratio_max": 0.20, "longest_max_s": 1.0})

    def test_duration_follows_frames_override(self):
        # 回归：241 帧却沿用 121 帧的时长窗口 → 10.1s 的产物被判 FAIL（真实事故 job 4ff021）
        wf = registry.get_workflow("h3_t2v_121f")
        exp_default = render.expect_for(wf, {"width": 768, "height": 512, "frames": 121})
        self.assertEqual(exp_default["duration_s"], [5.0, 5.4])
        exp = render.expect_for(wf, {"width": 768, "height": 512, "frames": 241})
        lo, hi = exp["duration_s"]
        self.assertLessEqual(lo, 10.125)          # 实测 241 帧渲染出 243 帧 = 10.125s
        self.assertGreaterEqual(hi, 10.125)
        self.assertGreater(lo, 9.0)

    def test_duration_follows_seconds_override(self):
        # Wan 口径：帧数 = fps*duration+1；duration 改了窗口也要跟着改
        wf = registry.get_workflow("pack_video_wan2_2_14b_i2v")
        exp = render.expect_for(wf, {"width": 640, "height": 640, "duration": 5})
        self.assertEqual(exp["duration_s"], [4.76, 5.42])
        exp8 = render.expect_for(wf, {"width": 640, "height": 640, "duration": 8})
        self.assertGreater(exp8["duration_s"][0], 7.0)
        self.assertGreaterEqual(exp8["duration_s"][1], 8.0)

    def test_fps_declared_on_video_workflows(self):
        # fps 必须和图里的真实帧率一致：h3=24、ltxv=25（CreateVideo.fps）、wan=16
        want = {"h3_t2v_121f": 24, "h3_i2v_121f": 24, "ltxv_2b_t2v": 25,
                "wan21_i2v_480p": 16, "pack_video_wan2_2_14b_i2v": 16}
        for key, fps in want.items():
            self.assertEqual(registry.get_workflow(key).get("fps"), fps, key)

    def test_default_window_matches_declared_fps(self):
        # 默认帧数 ÷ fps 应落在配置的默认时长窗口里（防止 fps 写错）
        for key in ("h3_t2v_121f", "ltxv_2b_t2v", "wan21_i2v_480p"):
            wf = registry.get_workflow(key)
            fps = wf["fps"]
            n = (wf.get("params") or {}).get("frames", {}).get("default")
            lo, hi = wf["expect_defaults"]["duration_s"]
            self.assertTrue(lo <= n / fps <= hi, f"{key}: {n}/{fps}={n/fps:.2f} 不在 [{lo},{hi}]")

    def test_requested_size_overrides_config_default(self):
        wf = registry.get_workflow("qwen_image_t2i")
        exp = render.expect_for(wf, {"width": 768, "height": 1024})
        self.assertEqual(exp["width"], [768])
        self.assertEqual(exp["height"], [1024])

    def test_batch_images_resolved_from_param(self):
        wf = registry.get_workflow("qwen_image_t2i")
        for n in (1, 2, 4):
            exp = render.expect_for(wf, {"width": 1024, "height": 1024, "batch_size": n})
            self.assertEqual(exp["images"], n)
            g, _ = render.render(wf, registry.get_model("qwen_image"),
                                 dict(CASES["qwen_image_t2i"], batch_size=n), "J")
            self.assertEqual(g["lat"]["inputs"]["batch_size"], n)

    def test_images_dropped_when_batch_missing(self):
        wf = registry.get_workflow("qwen_image_t2i")
        exp = render.expect_for(wf, {"width": 1024, "height": 1024})
        self.assertNotIn("images", exp)

    def test_render_job(self):
        job = {
            "job_id": "p1-20261101T0000-abcdef",
            "model": {"key": "qwen_image",
                      "workflow": "workflows/qwen_image_t2i.api.json",
                      "registry": {"workflow_key": "qwen_image_t2i",
                                   "workflow_version": registry.get_workflow(
                                       "qwen_image_t2i")["version"],
                                   "model_key": "qwen_image",
                                   "model_version": 1}},
            "params": CASES["qwen_image_t2i"],
        }
        g, meta = render.render_job(job)
        self.assertEqual(meta["workflow_key"], "qwen_image_t2i")
        self.assertEqual(meta["compat"]["status"], "verified")
        self.assertEqual(g["save"]["inputs"]["filename_prefix"], job["job_id"])


if __name__ == "__main__":
    unittest.main()
