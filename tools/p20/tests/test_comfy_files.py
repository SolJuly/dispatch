# -*- coding: utf-8 -*-
"""extract_files：只收 type=output 的产物，过滤输入回显与 temp 预览。"""
from __future__ import annotations

import unittest

from tools.p20 import comfy


class ExtractFilesTest(unittest.TestCase):
    def test_filters_input_and_temp(self):
        entry = {"outputs": {
            "1": {"images": [{"filename": "in-abc.mp4", "subfolder": "", "type": "input"}]},
            "7": {"images": [{"filename": "out_00001_.mp4", "subfolder": "v", "type": "output"}]},
            "9": {"images": [{"filename": "ComfyUI_temp_x.png", "subfolder": "", "type": "temp"}]},
        }}
        self.assertEqual(comfy.extract_files(entry), [("v", "out_00001_.mp4")])

    def test_default_type_is_output(self):
        entry = {"outputs": {"5": {"images": [{"filename": "a.png"}]}}}
        self.assertEqual(comfy.extract_files(entry), [("", "a.png")])

    def test_dedupes(self):
        entry = {"outputs": {
            "5": {"images": [{"filename": "a.png", "type": "output"}]},
            "6": {"images": [{"filename": "a.png", "type": "output"}]},
        }}
        self.assertEqual(comfy.extract_files(entry), [("", "a.png")])


if __name__ == "__main__":
    unittest.main()


class TestAutoLink(unittest.TestCase):
    """模型卡 → /model 自动找文件再软链（静态 compat_links.txt 覆盖不到新模型）。"""

    def _model(self):
        return {"files": {
            "clip": {"name": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
                     "loader": "CLIPLoader", "input": "clip_name"},
            "unet": {"name": "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors",
                     "loader": "UNETLoader", "input": "unet_name"},
            "vae_2": {"name": "minimax_h3_audio_vae_fp32.safetensors",
                      "loader": "VAELoader", "input": "vae_name"},
        }}

    def test_filenames_map_to_comfy_dirs(self):
        from tools.p20 import assets
        got = {n: d for d, n, _l in assets.model_filenames(self._model())}
        self.assertEqual(got["qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"], "text_encoders")
        self.assertEqual(got["fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors"],
                         "diffusion_models")
        self.assertEqual(got["minimax_h3_audio_vae_fp32.safetensors"], "vae")

    def test_find_command_lists_every_name(self):
        from tools.p20 import assets
        cmd = assets.build_find_command(self._model())
        self.assertIn("-name qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", cmd)
        self.assertIn("/model", cmd)

    def test_parse_find_output(self):
        from tools.p20 import assets
        out = ("qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors\t/model/x/y/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors\n"
               "noise line\n")
        got = assets.parse_find_output(out)
        self.assertEqual(got["qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"],
                         "/model/x/y/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")

    def test_link_script_reports_missing(self):
        from tools.p20 import assets
        found = {"qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": "/model/a/qwen.safetensors"}
        script, missing = assets.build_link_script(self._model(), found)
        self.assertIn("ln -sf /model/a/qwen.safetensors", script)
        self.assertIn("text_encoders", script)
        self.assertEqual(sorted(missing), [
            "fastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors",
            "minimax_h3_audio_vae_fp32.safetensors"])

    def test_link_model_files_calls_ssh_twice(self):
        from tools.p20 import assets
        calls = []

        def ssh(cmd, timeout=60):
            calls.append(cmd)
            if "find /model" in cmd:
                return 0, ("qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors\t"
                           "/model/q.safetensors\nfastvideo_fasth3_8step_v2_pruned_int8_convrot.safetensors\t"
                           "/model/f.safetensors\nminimax_h3_audio_vae_fp32.safetensors\t/model/a.safetensors\n")
            return 0, "LINK_DONE"

        missing = assets.link_model_files(self._model(), ssh)
        self.assertEqual(missing, [])
        self.assertEqual(len(calls), 2)
        self.assertIn("ln -sf", calls[1])
