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

    def test_image_2_normalized(self):
        # 官方模板的 FLF2V / 多图编辑用 image_2；必须和 image 一样归一化
        out = p20ctl._normalize_image({"image": str(self.src), "image_2": str(self.src)})
        self.assertRegex(out["image_2"], r"^in-[0-9a-f]{8}\.png$")
        self.assertTrue((p20ctl.INPUTS_DIR / out["image_2"]).is_file())

    def test_audio_video_normalized(self):
        a = self.root / "a.wav"; a.write_bytes(b"RIFF" + b"x" * 100)
        v = self.root / "v.mp4"; v.write_bytes(b"\x00\x00\x00\x18ftyp" + b"x" * 100)
        out = p20ctl._normalize_image({"audio": str(a), "video": str(v)})
        self.assertRegex(out["audio"], r"^in-[0-9a-f]{8}\.wav$")
        self.assertRegex(out["video"], r"^in-[0-9a-f]{8}\.mp4$")
        self.assertTrue((p20ctl.INPUTS_DIR / out["audio"]).is_file())
        self.assertTrue((p20ctl.INPUTS_DIR / out["video"]).is_file())

    def test_missing_file_left_alone(self):
        params = {"image": str(self.root / "nope.png")}
        self.assertEqual(p20ctl._normalize_image(params)["image"], params["image"])

    def test_already_remote_name_left_alone(self):
        params = {"image": "in-12345678.png"}
        self.assertEqual(p20ctl._normalize_image(dict(params))["image"], "in-12345678.png")


if __name__ == "__main__":
    unittest.main()
