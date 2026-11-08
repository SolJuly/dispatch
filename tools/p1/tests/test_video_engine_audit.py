# -*- coding: utf-8 -*-
"""2026-11-07 视频引擎审计的回归：崩溃检测、驱动视频 trim/估价、主产物判定、时长期望。"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.p1 import run, verify
from tools.p20 import p20ctl, render
from webui import budget

ROOT = Path(__file__).resolve().parents[3]


class TestComfyAliveParse(unittest.TestCase):
    def test_wrapper_self_match_is_not_alive(self):
        """旧实现命中的是 ssh 的 bash 包装行，这里必须判死而不是放行。"""
        out = ("4321 bash -c pgrep -af 'ComfyUI.*main.py' | head -3; echo '---'; "
               "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader\n"
               "---PROC---\n000")
        self.assertIs(run._parse_comfy_alive(out, 0), False)

    def test_real_process_is_alive(self):
        out = ("1234 python -u main.py --listen 0.0.0.0 --port 8188\n"
               "---PROC---\n200")
        self.assertIs(run._parse_comfy_alive(out, 0), True)

    def test_http_ok_counts_as_alive(self):
        self.assertIs(run._parse_comfy_alive("---PROC---\n200", 0), True)

    def test_ssh_down_is_unknown(self):
        self.assertIsNone(run._parse_comfy_alive("ssh: connect to host: refused", 255))

    def test_probe_command_avoids_self_match(self):
        seen = {}

        def fake_ssh(host, port, pwd, cmd, timeout=600):
            seen["cmd"] = cmd
            return 0, "---PROC---\n0"

        with mock.patch.object(run, "_ssh_run", side_effect=fake_ssh):
            run._comfy_alive("h", 22, "p")
        self.assertNotIn("ComfyUI.*main.py", seen["cmd"])
        self.assertIn("[m]ain", seen["cmd"])
        self.assertIn("system_stats", seen["cmd"])


class TestVideoKeys(unittest.TestCase):
    def test_registry_video_param_names(self):
        for key, expect_key in (("pack_video_wan_animate2_distilled", "image_2"),
                                ("pack_video_wan_animate2", "video"),
                                ("pack_video_wan_vace_14b_v2v", "image_2")):
            wf = json.loads((ROOT / "registry/workflows" / f"{key}.json")
                            .read_text(encoding="utf-8"))
            self.assertIn(expect_key, p20ctl._video_keys(wf), key)

    def test_trim_covers_image_2(self):
        params = {"image_2": "in-abc.mp4", "frames": 77, "trim_seconds": 4}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "in-abc.mp4").write_bytes(b"\x00" * 16)

            def fake_ffmpeg(cmd, **kw):
                # 真实 ffmpeg 会写文件；旧 mock 不写也能"成功"，掩盖了残片问题
                Path(cmd[-1]).write_bytes(b"TRIMMED")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(p20ctl, "INPUTS_DIR", Path(d)), \
                    mock.patch("subprocess.run", side_effect=fake_ffmpeg) as sub:
                p20ctl._trim_video(params, video_keys={"image_2"})
            self.assertTrue(sub.called)
            self.assertIn("-t4-16fps", params["image_2"])
            self.assertTrue((Path(d) / params["image_2"]).is_file())

    def test_trim_ignores_undeclared_keys(self):
        params = {"image_2": "in-abc.mp4", "trim_seconds": 4}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "in-abc.mp4").write_bytes(b"\x00" * 16)
            with mock.patch.object(p20ctl, "INPUTS_DIR", Path(d)), \
                    mock.patch("subprocess.run") as sub:
                p20ctl._trim_video(params, video_keys={"video"})
            self.assertFalse(sub.called)
            self.assertEqual(params["image_2"], "in-abc.mp4")


class TestBudgetProbe(unittest.TestCase):
    def test_driving_video_frames_beat_template_default(self):
        params = {"image_2": "in-abc.mp4", "frames": 77}
        with mock.patch.object(budget, "_probe_frames", return_value=563):
            ratio = budget._scale(params, 77, 768, 512, video_keys={"image_2"})
        self.assertGreater(ratio, 5.0)

    def test_without_video_keys_keeps_template_default(self):
        params = {"image_2": "in-abc.mp4", "frames": 77}
        with mock.patch.object(budget, "_probe_frames", return_value=563):
            ratio = budget._scale(params, 77, 768, 512, video_keys=set())
        self.assertAlmostEqual(ratio, 1.0, places=3)

    def test_estimate_uses_workflow_schema(self):
        wf = json.loads((ROOT / "registry/workflows/pack_video_wan_animate2_distilled.json")
                        .read_text(encoding="utf-8"))
        with mock.patch.object(budget, "_probe_frames", return_value=563):
            est = budget.estimate(wf, {"image_2": "in-abc.mp4", "frames": 77})
        self.assertGreater(est["seconds"], 1000)


class TestVerifyPrimary(unittest.TestCase):
    def test_primary_comes_from_output_node_not_name_order(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d)
            (art / "00-primary-ComfyUI_00001.mp4").write_bytes(b"\x00" * 1024)
            (art / "01-ComfyUI_00002.png").write_bytes(b"\x00" * 64)
            job = {"expect": {"ext": "mp4", "min_bytes": 1}, "params": {}}
            wf_config = {"output": {"node": "246", "kind": "video"}}
            res = verify.verify_job(job, art, None, wf_config=wf_config)
        self.assertEqual(res["kind"], "video")
        self.assertEqual(res["primary"], "00-primary-ComfyUI_00001.mp4")

    def test_cover_png_does_not_downgrade_kind(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d)
            (art / "01-ComfyUI_00002.png").write_bytes(b"\x00" * 64)
            job = {"expect": {"ext": "png", "min_bytes": 1}, "params": {}}
            res = verify.verify_job(job, art, None,
                                    wf_config={"output": {"node": "9", "kind": "video"}})
        self.assertEqual(res["kind"], "video")


class TestGraphTiming(unittest.TestCase):
    def _wf(self, key):
        return json.loads((ROOT / "registry/workflows" / f"{key}.json")
                          .read_text(encoding="utf-8"))

    def test_static_math_resolves(self):
        dur, fps = render._graph_timing(self._wf("pack_video_ltx2_3_i2v"))
        self.assertEqual(fps, 25.0)
        self.assertAlmostEqual(dur, 5.04, places=2)

    def test_looped_workflow_is_skipped(self):
        dur, fps = render._graph_timing(self._wf("pack_video_wan_animate2"))
        self.assertIsNone(dur)
        self.assertIsNone(fps)

    def test_expect_gets_duration_window(self):
        exp = render.expect_for(self._wf("pack_video_minimax_h3_r2v"), {})
        self.assertIn("duration_s", exp)
        lo, hi = exp["duration_s"]
        self.assertLess(lo, 5.17)
        self.assertGreater(hi, 5.17)


class TestFramesConstraint(unittest.TestCase):
    """frames 默认值 81/77 必须能通过自己的校验（4n+1 惯例）。"""

    def test_4n_plus_1_is_valid(self):
        from tools.p20 import registry as reg
        spec = {"type": "int", "multiple": 4, "min": 17, "max": 201}
        self.assertEqual(reg._validate_value("frames", spec, 81), [])
        self.assertEqual(reg._validate_value("frames", spec, 77), [])

    def test_plain_multiple_is_valid(self):
        from tools.p20 import registry as reg
        spec = {"type": "int", "multiple": 4, "min": 17, "max": 201}
        self.assertEqual(reg._validate_value("frames", spec, 84), [])

    def test_other_values_still_rejected(self):
        from tools.p20 import registry as reg
        spec = {"type": "int", "multiple": 4, "min": 17, "max": 201}
        self.assertTrue(reg._validate_value("frames", spec, 83))

    def test_resolve_accepts_default_frames(self):
        from tools.p20 import registry as reg
        r = reg.resolve("pack_video_wan_animate2_distilled",
                        params={"image": "a.png", "image_2": "b.mp4"})
        self.assertEqual(r["params"]["frames"], 81)


class TestUploadStreaming(unittest.TestCase):
    """大文件走磁盘：内存只留缓冲，两份目录用硬链接避免双倍占用。"""

    def test_save_upload_file_dedupes_with_hardlink(self):
        import os as _os
        from webui import api
        if not api.INPUTS_DIR.exists():
            self.skipTest("inputs 目录未初始化")
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "big.mp4"
            src.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"1" * 4096)
            with mock.patch.object(api, "INPUTS_DIR", Path(d) / "inputs"), \
                    mock.patch.object(api, "SHARE_INPUTS", Path(d) / "share"):
                info = api.save_upload_file("big.mp4", src)
                a = (Path(d) / "inputs" / info["name"]).stat()
                b = (Path(d) / "share" / info["name"]).stat()
        self.assertEqual(info["bytes"], 4108)
        self.assertTrue(info["name"].startswith("in-"))
        self.assertEqual((a.st_dev, a.st_ino), (b.st_dev, b.st_ino), "应为硬链接而非两份拷贝")

    def test_big_file_is_rejected_before_copy(self):
        from webui import api
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "big.mp4"
            src.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"1" * 64)
            with mock.patch.object(api, "MAX_UPLOAD_VIDEO", 32):
                with self.assertRaises(api.ApiError) as cm:
                    api.save_upload_file("big.mp4", src)
        self.assertEqual(cm.exception.code, 413)


class TestEstimateBasis(unittest.TestCase):
    def test_measured_workflow_marked(self):
        e = budget.estimate({"key": "pack_video_wan2_2_14b_i2v"},
                            {"duration": 5, "width": 640, "height": 640})
        self.assertEqual(e["basis"], "measured")
        self.assertNotIn("未实测", e["hint"])

    def test_family_baseline_for_unmeasured(self):
        e = budget.estimate({"key": "pack_video_ltx2_3_i2v"},
                            {"width": 640, "height": 640})
        self.assertEqual(e["basis"], "family")
        self.assertIn("同族", e["hint"])

    def test_overrun_refused_before_submit(self):
        with self.assertRaises(budget.BudgetError) as cm:
            budget.check_budget({"key": "pack_video_ltx2_3_i2v",
                                 "limits": {"max_minutes": 5}},
                                {"width": 1280, "height": 720, "frames": 241}, 99)
        self.assertIn("运行上限", str(cm.exception))


class TestLegacyPrimary(unittest.TestCase):
    def test_legacy_without_prefix_prefers_expected_ext(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d)
            (art / "01-cover.png").write_bytes(b"\x00" * 64)
            (art / "02-main.mp4").write_bytes(b"\x00" * 1024)
            job = {"expect": {"ext": "mp4", "min_bytes": 1}, "params": {}}
            res = verify.verify_job(job, art, None)
        self.assertEqual(res["kind"], "video")
        self.assertEqual(res["primary"], "02-main.mp4")


class TestExposedSizeKnobs(unittest.TestCase):
    def test_new_width_height_params_resolve(self):
        from tools.p20 import registry
        r = registry.resolve("pack_utility_void_video_inpainting",
                             params={"image": "a.png"}, allow_experimental=True)
        self.assertEqual(r["params"]["width"], 672)
        self.assertEqual(r["params"]["height"], 384)
        exp = render.expect_for(r["workflow"], r["params"])
        self.assertEqual(exp["width"], [672])
        self.assertEqual(exp["height"], [384])


class TestGraphCacheFreshness(unittest.TestCase):
    """图内容变了，_graph_timing 必须重算（2026-11-08 审计发现：同秒同长度改写命中旧缓存）。"""

    def test_same_second_rewrite_is_seen(self):
        import json
        import tempfile
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as d:
            wf = _P(d) / "w.json"
            wf.write_text(json.dumps({
                "b": {"class_type": "WanVideoToVideo", "inputs": {"length": 121}},
                "a": {"class_type": "CreateVideo", "inputs": {"fps": 24}}}), encoding="utf-8")
            w = {"graph": str(wf), "kind": "video"}
            self.assertEqual(render._graph_timing(w)[1], 24.0)
            # 同长度内容改写：本环境上 mtime_ns/ctime_ns/size 全部不变
            wf.write_text(json.dumps({
                "b": {"class_type": "WanVideoToVideo", "inputs": {"length": 121}},
                "a": {"class_type": "CreateVideo", "inputs": {"fps": 25}}}), encoding="utf-8")
            dur, fps = render._graph_timing(w)
            self.assertEqual(fps, 25.0, "缓存返回了旧图")
            self.assertAlmostEqual(dur, 121 / 25, places=2)


class TestExpressionEvaluator(unittest.TestCase):
    """ComfyMathExpression 求值：覆盖 54 张图里全部 14 个真实表达式，且拒绝逃逸。

    2026-11-08 审计：原白名单缺 floor/ceil/位运算/比较，14 个真实表达式里 6 个被拒，
    这些工作流因此推不出 duration_s。
    """

    REAL = [
        "a", "a + 1", "a * b + 1", "a / 81", "a/2", "round (a * 24)",
        "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17",
        "min(a,b)", "floor (a * b + 1)", "floor((a - 2) / (b - 1))",
        "4 * ceil((a - b * c + c - 1) / 4) + 1", "a & ~1", "a > 2048",
        "(a % 4 == 1) and ((b - 1) % 4 == 0)",
    ]

    def test_all_real_expressions_evaluate(self):
        for e in self.REAL:
            with self.subTest(expr=e):
                self.assertIsNotNone(
                    render._eval_num_expr(e, {"a": 5.0, "b": 25.0, "c": 1.0}), e)

    def test_bitwise_needs_int_coercion(self):
        # 上游一律给 float；`a & ~1` 必须能把 5.0 当 5 处理
        self.assertEqual(render._eval_num_expr("a & ~1", {"a": 5.0}), 4.0)

    def test_bool_results_are_numeric(self):
        self.assertEqual(render._eval_num_expr("a > 2048", {"a": 5.0}), 0.0)
        self.assertEqual(render._eval_num_expr("a > 1", {"a": 5.0}), 1.0)

    def test_escapes_are_rejected(self):
        for e in ["__import__('os').system('echo x')", "open('/etc/passwd')",
                  "a if 1 else 0", "(lambda:1)()", "[x for x in range(3)]",
                  "a.b", "().__class__", "exec('1')", "globals()",
                  "a[0]", "{'k':1}"]:
            with self.subTest(expr=e):
                self.assertIsNone(render._eval_num_expr(e, {"a": 5.0}), e)


class TestTrimRobustness(unittest.TestCase):
    """截取失败/残片不能污染 params（2026-11-08 审计：0 字节残片会被当成已完成）。"""

    def test_failure_keeps_original_and_leaves_no_residue(self):
        import subprocess as sp
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "in-x.mp4").write_bytes(b"\x00" * 64)
            params = {"video": "in-x.mp4", "trim_seconds": 2}
            with mock.patch.object(p20ctl, "INPUTS_DIR", Path(d)), \
                    mock.patch("subprocess.run",
                               side_effect=sp.TimeoutExpired(cmd="ffmpeg", timeout=1)):
                p20ctl._trim_video(params, video_keys={"video"})
            self.assertEqual(params["video"], "in-x.mp4", "失败时应保留原始素材")
            names = sorted(x.name for x in Path(d).iterdir())
            self.assertEqual(names, ["in-x.mp4"], f"残留了中间文件：{names}")

    def test_zero_byte_output_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "in-x.mp4").write_bytes(b"\x00" * 64)
            stale = Path(d) / "in-x-t2-16fps.mp4"
            stale.write_bytes(b"")            # 上次失败留下的 0 字节残片
            params = {"video": "in-x.mp4", "trim_seconds": 2}

            def fake(cmd, **kw):
                Path(cmd[-1]).write_bytes(b"GOOD")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(p20ctl, "INPUTS_DIR", Path(d)), \
                    mock.patch("subprocess.run", side_effect=fake):
                p20ctl._trim_video(params, video_keys={"video"})
            self.assertEqual(params["video"], "in-x-t2-16fps.mp4")
            self.assertEqual(stale.read_bytes(), b"GOOD", "残片应被重新编码覆盖")


class TestEnumValidation(unittest.TestCase):
    """enum 参数校验（2026-11-08 审计：读 enum 键但配置用 values，校验全失效）。"""

    def test_invalid_enum_rejected(self):
        from tools.p20 import registry as reg
        with self.assertRaises(reg.RegistryError):
            reg.resolve("sdxl_t2i", params={"prompt": "x", "seed": 1,
                                            "sampler": "NO_SUCH_SAMPLER"})

    def test_valid_enum_accepted(self):
        from tools.p20 import registry as reg
        r = reg.resolve("sdxl_t2i", params={"prompt": "x", "seed": 1, "sampler": "euler"})
        self.assertEqual(r["params"]["sampler"], "euler")

    def test_all_enum_params_still_resolve(self):
        """所有带 values 的参数，默认值必须能通过校验。"""
        import json as _json, glob as _glob
        from pathlib import Path as _P
        from tools.p20 import registry as reg
        ok = 0
        for path in sorted(_glob.glob(str(ROOT / "registry/workflows/*.json"))):
            wf = _json.loads(_P(path).read_text(encoding="utf-8"))
            for name, spec in (wf.get("params") or {}).items():
                if not isinstance(spec, dict) or not spec.get("values"):
                    continue
                dflt = spec.get("default")
                if dflt is None:
                    continue
                self.assertIn(dflt, spec["values"], f"{wf['key']}.{name} 默认值不在域内")
                ok += 1
        self.assertGreater(ok, 0)


if __name__ == "__main__":
    unittest.main()
