# -*- coding: utf-8 -*-
"""OpenAPI 契约校验：$ref 可解析、operationId 唯一、示例齐全、
implemented 的路径在 fileserver.py 里真的有对应实现、Mock 能覆盖所有已实现接口。
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
SERVER = (ROOT / "fileserver.py").read_text(encoding="utf-8")

# 路径 → fileserver.py 里的实现标记（防止"文档写了、代码没有"）
IMPL_MARKERS = {
    "/api/p2/jobs": 'rest[2:3] == ["jobs"]',
    "/api/p2/jobs/{job_id}": "def _api_jobs",
    "/api/p2/jobs/{job_id}/cancel": '"cancel"',
    "/api/p2/jobs/zip": 'rest == ["zip"]',
    "/api/p2/uploads": "def _api_upload",
    "/api/p2/queue": 'sub == ["queue"]',
    "/api/p2/queue/jobs": 'rest[2:5] == ["queue", "jobs"]',
    "/api/p2/registry": "def _api_registry",
    "/api/p2/status": 'rest[1] == "status"',
    "/api/p2/version": "def _api_version",
}


def walk_refs(node, out):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.append(ref)
        for v in node.values():
            walk_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_refs(v, out)


class TestSpecStructure(unittest.TestCase):
    def test_openapi_version(self):
        self.assertTrue(SPEC["openapi"].startswith("3."))
        self.assertTrue(SPEC["info"]["title"])

    def test_all_refs_resolve(self):
        refs = []
        walk_refs(SPEC, refs)
        self.assertGreater(len(refs), 10)
        for ref in refs:
            self.assertTrue(ref.startswith("#/"), ref)
            node = SPEC
            for part in ref[2:].split("/"):
                self.assertIn(part, node, f"$ref 无法解析：{ref}")
                node = node[part]

    def test_operation_ids_unique(self):
        ids = []
        for path, item in SPEC["paths"].items():
            for method, op in item.items():
                if method in ("get", "post", "put", "delete"):
                    ids.append(op["operationId"])
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_operation_has_contract(self):
        tags = {t["name"] for t in SPEC["tags"]}
        for path, item in SPEC["paths"].items():
            for method, op in item.items():
                if method not in ("get", "post", "put", "delete"):
                    continue
                with self.subTest(op=op["operationId"]):
                    self.assertIn(op["x-status"], ("implemented", "planned"))
                    self.assertTrue(set(op["tags"]) <= tags)
                    codes = [c for c in op["responses"] if c.startswith("2")]
                    self.assertTrue(codes, "缺 2xx 响应")
                    resp = op["responses"][codes[0]]
                    if "$ref" in resp:
                        continue
                    content = (resp.get("content") or {})
                    self.assertTrue(content, "2xx 没有 content（前端无法 Mock）")

    def test_implemented_paths_have_examples(self):
        for path, item in SPEC["paths"].items():
            for method, op in item.items():
                if method not in ("get", "post") or op["x-status"] != "implemented":
                    continue
                code = next(c for c in op["responses"] if c.startswith("2"))
                content = op["responses"][code].get("content") or {}
                if "application/json" not in content:
                    continue          # 二进制下载（zip）不要求 JSON 示例
                ex = (content["application/json"].get("examples") or {}).get("default")
                self.assertIsNotNone(ex, f"{path} {method} 缺示例")


class TestCodeMatchesSpec(unittest.TestCase):
    def test_implemented_paths_exist_in_fileserver(self):
        for path, item in SPEC["paths"].items():
            mark = IMPL_MARKERS.get(path)
            if not mark:
                continue
            with self.subTest(path=path):
                self.assertIn(mark, SERVER, f"{path} 在 fileserver.py 里找不到实现标记 {mark!r}")

    def test_planned_paths_not_marked_implemented(self):
        for path in ("/api/p2/presets", "/api/p2/share/{job_id}",
                     "/api/p2/jobs/{job_id}/rerun"):
            for method, op in SPEC["paths"][path].items():
                self.assertEqual(op["x-status"], "planned", path)


class TestMockCoverage(unittest.TestCase):
    def test_mock_covers_every_implemented_json_get(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("mock_api", ROOT / "scripts/mock_api.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        for path, item in SPEC["paths"].items():
            for method, op in item.items():
                if method != "get" or op["x-status"] != "implemented":
                    continue
                code = next(c for c in op["responses"] if c.startswith("2"))
                if "application/json" not in op["responses"][code].get("content", {}):
                    continue
                concrete = path.replace("{job_id}", "p1-20261103T1219-b2df1a") \
                               .replace("{key}", "sdxl_t2i")
                with self.subTest(path=concrete):
                    self.assertIsNotNone(mod.match_spec("get", concrete),
                                         f"Mock 覆盖不到 {concrete}")


if __name__ == "__main__":
    unittest.main()
