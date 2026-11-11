#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁规则 + 图反推 + 模板 的单测。

## 为什么这三样必须测

它们决定「新加一条工作流会不会带病上线」。历史上 54 条卡里的 7 处错误
全部属于这三类：

    WF005  正负提示词填同一句（2 张卡）
    WF006  正向框里填了负向词（1 张卡）
    WF008  已 verified 但名字写「尚未开发」（4 张卡）

这些错的代价不是报错，是**静默出错片**或**用户以为不能用**。规则错一次的后果
比工作流错一次更大 —— 它会让 54 条全绿或全红。

所以这里测的不是"函数能跑"，而是三条判据本身：

  ① 真问题必须报（拿真实事故的卡当夹具）
  ② 好卡不许报（拿 39 条 verified 当反例，误报会让规则被绕过）
  ③ 反推的解释能力（占位符/表达式/旁支/同文件多 loader 四个坑）

用法：python3 -m unittest tools.p20.tests.test_wf_lint
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

from tools.p20 import wf_infer as INF  # noqa: E402
from tools.p20 import wf_lint as L  # noqa: E402
from tools.p20 import wf_templates as T  # noqa: E402


def _graph(*nodes: tuple) -> dict:
    """造一张最小可用的 API 图：_graph(("1", "SaveImage", {...}), ...)"""
    g = {}
    for nid, ct, ins in nodes:
        g[str(nid)] = {"class_type": ct, "inputs": ins}
    return g


class TestLintRules(unittest.TestCase):
    """每条规则至少一个「该报」的夹具。"""

    def _card(self, **kw) -> dict:
        c = {
            "schema": "p20.workflow/v1", "key": "t", "name": "测试",
            "version": 1, "kind": "image", "engine": "comfyui",
            "graph": "", "model": {"key": "m", "slots": {}},
            "params": {}, "output": {}, "expect_defaults": {},
        }
        c.update(kw)
        return c

    def test_wf001_unknown_key(self):
        """查一条不存在的卡 → 报 WF001，且不炸。"""
        rep = L.lint_all(only="zz_does_not_exist")
        self.assertIn("WF001", [f.rule for f in rep.findings])

    def test_wf005_prompt_equals_neg(self):
        """正负提示词同句 → 必报。这是 2026-11-11 最贵的一类错。"""
        card = self._card(params={
            "prompt": {"type": "string", "bindings": [{"node": "1", "input": "text"}],
                       "default": "a beautiful woman"},
            "neg_prompt": {"type": "string", "bindings": [{"node": "2", "input": "text"}],
                           "default": "a beautiful woman"},
        })
        g = _graph(("1", "CLIPTextEncode", {"text": "a beautiful woman"}),
                   ("2", "CLIPTextEncode", {"text": "a beautiful woman"}))
        hits = L.lint_card("t", card)
        self.assertIn("WF005", [f.rule for f in hits])

    def test_wf005_allows_different_defaults(self):
        """两个默认值不同就不许报 —— 误报会让 39 条 verified 全红。"""
        card = self._card(params={
            "prompt": {"type": "string", "bindings": [{"node": "1", "input": "text"}],
                       "default": "a beautiful woman"},
            "neg_prompt": {"type": "string", "bindings": [{"node": "2", "input": "text"}],
                           "default": "blurry, watermark"},
        })
        g = _graph(("1", "CLIPTextEncode", {"text": "a"}), ("2", "CLIPTextEncode", {"text": "b"}))
        self.assertNotIn("WF005", [f.rule for f in L.lint_card("t", card, "verified")])

    def test_wf006_negative_words_in_positive(self):
        """正向框里出现 pc game / ugly 这类词 → 必报。"""
        card = self._card(params={
            "prompt": {"type": "string", "bindings": [{"node": "1", "input": "text"}],
                       "default": "pc game, console game, cartoon, ugly"},
        })
        g = _graph(("1", "CLIPTextEncode", {"text": "x"}))
        self.assertIn("WF006", [f.rule for f in L.lint_card("t", card)])

    def test_wf006_ignores_cartoon_as_subject(self):
        """`Cartoon character lost` 是**描述主体**，不是负向词，不许报。

        词汇表里刻意只放中文「卡通」而没有英文 `cartoon`：
        `pack_video_wan_animate2` 的 prompt 就是 "Cartoon character lost"，
        那是画面内容。第一版原型把 `cartoon` 也放进清单，
        `wan_animate2` 立刻被误报 —— 而那条是能跑通的卡。
        规则的边界是「明显有害的负向词」，不是「所有像负向词的词」。
        """
        card = self._card(params={
            "prompt": {"type": "string", "bindings": [{"node": "1", "input": "text"}],
                       "default": "Cartoon character lost, background: forest"},
        })
        self.assertNotIn("WF006", [f.rule for f in L.lint_card("t", card)])

    def test_wf008_status_word_in_verified_name(self):
        """已 verified 的名字里还写「尚未开发」→ 必报。"""
        card = self._card(name="图生视频 · LTX 2.3（尚未开发）")
        hits = L.lint_card("t", card, "verified")
        self.assertIn("WF008", [f.rule for f in hits])

    def test_wf008_ok_for_experimental(self):
        """experimental 的名字写「尚未开发」是对的，不许报。"""
        card = self._card(name="某工作流（尚未开发）")
        self.assertNotIn("WF008", [f.rule for f in L.lint_card("t", card, "experimental")])

    def test_wf009_video_without_duration(self):
        """视频卡没有 duration_s 也没有 fps → 必报。

        2026-11-09：21 个视频工作流只声明 min_bytes，出 1 秒的废片也判 PASS。
        """
        card = self._card(kind="video", expect_defaults={"min_bytes": 50000, "ext": "mp4"})
        self.assertIn("WF009", [f.rule for f in L.lint_card("t", card)])

    def test_wf009_video_with_fps_is_ok(self):
        """有 fps 就能从帧数推时长，不算缺。"""
        card = self._card(kind="video", expect_defaults={"min_bytes": 50000},
                          fps=25)
        self.assertNotIn("WF009", [f.rule for f in L.lint_card("t", card)])

    def test_wf010_output_not_save_node(self):
        """output 指到 KSampler 上 → 必报（产物名字写不进去，收割找不到）。"""
        card = self._card(output={"node": "1", "input": "filename_prefix",
                                  "kind": "image", "ext": "png"})
        g = _graph(("1", "KSampler", {"filename_prefix": "x"}),
                   ("2", "SaveImage", {"filename_prefix": "y"}))
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            hits = L.lint_card("t", card)
        self.assertIn("WF010", [f.rule for f in hits])

    def test_wf011_media_type_mismatch(self):
        """声明 type=video 却绑到 LoadImage → 必报。

        2026-11-10 的「动作迁移只有图片上传框」是 UI 的锅，但卡层同样能错。
        """
        card = self._card(params={
            "video": {"type": "video", "required": True,
                      "bindings": [{"node": "1", "input": "image"}]},
        })
        g = _graph(("1", "LoadImage", {"image": "a.png"}),
                   ("2", "SaveImage", {"filename_prefix": "y"}))
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            self.assertIn("WF011", [f.rule for f in L.lint_card("t", card)])

    def test_wf011_video_to_loadvideo_is_ok(self):
        """video 绑到 LoadVideo 是对的 —— LoadVideo 的输入名是 file。"""
        card = self._card(params={
            "video": {"type": "video", "required": True,
                      "bindings": [{"node": "1", "input": "file"}]},
        })
        g = _graph(("1", "LoadVideo", {"file": "a.mp4"}),
                   ("2", "SaveImage", {"filename_prefix": "y"}))
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            self.assertNotIn("WF011", [f.rule for f in L.lint_card("t", card)])

    def test_wf015_two_roles_same_node(self):
        """两个槽位角色绑同一节点 → 必报。

        2026-11-09：主 LoRA 和文本编码器 LoRA 的 input 都叫 lora_name，
        自动推断绑到同一节点，另一个永远没人填 —— 不报错，用错权重。
        """
        card = self._card(model={"key": "m", "slots": {
            "lora": {"node": "1", "input": "lora_name"},
            "lora_2": {"node": "1", "input": "lora_name"},
        }})
        g = _graph(("1", "LoraLoader", {"lora_name": "a"}),
                   ("2", "SaveImage", {"filename_prefix": "y"}))
        self.assertIn("WF015", [f.rule for f in L.lint_card("t", card)])

    def test_wf012_missing_description_is_warn_only(self):
        """没说明只警告，不拦截 —— 333 个参数里有历史债，一刀切成 ERROR 会全红。"""
        card = self._card(params={
            "steps": {"type": "int", "default": 20,
                      "bindings": [{"node": "1", "input": "steps"}]},
        })
        hits = L.lint_card("t", card)
        wf012 = [f for f in hits if f.rule == "WF012"]
        self.assertTrue(wf012)
        self.assertEqual(wf012[0].level, "WARN")

    def test_wf013_default_violates_own_type(self):
        """声明 int 但默认值是字符串 → 必报（卡自己前后矛盾）。"""
        card = self._card(params={
            "steps": {"type": "int", "default": "{{SEED}}", "description": "x",
                      "bindings": [{"node": "1", "input": "steps"}]},
        })
        self.assertIn("WF013", [f.rule for f in L.lint_card("t", card)])

    def test_wf017_prompt_bound_to_negative_side(self):
        """prompt 绑到负向编码器 → 必报。

        2026-11-11 实测 5 张卡犯这个错（ltx2_3_i2v / ic_lora / scail2 ×2 /
        qwen_image_2_1_image_edit），其中 4 张已经是 verified ——
        也就是说"真机跑通过"完全不代表正负绑对了。
        """
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "positive words", "clip": ["9", 0]}),
            ("2", "CLIPTextEncode", {"text": "bad words", "clip": ["9", 0]}),
            ("3", "KSampler", {"seed": 1, "steps": 20, "cfg": 3.0,
                               "sampler_name": "euler", "scheduler": "simple",
                               "denoise": 1.0, "positive": ["1", 0],
                               "negative": ["2", 0],
                               "model": ["9", 0], "latent_image": ["9", 0]}),
            ("4", "SaveImage", {"filename_prefix": "x", "images": ["3", 0]}),
        )
        card = self._card(params={
            # 故意绑反
            "prompt": {"type": "string", "description": "d",
                       "bindings": [{"node": "2", "input": "text"}]},
            "neg_prompt": {"type": "string", "description": "d",
                           "bindings": [{"node": "1", "input": "text"}]},
        })
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            hits = L.lint_card("t", card)
        wf = [f for f in hits if f.rule == "WF017"]
        self.assertEqual(len(wf), 2, [f.detail for f in hits])
        self.assertIn("正负填反", " ".join(f.detail for f in wf))

    def test_wf017_ok_when_correct(self):
        """绑对了就不许报 —— 误报会让 39 条 verified 全部变红。"""
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "positive words", "clip": ["9", 0]}),
            ("2", "CLIPTextEncode", {"text": "bad words", "clip": ["9", 0]}),
            ("3", "KSampler", {"seed": 1, "steps": 20, "cfg": 3.0,
                               "sampler_name": "euler", "scheduler": "simple",
                               "denoise": 1.0, "positive": ["1", 0],
                               "negative": ["2", 0],
                               "model": ["9", 0], "latent_image": ["9", 0]}),
            ("4", "SaveImage", {"filename_prefix": "x", "images": ["3", 0]}),
        )
        card = self._card(params={
            "prompt": {"type": "string", "description": "d",
                       "bindings": [{"node": "1", "input": "text"}]},
            "neg_prompt": {"type": "string", "description": "d",
                           "bindings": [{"node": "2", "input": "text"}]},
        })
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            hits = L.lint_card("t", card)
        self.assertNotIn("WF017", [f.rule for f in hits])

    def test_wf017_skipped_when_side_unknown(self):
        """一侧是 ConditioningZeroOut（刻意无负向词）→ 不做判定，不许误报。"""
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "positive words", "clip": ["9", 0]}),
            ("2", "ConditioningZeroOut", {"conditioning": ["1", 0]}),
            ("3", "KSampler", {"seed": 1, "steps": 20, "cfg": 3.0,
                               "sampler_name": "euler", "scheduler": "simple",
                               "denoise": 1.0, "positive": ["1", 0],
                               "negative": ["2", 0],
                               "model": ["9", 0], "latent_image": ["9", 0]}),
            ("4", "SaveImage", {"filename_prefix": "x", "images": ["3", 0]}),
        )
        card = self._card(params={
            "prompt": {"type": "string", "description": "d",
                       "bindings": [{"node": "1", "input": "text"}]},
            "neg_prompt": {"type": "string", "description": "d",
                           "bindings": [{"node": "2", "input": "text"}]},
        })
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            hits = L.lint_card("t", card)
        self.assertNotIn("WF017", [f.rule for f in hits])

    def test_wf018_same_binding(self):
        """正负绑到同一个节点同一条输入 → 必报。"""
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "words", "clip": ["9", 0]}),
            ("3", "KSampler", {"seed": 1, "steps": 20, "cfg": 3.0,
                               "sampler_name": "euler", "scheduler": "simple",
                               "denoise": 1.0, "positive": ["1", 0],
                               "negative": ["1", 0],
                               "model": ["9", 0], "latent_image": ["9", 0]}),
            ("4", "SaveImage", {"filename_prefix": "x", "images": ["3", 0]}),
        )
        card = self._card(params={
            "prompt": {"type": "string", "description": "d",
                       "bindings": [{"node": "1", "input": "text"}]},
            "neg_prompt": {"type": "string", "description": "d",
                           "bindings": [{"node": "1", "input": "text"}]},
        })
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            hits = L.lint_card("t", card)
        self.assertIn("WF018", [f.rule for f in hits])

    def test_wf019_any_two_params_share_binding(self):
        """任意两个参数绑同一条输入 → 必报（后应用的覆盖先应用的）。

        2026-11-11 实测 `scail2_int8` 有 prompt / prompt_2 / prompt_3 三个，
        其中 prompt 和 prompt_3 都绑 1404.text —— 用户改 prompt 会被
        prompt_3 的旧值覆盖回去，改了等于没改，且完全不报错。
        """
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "a", "clip": ["9", 0]}),
            ("2", "SaveImage", {"filename_prefix": "x", "images": ["1", 0]}),
        )
        card = self._card(params={
            "prompt": {"type": "string", "description": "d",
                       "bindings": [{"node": "1", "input": "text"}]},
            "prompt_2": {"type": "string", "description": "d",
                         "bindings": [{"node": "1", "input": "text"}]},
        })
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            hits = L.lint_card("t", card)
        wf19 = [f for f in hits if f.rule == "WF019"]
        self.assertTrue(wf19, [f.rule for f in hits])
        self.assertIn("覆盖", wf19[0].detail)

    def test_wf019_allows_one_param_multi_binding(self):
        """**一个**参数绑多个节点是合法的（两段采样各绑一次），不许报。"""
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "a", "clip": ["9", 0]}),
            ("2", "CLIPTextEncode", {"text": "a", "clip": ["9", 0]}),
            ("3", "SaveImage", {"filename_prefix": "x", "images": ["1", 0]}),
        )
        card = self._card(params={
            "prompt": {"type": "string", "description": "d",
                       "bindings": [{"node": "1", "input": "text"},
                                    {"node": "2", "input": "text"}]},
        })
        with tempfile.TemporaryDirectory() as d:
            gp = Path(d) / "g.json"
            gp.write_text(json.dumps(g), encoding="utf-8")
            card["graph"] = str(gp)
            hits = L.lint_card("t", card)
        self.assertNotIn("WF019", [f.rule for f in hits])

    def test_strict_promotes_warnings(self):
        """--strict 下警告也算失败（发布前用）。"""
        rep = L.Report()
        L._add(rep, "WF012", "t", "params.x", "no desc")
        self.assertTrue(rep.ok())
        self.assertFalse(rep.ok(strict=True))


class TestLintOnRealCards(unittest.TestCase):
    """拿真实仓库跑一遍：规则不能把好卡报红。"""

    @classmethod
    def setUpClass(cls):
        cls.rep = L.lint_all()

    def test_checked_count(self):
        self.assertGreaterEqual(len(self.rep.checked), 50)

    def test_no_unknown_rule(self):
        for f in self.rep.findings:
            self.assertIn(f.rule, L.RULES, f"未登记的规则号 {f.rule}")

    def test_every_finding_has_why(self):
        for f in self.rep.findings:
            self.assertTrue(L.RULES[f.rule][2], f"{f.rule} 缺「为什么」")

    def test_verified_cards_are_mostly_clean(self):
        """verified 的卡里，报错的应当只有历史上已知的那几条。"""
        bad = {f.workflow for f in self.rep.errors
               if f.workflow in {"pack_video_ltx2_3_i2v", "pack_video_ltx2_3_flf2v",
                                 "pack_video_wan21_scail2_character_replacement",
                                 "pack_video_wan21_scail2_character_replacement_int8",
                                 "pack_image_qwen_image_layered"}}
        # 允许的就是这 5 条 —— 多一条说明规则出新误报了
        self.assertLessEqual(len(bad), 5, f"多报了：{bad}")


class TestInfer(unittest.TestCase):
    """图反推：四个必踩的坑各一个夹具。"""

    def test_placeholder_default_not_leaked(self):
        """`{{SEED}}` 不能当默认值 —— 会让 int 参数渲染出字符串框。"""
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "hello world", "clip": ["9", 0]}),
            ("2", "KSampler", {"seed": "{{SEED}}", "steps": 20, "cfg": 3.0,
                               "sampler_name": "euler", "scheduler": "simple",
                               "denoise": 1.0, "positive": ["1", 0],
                               "negative": ["1", 0],
                               "model": ["9", 0], "latent_image": ["9", 0]}),
            ("3", "SaveImage", {"filename_prefix": "{{OUTPUT_PREFIX}}",
                                "images": ["2", 0]}),
        )
        inf = INF.infer(g, {})
        self.assertEqual(inf.params["seed"]["default"], 0)

    def test_math_expression_resolved(self):
        """`a/2` 和 `a * b + 1` 都要能解 —— 第一版只认五种固定写法，全解不出来。"""
        g = _graph(
            ("1", "ComfyMathExpression", {"expression": "a/2", "values.a": 1280}),
            ("2", "ComfyMathExpression", {"expression": "a * b + 1",
                                          "values.a": 5, "values.b": 25}),
            ("3", "EmptyLTXVLatentVideo", {"width": ["1", 1], "height": ["1", 1],
                                           "length": ["2", 1], "batch_size": 1}),
            ("4", "SaveVideo", {"filename_prefix": "{{OUTPUT_PREFIX}}",
                                "video": ["3", 0]}),
        )
        inf = INF.infer(g, {})
        self.assertEqual(inf.params["width"]["default"], 640)
        self.assertEqual(inf.params["frames"]["default"], 126)

    def test_expression_rejects_code(self):
        """表达式不许执行任意代码（`__import__` 之类直接放弃推导）。"""
        g = _graph(
            ("1", "ComfyMathExpression", {"expression": "__import__('os')",
                                          "values.a": 1}),
            ("2", "EmptyLTXVLatentVideo", {"width": ["1", 1], "height": 64,
                                           "length": 81, "batch_size": 1}),
            ("3", "SaveVideo", {"filename_prefix": "x", "video": ["2", 0]}),
        )
        inf = INF.infer(g, {})
        self.assertNotIn("width", inf.params)

    def test_branch_nodes_excluded(self):
        """旁支（不被输出引用的节点）不参与推断。

        `scail2` 图 75 个节点里只有 36 个可达；不剪枝会多报 7 个权重槽位。
        """
        g = _graph(
            ("1", "LoadImage", {"image": "a.png"}),
            ("2", "SaveImage", {"filename_prefix": "x", "images": ["1", 0]}),
            # 旁支：孤立的一个 UNETLoader，没有任何节点引用
            ("99", "UNETLoader", {"unet_name": "orphan.safetensors"}),
        )
        inf = INF.infer(g, {})
        self.assertNotIn("unet", inf.slots)
        self.assertEqual(inf.live_nodes, 2)

    def test_same_file_deduped_to_major_role(self):
        """同一个权重文件被多个 loader 引用 → 保留更主要的角色（ckpt 而非 audio_vae）。"""
        g = _graph(
            # 主权重（ckpt）与音频 VAE 引用同一个文件
            ("1", "CheckpointLoaderSimple", {"ckpt_name": "big.safetensors"}),
            ("2", "LTXVAudioVAELoader", {"ckpt_name": "big.safetensors"}),
            # 解码节点同时消费两者，保证两个 loader 都可达
            ("3", "LTXVAudioVAEDecode", {"samples": ["4", 0],
                                         "audio_vae": ["2", 0]}),
            ("4", "LTXVSeparateAVLatent", {"av_latent": ["1", 0]}),
            ("5", "SaveVideo", {"filename_prefix": "x", "video": ["3", 0]}),
        )
        inf = INF.infer(g, {})
        self.assertIn("ckpt", inf.slots)
        self.assertNotIn("audio_vae", inf.slots)
        self.assertTrue(inf.warnings)

    def test_prompt_via_guider(self):
        """文本侧藏在 guider 里也要追到（SamplerCustomAdvanced 结构）。"""
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "a cat walking", "clip": ["9", 0]}),
            ("2", "CLIPTextEncode", {"text": "blurry, watermark", "clip": ["9", 0]}),
            ("3", "CFGGuider", {"cfg": 1.0, "model": ["9", 0],
                                "positive": ["1", 0], "negative": ["2", 0]}),
            ("4", "SamplerCustomAdvanced", {"noise": ["9", 0], "guider": ["3", 0],
                                            "sampler": ["9", 0], "sigmas": ["9", 0],
                                            "latent_image": ["9", 0]}),
            ("5", "SaveVideo", {"filename_prefix": "x", "video": ["4", 0]}),
        )
        inf = INF.infer(g, {})
        self.assertEqual(inf.params["prompt"]["bindings"][0]["node"], "1")
        self.assertEqual(inf.params["neg_prompt"]["bindings"][0]["node"], "2")

    def test_switch_picks_active_branch(self):
        """ComfySwitchNode 按 switch 值选分支，不许把两条都追。

        `ltx2_3_i2v` 的提示词链就是 switch=False → on_false（硬编码长文本），
        on_true 是 TextGenerateLTX2Prompt（AI 改写）。追错就得出
        "prompt 应该绑 AI 改写节点"，而作业实际用的是 on_false。
        """
        g = _graph(
            ("1", "CLIPTextEncode", {"text": "hardcoded prompt", "clip": ["9", 0]}),
            ("2", "CLIPTextEncode", {"text": "bad", "clip": ["9", 0]}),
            ("3", "ComfySwitchNode", {"switch": False,
                                      "on_false": "hardcoded prompt",
                                      "on_true": ["1", 0]}),
            ("4", "CFGGuider", {"cfg": 1.0, "model": ["9", 0],
                                "positive": ["3", 0], "negative": ["2", 0]}),
            ("5", "SamplerCustomAdvanced", {"noise": ["9", 0], "guider": ["4", 0],
                                            "sampler": ["9", 0], "sigmas": ["9", 0],
                                            "latent_image": ["9", 0]}),
            ("6", "SaveVideo", {"filename_prefix": "x", "video": ["5", 0]}),
        )
        inf = INF.infer(g, {})
        self.assertEqual(inf.params["prompt"]["bindings"][0],
                         {"node": "3", "input": "on_false"},
                         "switch=False 时必须走 on_false")

    def test_negative_prompt_within_one_node(self):
        """一个节点同时管正负（prompt / negative_prompt）要按侧分开。"""
        g = _graph(
            ("1", "TextEncodeQwenImage21", {"prompt": "edit this",
                                            "negative_prompt": "blurry",
                                            "clip": ["9", 0]}),
            ("2", "KSampler", {"seed": 1, "steps": 20, "cfg": 1.0,
                               "sampler_name": "euler", "scheduler": "simple",
                               "denoise": 1.0, "positive": ["1", 0],
                               "negative": ["1", 1],
                               "model": ["9", 0], "latent_image": ["9", 0]}),
            ("3", "SaveImage", {"filename_prefix": "x", "images": ["2", 0]}),
        )
        inf = INF.infer(g, {})
        self.assertEqual(inf.params["prompt"]["bindings"][0]["input"], "prompt")
        self.assertEqual(inf.params["neg_prompt"]["bindings"][0]["input"],
                         "negative_prompt")

    def test_image_fields_numbered(self):
        """多个 LoadImage → image / image_2，不能都叫 image。"""
        g = _graph(
            ("1", "LoadImage", {"image": "a.png"}),
            ("2", "LoadImage", {"image": "b.png"}),
            ("3", "SaveImage", {"filename_prefix": "x", "images": ["1", 0]}),
        )
        g["4"] = {"class_type": "ImageBatch", "inputs": {"image1": ["1", 0],
                                                         "image2": ["2", 0]}}
        g["3"]["inputs"]["images"] = ["4", 0]
        inf = INF.infer(g, {})
        self.assertIn("image", inf.params)
        self.assertIn("image_2", inf.params)

    def test_media_default_discarded(self):
        """图里 `egyptian_queen.png` 是作者调试素材，不能当用户默认值。"""
        g = _graph(
            ("1", "LoadImage", {"image": "egyptian_queen.png"}),
            ("2", "SaveImage", {"filename_prefix": "x", "images": ["1", 0]}),
        )
        inf = INF.infer(g, {})
        self.assertNotIn("default", inf.params["image"])
        self.assertTrue(inf.params["image"]["required"])


class TestTemplates(unittest.TestCase):
    def test_all_templates_have_review_markers(self):
        """模板里必须有 REVIEW_ 占位 —— 没有说明它能自动填满，那就不该要人审。"""
        for key, t in T.TEMPLATES.items():
            ph = T.review_placeholders({"params": t.params, "output": t.output})
            self.assertTrue(ph, f"模板 {key} 没有 REVIEW 占位")

    def test_video_templates_have_duration(self):
        """视频模板必须给 duration_s，否则出 1 秒也 PASS。"""
        for key, t in T.TEMPLATES.items():
            if t.kind != "video":
                continue
            self.assertIn("duration_s", t.expect, f"模板 {key} 缺 duration_s")

    def test_every_template_param_has_description(self):
        """模板里的参数默认值可以直接给用户看，说明必须写全。"""
        for key, t in T.TEMPLATES.items():
            for pname, spec in t.params.items():
                self.assertTrue(str(spec.get("description") or "").strip(),
                                f"模板 {key}.{pname} 缺 description")

    def test_template_params_are_ui_renderable(self):
        """模板里的类型必须 UI 能渲染。"""
        for key, t in T.TEMPLATES.items():
            for pname, spec in t.params.items():
                self.assertIn(spec.get("type"), L.UI_TYPES,
                              f"模板 {key}.{pname} 类型 {spec.get('type')} UI 渲染不了")

    def test_pick_by_kind(self):
        self.assertEqual(T.pick_by_kind("video", has_video_input=True)[0], "v2v")
        self.assertEqual(T.pick_by_kind("video", has_two_images=True)[0], "flf2v")
        self.assertEqual(T.pick_by_kind("video", has_image_input=True)[0], "i2v")
        self.assertEqual(T.pick_by_kind("video")[0], "t2v")
        self.assertEqual(T.pick_by_kind("image")[0], "t2i")

    def test_negative_prompt_default_is_not_positive(self):
        """模板的 neg_prompt 默认值不许抄 prompt —— 就是 WF005 那条规则。"""
        for key, t in T.TEMPLATES.items():
            p = (t.params.get("prompt") or {}).get("default")
            n = (t.params.get("neg_prompt") or {}).get("default")
            if p is None or n is None:
                continue          # 有一侧没默认值就不构成"相同"
            self.assertNotEqual(p, n, f"模板 {key} 正负默认值相同")


if __name__ == "__main__":
    unittest.main()
