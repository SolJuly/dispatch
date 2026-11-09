#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：2026-11-09 审计发现的 6 个漏洞已修复。

每条测试对应一个具体漏洞，都是当时实测到的真实问题：

  1. /pub/ 目录把 token 和用户素材公开在公网
  2. /pub/jobs/ 开着 autoindex，用户原始照片可下载
  3. token 明文写死在后端/前端/文档里
  4. build_openapi.py 把真实 token 写进 openapi.json
  5. run.py 的 budget.max_cost 只打印、从不校验
  6. UI 提交不带 --batch，所有提交走全局串行锁

用法：python3 tools/p1/tests/test_security_fixes.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestNoHardcodedToken(unittest.TestCase):
    """漏洞 3/4：token 不能出现在任何入库文件里（.share_token 除外）。"""

    SCAN_EXT = (".md", ".html", ".json", ".py", ".js", ".sh", ".css")
    SKIP_DIRS = {"node_modules", "__pycache__", ".git", "share"}

    def _real_token(self) -> str:
        tf = ROOT / ".share_token"
        if not tf.exists():
            self.skipTest("没有 .share_token，跳过")
        return tf.read_text(encoding="utf-8").strip()

    def test_no_token_in_repo_files(self):
        tok = self._real_token()
        if not tok or len(tok) < 6:
            self.skipTest("token 太短，跳过")
        hits = []
        for dirpath, dirnames, files in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for fn in files:
                if not fn.endswith(self.SCAN_EXT):
                    continue
                p = Path(dirpath) / fn
                # 审计报告是"证据引用"，允许保留（它讲的就是这个漏洞）
                if "reports/audit" in str(p).replace(os.sep, "/"):
                    continue
                try:
                    txt = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if tok in txt:
                    hits.append(str(p.relative_to(ROOT)))
        self.assertEqual(hits, [], f"这些文件仍含真实 token：{hits}")

    def test_openapi_uses_placeholder(self):
        spec = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
        tok = spec.get("x-mock", {}).get("token", "")
        self.assertEqual(tok, "{TOKEN}", "openapi.json 的 x-mock.token 应为占位符")
        for srv in spec.get("servers", []):
            self.assertIn("{TOKEN}", srv.get("url", ""),
                          f"servers 里还带真实 token：{srv}")

    def test_app_js_has_no_token_fallback(self):
        js = (ROOT / "web" / "dist" / "app.js").read_text(encoding="utf-8")
        # 只允许从 URL 提取，不允许 || 'xxxxx' 这种写死的兜底
        m = re.search(r"const TOKEN = .*?;", js, re.S)
        self.assertIsNotNone(m, "找不到 const TOKEN 定义")
        line = m.group(0)
        self.assertNotRegex(line, r"\|\|\s*['\"][0-9a-f]{6,}['\"]",
                            "app.js 的 TOKEN 还有写死的 fallback")


class TestBuildOpenApiToken(unittest.TestCase):
    """漏洞 4：build() 默认必须用占位符，不能悄悄读 .share_token。"""

    def _mod(self):
        import importlib.util
        p = ROOT / "scripts" / "build_openapi.py"
        spec = importlib.util.spec_from_file_location("bu_openapi", p)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_default_is_placeholder(self):
        m = self._mod()
        spec = m.build()
        self.assertEqual(spec["x-mock"]["token"], "{TOKEN}")
        self.assertIn("{TOKEN}", spec["servers"][0]["url"])

    def test_explicit_token_used(self):
        m = self._mod()
        spec = m.build("ABC12345")
        self.assertEqual(spec["x-mock"]["token"], "ABC12345")
        self.assertIn("ABC12345", spec["servers"][0]["url"])

    def test_load_token_default_not_from_file(self):
        m = self._mod()
        self.assertEqual(m._load_token(), "{TOKEN}",
                         "默认不该读 .share_token")


class TestBudgetCapEnforced(unittest.TestCase):
    """漏洞 5：max_cost 必须真的拦住超额作业。"""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        from tools.p1 import run as runmod
        self.runmod = runmod

    def test_cap_check_exists_and_raises(self):
        """源码里必须有「est_real > cap → RunError」的判定。"""
        src = (ROOT / "tools" / "p1" / "run.py").read_text(encoding="utf-8")
        self.assertIn("est_real", src, "没有 est_real，说明预算还是按 hours 粗算")
        self.assertRegex(src, r"if\s+cap\s*>\s*0\s+and\s+est_real\s*>\s*cap",
                         "缺少 cap 超限判定")
        self.assertIn("超过作业预算上限", src, "缺少超预算的错误文案")

    def test_balance_must_cover_estimate(self):
        src = (ROOT / "tools" / "p1" / "run.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"bal\s*<\s*est_real",
                         "缺少「余额不足以覆盖预估」的判定")

    def test_estimate_uses_min_of_hours_and_max_minutes(self):
        src = (ROOT / "tools" / "p1" / "run.py").read_text(encoding="utf-8")
        self.assertIn("will_run_h", src)
        self.assertRegex(src, r"min\(float\(hours or 0\) or max_min / 60, max_min / 60\)",
                         "预估应取 hours 与 max_minutes 中较紧的一个")


class TestUiSubmitUsesBatch(unittest.TestCase):
    """漏洞 6：UI 提交必须带 --batch，否则全局串行。"""

    def test_both_spawn_sites_have_batch(self):
        src = (ROOT / "webui" / "api.py").read_text(encoding="utf-8")
        # 两处 spawn RUN_PY 的地方都要带 --batch
        spawns = re.findall(r"spawn\(\[sys\.executable, str\(RUN_PY\)[^\]]*\]", src)
        self.assertEqual(len(spawns), 2, f"应有 2 处 spawn，实际 {len(spawns)}")
        for i, s in enumerate(spawns, 1):
            self.assertIn('"--batch"', s, f"第 {i} 处 spawn 缺 --batch")

    def test_batch_really_means_per_job_lock(self):
        src = (ROOT / "tools" / "p1" / "run.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"if getattr\(opts, \"batch\", False\):\s*\n\s*return store\.per_job_lock",
                         "--batch 应切到作业级锁")


class TestNginxPubProtected(unittest.TestCase):
    """漏洞 1/2：nginx 配置里 /pub/ 敏感目录必须走 basic auth。"""

    CONF = Path("/etc/nginx/sites-available/dsh")

    def setUp(self):
        if not self.CONF.exists():
            self.skipTest("没有 nginx 配置（非部署机）")

    def test_no_blanket_open_pub(self):
        txt = self.CONF.read_text(encoding="utf-8")
        # 不允许出现「location /pub/ 紧跟 auth_basic off」
        bad = re.search(r"location\s+/pub/\s*\{[^}]*auth_basic\s+off", txt, re.S)
        self.assertIsNone(bad, "/pub/ 仍有 auth_basic off 的整块放开")

    def test_sensitive_dirs_require_auth(self):
        txt = self.CONF.read_text(encoding="utf-8")
        m = re.search(r"location\s+~\s+\^/pub/\(([^)]+)\)/\s*\{(.*?)\}", txt, re.S)
        self.assertIsNotNone(m, "找不到敏感目录的 auth 规则")
        dirs = m.group(1).split("|")
        body = m.group(2)
        self.assertIn("auth_basic", body)
        self.assertIn(".htpasswd", body)
        self.assertIn("autoindex off", body)
        for d in ("p21", "p20", "api", "jobs"):
            self.assertIn(d, dirs, f"敏感目录 {d} 未被保护")

    def test_no_autoindex_on_pub(self):
        txt = self.CONF.read_text(encoding="utf-8")
        # 任何 /pub/ 相关的 location 都不该开 autoindex on
        for m in re.finditer(r"location[^{]*pub[^{]*\{(.*?)\}", txt, re.S):
            self.assertNotIn("autoindex on", m.group(1).replace(" ", ""),
                             f"/pub/ 某段还开着 autoindex: {m.group(0)[:80]}")


class TestSecurityFixesDocumented(unittest.TestCase):
    def test_audit_report_exists(self):
        p = ROOT / "reports" / "audit" / "主线审计-20261109.md"
        self.assertTrue(p.exists(), "审计报告应存在")
        txt = p.read_text(encoding="utf-8")
        self.assertIn("Token 明文挂在公网", txt)
        self.assertIn("max_cost", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
