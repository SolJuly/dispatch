#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试：跨云环境 manifest + provision 脚本 —— 不花钱，纯本地。

覆盖 2026-11-09 的跨云改造：把「环境」从「机器快照」拆成
「声明式 manifest + 可执行安装脚本」，让同一份描述能在智云（镜像）和
其它云（脚本）上分别落地。

覆盖点：
  · manifest 结构合法（必填字段、custom_nodes 有 repo+commit）
  · manifest 与 install-nodes.sh 不漂移（10 个节点 commit 一致）
  · torch 版本与 v4 实机一致（2.8.0+cu128，不是 2.10+cu130）
  · constraints 里没有 cu13 残留（和 torch 2.8+cu128 冲突）
  · provision.sh 语法正确、可执行、有 --dry-run / --verify-only
  · 无外网时 provision.sh 拒绝执行（智云上不该被误跑）
  · idle_billing_probe 的判定逻辑

用法：python3 tools/p2/tests/test_cross_cloud.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MANIFEST = ROOT / "deploy" / "env" / "comfyui-wan-p21.env.json"
PROVISION = ROOT / "deploy" / "env" / "provision.sh"
INSTALL_NODES = ROOT / "docker" / "install-nodes.sh"
CONSTRAINTS = ROOT / "docker" / "constraints.txt"
DOCKERFILE = ROOT / "docker" / "Dockerfile"
PROBE = ROOT / "tools" / "p2" / "idle_billing_probe.py"


class TestManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_schema_and_required(self):
        self.assertEqual(self.d["schema"], "p2.env/v1")
        for k in ("name", "base", "python", "comfyui", "custom_nodes",
                  "models", "service", "verification"):
            self.assertIn(k, self.d, f"manifest 缺字段 {k}")

    def test_no_absolute_paths_outside_root(self):
        # manifest 不能引用绝对路径（换云后路径不同）
        text = MANIFEST.read_text(encoding="utf-8")
        bad = re.findall(r'"(/root/[^"]*)"', text)
        # /root/ComfyUI 作为默认值可以出现，但应只在 env 默认里
        self.assertLessEqual(len(bad), 2, f"manifest 里硬编码绝对路径过多: {bad}")

    def test_custom_nodes_have_repo_and_commit(self):
        nodes = self.d["custom_nodes"]
        self.assertEqual(len(nodes), 10, "应为 10 个 custom_nodes")
        for n in nodes:
            self.assertIn("dir", n)
            self.assertIn("repo", n)
            self.assertRegex(n["commit"], r"^[0-9a-f]{40}$",
                             f"{n['dir']} 的 commit 不是 40 位 sha")

    def test_comfy_commit_matches_version(self):
        self.assertEqual(self.d["comfyui"]["version"], "0.36.0")
        self.assertTrue(self.d["comfyui"]["commit"].startswith("3c80da7f"))

    def test_platform_notes_record_compshare_limits(self):
        pn = self.d["platform_notes"]["compshare"]
        self.assertFalse(pn["can_pull_oci"])
        self.assertFalse(pn["instance_has_internet"])


class TestManifestVsInstallScript(unittest.TestCase):
    """manifest 与既有 install-nodes.sh 必须一致，否则两边会漂移。"""

    def test_node_commits_match(self):
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        script = INSTALL_NODES.read_text(encoding="utf-8")
        # install-nodes.sh 里每两行一组：install_node <dir> \ \n <url> <commit>
        pairs = re.findall(
            r"install_node\s+(\S+)\s*\\?\s*\n\s*(\S+)\s+([0-9a-f]{40})", script)
        self.assertEqual(len(pairs), 10, f"install-nodes.sh 解析到 {len(pairs)} 个节点")
        from_script = {d0: (url, c) for d0, url, c in pairs}
        for n in d["custom_nodes"]:
            self.assertIn(n["dir"], from_script, f"manifest 里的 {n['dir']} 不在 install-nodes.sh")
            url, commit = from_script[n["dir"]]
            self.assertEqual(commit, n["commit"],
                             f"{n['dir']} commit 不一致：manifest={n['commit']} script={commit}")
            self.assertEqual(url, n["repo"], f"{n['dir']} repo 不一致")


class TestTorchVersion(unittest.TestCase):
    """2026-11-09 发现：Dockerfile 写的是 2.10+cu130，但 v4 实机是 2.8+cu128。"""

    def test_dockerfile_uses_cu128(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("cu128", text)
        self.assertNotIn("2.10.0+cu130", text, "Dockerfile 还在用已废弃的 torch 2.10")

    def test_constraints_match_v4(self):
        text = CONSTRAINTS.read_text(encoding="utf-8")
        self.assertIn("torch==2.8.0+cu128", text)
        # torch 2.8 用 cu12 系列 nvidia 包；不能混入 cu13
        self.assertNotIn("cu13", text, "constraints 里混入了 cu13 包")

    def test_manifest_pins_match_constraints(self):
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        text = CONSTRAINTS.read_text(encoding="utf-8")
        for pkg, ver in d["python"]["pins"].items():
            self.assertIn(f"{pkg}=={ver}", text,
                          f"manifest 的 {pkg}=={ver} 与 constraints 不一致")

    def test_xformers_excluded(self):
        d = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertIn("xformers", d["python"]["exclude"])


class TestProvisionScript(unittest.TestCase):
    def test_syntax(self):
        p = subprocess.run(["bash", "-n", str(PROVISION)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_executable(self):
        import os
        self.assertTrue(os.access(PROVISION, os.X_OK), "provision.sh 没有可执行权限")

    def test_has_modes(self):
        text = PROVISION.read_text(encoding="utf-8")
        for flag in ("--dry-run", "--verify-only", "--skip-torch", "--skip-nodes",
                     "--manifest"):
            self.assertIn(flag, text, f"缺 {flag}")

    def test_dry_run_works(self):
        p = subprocess.run(["bash", str(PROVISION), "--dry-run"],
                           capture_output=True, text=True, timeout=120, cwd=str(ROOT))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("DRY-RUN", p.stdout)
        # 应该列出 10 个节点
        self.assertEqual(p.stdout.count("·"), 10, "dry-run 应列出 10 个 custom_nodes")

    def test_refuses_on_deploy_host(self):
        """部署机（有 .git/.share_token/tools）不是目标机 —— 必须拒绝。

        2026-11-09 事故：在部署机上跑 provision.sh，它把「有外网」当成
        「可以是目标机」，真的开始 pip install torch，吃了 7GB 盘。
        """
        p = subprocess.run(["bash", str(PROVISION)],
                           capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        combined = p.stdout + p.stderr
        self.assertEqual(p.returncode, 1, combined)
        self.assertIn("拒绝在部署机上执行", combined)
        self.assertIn("PROVISION_ALLOW_DEPLOY_HOST", combined,
                      "应告诉用户怎么绕过（本机就是 GPU 机的场景）")

    def test_allow_deploy_host_override(self):
        """显式覆盖后应放行身份守卫（但会因为别的原因在 dry-run 里不下结论）。"""
        import os
        env = dict(os.environ, PROVISION_ALLOW_DEPLOY_HOST="1")
        p = subprocess.run(["bash", str(PROVISION), "--dry-run"],
                           capture_output=True, text=True, timeout=90,
                           cwd=str(ROOT), env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("拒绝在部署机上执行", p.stdout)

    def test_dry_run_bypasses_guard_but_install_does_not(self):
        """dry-run 不写盘，应放行；真装必须拒绝。"""
        d = subprocess.run(["bash", str(PROVISION), "--dry-run"],
                           capture_output=True, text=True, timeout=90, cwd=str(ROOT))
        self.assertEqual(d.returncode, 0, d.stdout + d.stderr)
        i = subprocess.run(["bash", str(PROVISION)],
                           capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        self.assertEqual(i.returncode, 1)


class TestIdleProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        import importlib.util
        spec = importlib.util.spec_from_file_location("idle_probe", PROBE)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def test_has_subcommands(self):
        for cmd in ("start", "sample", "report", "stop"):
            self.assertTrue(hasattr(self.m, f"cmd_{cmd}"))

    def test_eps_reasonable(self):
        # 平台返回两位小数，噪声阈值应小于一分钱
        self.assertLess(self.m.EPS, 0.01)
        self.assertGreater(self.m.EPS, 0)

    def test_separate_ledger(self):
        """不能污染主 ledger —— 这是受控实验。"""
        self.assertNotEqual(self.m.PROBE, self.m.MAIN_LEDGER)
        self.assertIn("idle_probe", str(self.m.PROBE))


class TestNoRegressionInDocs(unittest.TestCase):
    def test_docker_readme_warns_not_on_compshare(self):
        readme = (ROOT / "docker" / "README.md")
        if not readme.exists():
            self.skipTest("docker/README.md 不存在")
        text = readme.read_text(encoding="utf-8")
        self.assertIn("不能在 CompShare 上直接跑", text,
                      "docker/README 必须写明智云不能拉 OCI")


if __name__ == "__main__":
    unittest.main(verbosity=2)
