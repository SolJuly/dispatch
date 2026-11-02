# -*- coding: utf-8 -*-
"""多图作业的产物张数校验（batch_size > 1 时 fetch 漏拉必须失败）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.p1 import verify as v


class TestVerifyImagesCount(unittest.TestCase):
    def test_count_mismatch_fails_before_decode(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            res = v.verify_job({"expect": {"images": 2}, "params": {}}, Path(d))
            self.assertEqual(res["verdict"], "FAIL")
            self.assertIn("张数不符", res["reason"])
            self.assertEqual(res["expect_images"], 2)
            self.assertEqual(res["files"], ["a.png"])

    def test_no_images_expect_is_not_enforced(self):
        """老作业/视频没有 expect.images，不能被张数规则误伤。"""
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
            res = v.verify_job({"expect": {}, "params": {}}, Path(d))
            self.assertNotIn("expect_images", res)


if __name__ == "__main__":
    unittest.main()
