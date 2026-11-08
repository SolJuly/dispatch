# -*- coding: utf-8 -*-
"""`/ui/` 前端路由单测：SPA 产物托管 + CSP 边界 + 缓存策略 + 穿越防护。

为什么单开一个文件：这条路由的核心价值是「和 /f/ 的严格 CSP 分开」。
如果哪天有人图省事把 /ui/ 也走 _file()，页面会白屏但不报错——
所以用测试把「脚本必须放行」钉死。

真起 ThreadingHTTPServer（端口 0），走真实 HTTP，不 mock Handler。
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

INDEX_BODY = b"<!doctype html><title>spa</title><script type=module src=./assets/a.js></script>"
JS_BODY = b"console.log('spa');"

# fileserver 在 import 时把 SHARE_ROOT/P21_PUB_DIR 固化成模块级常量。
# 谁先 import 谁决定这些常量，所以本文件必须「存环境 → 强制重导 → 用完还原」，
# 否则会按字母序污染后面的 test_version_route（PUB_DIR 指向别处 → pub 路由 404）。
ENV_KEYS = ("SHARE_ROOT", "SHARE_TOKEN_FILE", "SHARE_PORT", "SHARE_BASE", "P21_PUB_DIR", "UI_ROOT")


class TestUiRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "share").mkdir()

        cls._saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ.update({
            "SHARE_ROOT": str(root / "share"),
            "SHARE_TOKEN_FILE": str(root / ".share_token"),
            "SHARE_PORT": "0",
            "SHARE_BASE": "/files",
            "P21_PUB_DIR": str(root / "pub"),
        })
        os.environ.pop("UI_ROOT", None)

        cls.dist = root / "dist"
        (cls.dist / "assets").mkdir(parents=True)
        (cls.dist / "index.html").write_bytes(INDEX_BODY)
        (cls.dist / "assets" / "a.js").write_bytes(JS_BODY)
        (cls.dist / "assets" / "a.css").write_bytes(b"body{margin:0}")
        # UI_ROOT 之外的文件：用来验证穿越读不到
        (root / "secret.txt").write_bytes(b"TOP-SECRET")

        sys.modules.pop("fileserver", None)          # 丢弃别人导入的实例，按本文件的环境重导
        cls.mod = importlib.import_module("fileserver")
        cls._saved_root = cls.mod.UI_ROOT
        cls.mod.UI_ROOT = cls.dist.resolve()         # 环境变量在 import 时已固化，这里直接改
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.mod.UI_ROOT = cls._saved_root
        sys.modules.pop("fileserver", None)          # 让后续测试按它们自己的环境重新导入
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls._tmp.cleanup()

    def _get(self, path: str, method: str = "GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    # ---------------- CSP 边界：这一组是防白屏的核心
    def test_index_allows_self_scripts(self):
        code, body, h = self._get(f"/{self.mod.TOKEN}/ui/")
        self.assertEqual(code, 200)
        self.assertEqual(body, INDEX_BODY)
        csp = h.get("Content-Security-Policy", "")
        # 必须是 'self' 而非 'none'，否则 bundle 一个字节都不执行
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)   # 同源 fetch 依赖这条
        self.assertEqual(h.get("X-Content-Type-Options"), "nosniff")

    def test_asset_has_no_restrictive_csp(self):
        # 静态资源不设 CSP：设了会按文件生效，反而可能拦住 worker/sourcemap 之外的加载
        code, body, h = self._get(f"/{self.mod.TOKEN}/ui/assets/a.js")
        self.assertEqual(code, 200)
        self.assertEqual(body, JS_BODY)
        self.assertIn("javascript", h.get("Content-Type", ""))
        self.assertIsNone(h.get("Content-Security-Policy"))

    def test_strict_f_dir_csp_is_unchanged(self):
        """对照组：/f/ 下的 html 仍必须是 default-src 'none'（共享目录可写，防窃 token）。"""
        tmp_html = Path(os.environ["SHARE_ROOT"]) / "r.html"
        tmp_html.write_bytes(INDEX_BODY)
        try:
            code, _, h = self._get(f"/{self.mod.TOKEN}/f/r.html")
            self.assertEqual(code, 200)
            self.assertIn("default-src 'none'", h.get("Content-Security-Policy", ""))
        finally:
            tmp_html.unlink()

    # ---------------- history 路由回落
    def test_spa_fallback_for_deep_route(self):
        for path in ("ui/jobs/p1-20260101T0000-aaaaaa", "ui/jobs", "ui/registry"):
            with self.subTest(path=path):
                code, body, h = self._get(f"/{self.mod.TOKEN}/{path}")
                self.assertEqual(code, 200)
                self.assertEqual(body, INDEX_BODY)   # 回落 index，交给前端路由
                self.assertIn("script-src 'self'", h.get("Content-Security-Policy", ""))

    # ---------------- 缓存策略
    def test_html_not_cached_asset_immutable(self):
        _, _, h = self._get(f"/{self.mod.TOKEN}/ui/")
        self.assertEqual(h.get("Cache-Control"), "no-cache")   # 发版即生效
        _, _, h = self._get(f"/{self.mod.TOKEN}/ui/assets/a.js")
        # Vite 产物名带内容 hash，长缓存安全
        self.assertEqual(h.get("Cache-Control"), "public, max-age=31536000, immutable")

    def test_no_duplicate_cache_control_header(self):
        """回归：_send 曾无条件补 no-store，与调用方的 Cache-Control 重复下发，
        浏览器取第一个 → immutable 被静默降级成 no-store，前端指纹缓存全失效。"""
        for path in (f"/{self.mod.TOKEN}/ui/", f"/{self.mod.TOKEN}/ui/assets/a.js",
                     f"/{self.mod.TOKEN}/static/app.css"):
            with self.subTest(path=path):
                _, _, h = self._get(path)
                vals = h.get_all("Cache-Control") or []
                self.assertEqual(len(vals), 1, f"{path} 下发了多条 Cache-Control: {vals}")

    # ---------------- 穿越防护
    def test_traversal_cannot_escape_ui_root(self):
        samples = (
            f"/{self.mod.TOKEN}/ui/..%2f..%2fsecret.txt",
            f"/{self.mod.TOKEN}/ui/%2e%2e/%2e%2e/secret.txt",
            f"/{self.mod.TOKEN}/ui/assets/..%2f..%2f..%2fsecret.txt",
        )
        for path in samples:
            with self.subTest(path=path):
                code, body, _ = self._get(path)
                self.assertNotIn(b"TOP-SECRET", body)
                # 解析出界一律回落 index（前端路由语义），不会 500
                self.assertEqual(code, 200)
                self.assertEqual(body, INDEX_BODY)

    def test_missing_dist_gives_actionable_404(self):
        saved = self.mod.UI_ROOT
        self.mod.UI_ROOT = self.dist.parent / "nope"
        try:
            code, body, _ = self._get(f"/{self.mod.TOKEN}/ui/")
            self.assertEqual(code, 404)
            self.assertIn("npm run build".encode(), body)   # 提示怎么修，不是干巴巴 500
        finally:
            self.mod.UI_ROOT = saved

    def test_ui_requires_token(self):
        code, _, _ = self._get("/ui/")
        self.assertEqual(code, 404)

    def test_ui_head(self):
        code, body, _ = self._get(f"/{self.mod.TOKEN}/ui/", method="HEAD")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
