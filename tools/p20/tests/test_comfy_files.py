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
