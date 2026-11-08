# -*- coding: utf-8 -*-
"""前端 JS 单测的执行入口：把 `web/tests/*.test.js` 纳入 Python 测试套件。

为什么放在这里：这个项目的主力测试是 Python（unittest discover），
如果前端测试只能手动 `node xxx.js` 跑，就等于没人跑。
本文件让 `python3 -m unittest discover -s tools/webui/tests` 顺带覆盖前端。

没有 node 时跳过，不把环境缺失当成失败。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

PROJ = Path(__file__).resolve().parents[3]
WEB = PROJ / "web"
TESTS = WEB / "tests"
NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "未安装 node，跳过前端单测")
class TestFrontendUnits(unittest.TestCase):
    def _run(self, script: Path):
        r = subprocess.run([NODE, str(script)], cwd=str(WEB),
                           capture_output=True, text=True, timeout=120)
        return r

    def test_wf_group_js(self):
        """工作流分组/排序：视频与图片分开，未开发排在后面且收进折叠区。

        判据必须是 compat.status，不能用名字里有没有「尚未开发」——
        真实数据里有 2 个 experimental 的名字没标注，靠名字判会误放进「可用」区。
        """
        script = TESTS / "wf_group.test.js"
        self.assertTrue(script.is_file(), f"缺测试脚本：{script}")
        r = self._run(script)
        self.assertEqual(r.returncode, 0,
                         f"前端单测失败：\n{r.stdout}\n{r.stderr}")
        self.assertIn("0 failed", r.stdout)

    def test_js_syntax_ok(self):
        """app.js 必须能通过 node --check（防止手改引入语法错误导致整页白屏）。"""
        r = subprocess.run([NODE, "--check", str(WEB / "dist" / "app.js")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_css_group_rules_present(self):
        """分组样式必须存在：wfgroup / wfold 是折叠区与分组标题的锚点。"""
        css = (WEB / "dist" / "app.css").read_text(encoding="utf-8")
        for sel in (".wfgroup{", ".wfgroup-h{", ".wfgroup-t{", ".wfold{",
                    ".mcard.undeveloped{"):
            with self.subTest(sel=sel):
                self.assertIn(sel, css, f"CSS 缺少 {sel}")

    def test_index_has_no_stale_models_class(self):
        """回归：`#workflows` 曾带 class="models"（网格容器），
        导致插入的两个 .wfgroup 被当成网格项并排挤成两列，卡片宽度腰斩。"""
        html = (WEB / "dist" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="workflows"', html)
        self.assertNotIn('class="models" id="workflows"', html)

    def test_fixture_matches_backend_shape(self):
        """快照必须还带 kind / compat.status 两个字段，否则分组逻辑无据可依。"""
        snap = TESTS / "fixtures" / "workflows.json"
        self.assertTrue(snap.is_file(), "缺 fixtures/workflows.json")
        data = json.loads(snap.read_text(encoding="utf-8"))
        items = data if isinstance(data, list) else (data.get("workflows") or [])
        self.assertGreater(len(items), 0)
        for w in items:
            self.assertIn("kind", w, f"{w.get('key')} 缺 kind")
            self.assertIn("compat", w, f"{w.get('key')} 缺 compat")


if __name__ == "__main__":
    unittest.main()


class TestHistoryDeleteUi(unittest.TestCase):
    """历史页删除能力：按钮必须存在、事件必须绑定、文案不能误导。

    回归背景：「清空」按钮原本只调 S.filter 重置筛选，一个字都不删，
    但文案让人以为是清空数据 —— 用户点了发现历史还在，报「无法清空」。
    现在拆成「删除选中」（真删，进 .trash）与「重置筛选」（只改筛选）。
    """

    def setUp(self):
        self.html = (WEB / "dist" / "index.html").read_text(encoding="utf-8")
        self.js = (WEB / "dist" / "app.js").read_text(encoding="utf-8")

    def test_delete_button_exists(self):
        self.assertIn('id="del"', self.html)
        self.assertIn("删除选中", self.html)

    def test_reset_label_is_not_misleading(self):
        """「清空」会让人以为清数据；必须改名成能表达「只重置筛选」的说法。"""
        self.assertNotIn('id="reset">清空<', self.html)
        self.assertIn("重置筛选", self.html)

    def test_delete_event_bound(self):
        self.assertIn("$('#del').addEventListener", self.js)
        self.assertIn("deleteJobs", self.js)

    def test_delete_uses_backend_endpoint(self):
        """必须真的调 /jobs/delete，不能只做前端过滤。"""
        self.assertIn("post('/jobs/delete'", self.js)

    def test_force_retry_path_exists(self):
        """被拒后要有 force 重试通道，否则 CREATED 僵尸永远是清的。"""
        self.assertIn("force: true", self.js)
        self.assertIn("REFUSED", self.js)

    def test_delete_button_disabled_without_selection(self):
        """没勾选时按钮必须禁用，避免误点全删。"""
        n = self.js.count("$('#del').disabled")
        self.assertGreaterEqual(n, 3, f"禁用状态控制点过少：{n}")


class TestToastNotCrossPage(unittest.TestCase):
    """提示必须在任何页面都能用。

    回归背景：deleteJobs() 曾写 $('#submitMsg')，而 submitMsg 只存在于创作台
    （view-create）。在历史页取到 null → TypeError → 后续 tick() 与 finally
    全被跳过，删除其实成功了，界面却毫无反应。用户报「点了清不掉」。
    """

    def setUp(self):
        self.html = (WEB / "dist" / "index.html").read_text(encoding="utf-8")
        self.js = (WEB / "dist" / "app.js").read_text(encoding="utf-8")
        self.css = (WEB / "dist" / "app.css").read_text(encoding="utf-8")

    def test_toast_node_is_global(self):
        """toast 节点必须挂在 body 末尾，不能在某个 view-* 区块里。"""
        import re
        i = self.html.find('id="toast"')
        self.assertGreater(i, 0, "缺少 #toast 节点")
        # 它必须出现在最后一个 </section> 之后
        last_section = self.html.rfind("</section>")
        self.assertGreater(i, last_section, "#toast 必须放在所有 section 之外")

    def test_toast_function_exists(self):
        self.assertIn("function toast(", self.js)

    def test_delete_does_not_touch_submit_msg(self):
        """删除逻辑不许再用 #submitMsg（跨页元素）。"""
        import re
        m = re.search(r"async function deleteJobs\(ids\)\s*\{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m, "找不到 deleteJobs 函数")
        body = m.group(1)
        self.assertNotIn("submitMsg", body, "deleteJobs 里不该再碰 #submitMsg")
        self.assertIn("toast(", body, "deleteJobs 应通过 toast() 反馈")

    def test_toast_css_present(self):
        self.assertIn(".toast{", self.css)
        self.assertIn(".toast.ok{", self.css)

    def test_delete_error_path_reports(self):
        """catch 分支必须有提示，不能静默吞掉。"""
        import re
        m = re.search(r"async function deleteJobs\(ids\)\s*\{(.*?)\n\}", self.js, re.S)
        body = m.group(1)
        self.assertIn("catch (e)", body)
        self.assertIn("'err'", body, "失败应走 toast(..., 'err')")
