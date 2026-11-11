#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端：`wfctl new` 的七步流程必须**真的能一条命令跑通**。

## 为什么要有这个测试

`wf_templates` / `wf_infer` / `wf_lint` 各自单测通过，不代表串起来能用。
2026-11-11 的实际过程就是这样：三个工具都绿着，但第一次真跑 `wfctl new`
连挂三次：

  ① f-string 里塞了反斜杠 → 语法错
  ② 槽位推出来后模型卡覆盖不了 → 等 `p20ctl import` 才抛，图已经拷进库
  ③ 模板推荐了 `flf2v`，但推不出 seed → 参数被静默丢掉

每一次都是"组件对、拼起来不对"。所以这里跑**真实的命令**（subprocess），
用临时 key，跑完清理干净。

## 这个测试会动仓库

它在 `registry/workflows/` 下临时建卡、`workflows/` 下临时建图，
结束时删干净并把索引重建回去。用 `zz_e2e_` 前缀就是为了不撞真卡。

用法：python3 -m unittest tools.p20.tests.test_wfctl_new_e2e
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

TEST_KEY = "zz_e2e_newflow"
SRC_GRAPH = _ROOT / "workflows" / "pack" / "video_ltx2_3_flf2v.api.json"
CARD = _ROOT / "registry" / "workflows" / f"{TEST_KEY}.json"
GRAPH = _ROOT / "workflows" / f"{TEST_KEY}.api.json"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", *args], cwd=str(_ROOT),
                          capture_output=True, text=True, timeout=300)


class TestWfctlNewEndToEnd(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for p in (CARD, GRAPH):
            p.unlink(missing_ok=True)
        # 索引是派生的，重建回真实状态
        _run("tools.p20.p20ctl", "import")

    def test_01_new_runs_or_fails_cleanly(self):
        """`wfctl new` 要么成功，要么给出**可操作的**失败原因。

        不许出现 Python traceback —— 那说明错误没被接住，
        用户看到的是调用栈而不是"该改哪个文件的哪一段"。
        """
        r = _run("tools.wfctl", "new", "--key", TEST_KEY,
                 "--name", "端到端测试", "--kind", "video",
                 "--graph", str(SRC_GRAPH),
                 "--model", "ltx_2_3_22b_dev_fp8",
                 "--template", "flf2v")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertNotIn("Traceback", out, out[-2000:])
        # 这条图会推出 text_encoder 槽位，而该模型卡没有 → 应当在**这里**被拦，
        # 而不是等到 p20ctl import 抛异常
        self.assertIn("覆盖不了图里推出来的槽位", out)
        self.assertIn("--drop-slots", out)
        # 拦住时不许留下半成品
        self.assertFalse(GRAPH.exists(), "被拦住时不该留下图文件")

    def test_02_new_succeeds_with_drop_slots(self):
        """明确放弃那个槽位后，流程应当一次跑通。"""
        r = _run("tools.wfctl", "new", "--key", TEST_KEY,
                 "--name", "端到端测试", "--kind", "video",
                 "--graph", str(SRC_GRAPH),
                 "--model", "ltx_2_3_22b_dev_fp8",
                 "--template", "flf2v",
                 "--drop-slots", "text_encoder")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertNotIn("Traceback", out, out[-2000:])
        self.assertEqual(r.returncode, 0, out[-2000:])
        self.assertIn("卡已生成", out)
        self.assertIn("门禁通过", out)
        self.assertIn("索引已重建", out)

    def test_03_generated_card_is_usable(self):
        """生成的卡必须：门禁全绿、参数有说明、正负不同句、时长判据在位。"""
        self.assertTrue(CARD.is_file(), "上一步没生成卡")
        card = json.loads(CARD.read_text(encoding="utf-8"))

        from tools.p20 import wf_lint as L
        findings = L.lint_card(TEST_KEY, card, "experimental")
        errors = [f for f in findings if f.level == "ERROR"]
        self.assertEqual(errors, [], [f.detail for f in errors])

        # 参数不能是空壳
        self.assertGreaterEqual(len(card["params"]), 4)
        for name, spec in card["params"].items():
            self.assertTrue(spec.get("bindings"), f"{name} 没有绑到任何节点")
            for b in spec["bindings"]:
                self.assertIn(b["node"], json.loads(
                    GRAPH.read_text(encoding="utf-8")),
                    f"{name} 绑到不存在的节点 {b['node']}")

        # 视频卡必须有可判的期望
        exp = card["expect_defaults"]
        self.assertIn("min_bytes", exp)
        self.assertTrue("duration_s" in exp or "fps" in card,
                        "视频卡没有时长判据 —— 出 1 秒也会判 PASS")

    def test_04_reimport_is_stable(self):
        """改完不跑 import，UI 就是旧的 —— 这条验证 import 幂等。"""
        r1 = _run("tools.p20.p20ctl", "import")
        r2 = _run("tools.p20.p20ctl", "import")
        self.assertEqual(r1.returncode, 0, r1.stderr[-800:])
        self.assertEqual(r2.returncode, 0, r2.stderr[-800:])
        r3 = _run("tools.p20.p20ctl", "import", "--check")
        self.assertEqual(r3.returncode, 0, (r3.stdout or "") + (r3.stderr or ""))

    def test_05_verify_refuses_without_job(self):
        """verify 是唯一能写 verified 的入口，不许在证据缺失时放行。"""
        r = _run("tools.wfctl", "verify", "--key", TEST_KEY,
                 "--job-id", "p1-19700101T0000-000000")
        out = (r.stdout or "") + (r.stderr or "")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("作业", out)


class TestWfctlGuideAndLint(unittest.TestCase):
    def test_guide_lists_seven_steps(self):
        r = _run("tools.wfctl", "guide")
        out = r.stdout or ""
        self.assertEqual(r.returncode, 0)
        for mark in ("①", "②", "③", "④", "⑤", "⑥", "⑦"):
            self.assertIn(mark, out, f"guide 缺第 {mark} 步")
        # 模板清单要在
        for t in ("t2i", "i2v", "v2v", "upscale"):
            self.assertIn(t, out)

    def test_lint_rules_all_have_reason(self):
        """每条规则必须写清「防的是哪次事故」，否则新人不理解就会绕过。"""
        from tools.p20 import wf_lint as L
        r = _run("tools.p20.wf_lint", "--rules")
        out = r.stdout or ""
        for rid, (level, title, why) in L.RULES.items():
            self.assertIn(rid, out)
            self.assertIn("为什么", out)
            self.assertGreater(len(why), 20, f"{rid} 的「为什么」太短")

    def test_lint_exit_code_reflects_result(self):
        """门禁的退出码要能直接进 CI。"""
        from tools.p20 import wf_lint as L
        rep = L.lint_all()
        r = _run("tools.p20.wf_lint")
        want = 0 if rep.ok() else 1
        self.assertEqual(r.returncode, want)


class TestTemplatesAreData(unittest.TestCase):
    """模板是数据不是代码 —— 加一类形态只加一个 Template。"""

    def test_no_template_needs_code_change(self):
        from tools.p20 import wf_templates as T
        for key, t in T.TEMPLATES.items():
            self.assertIsInstance(t.as_dict(), dict)
            json.dumps(t.as_dict(), ensure_ascii=False)   # 必须可序列化

    def test_review_helper_finds_all_markers(self):
        from tools.p20 import wf_templates as T
        obj = {"a": T.REVIEW_MODEL, "b": [T.REVIEW_SLOTS, {"c": T.REVIEW_PROMPT}]}
        found = T.review_placeholders(obj)
        self.assertEqual(sorted(found),
                         sorted([T.REVIEW_MODEL, T.REVIEW_SLOTS, T.REVIEW_PROMPT]))


if __name__ == "__main__":
    unittest.main()
