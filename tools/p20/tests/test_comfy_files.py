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
        """find 不再逐个列举名字，而是按后缀把候选全捞回来。

        改过一次（2026-11-10）：逐个 `-name` 精确比对，遇到
        `ltx_2.3_...` vs `ltx-2.3-...` 这种连接符差异就找不到，作业开完机
        才报资产不可见。现在由远端返回全部权重、本地归一后比对。

        这个测试因此改成断言"捞得全"，而不是"点名了某个文件"。
        """
        from tools.p20 import assets
        cmd = assets.build_find_command(self._model())
        self.assertIn("/model", cmd)
        self.assertIn("-iname", cmd, "必须忽略大小写")
        for ext in (".safetensors", ".ckpt", ".pt", ".pth", ".gguf"):
            self.assertIn(f"*{ext}", cmd, f"必须覆盖 {ext} 后缀")
        # 上限要够大：/model 实测 5211 个权重，head 太小会截断
        self.assertRegex(cmd, r"head -?(\d{4,})")
        n = int(cmd.rsplit("head ", 1)[1].lstrip("-"))
        self.assertGreaterEqual(n, 5000, "head 上限不能小于实测文件数")

    def test_find_command_does_not_enumerate_names(self):
        """反向断言：不再把每个文件名写进命令行（那样必然漏掉写法差异）。"""
        from tools.p20 import assets
        cmd = assets.build_find_command(self._model())
        self.assertNotIn("qwen3vl_32b_minimax_h3_nvfp4_awq", cmd,
                         "不该按名字枚举 —— 名字在不同机器上写法不同")

    def test_norm_name_equates_separators(self):
        """归一化只抹平连接符与大小写，版本号/精度必须保持可区分。"""
        from tools.p20 import assets
        a = assets._norm_name("ltx-2.3-22b-distilled-1.1_lora-dynamic_fro09_rank_111.safetensors")
        b = assets._norm_name("ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_rank_111.safetensors")
        self.assertEqual(a, b, "同一文件的两种写法必须归一到同一个键")
        self.assertNotEqual(assets._norm_name("rank_111"), assets._norm_name("rank_105"))
        self.assertNotEqual(assets._norm_name("seedvr2_7b_fp8"),
                            assets._norm_name("seedvr2_7b_int8"))

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
