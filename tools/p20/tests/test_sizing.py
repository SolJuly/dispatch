#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尺寸推导（resolution / aspect → width, height）的正确性。

## 为什么需要这组测试（2026-11-16）

H3 卡的 `width` / `height` 原本是两个各自独立的字面量，默认 768×512。
两个后果：

### ① 说明文字撒谎

`height` 的描述写着「与宽度配套调整，保持原始素材的宽高比」，
但改 width 时 height 纹丝不动 —— 出来的画面被拉伸变形。
用户在界面上看到这条说明会以为它会联动。

### ② 默认值只有官方 38% 的像素

H3 是 768p 模型，官方 `MiniMaxH3ImageToVideo` 默认 `1344×768`，
**短边 768**。我们默认 768×512，短边 512：

        1344 × 768 = 1032192   官方
         768 × 512 =  393216   我们 = 38%

短边决定清晰度。「画质糊」有一部分是这里欠的账。

## 测试重点

· 短边锚定：不管比例多少，短边必须等于（或贴近）选定的 resolution
· 比例保持：宽高比和素材一致，不拉伸
· 对齐：两边都是 multiple 的倍数（模型要求 16/32）
· 优先级：显式 width/height > 推导 > 卡片默认
· 只给一个边时，另一边跟着算（这才是「联动」的实际含义）
"""
from __future__ import annotations

import struct
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p20 import sizing  # noqa: E402


def make_png(path: Path, w: int, h: int) -> None:
    """写一个只有 IHDR 的最小 PNG（sizing 只读头部）。"""
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    import zlib
    chunk = (struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
             + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk)


class FitTest(unittest.TestCase):
    """纯算术：短边 + 比例 → 对齐后的 (w, h)。"""

    def test_portrait_pins_short_side_to_width(self):
        w, h = sizing.fit(768, 9 / 16)
        self.assertEqual(w, 768)          # 竖屏的短边是宽度
        self.assertEqual(h % 16, 0)
        self.assertGreater(h, w)

    def test_landscape_pins_short_side_to_height(self):
        w, h = sizing.fit(768, 16 / 9)
        self.assertEqual(h, 768)          # 横屏的短边是高度
        self.assertEqual(w % 16, 0)
        self.assertGreater(w, h)

    def test_square(self):
        self.assertEqual(sizing.fit(768, 1.0), (768, 768))

    def test_both_sides_are_multiples(self):
        for ratio in (9 / 16, 16 / 9, 1.0, 3 / 4, 4 / 3, 2.39):
            w, h = sizing.fit(640, ratio)
            self.assertEqual(w % 16, 0, f"width {w} 不是 16 的倍数（ratio={ratio}）")
            self.assertEqual(h % 16, 0, f"height {h} 不是 16 的倍数（ratio={ratio}）")

    def test_clamped_to_max(self):
        """极端比例不能突破上限 —— 否则显存直接爆。"""
        w, h = sizing.fit(1024, 2.39, hi=1280)
        self.assertLessEqual(w, 1280)
        self.assertLessEqual(h, 1280)

    def test_clamped_to_min(self):
        w, h = sizing.fit(256, 0.1, lo=256)
        self.assertGreaterEqual(w, 256)
        self.assertGreaterEqual(h, 256)


class ProbeTest(unittest.TestCase):
    """读素材真实尺寸。渲染路径上要纯标准库，不能依赖 ffprobe。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_png(self):
        p = self.tmp / "a.png"
        make_png(p, 1620, 1080)
        self.assertEqual(sizing.probe_size(p), (1620, 1080))

    def test_missing_file_returns_none(self):
        self.assertIsNone(sizing.probe_size(self.tmp / "nope.png"))

    def test_garbage_returns_none(self):
        p = self.tmp / "bad.png"
        p.write_bytes(b"not an image at all")
        self.assertIsNone(sizing.probe_size(p))


class ApplyTest(unittest.TestCase):
    """`sizing.apply` 的优先级与联动 —— 这是用户真正看到的行为。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wf = {
            "key": "t",
            "params": {
                "resolution": {"type": "enum", "default": "768",
                               "values": ["512", "768", "1024"]},
                "aspect": {"type": "enum", "default": "source"},
                "width": {"type": "int", "default": 768, "multiple": 16,
                          "min": 256, "max": 1280},
                "height": {"type": "int", "default": 512, "multiple": 16,
                           "min": 256, "max": 1280},
                "image": {"type": "image"},
            },
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _apply(self, params, provided=None, applied=("width", "height")):
        return sizing.apply(self.wf, params, provided=provided or {},
                            defaults_applied=list(applied),
                            inputs_dir=self.tmp)

    def test_no_resolution_param_is_noop(self):
        """没声明 resolution 的工作流（其余 52 个）行为必须完全不变。"""
        wf = {"params": {"width": {"type": "int", "default": 768},
                         "height": {"type": "int", "default": 512}}}
        out = sizing.apply(wf, {"width": 768, "height": 512}, provided={},
                           defaults_applied=["width", "height"],
                           inputs_dir=self.tmp)
        self.assertEqual(out["width"], 768)
        self.assertEqual(out["height"], 512)

    def test_source_aspect_follows_image(self):
        make_png(self.tmp / "p.png", 768, 1024)   # 竖屏 3:4
        out = self._apply({"resolution": "768", "aspect": "source",
                           "image": "p.png"})
        self.assertEqual(out["width"] % 16, 0)
        self.assertEqual(out["height"] % 16, 0)
        self.assertGreater(out["height"], out["width"], "竖屏素材应出竖屏")

    def test_short_side_matches_resolution(self):
        make_png(self.tmp / "p.png", 768, 1024)
        out = self._apply({"resolution": "512", "aspect": "source",
                           "image": "p.png"})
        self.assertEqual(out["width"], 512,
                         "竖屏时短边=宽，必须等于 resolution")

    def test_explicit_both_wins(self):
        """用户两个都显式给了，推导一步都不许动。"""
        make_png(self.tmp / "p.png", 768, 1024)
        out = self._apply({"resolution": "1024", "aspect": "source",
                           "image": "p.png", "width": 640, "height": 640},
                          provided={"width": 640, "height": 640})
        self.assertEqual((out["width"], out["height"]), (640, 640))

    def test_explicit_width_derives_height(self):
        """只给 width —— 这正是「height 跟随 width」的实际含义。

        修之前：height 停在 512，画面被拉伸。
        修之后：height 按素材比例算出来。
        """
        make_png(self.tmp / "p.png", 1600, 1000)   # 1.6:1
        out = self._apply({"resolution": "768", "aspect": "source",
                           "image": "p.png", "width": 800},
                          provided={"width": 800})
        self.assertEqual(out["width"], 800)
        # 800 / 1.6 = 500，对齐 16 -> 496
        self.assertEqual(out["height"], 496)
        self.assertGreater(out["height"], 0)

    def test_aspect_override(self):
        make_png(self.tmp / "p.png", 1600, 1000)
        out = self._apply({"resolution": "768", "aspect": "9:16",
                           "image": "p.png"})
        self.assertEqual(out["width"], 768)
        self.assertGreater(out["height"], out["width"] * 1.5)

    def test_missing_image_falls_back(self):
        """素材读不到时不能崩，回落到 16:9。"""
        out = self._apply({"resolution": "768", "aspect": "source",
                           "image": "nope.png"})
        self.assertGreater(out["width"], out["height"])

    def test_alignment_always_holds(self):
        """任何组合下两边都得是 16 的倍数，否则模型直接报错。"""
        make_png(self.tmp / "p.png", 1234, 987)
        for res in ("512", "768", "1024"):
            for asp in ("source", "9:16", "16:9", "1:1", "3:4", "4:3"):
                out = self._apply({"resolution": res, "aspect": asp,
                                   "image": "p.png"})
                self.assertEqual(out["width"] % 16, 0,
                                 f"res={res} aspect={asp} width={out['width']}")
                self.assertEqual(out["height"] % 16, 0,
                                 f"res={res} aspect={asp} height={out['height']}")


class ResolveIntegrationTest(unittest.TestCase):
    """走真实 registry.resolve，确认卡里的配置真的生效。"""

    @classmethod
    def setUpClass(cls):
        from tools.p20 import registry as R
        cls.R = R

    def _resolve(self, wf, **params):
        base = {"prompt": "x", "seed": 1}
        base.update(params)
        return self.R.resolve(wf, params=base)["params"]

    def test_h3_i2v_uses_official_short_side(self):
        """默认必须对齐 H3 官方短边 768，而不是原来的 512。"""
        d = self._resolve("h3_i2v_121f", image="in-d9b9cba6.png")
        self.assertEqual(min(d["width"], d["height"]), 768,
                         f"H3 短边应为 768（官方原生），实际 {min(d['width'], d['height'])}")

    def test_h3_i2v_area_much_larger_than_old(self):
        """768×512 = 393216。新默认必须明显更大，否则这次修改没意义。"""
        d = self._resolve("h3_i2v_121f", image="in-d9b9cba6.png")
        area = d["width"] * d["height"]
        self.assertGreater(area, 393216 * 1.5,
                           f"面积 {area} 相比旧的 393216 提升不明显")

    def test_h3_t2v_default_is_16_9(self):
        d = self._resolve("h3_t2v_121f")
        self.assertEqual(d["aspect"], "16:9")
        self.assertGreater(d["width"], d["height"])
        self.assertEqual(min(d["width"], d["height"]), 768)

    def test_h3_resolution_512_is_smaller(self):
        big = self._resolve("h3_t2v_121f", resolution="768")
        small = self._resolve("h3_t2v_121f", resolution="512")
        self.assertLess(small["width"] * small["height"],
                        big["width"] * big["height"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
