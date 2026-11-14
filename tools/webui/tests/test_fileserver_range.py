#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fileserver 的 Range / 流式发送测试。零依赖、零网络，只用标准库。

覆盖 2026-11-14 修的「视频加载不出来」：
  · _parse_range 的边界（空 / 单区间 / 尾区间 / 越界 / 多区间 / 畸形）
  · _send_media 的响应码与头（200 vs 206 vs 416、Accept-Ranges、Content-Range）
  · 分段下载拼接后与原文件字节一致（这是 Range 正确性的唯一硬标准）
  · MEDIA_EXT 判定不误伤图片/文本

用法：python3 -m unittest tools.webui.tests.test_fileserver_range
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class ParseRangeTest(unittest.TestCase):
    """纯函数测试：不碰网络、不碰文件。"""

    @classmethod
    def setUpClass(cls):
        # 直接从 fileserver.py 取静态方法，避免起服务的副作用
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dsh_fileserver", _ROOT / "fileserver.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.parse = staticmethod(cls.mod.Handler.__dict__["_parse_range"].__func__)

    def test_empty_means_whole_file(self):
        self.assertIsNone(self.parse("", 1000))
        self.assertIsNone(self.parse(None, 1000))

    def test_normal_range(self):
        self.assertEqual(self.parse("bytes=0-99", 1000), (0, 99))
        self.assertEqual(self.parse("bytes=100-199", 1000), (100, 199))

    def test_open_ended(self):
        self.assertEqual(self.parse("bytes=500-", 1000), (500, 999))

    def test_suffix_range(self):
        self.assertEqual(self.parse("bytes=-100", 1000), (900, 999))
        self.assertEqual(self.parse("bytes=-1", 1000), (999, 999))

    def test_single_byte(self):
        self.assertEqual(self.parse("bytes=0-0", 1000), (0, 0))

    def test_end_clamped_to_size(self):
        """end 超出文件长度不该报错，按 RFC 截到最后一字节。"""
        self.assertEqual(self.parse("bytes=999-99999", 1000), (999, 999))

    def test_out_of_bounds_is_416(self):
        self.assertEqual(self.parse("bytes=1000-", 1000), "invalid")
        self.assertEqual(self.parse("bytes=5000-6000", 1000), "invalid")

    def test_reversed_is_416(self):
        self.assertEqual(self.parse("bytes=50-10", 1000), "invalid")

    def test_malformed_is_416(self):
        self.assertEqual(self.parse("bytes=abc", 1000), "invalid")
        self.assertEqual(self.parse("bytes=-", 1000), "invalid")
        self.assertEqual(self.parse("bytes=-0", 1000), "invalid")

    def test_unknown_unit_ignored(self):
        """非 bytes 单位不认，按整份发（回 None），不是 416。"""
        self.assertIsNone(self.parse("items=0-10", 1000))

    def test_multi_range_takes_first(self):
        """多区间只取第一段 —— 视频播放器从不用 multipart，不值得实现。"""
        self.assertEqual(self.parse("bytes=0-99,200-299", 1000), (0, 99))


class MediaExtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dsh_fileserver2", _ROOT / "fileserver.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_video_is_media(self):
        for ext in (".mp4", ".webm", ".mov", ".m4v", ".mkv"):
            self.assertIn(ext, self.mod.MEDIA_EXT, ext)

    def test_image_not_media(self):
        for ext in (".png", ".jpg", ".gif", ".webp"):
            self.assertNotIn(ext, self.mod.MEDIA_EXT, ext)

    def test_text_not_media(self):
        for ext in (".md", ".json", ".py"):
            self.assertNotIn(ext, self.mod.MEDIA_EXT, ext)


class SendMediaTest(unittest.TestCase):
    """端到端：真的起一个 Handler 吗？不需要 —— 用假 request 走 send 逻辑。

    这里只测「响应头/状态码/字节数」，不依赖 socket：
    用一个最小 stub 替换 wfile，捕获写入的字节。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dsh_fileserver3", _ROOT / "fileserver.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _call(self, path: Path, range_header: str = ""):
        """构造一个最小 Handler 实例，跑 _send_media，收集响应。"""
        H = self.mod.Handler
        inst = H.__new__(H)
        captured = {"status": None, "headers": {}, "body": b""}

        class W:
            def write(self, b):
                captured["body"] += b

            def flush(self):
                pass

        inst.wfile = W()
        inst.command = "GET"
        inst.headers = {"Range": range_header} if range_header else {}

        def send_response(code, message=None):
            captured["status"] = code

        def send_header(k, v):
            captured["headers"][k] = v

        def end_headers():
            pass

        inst.send_response = send_response
        inst.send_header = send_header
        inst.end_headers = end_headers
        inst._send_media(path, "video/mp4", inline=True)
        return captured

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        # 内容必须可复现：随机字节让「拼接等于原文」的断言更有意义
        self.data = bytes(range(256)) * 400          # 102400 字节
        self.tmp.write(self.data)
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_full_request_is_200(self):
        r = self._call(self.path)
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["headers"].get("Accept-Ranges"), "bytes")
        self.assertEqual(len(r["body"]), len(self.data))

    def test_range_request_is_206(self):
        r = self._call(self.path, "bytes=0-1023")
        self.assertEqual(r["status"], 206)
        self.assertEqual(r["headers"].get("Content-Range"),
                         f"bytes 0-1023/{len(self.data)}")
        self.assertEqual(len(r["body"]), 1024)
        self.assertEqual(r["body"], self.data[:1024])

    def test_out_of_range_is_416(self):
        r = self._call(self.path, f"bytes={len(self.data) + 100}-")
        self.assertEqual(r["status"], 416)
        self.assertEqual(r["headers"].get("Content-Range"),
                         f"bytes */{len(self.data)}")

    def test_split_fetch_reassembles_exactly(self):
        """Range 正确性的硬标准：两段拼回来必须等于原文。"""
        n = len(self.data)
        mid = n // 2
        a = self._call(self.path, f"bytes=0-{mid - 1}")
        b = self._call(self.path, f"bytes={mid}-")
        self.assertEqual(a["status"], 206)
        self.assertEqual(b["status"], 206)
        self.assertEqual(a["body"] + b["body"], self.data)

    def test_head_has_headers_but_no_body(self):
        H = self.mod.Handler
        inst = H.__new__(H)
        cap = {"status": None, "headers": {}, "body": b""}

        class W:
            def write(self, b):
                cap["body"] += b

            def flush(self):
                pass

        inst.wfile = W()
        inst.command = "HEAD"
        inst.headers = {}
        inst.send_response = lambda c, m=None: cap.__setitem__("status", c)
        inst.send_header = lambda k, v: cap["headers"].__setitem__(k, v)
        inst.end_headers = lambda: None
        inst._send_media(self.path, "video/mp4", inline=True)
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["headers"].get("Content-Length"), str(len(self.data)))
        self.assertEqual(cap["body"], b"")     # HEAD 不发 body


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CacheHeaderTest(unittest.TestCase):
    """媒体响应必须可缓存 —— no-store 会让视频拖动每次都从头拉。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dsh_fileserver_cache", _ROOT / "fileserver.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _call(self, path):
        H = self.mod.Handler
        inst = H.__new__(H)
        cap = {"status": None, "headers": {}, "body": b""}

        class W:
            def write(self, b):
                cap["body"] += b

            def flush(self):
                pass

        inst.wfile = W()
        inst.command = "GET"
        inst.headers = {}
        inst.send_response = lambda c, m=None: cap.__setitem__("status", c)
        inst.send_header = lambda k, v: cap["headers"].__setitem__(k, v)
        inst.end_headers = lambda: None
        inst._send_media(path, "video/mp4", inline=True)
        return cap

    def test_cache_control_is_not_no_store(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"x" * 1000)
            name = f.name
        try:
            cap = self._call(Path(name))
            cc = cap["headers"].get("Cache-Control", "")
            self.assertNotIn("no-store", cc,
                             "no-store 会让拖动进度条每次都重新拉整个区间")
            self.assertIn("max-age", cc.lower())
        finally:
            os.unlink(name)

    def test_etag_present(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"x" * 1000)
            name = f.name
        try:
            cap = self._call(Path(name))
            self.assertIn("ETag", cap["headers"])
        finally:
            os.unlink(name)

    def test_chunk_size_at_least_128k(self):
        """64KB 分块在平板上拖进度条会明显卡（syscall 过多）。"""
        self.assertGreaterEqual(self.mod.STREAM_CHUNK, 128 * 1024)


class ImageRangeTest(unittest.TestCase):
    """大 PNG 也要走流式 + Range（1536² 的图有 2.9MB）。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "dsh_fileserver_img", _ROOT / "fileserver.py")
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_png_in_img_ext(self):
        self.assertIn(".png", self.mod.IMG_EXT)

    def test_send_media_works_for_png(self):
        H = self.mod.Handler
        inst = H.__new__(H)
        cap = {"status": None, "headers": {}, "body": b""}

        class W:
            def write(self, b):
                cap["body"] += b

            def flush(self):
                pass

        inst.wfile = W()
        inst.command = "GET"
        inst.headers = {"Range": "bytes=0-99"}
        inst.send_response = lambda c, m=None: cap.__setitem__("status", c)
        inst.send_header = lambda k, v: cap["headers"].__setitem__(k, v)
        inst.end_headers = lambda: None
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG" + b"z" * 500)
            name = f.name
        try:
            inst._send_media(Path(name), "image/png", inline=True)
            self.assertEqual(cap["status"], 206)
            self.assertEqual(len(cap["body"]), 100)
        finally:
            os.unlink(name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
