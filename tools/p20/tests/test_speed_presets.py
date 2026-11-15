#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""加速档位（presets）与输出码率参数的正确性。

## 为什么需要这组测试（2026-11-15 实测踩到三次）

### ① 只改 graph 不改 params，会被默认值覆盖回去

H3 的加速 LoRA 要同时改「采样步数」和「LoRA 强度」。我一开始在实验脚本里
只改渲染后的图，结果 `resolve` 先把卡里的 `steps=20` 填进 params，
`apply_params` 又把它写回图里 —— 想跑 8 步，实际跑了 20 步（409s），
看起来像"提速没生效"。

同一个坑踩了两次：`crf` 也一样，设 2 实际跑 12。

### ② 用户不该知道"PDD 是 8 步档"

如果把 steps 和 lora_strength 都暴露给用户，很容易配出
「8 步 + 强度 0」这种组合 —— 既不加速也没省时间，纯浪费。
所以做成一个 `speed_mode` 档位，选档位时由 `presets` 同时改两个参数。

### ③ 输出码率：crf 必须配全三件套

`SaveVideo` 的 `format` / `codec` 是 COMFY_DYNAMICCOMBO_V3，
选了某个 key 之后**子参数是平铺的**：

    format="mp4" → codec
    codec="h264" → encoding
    encoding="re-encode" → crf

少任何一层，`crf` 都会被静默忽略（实测 crf=2 和 crf=12 产出完全相同的文件）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p20 import registry, render  # noqa: E402

IMG = "in-silverice.png"


def _resolve(wf: str, **kw):
    params = {"prompt": "x", "seed": 1, **kw}
    if wf == "h3_i2v_121f":
        params["image"] = IMG
    return registry.resolve(wf, params=params, allow_experimental=True)


class TestSpeedModePresets(unittest.TestCase):
    """speed_mode 必须同时决定 steps 和 LoRA 强度。"""

    EXPECT = {
        "quality":  (20, 0.0),
        "balanced": (8, 1.0),
        "fast":     (4, 1.0),
    }

    def test_each_mode_sets_both_params(self):
        for mode, (steps, strength) in self.EXPECT.items():
            with self.subTest(mode=mode):
                r = _resolve("h3_i2v_121f", speed_mode=mode)
                self.assertEqual(r["params"]["steps"], steps)
                self.assertEqual(r["params"]["lora_strength"], strength)

    def test_preset_wins_over_explicit_steps(self):
        """档位是物理约束：用户显式给 steps=30 也要被档位覆盖。

        理由：PDD Acc-8Step LoRA 就是按 8 步训练的。跑 30 步配 8 步 LoRA
        会同时失去「快」和「好」——既没有蒸馏的加速，也不是 base 模型的
        正常采样轨迹。想手调步数应该选 quality 档（它把强度设 0）。
        """
        r = _resolve("h3_i2v_121f", speed_mode="balanced", steps=30)
        self.assertEqual(r["params"]["steps"], 8,
                         "balanced 档必须固定 8 步，不能被 steps=30 覆盖")

    def test_quality_disables_lora(self):
        """quality 档的强度必须是 0（等价直通），否则"最高画质"名不副实。"""
        r = _resolve("h3_i2v_121f", speed_mode="quality")
        self.assertEqual(r["params"]["lora_strength"], 0.0)

    def test_render_writes_both_bindings(self):
        """resolve 对了还不够 —— 要确认真的落进图里的两个节点。"""
        for mode, (steps, strength) in self.EXPECT.items():
            with self.subTest(mode=mode):
                r = _resolve("h3_i2v_121f", speed_mode=mode)
                g = render.render(r["workflow"], r["model"], r["params"], "J")[0]
                self.assertEqual(g["ks"]["inputs"]["steps"], steps)
                self.assertEqual(g["lora"]["inputs"]["strength_model"], strength)

    def test_default_mode_is_balanced(self):
        r = _resolve("h3_i2v_121f")
        self.assertEqual(r["params"]["speed_mode"], "balanced")
        self.assertEqual(r["params"]["steps"], 8)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(Exception):
            _resolve("h3_i2v_121f", speed_mode="turbo9000")


class TestStylePresets(unittest.TestCase):
    """画面风格档位：style 必须同时决定 LoRA 文件名和强度。

    背景（2026-11-15）：H3 的 LoRA 是**模型族专属**的 —— 权重形状由基础模型的
    层结构决定，H3 的 LoRA 不能用于 Wan/LTX/Qwen。所以"选风格"这件事
    只能在 H3 的工作流里做，而换了风格就要换文件，不是只调强度。
    """

    EXPECT = {
        "none":    (0.0, "realism"),   # 强度 0 时文件名无所谓（等价直通）
        "realism": (1.0, "realism"),
        "cinema":  (1.0, "电影感"),
        "davham":  (0.8, "davham"),
    }

    def test_each_style_sets_strength(self):
        for style, (strength, _) in self.EXPECT.items():
            with self.subTest(style=style):
                r = _resolve("h3_t2v_121f", style=style)
                self.assertEqual(r["params"]["style_strength"], strength)

    def test_each_style_sets_matching_file(self):
        for style, (_, marker) in self.EXPECT.items():
            with self.subTest(style=style):
                r = _resolve("h3_t2v_121f", style=style)
                self.assertIn(marker, r["params"]["style_lora"],
                              f"{style} 应该加载含 {marker!r} 的 LoRA")

    def test_render_binds_style_node(self):
        """确认真的写进 lora_style 节点（不是只改了 params）。"""
        for style, (strength, _) in self.EXPECT.items():
            with self.subTest(style=style):
                r = _resolve("h3_t2v_121f", style=style)
                g = render.render(r["workflow"], r["model"], r["params"], "J")[0]
                node = g.get("lora_style")
                self.assertIsNotNone(node, "图里必须有 lora_style 节点")
                self.assertEqual(node["inputs"]["strength_model"], strength)

    def test_none_disables_without_removing_node(self):
        """none 档不删节点、只把强度设 0。

        为什么保留节点：删节点会让图结构随参数变化，图哈希、等价性测试、
        缓存全部失效。强度 0 在 LoraLoaderModelOnly 里等价直通，
        输出与完全不加 LoRA 一致（实测 1829988 vs 1829752 字节，
        差异来自编码器非确定性，不是 LoRA 影响）。
        """
        r = _resolve("h3_t2v_121f", style="none")
        g = render.render(r["workflow"], r["model"], r["params"], "J")[0]
        self.assertIn("lora_style", g)
        self.assertEqual(g["lora_style"]["inputs"]["strength_model"], 0.0)

    def test_style_chain_order(self):
        """链路必须是 unet → lora(PDD) → lora_style → shift。

        顺序错了（比如 lora_style 接在 unet 上）会让加速 LoRA 失效。
        """
        r = _resolve("h3_t2v_121f", style="cinema")
        g = render.render(r["workflow"], r["model"], r["params"], "J")[0]
        self.assertEqual(g["lora"]["inputs"]["model"], ["unet", 0])
        self.assertEqual(g["lora_style"]["inputs"]["model"], ["lora", 0])
        self.assertEqual(g["shift"]["inputs"]["model"], ["lora_style", 0])


class TestOutputEncoding(unittest.TestCase):
    """SaveVideo 的三层子参数必须齐全，否则 crf 被静默忽略。"""

    REQ = ("format", "codec", "encoding", "crf")

    def test_save_node_has_full_encoding_chain(self):
        for wf in ("h3_i2v_121f", "h3_t2v_121f"):
            with self.subTest(workflow=wf):
                r = _resolve(wf, speed_mode="balanced")
                g = render.render(r["workflow"], r["model"], r["params"], "J")[0]
                ins = g["save"]["inputs"]
                missing = [k for k in self.REQ if k not in ins]
                self.assertEqual(missing, [],
                                 f"{wf} 的 SaveVideo 缺 {missing}，crf 会被忽略")
                self.assertEqual(ins["format"], "mp4")
                self.assertEqual(ins["codec"], "h264")
                self.assertEqual(ins["encoding"], "re-encode",
                                 "必须是 re-encode，auto 会保留上游低码率流")

    def test_crf_is_exposed_and_bounded(self):
        ps = registry.get_workflow("h3_i2v_121f")["params"]
        crf = ps.get("crf")
        self.assertIsNotNone(crf, "crf 应该作为用户可调参数暴露")
        self.assertEqual(crf["min"], 0.0)
        self.assertEqual(crf["max"], 51.0)


class TestExpandPresetsUnit(unittest.TestCase):
    """render.expand_presets 的纯函数行为。"""

    def test_preset_overrides_user_value(self):
        wf = {"params": {"mode": {"presets": {"a": {"steps": 8}}}}}
        out = render.expand_presets(wf, {"mode": "a", "steps": 30})
        self.assertEqual(out["steps"], 8)

    def test_no_preset_leaves_params_alone(self):
        wf = {"params": {"steps": {"type": "int"}}}
        out = render.expand_presets(wf, {"steps": 30})
        self.assertEqual(out["steps"], 30)

    def test_unknown_choice_is_noop(self):
        wf = {"params": {"mode": {"presets": {"a": {"steps": 8}}}}}
        out = render.expand_presets(wf, {"mode": "zzz", "steps": 30})
        self.assertEqual(out["steps"], 30)


if __name__ == "__main__":
    unittest.main()
