# -*- coding: utf-8 -*-
"""img2img 输入图归一化：本地路径 → 实例 input/ 文件名 + 内容寻址副本。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.p20 import p20ctl


class TestNormalizeImage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "photo.png"
        self.src.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 200)
        self._orig = p20ctl.INPUTS_DIR
        p20ctl.INPUTS_DIR = self.root / "inputs"

    def tearDown(self):
        p20ctl.INPUTS_DIR = self._orig
        self.tmp.cleanup()

    def test_local_path_becomes_remote_name(self):
        out = p20ctl._normalize_image({"image": str(self.src), "prompt": "x"})
        self.assertRegex(out["image"], r"^in-[0-9a-f]{8}\.png$")
        self.assertNotIn("image_local", out)          # 不能往 params 里塞额外键
        self.assertTrue((p20ctl.INPUTS_DIR / out["image"]).is_file())
        self.assertEqual((p20ctl.INPUTS_DIR / out["image"]).read_bytes(), self.src.read_bytes())

    def test_same_content_same_name(self):
        a = p20ctl._normalize_image({"image": str(self.src)})["image"]
        b = p20ctl._normalize_image({"image": str(self.src)})["image"]
        self.assertEqual(a, b)

    def test_missing_file_left_alone(self):
        params = {"image": str(self.root / "nope.png")}
        self.assertEqual(p20ctl._normalize_image(params)["image"], params["image"])

    def test_already_remote_name_left_alone(self):
        params = {"image": "in-12345678.png"}
        self.assertEqual(p20ctl._normalize_image(dict(params))["image"], "in-12345678.png")


if __name__ == "__main__":
    unittest.main()
