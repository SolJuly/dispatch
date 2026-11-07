# -*- coding: utf-8 -*-
"""执行器接入点单测：只测新增的 registry 分支，legacy 行为由 P1 cases 回归覆盖。

用 monkeypatch 替换 run._ssh_run / run.store.atomic_write_json，
不建机、不花钱、不需要 ComfyUI。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from tools.p1 import run, state
from tools.p20 import assets, registry


def _job(workflow="qwen_image_t2i", model="qwen_image"):
    return {
        "job_id": "p1-20261101T0000-abcdef",
        "kind": "image",
        "model": {
            "key": model,
            "workflow": "workflows/qwen_image_t2i.api.json",
            "checkpoint": "qwen/qwen_image_fp8_e4m3fn_scaled.safetensors",
            "registry": {"workflow_key": workflow,
                         "workflow_version": registry.get_workflow(workflow)["version"],
                         "model_key": model, "model_version": 1,
                         "setup_need": "qwen"},
        },
        "params": {"prompt": "a cat", "neg_prompt": "bad", "seed": 7,
                   "width": 1024, "height": 1024, "steps": 20, "cfg": 2.5},
        "expect": {"ext": "png", "min_bytes": 100, "width": [1024], "height": [1024]},
        "budget": {"max_minutes": 1, "max_cost": 0.1},
    }


class TestNeed(unittest.TestCase):
    def test_registry_setup_need(self):
        self.assertEqual(run._need(_job()), "qwen")

    def test_legacy_fallback(self):
        j = _job()
        j["model"].pop("registry")
        self.assertEqual(run._need(j), "qwen")
        j["model"]["key"] = "h3_t2v"
        self.assertEqual(run._need(j), "h3")


class TestReady(unittest.TestCase):
    def _check_json(self, ok=True):
        model = registry.get_model("qwen_image")
        d = {}
        for i, a in enumerate(assets.asset_rules(model)):
            d[a["key"]] = {"ok": ok if i == 0 else True, "why": "" if ok else "missing"}
        return "noise\nASSET_CHECK:" + json.dumps(d)

    def test_registry_assets_ok(self):
        with mock.patch.object(run, "_ssh_run", return_value=(0, self._check_json(True))):
            run.step_ready(_job(), None, {"host": "h", "port": 23, "password": "p"})

    def test_registry_assets_missing(self):
        with mock.patch.object(run, "_ssh_run", return_value=(0, self._check_json(False))):
            with self.assertRaises(run.RunError) as cm:
                run.step_ready(_job(), None, {"host": "h", "port": 23, "password": "p"})
        self.assertEqual(cm.exception.fail_state, state.FAILED_PROVISION)
        self.assertIn("模型资产不可见", str(cm.exception))


class TestSubmit(unittest.TestCase):
    def test_registry_render_and_submit(self):
        captured = {}

        def fake_ssh(host, port, pwd, cmd, timeout=600):
            captured["cmd"] = cmd
            return 0, '{"prompt_id": "pid-123", "number": 1}'

        written = {}
        with mock.patch.object(run, "_ssh_run", side_effect=fake_ssh), \
                mock.patch.object(run.store, "atomic_write_json",
                                  side_effect=lambda p, o: written.update({"path": str(p), "obj": o})):
            ctx = {"host": "h", "port": 23, "password": "p"}
            run.step_submit(_job(), None, ctx)
        self.assertEqual(ctx["prompt_id"], "pid-123")
        self.assertNotIn("{{", captured["cmd"])
        self.assertIn("p1-20261101T0000-abcdef", captured["cmd"])
        self.assertEqual(written["obj"]["schema"], "p20.render/v1")
        self.assertEqual(written["obj"]["meta"]["workflow_key"], "qwen_image_t2i")


class TestWait(unittest.TestCase):
    def _run_wait(self, entry_text):
        with mock.patch.object(run, "_ssh_run", return_value=(0, entry_text)), \
                mock.patch.object(run.time, "sleep", return_value=None):
            return run.step_wait(_job(), None, {"host": "h", "port": 23, "password": "p",
                                                "prompt_id": "pid"})

    def test_success_files(self):
        entry = {"status": {"status_str": "success"},
                 "outputs": {"save": {"images": [{"subfolder": "", "filename": "a.png"}]}}}
        files = self._run_wait(json.dumps(entry))
        self.assertEqual(files, [("", "a.png")])

    def test_error_status(self):
        entry = {"status": {"status_str": "error", "messages": [
            ["execution_error", {"node_type": "UNETLoader", "exception_message": "bad model"}]]}}
        with self.assertRaises(run.RunError) as cm:
            self._run_wait(json.dumps(entry))
        self.assertEqual(cm.exception.fail_state, state.FAILED_RUN)
        self.assertIn("UNETLoader", str(cm.exception))

    def test_success_without_outputs(self):
        entry = {"status": {"status_str": "success"}, "outputs": {}}
        with self.assertRaises(run.RunError) as cm:
            self._run_wait(json.dumps(entry))
        self.assertEqual(cm.exception.fail_state, state.FAILED_FETCH)


class TestModelLinks(unittest.TestCase):
    def test_links_applied(self):
        calls = []

        def fake_ssh(host, port, pwd, cmd, timeout=600):
            calls.append(cmd)
            return 0, "LINK_OK"

        with mock.patch.object(run, "_ssh_run", side_effect=fake_ssh):
            run._apply_model_links(_job(), {"host": "h", "port": 23, "password": "p"})
        model = registry.get_model("qwen_image")
        # 显式声明的软链 + 自动补链的 find 一趟；find 没输出时不再执行 ln 脚本
        # （find 自己挂了（超时/路径不通）不能当成"权重缺失"，交给 ready 阶段报错）
        self.assertEqual(len(calls), len(model["setup"]["links"]) + 1)
        self.assertTrue(any("find /model" in c for c in calls))

    def test_links_failure_raises(self):
        with mock.patch.object(run, "_ssh_run", return_value=(1, "ln: permission denied")):
            with self.assertRaises(run.RunError) as cm:
                run._apply_model_links(_job(), {"host": "h", "port": 23, "password": "p"})
        self.assertEqual(cm.exception.fail_state, state.FAILED_PROVISION)


class TestWaitDetectsDeadServer(unittest.TestCase):
    """ComfyUI 挂了要立刻失败，别傻等到 45 分钟预算上限（2026-11-07 真实事故）。"""

    def test_dead_process_raises_fast(self):
        import time as _t
        calls = {"n": 0}

        def fake_ssh(host, port, pwd, cmd, timeout=600):
            calls["n"] += 1
            if "pgrep" in cmd:
                return 0, "---\n0 %"          # 没有 main.py → 进程没了
            return 1, "ssh: connect to host port 23: Connection refused"

        job = {"job_id": "p1-20261107T0000-abcdef", "budget": {"max_minutes": 45}}
        with mock.patch.object(run, "_ssh_run", side_effect=fake_ssh):
            t0 = _t.time()
            with self.assertRaises(run.RunError) as cm:
                run.step_wait(job, mock.Mock(), {"host": "h", "port": 23,
                                                 "password": "p", "prompt_id": "x"})
            spent = _t.time() - t0
        self.assertEqual(cm.exception.fail_state, state.FAILED_RUN)
        self.assertIn("ComfyUI", str(cm.exception))
        self.assertLess(spent, 30, "必须在几十秒内失败，而不是等到预算上限")

    def test_alive_process_keeps_waiting(self):
        n = {"i": 0}

        def fake_ssh(host, port, pwd, cmd, timeout=600):
            n["i"] += 1
            if "pgrep" in cmd:
                return 0, "/root/ComfyUI/main.py --listen\n99 %"
            if n["i"] == 1:
                return 1, "temporary blip"      # 先抖一下：触发 pgrep，进程还在
            return 0, '{"status": {"status_str": "error", "messages": []}, "outputs": {}}' 

        job = {"job_id": "p1-20261107T0000-abcdef", "budget": {"max_minutes": 1}}
        with mock.patch.object(run, "_ssh_run", side_effect=fake_ssh):
            with self.assertRaises(run.RunError) as cm:
                run.step_wait(job, mock.Mock(), {"host": "h", "port": 23,
                                                 "password": "p", "prompt_id": "x"})
        self.assertEqual(cm.exception.fail_state, state.FAILED_RUN)
        self.assertIn("推理失败", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
