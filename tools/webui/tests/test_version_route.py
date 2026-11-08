# -*- coding: utf-8 -*-
"""文件服务路由单测：`/api/p2/version` 是前端识别"线上是旧后端"的唯一依据。

真起一个 ThreadingHTTPServer（端口 0），走真实 HTTP，不 mock Handler。
"""
from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path


class TestVersionRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "share").mkdir()
        os.environ["SHARE_ROOT"] = str(root / "share")
        os.environ["SHARE_TOKEN_FILE"] = str(root / ".share_token")
        os.environ["SHARE_PORT"] = "0"
        os.environ["SHARE_BASE"] = "/files"
        os.environ["P21_PUB_DIR"] = str(root / "pub")
        (root / "pub" / "jobs" / "p1-20260101T0000-aaaaaa").mkdir(parents=True)
        (root / "pub" / "jobs" / "p1-20260101T0000-aaaaaa" / "01-a.png").write_bytes(
            b"\x89PNG\r\n\x1a\n")
        cls.mod = importlib.import_module("fileserver")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls._tmp.cleanup()
        for k in ("SHARE_ROOT", "SHARE_TOKEN_FILE", "SHARE_PORT", "SHARE_BASE", "P21_PUB_DIR"):
            os.environ.pop(k, None)

    def _get(self, path: str, method: str = "GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_version_payload(self):
        code, body, headers = self._get(f"/{self.mod.TOKEN}/api/p2/version")
        self.assertEqual(code, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        d = json.loads(body)
        self.assertEqual(d["api"], "p2.1")
        self.assertEqual(d["contract"], "openapi-3.1")
        self.assertTrue(d["features"]["version"])
        self.assertTrue(d["features"]["console"])
        self.assertIn("commit", d["build"])
        self.assertEqual(d["pid"], os.getpid())
        self.assertIn("version", d["capabilities"])

    def test_version_requires_token(self):
        # 首段必须等于 token，否则一律 404（不区分"没权限"与"不存在"）
        code, _, _ = self._get("/api/p2/version")
        self.assertEqual(code, 404)
        code, _, _ = self._get("/deadbeef/api/p2/version")
        self.assertEqual(code, 404)

    def test_pub_route_serves_published_artifact(self):
        code, body, headers = self._get(
            f"/{self.mod.TOKEN}/pub/jobs/p1-20260101T0000-aaaaaa/01-a.png")
        self.assertEqual(code, 200)
        self.assertTrue(headers.get("Content-Type", "").startswith("image/png"))
        self.assertEqual(body, b"\x89PNG\r\n\x1a\n")

    def test_pub_route_blocks_traversal_and_missing(self):
        code, _, _ = self._get(f"/{self.mod.TOKEN}/pub/jobs/../secret.txt")
        self.assertEqual(code, 404)
        code, _, _ = self._get(f"/{self.mod.TOKEN}/pub/jobs/nope.png")
        self.assertEqual(code, 404)

    def test_unknown_p2_path_is_404(self):
        code, _, _ = self._get(f"/{self.mod.TOKEN}/api/p2/nope")
        self.assertEqual(code, 404)

    def test_version_head(self):
        code, body, _ = self._get(f"/{self.mod.TOKEN}/api/p2/version", method="HEAD")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
