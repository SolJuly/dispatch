# -*- coding: utf-8 -*-
"""本地 mock ComfyUI 集成测试：JSON 配置 → 渲染 → /prompt → /history → /view → 复验。

不连 GPU、不花钱；验证的是 ComfyUI 协议契约与结果回传链路。
真机链路由 P2.0 任务 4/5 的实跑验收覆盖。
"""
from __future__ import annotations

import json
import struct
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.p1 import verify as verify_mod
from tools.p20 import comfy, registry, render


def png_bytes(w: int = 1024, h: int = 1024) -> bytes:
    row = b"\x00" + bytes([30, 90, 160]) * w
    raw = row * h

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


PNG = png_bytes()


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code: int, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != "/prompt":
            return self._json(404, {"error": "not found"})
        graph = body.get("prompt") or {}
        if any((n or {}).get("class_type") == "__bad__" for n in graph.values()):
            return self._json(400, {"error": "invalid node", "node_errors": {}})
        pid = "mock-prompt-1"
        self.server.payloads[pid] = body
        return self._json(200, {"prompt_id": pid, "number": 1, "node_errors": {}})

    def do_GET(self):
        if self.path.startswith("/history/"):
            pid = self.path.rsplit("/", 1)[-1]
            if pid not in self.server.payloads:
                return self._json(200, {})
            return self._json(200, {pid: {
                "status": {"status_str": "success", "completed": True, "messages": []},
                "outputs": {"save": {"images": [
                    {"filename": "mock.png", "subfolder": "", "type": "output"}]}},
            }})
        if self.path.startswith("/view"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return
        return self._json(404, {"error": "not found"})


class TestMockComfyChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        cls.server.payloads = {}
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.wf = registry.get_workflow("qwen_image_t2i")
        cls.model = registry.get_model("qwen_image")
        cls.params = {"prompt": "a red apple on a wooden table", "neg_prompt": "blur",
                      "seed": 42, "width": 1024, "height": 1024, "steps": 20, "cfg": 2.5,
                      "sampler": "euler", "scheduler": "simple", "batch_size": 1}
        cls.graph, cls.meta = render.render(cls.wf, cls.model, cls.params, "mock-job")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_full_chain(self):
        # 1) 提交
        p = comfy.http_post_json(comfy.prompt_url(port=self.port),
                                 render.build_payload(self.graph, "itest"))
        self.assertIn("prompt_id", p)
        pid = p["prompt_id"]
        sent = self.server.payloads[pid]["prompt"]
        self.assertEqual(sent["save"]["inputs"]["filename_prefix"], "mock-job")
        self.assertNotIn("{{", json.dumps(sent, ensure_ascii=False))

        # 2) 轮询
        h = comfy.http_get_json(comfy.history_url(port=self.port, pid=pid))
        entry = h[pid]
        self.assertEqual(comfy.status_of(entry), "success")
        files = comfy.extract_files(entry)
        self.assertEqual(files, [("", "mock.png")])

        # 3) 回传 + 4) 复验
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "artifacts" / "01-mock.png"
            n = comfy.http_download(comfy.view_url(port=self.port, filename="mock.png"),
                                    dest)
            self.assertGreater(n, 100)
            job = {"kind": "image", "params": self.params,
                   "model": {"key": "qwen_image",
                             "registry": {"workflow_key": "qwen_image_t2i",
                                          "model_key": "qwen_image"}},
                   "expect": {"ext": "png", "min_bytes": 100,
                              "width": [1024], "height": [1024]}}
            res = verify_mod.verify_job(job, dest.parent, rendered_graph=self.graph,
                                        wf_config=self.wf)
            self.assertEqual(res["verdict"], "PASS", res)
            self.assertTrue(res["artifacts"][0]["checks"]["decode"])
            self.assertTrue(res["artifacts"][0]["checks"]["geometry"])
            self.assertTrue(res["artifacts"][0]["checks"]["params"])

    def test_prompt_rejects_bad_node(self):
        bad = json.loads(json.dumps(self.graph))
        bad["oops"] = {"class_type": "__bad__", "inputs": {}}
        with self.assertRaises(Exception):
            comfy.http_post_json(comfy.prompt_url(port=self.port),
                                 render.build_payload(bad, "itest"))

    def test_error_parsing(self):
        entry = {"status": {"status_str": "error", "messages": [
            ["execution_error", {"node_type": "UNETLoader",
                                 "exception_message": "model not found"}]]}}
        self.assertEqual(comfy.status_of(entry), "error")
        self.assertIn("UNETLoader", comfy.extract_error(entry))


if __name__ == "__main__":
    unittest.main()
