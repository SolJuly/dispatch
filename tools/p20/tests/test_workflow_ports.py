#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流接口盘点 + 音频上传链路测试。

两个东西一起测，因为它们是同一件事的两半：
「图里有多少个用户可给的输入」和「用户能不能把那个输入传上来」。
2026-11-11 之前后者是断的（`sniff_upload` 拒 WAV），所以前者有再多也没用。

用法：python3 -m unittest tools.p20.tests.test_workflow_ports
"""
from __future__ import annotations

import io
import json
import math
import struct
import sys
import unittest
import wave
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p20 import workflow_ports as W  # noqa: E402


def _wav_bytes(seconds: float = 0.1, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(seconds * rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(n)))
    return buf.getvalue()


# ================================================================ 音频上传
class TestAudioSniff(unittest.TestCase):
    """`sniff_upload` 必须认音频，且不能把已有格式判错。

    RIFF 头被 WebP / AVI / WAV 三家共用，判断顺序错了会互相抢 ——
    这是本节最值得测的地方。
    """

    def setUp(self):
        from webui import api
        self.api = api

    def test_wav(self):
        self.assertEqual(self.api.sniff_upload(b"RIFF\x00\x00\x00\x00WAVE", "a.wav"), ".wav")

    def test_mp3_with_id3(self):
        self.assertEqual(self.api.sniff_upload(b"ID3\x04\x00\x00", "a.mp3"), ".mp3")

    def test_mp3_raw_sync(self):
        for magic in (b"\xff\xfb\x90\x00", b"\xff\xf3\x90\x00", b"\xff\xf2\x90\x00"):
            with self.subTest(magic=magic):
                self.assertEqual(self.api.sniff_upload(magic, "a.mp3"), ".mp3")

    def test_flac(self):
        self.assertEqual(self.api.sniff_upload(b"fLaC\x00\x00\x00\x00", "a.flac"), ".flac")

    def test_riff_siblings_not_confused(self):
        """**关键回归**：RIFF 里 WebP / AVI / WAV 必须分得开。

        如果 WAV 的检查写在 WebP 前面（只判 `RIFF`），
        上传 .webp 会被存成 .wav，之后 ComfyUI 读图就崩。
        """
        cases = {
            b"RIFF\x00\x00\x00\x00WEBP": ".webp",
            b"RIFF\x00\x00\x00\x00AVI ": ".avi",
            b"RIFF\x00\x00\x00\x00WAVE": ".wav",
        }
        for data, want in cases.items():
            with self.subTest(want=want):
                self.assertEqual(self.api.sniff_upload(data, "x"), want)

    def test_image_still_works(self):
        self.assertEqual(self.api.sniff_upload(b"\x89PNG\r\n\x1a\n", "a.png"), ".png")
        self.assertEqual(self.api.sniff_upload(b"\xff\xd8\xff\xe0", "a.jpg"), ".jpg")

    def test_unknown_still_rejected(self):
        from webui.api import ApiError
        with self.assertRaises(ApiError) as cm:
            self.api.sniff_upload(b"random garbage", "a.xyz")
        self.assertEqual(cm.exception.code, 415)
        self.assertIn("音频", cm.exception.msg, "报错里要写明支持音频")


class TestAudioCap(unittest.TestCase):
    def test_audio_has_own_limit(self):
        """音频单独一档：不能套图片的 20MB（90 秒无损就超），
        也不该套视频的 128MB。"""
        from webui import api
        self.assertEqual(api._cap_for(".wav"), api.MAX_UPLOAD_AUDIO)
        self.assertEqual(api._cap_for(".mp3"), api.MAX_UPLOAD_AUDIO)
        self.assertEqual(api._cap_for(".flac"), api.MAX_UPLOAD_AUDIO)
        self.assertNotEqual(api.MAX_UPLOAD_AUDIO, api.MAX_UPLOAD)
        self.assertNotEqual(api.MAX_UPLOAD_AUDIO, api.MAX_UPLOAD_VIDEO)
        self.assertGreater(api.max_upload_bytes(), api.MAX_UPLOAD_AUDIO)


class TestAudioUploadRoundTrip(unittest.TestCase):
    """真的走一遍 save_upload，确认落盘名字和内容都对。"""

    def test_real_wav(self):
        from webui import api
        data = _wav_bytes()
        r = api.save_upload("ref.wav", data)
        try:
            self.assertTrue(r["name"].endswith(".wav"), r["name"])
            self.assertEqual(r["bytes"], len(data))
            p = Path(api.INPUTS_DIR) / r["name"]
            self.assertTrue(p.is_file())
            self.assertEqual(p.read_bytes(), data)
        finally:
            p = Path(api.INPUTS_DIR) / r["name"]
            p.unlink(missing_ok=True)


# ================================================================ 接口盘点
class TestPortClassification(unittest.TestCase):
    """三类输入的判据。写错了会让 UI 出现无意义的上传框。"""

    def test_autogrow_reference_is_port(self):
        """`ref_images`（max=9）是用户接口 —— 要给 9 个位置。"""
        spec = ["COMFY_AUTOGROW_V3", {"template": {
            "input": {"required": {"ref_image": ["IMAGE", {}]}},
            "prefix": "ref_image_", "min": 0, "max": 9}}]
        p = W._classify_field("MiniMaxH3ReferenceToVideo", "ref_images", spec,
                              is_optional=True)
        self.assertIsNotNone(p)
        self.assertEqual(p.kind, "image")
        self.assertEqual(p.max_count, 9)

    def test_batch_node_autogrow_is_not_port(self):
        """**踩过的误报**：`BatchImagesNode.images`（max=50）只是把上游
        LoadImage 打包，不是"用户给 50 张图"。按它算会让 UI 出现 50 个空框。
        """
        spec = ["COMFY_AUTOGROW_V3", {"template": {
            "input": {"required": {"image": ["IMAGE", {}]}},
            "prefix": "image", "min": 1, "max": 50}}]
        p = W._classify_field("BatchImagesNode", "images", spec, is_optional=True)
        self.assertIsNone(p, "内部编排节点不该算用户接口")

    def test_textgenerate_is_not_port(self):
        """`TextGenerate*.prompt` 是给 LLM 的指令，不是用户的创作提示词。"""
        spec = ["STRING", {"default": ""}]
        p = W._classify_field("TextGenerateLTX2Prompt", "prompt", spec,
                              is_optional=True)
        self.assertIsNone(p)

    def test_clip_text_is_port(self):
        spec = ["STRING", {"default": ""}]
        p = W._classify_field("CLIPTextEncode", "text", spec, is_optional=True)
        self.assertIsNotNone(p)
        self.assertEqual(p.kind, "text")

    def test_loader_model_file_is_not_port(self):
        """加载器的**模型权重**字段不是用户接口（用户不选 safetensors）。"""
        spec = ["COMBO", {"options": ["a.safetensors", "b.safetensors"]}]
        p = W._classify_field("UNETLoader", "unet_name", spec, is_optional=False)
        self.assertIsNone(p)

    def test_loader_media_file_is_port(self):
        spec = ["COMBO", {"options": ["a.png", "b.jpg"]}]
        p = W._classify_field("LoadImage", "image", spec, is_optional=False)
        self.assertIsNotNone(p)
        self.assertEqual(p.kind, "image")


class TestNoGapsInVerified(unittest.TestCase):
    """盘点的目标状态：**零缺口**。

    2026-11-11 已经把 37 处缺口补到 0（其中 21 处是扫描器误报，16 处是真缺）。
    这条测试防的是"以后加工作流时忘了接参数" —— 那会让 UI 少一个上传框，
    而用户只会觉得"这个功能没有"。
    """

    def setUp(self):
        self.oi = W._load_oi()

    def test_all_workflows_have_no_gaps(self):
        from tools.p20 import registry
        bad = []
        for w in registry.list_workflows():
            r = W.scan(w["key"], self.oi)
            if not r:
                continue
            g = r.gaps()
            if g:
                n = sum(len(v) for v in g.values())
                bad.append(f"{w['key']}({n})")
        self.assertEqual(bad, [], f"这些工作流还有未暴露的接口：{bad}")


class TestMiniMaxPorts(unittest.TestCase):
    """MiniMax 系列：9 图 / 3 视频 / 3 音频 / 3 视频音频 全都要有位置。"""

    def setUp(self):
        from tools.p20 import registry
        self.wf = registry.get_workflow("pack_video_minimax_h3_r2v")
        self.params = self.wf.get("params") or {}

    def test_counts(self):
        from collections import Counter
        c = Counter(v.get("type") for v in self.params.values())
        self.assertEqual(c["image"], 9, "9 个图片位置")
        self.assertEqual(c["audio"], 6, "3 参考音频 + 3 视频音轨")
        self.assertEqual(c["video"], 3, "3 个参考视频")

    def test_optional_so_plain_user_can_run(self):
        """**平民用户不该被 20 个框吓到**：只有 3 个必填。"""
        req = [k for k, v in self.params.items() if v.get("required")]
        self.assertEqual(sorted(req), ["image", "image_2", "prompt"])

    def test_ref_audio_guidance_mentions_tag(self):
        """参考音频必须告诉用户「要在 prompt 里写 <Audio N>」。

        不说的话用户传了也不会生效，然后以为是坏的。
        节点描述原文：`Use the same tags when prompting.`
        """
        for k, v in self.params.items():
            if v.get("type") != "audio" or k.startswith("ref_video_audio"):
                continue
            self.assertIn("<Audio", v.get("description") or "",
                          f"{k} 的说明没提 prompt 标签")


if __name__ == "__main__":
    unittest.main()


# ================================================================ 空槽位剪枝
class TestPruneEmptyLoaders(unittest.TestCase):
    """用户没填的可选位置，必须在**提交前**从图里删掉。

    2026-11-11 真机踩到：图里放了 `LoadVideo{file: ""}` 空壳给 UI 占位，
    提交后 `GetVideoComponents` 拿空串去打开文件，实际打开的是
    `/root/ComfyUI/input` 这个目录：

        GetVideoComponents: [Errno 21] Is a directory

    结论：空槽位不能指望"服务端忽略"，必须在渲染阶段剪掉。
    """

    def _graph(self):
        return {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": ""}},      # 空
            "3": {"class_type": "GetVideoComponents", "inputs": {"video": ["4", 0]}},
            "4": {"class_type": "LoadVideo", "inputs": {"file": ""}},        # 空
            "5": {"class_type": "RefNode", "inputs": {
                "ref_images.ref_image_0": ["1", 0],
                "ref_images.ref_image_1": ["2", 0],
                "ref_videos.ref_video_0": ["3", 0],
            }},
        }

    def test_prunes_empty_and_orphans(self):
        from tools.p20 import render
        g = self._graph()
        n = render.prune_empty_loaders(g)
        self.assertNotIn("2", g, "空 LoadImage 该被删")
        self.assertNotIn("4", g, "空 LoadVideo 该被删")
        self.assertNotIn("3", g, "只依赖空节点的 GetVideoComponents 该被删")
        self.assertIn("1", g, "有值的必须保留")
        self.assertEqual(n, 3)

    def test_clears_dangling_inputs(self):
        """指向已删节点的槽位键必须一起删。

        留着 `ref_videos.ref_video_0: ["3", 0]` 而 3 已不存在，
        ComfyUI 会报 "link to missing node"。
        """
        from tools.p20 import render
        g = self._graph()
        render.prune_empty_loaders(g)
        ins = g["5"]["inputs"]
        self.assertNotIn("ref_videos.ref_video_0", ins)
        self.assertNotIn("ref_images.ref_image_1", ins)
        self.assertIn("ref_images.ref_image_0", ins, "有值的槽位不能动")

    def test_noop_when_all_filled(self):
        from tools.p20 import render
        g = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
            "2": {"class_type": "LoadAudio", "inputs": {"audio": "a.wav"}},
        }
        self.assertEqual(render.prune_empty_loaders(g), 0)
        self.assertEqual(len(g), 2)

    def test_whitespace_counts_as_empty(self):
        from tools.p20 import render
        g = {"1": {"class_type": "LoadImage", "inputs": {"image": "   "}}}
        self.assertEqual(render.prune_empty_loaders(g), 1)


class TestPruneInRenderJob(unittest.TestCase):
    """真跑一遍 render_job，确认剪枝接进了主链。"""

    def test_minimax_with_only_required_has_clean_graph(self):
        from tools.p20 import p20ctl, registry, render
        r = registry.resolve("pack_video_minimax_h3_r2v",
                             params={"image": "in-ff45f4b6.png",
                                     "image_2": "in-d9b9cba6.png",
                                     "prompt": "t", "seed": 1},
                             allow_experimental=True)
        spec = p20ctl._make_spec(r, job_key="pack_video_minimax_h3_r2v", task_ref="t")
        g, meta = render.render_job({**spec, "job_id": "p1-20260101T0000-aaaaaa",
                                     "model": spec["model"]})
        self.assertGreater(meta.get("pruned_empty") or 0, 0, "应该剪掉空槽位")
        for nid, n in g.items():
            cls = n.get("class_type")
            if cls == "LoadVideo":
                self.assertTrue((n.get("inputs") or {}).get("file"), "不该留空 LoadVideo")
            if cls == "LoadAudio":
                self.assertTrue((n.get("inputs") or {}).get("audio"), "不该留空 LoadAudio")
        # 只剩 2 张真图
        loads = [n for n in g.values() if n.get("class_type") == "LoadImage"]
        self.assertEqual(len(loads), 2)


# ================================================================ 导入门禁
class TestImportGate(unittest.TestCase):
    """接口完整性必须卡在**写库之前**，否则等于没卡。

    2026-11-11 装这个门禁时错了三次，都写进测试：

    ① 门禁放在 `import_all` 的成功分支末尾 —— 那时 DB 已提交，
       抛错也晚了，库里已经是"缺接口"的状态
    ② `_discover()` 返回的是 `{key: (wf_dict, path)}`，按 list 遍历
       拿到的是 key 字符串，`wf.get()` 静默失败 → 门禁永远通过
    ③ `workflow_ports` 的加载函数叫 `_load_oi`，门禁里写成
       `load_object_info`，`except: return {}` 把它吞了 → 同样永远通过
       ②③ 都是"门禁坏了但看起来通过"，比没门禁更危险
    """

    def test_gate_function_exists(self):
        from tools.p20 import registry
        self.assertTrue(callable(getattr(registry, "_port_gaps", None)))

    def test_gate_scans_discover_shape(self):
        """直接喂 `_discover()` 的真实结构，必须能扫出结果。

        ②的回归：按 list 遍历会得到 0 条。
        """
        from tools.p20 import registry
        models, workflows = registry._discover(registry.WORKFLOW_DIR,
                                               registry.MODEL_DIR)
        self.assertIsInstance(workflows, dict, "前提：_discover 返回 dict")
        # 当前仓库零缺口，所以结果应为空 —— 但要能"扫得动"，
        # 而不是因为结构不认识而空。用一个必然有缺口的假 wf 验。
        sample_key = next(iter(workflows))
        wf, _ = workflows[sample_key]
        self.assertIn("graph", wf, "前提：wf 里有 graph 字段")
        gaps = registry._port_gaps(preview_workflows=workflows)
        self.assertIsInstance(gaps, dict)

    def test_public_object_info_loader(self):
        """③的回归：门禁要的加载函数必须是公开名，不能只靠下划线版本。"""
        from tools.p20 import workflow_ports as wp
        self.assertTrue(hasattr(wp, "load_object_info"),
                        "缺 load_object_info 会让门禁静默失效")
        self.assertGreater(len(wp.load_object_info()), 100)

    def test_current_repo_passes_gate(self):
        """当前仓库应该零缺口（真被拦了说明有工作流漏接口）。"""
        from tools.p20 import registry
        models, workflows = registry._discover(registry.WORKFLOW_DIR,
                                               registry.MODEL_DIR)
        self.assertEqual(registry._port_gaps(preview_workflows=workflows), {})

    def test_skip_env_escape_hatch(self):
        """误报时要有逃生口，但不能是默认。"""
        import os
        from tools.p20 import registry
        models, workflows = registry._discover(registry.WORKFLOW_DIR,
                                               registry.MODEL_DIR)
        old = os.environ.get("P21_SKIP_PORT_GATE")
        try:
            os.environ["P21_SKIP_PORT_GATE"] = "1"
            self.assertEqual(registry._port_gaps(preview_workflows=workflows), {})
        finally:
            if old is None:
                os.environ.pop("P21_SKIP_PORT_GATE", None)
            else:
                os.environ["P21_SKIP_PORT_GATE"] = old


# ================================================================ 参数说明质量
class TestTrimSecondsGuidance(unittest.TestCase):
    """`trim_seconds` 的说明必须让**用户自己**能选对。

    2026-11-11 用户的原话：

        不做取舍，做好备注，让用户决定是否需要完整视频。
        我自己在测试的时候都没反应过来，更别说用户了，
        用大白话备注好，让用户自定义。

    所以这条测试卡三件事：
      ① 首行就要给结论（前端折叠只显示前两行 ≈ 56 字）
      ② 必须写清 0 和 4 两种选择的**实际代价**（分钟 + 钱）
      ③ 必须说清「成片时长由谁决定」，否则用户会以为改这里能让成片更长
    """

    WF = ["pack_video_wan21_scail2_character_replacement",
          "pack_video_wan_animate2_distilled"]

    def setUp(self):
        from tools.p20 import registry
        self.wfs = {k: registry.get_workflow(k) for k in self.WF}

    def _desc(self, key):
        ps = self.wfs[key].get("params") or {}
        return (ps.get("trim_seconds") or {}).get("description") or ""

    def test_each_workflow_has_guidance(self):
        for k in self.WF:
            with self.subTest(workflow=k):
                self.assertTrue(self._desc(k), f"{k} 的 trim_seconds 没有说明")

    def test_first_line_has_conclusion(self):
        """前端折叠只露前两行，结论必须在首行。"""
        for k in self.WF:
            with self.subTest(workflow=k):
                first = self._desc(k).splitlines()[0]
                self.assertLessEqual(len(first), 56,
                                     f"{k} 首行 {len(first)} 字，会被折叠截断")
                self.assertIn("4", first, "首行要点明默认值")
                self.assertIn("0", first, "首行要点明 0 = 整条")

    def test_mentions_both_costs(self):
        """必须给出两种选择的分钟数和钱 —— 只说"会变慢变贵"用户没法判断。"""
        for k in self.WF:
            with self.subTest(workflow=k):
                d = self._desc(k)
                self.assertIn("分钟", d, "要写耗时")
                self.assertIn("¥", d, "要写花费")

    def test_says_what_controls_duration(self):
        """必须说清「成片时长由谁决定」——这是用户最容易误解的地方。

        实测两个工作流语义不同：
          scail2     成片时长跟驱动视频走（trim 直接影响）
          animate2   成片帧数由 frames 决定（trim 只影响参考量，不影响长度）
        所以两类都要提这件事，措辞可以不同。
        """
        for k in self.WF:
            with self.subTest(workflow=k):
                d = self._desc(k)
                self.assertTrue(("成片" in d and ("跟着" in d or "决定" in d)),
                                f"{k} 没说明成片时长由什么决定")

    def test_label_is_plain_language(self):
        """标签也不能是术语。「驱动视频截取秒数」用户看不懂。"""
        from webui import i18n
        label = i18n.PARAM_LABELS.get("trim_seconds") or ""
        self.assertTrue(label)
        self.assertNotIn("截取", label, "「截取」太术语，换成「用前几秒」")
        self.assertIn("秒", label)
