#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台唯一入口守卫的测试：**这条测试存在的意义是防止同类事故复发**。

2026-11-09~10 连续三次把功能加到了用户看不见的那一套前端上：

  ① 质量卡 → web/dist（用户在用 /app/，看不到）
  ② NEW 标签 → web/dist（同样看不到）
  ③ 修上面两次时才发现仓库里有两套前端

当时两套都返回 200、都有测试通过，所以"改哪套都看起来对"。
把守卫挂进测试，这类事故就变成 **CI 阶段失败**，而不是等用户发现界面没变。

用法：python3 -m unittest tools.webui.tests.test_console_guard
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools import console_guard as G  # noqa: E402


class TestCurrentRepoIsClean(unittest.TestCase):
    """当前仓库必须通过守卫 —— 这是回归防线。"""

    def test_audit_passes(self):
        r = G.audit()
        self.assertTrue(
            r.ok,
            "控制台入口不统一：\n" + G.format_report(r) +
            "\n\n改前端请写 web/dist/；加工作流请写 registry/")

    def test_no_warnings_about_ui_literals(self):
        r = G.audit()
        bad = [w for w in r.warnings if w.kind == "large_ui_literal"]
        self.assertEqual(bad, [], f"Python 里还有大段 HTML/CSS：{bad}")


class TestRetiredFiles(unittest.TestCase):
    """A 套三个文件被删了就不能复活。"""

    def test_retired_list_covers_a_ui(self):
        for rel in ("webui/pages.py", "webui/components.py", "webui/assets.py"):
            self.assertIn(rel, G.RETIRED, f"{rel} 必须登记在废弃清单里")

    def test_retired_files_absent(self):
        for rel in G.RETIRED:
            self.assertFalse((_ROOT / rel).exists(),
                             f"{rel} 不该存在（A 套已删除）")

    def test_detects_recreated_file(self):
        """守卫要能抓到"有人又把文件建回来"。"""
        r = G.AuditResult()
        with tempfile.TemporaryDirectory() as td:
            fake_root = Path(td)
            (fake_root / "webui").mkdir()
            (fake_root / "webui" / "pages.py").write_text("x = 1\n")
            orig = G.ROOT
            try:
                G.ROOT = fake_root
                G.check_retired_files(r)
            finally:
                G.ROOT = orig
        kinds = [v.kind for v in r.violations]
        self.assertIn("retired_file_present", kinds)


class TestRouteGuard(unittest.TestCase):
    def test_detects_app_page_method(self):
        r = G.AuditResult()
        with tempfile.TemporaryDirectory() as td:
            fs = Path(td) / "fileserver.py"
            fs.write_text("class H:\n    def _app_page(self, rest, q):\n        pass\n")
            orig = G.ROOT
            try:
                G.ROOT = Path(td)
                G.check_fileserver_routes(r)
            finally:
                G.ROOT = orig
        self.assertTrue(any(v.kind == "retired_route_alive" for v in r.violations))

    def test_detects_app_route_branch(self):
        r = G.AuditResult()
        with tempfile.TemporaryDirectory() as td:
            fs = Path(td) / "fileserver.py"
            fs.write_text('if kind == "app":\n    pass\n')
            orig = G.ROOT
            try:
                G.ROOT = Path(td)
                G.check_fileserver_routes(r)
            finally:
                G.ROOT = orig
        self.assertTrue(any("kind ==" in v.detail for v in r.violations))


class TestUIEntry(unittest.TestCase):
    def test_three_assets_exist(self):
        for name in G.UI_FILES:
            self.assertTrue((G.UI_ROOT / name).is_file(),
                            f"web/dist/{name} 缺失 —— 它是唯一前端入口")

    def test_missing_asset_detected(self):
        r = G.AuditResult()
        with tempfile.TemporaryDirectory() as td:
            orig = G.UI_ROOT
            try:
                G.UI_ROOT = Path(td)      # 空目录
                G.check_ui_root(r)
            finally:
                G.UI_ROOT = orig
        self.assertEqual(len(r.violations), len(G.UI_FILES))

    def test_dist_not_gitignored(self):
        """web/dist 是源码。历史上被当成构建产物忽略过，改动进不了库。"""
        r = G.AuditResult()
        G.check_dist_not_ignored(r)
        self.assertEqual([v for v in r.violations if v.kind == "dist_ignored"], [])

    def test_detects_dist_rule(self):
        r = G.AuditResult()
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".gitignore").write_text("web/dist\n__pycache__\n")
            orig = G.ROOT
            try:
                G.ROOT = Path(td)
                G.check_dist_not_ignored(r)
            finally:
                G.ROOT = orig
        self.assertTrue(any(v.kind == "dist_ignored" for v in r.violations))


class TestWorkflowSource(unittest.TestCase):
    """工作流必须来自 registry，且卡与图不能脱节。"""

    def test_all_cards_have_existing_graph(self):
        r = G.AuditResult()
        G.check_workflow_sources(r)
        broken = [v for v in r.violations if v.kind == "graph_missing"]
        self.assertEqual(broken, [],
                         "有工作流卡的 graph 指向不存在的文件：\n"
                         + "\n".join(f"  {v.path}: {v.detail}" for v in broken))

    def test_detects_card_without_graph(self):
        r = G.AuditResult()
        with tempfile.TemporaryDirectory() as td:
            wfdir = Path(td) / "registry" / "workflows"
            wfdir.mkdir(parents=True)
            (wfdir / "bad.json").write_text(json.dumps({"key": "bad"}))
            orig = G.ROOT
            try:
                G.ROOT = Path(td)
                G.WORKFLOW_DIR = wfdir
                G.COMPAT_JSON = Path(td) / "nope.json"
                G.check_workflow_sources(r)
            finally:
                G.ROOT = orig
        self.assertTrue(any(v.kind == "card_no_graph" for v in r.violations))

    def test_verified_requires_evidence_timestamp(self):
        """手改 compat.json 冒充 verified 不成立 —— 必须有 checked_at。"""
        r = G.AuditResult()
        with tempfile.TemporaryDirectory() as td:
            cj = Path(td) / "compat.json"
            cj.write_text(json.dumps({"entries": [
                {"workflow_key": "w1", "status": "verified"}]}))
            orig = G.COMPAT_JSON
            try:
                G.COMPAT_JSON = cj
                G.check_compat_evidence(r)
            finally:
                G.COMPAT_JSON = orig
        self.assertTrue(any(v.kind == "compat_without_evidence" for v in r.violations))

    def test_verified_with_timestamp_passes(self):
        r = G.AuditResult()
        with tempfile.TemporaryDirectory() as td:
            cj = Path(td) / "compat.json"
            cj.write_text(json.dumps({"entries": [
                {"workflow_key": "w1", "status": "verified",
                 "basis": "real-run", "checked_at": "2026-11-10T10:00:00"}]}))
            orig = G.COMPAT_JSON
            try:
                G.COMPAT_JSON = cj
                G.check_compat_evidence(r)
            finally:
                G.COMPAT_JSON = orig
        self.assertEqual(r.violations, [])


class TestCLI(unittest.TestCase):
    def test_exit_zero_on_clean_repo(self):
        r = subprocess.run([sys.executable, "-m", "tools.console_guard"],
                           cwd=_ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_json_output(self):
        r = subprocess.run([sys.executable, "-m", "tools.console_guard", "--json"],
                           cwd=_ROOT, capture_output=True, text=True, timeout=120)
        d = json.loads(r.stdout)
        self.assertIn("ok", d)
        self.assertTrue(d["ok"])

    def test_paths_hint(self):
        r = subprocess.run([sys.executable, "-m", "tools.console_guard", "--paths"],
                           cwd=_ROOT, capture_output=True, text=True, timeout=120)
        self.assertIn("web/dist", r.stdout)
        self.assertIn("registry/workflows", r.stdout)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
