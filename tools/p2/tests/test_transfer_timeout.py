#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 transfer 超时修复单测（¥0，不连实例，monkeypatch subprocess.run）。"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import tools.transfer as tr  # noqa: E402


class _DummyCS:
    def ssh_of(self, inst): return "ssh root@1.2.3.4"
    def public_ip(self, inst): return "1.2.3.4"
    def password_of(self, inst): return "pw"


def _mk():
    return tr.Transfer(_DummyCS(), {}, quiet=True, timeout=10)


class SshTimeoutTest(unittest.TestCase):
    def setUp(self):
        self._orig = tr.subprocess.run

    def tearDown(self):
        tr.subprocess.run = self._orig

    def test_ssh_timeout_returns_124(self):
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=60)

        tr.subprocess.run = boom
        rc, out = _mk().ssh("echo hi", timeout=60)
        self.assertEqual(rc, 124)
        self.assertIn("TIMEOUT", out)

    def test_ssh_retries_then_124(self):
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=60)

        tr.subprocess.run = boom
        rc, _ = _mk().ssh("echo hi", timeout=60, retries=2)
        self.assertEqual(rc, 124)
        self.assertEqual(calls["n"], 2)   # 重试了一次

    def test_classify_timeout_degrades_to_file(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=60)

        tr.subprocess.run = boom
        kind, items, base = _mk()._classify("/root/ComfyUI/output/x.mp4")
        self.assertEqual(kind, "file")
        self.assertEqual(items, ["x.mp4"])
        self.assertEqual(base, "/root/ComfyUI/output")

    def test_pull_probe_timeout_returns_false(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="ssh", timeout=60)

        tr.subprocess.run = boom
        ok = _mk().pull("/root/out/a.mp4", "/tmp/p2-pull-test")
        self.assertFalse(ok)   # 不抛异常，返回 False

    def test_classify_normal_regression(self):
        seq = [
            subprocess.CompletedProcess([], 0, "DIR\n", ""),        # test -d
            subprocess.CompletedProcess([], 0, "a.mp4\nb.mp4\n", ""),  # ls -1
        ]
        tr.subprocess.run = lambda *a, **k: seq.pop(0)
        kind, items, base = _mk()._classify("/root/out")
        self.assertEqual(kind, "dir")
        self.assertEqual(items, ["out"])
        self.assertEqual(base, "/root")

        tr.subprocess.run = lambda *a, **k: subprocess.CompletedProcess([], 0, "FILE\n", "")
        kind, items, base = _mk()._classify("/root/out/a.mp4")
        self.assertEqual(kind, "file")
        self.assertEqual(items, ["a.mp4"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
