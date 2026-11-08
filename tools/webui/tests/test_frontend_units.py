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


class TestGitignoreDoesNotSwallowSource(unittest.TestCase):
    """`web/dist` 是手写源码，不是构建产物，绝不能被 gitignore 掉。

    回归背景：最初按「构建产物不入库」的惯例写了 `web/dist/`，
    但本项目没有构建链，那三个文件就是源码本体，忽略了前端就永久丢失。
    更隐蔽的是根规则 `dist/`（不带斜杠）会匹配任意层级的 dist 目录，
    即使写了 `!web/dist/` 也救不回来 —— 必须把根规则锚定成 `/dist/`。
    """

    @classmethod
    def setUpClass(cls):
        cls.gi = (PROJ / ".gitignore").read_text(encoding="utf-8").splitlines()

    def test_root_dist_is_anchored(self):
        """根 dist/ 必须锚定，否则会连带忽略 web/dist。"""
        has_anchored = any(l.strip() == "/dist/" for l in self.gi)
        has_bare = any(l.strip() == "dist/" for l in self.gi
                       if not l.strip().startswith("#"))
        self.assertTrue(has_anchored, "缺少锚定的 /dist/")
        self.assertFalse(has_bare, "存在不带斜杠的 dist/，会吃掉 web/dist")

    def test_web_dist_not_ignored(self):
        """web/dist 下的源码文件不能被任何规则忽略。"""
        for f in ("web/dist/index.html", "web/dist/app.js", "web/dist/app.css"):
            with self.subTest(f=f):
                p = PROJ / f
                self.assertTrue(p.is_file(), f"{f} 不存在")

    def test_node_modules_ignored(self):
        joined = "\n".join(self.gi)
        self.assertIn("web/node_modules", joined)


class TestReadmeHasWorkflow(unittest.TestCase):
    """开发流程必须写在 README 里，否则下次又要重新纠结「分支还是主线」。"""

    @classmethod
    def setUpClass(cls):
        cls.md = (WEB / "README.md").read_text(encoding="utf-8")

    def test_documents_both_routes(self):
        self.assertIn("小改", self.md, "缺少「小改走 main」的说明")
        self.assertIn("大改", self.md, "缺少「大改开分支」的说明")

    def test_documents_worktree(self):
        self.assertIn("git worktree add", self.md)

    def test_documents_repo_guard(self):
        self.assertIn("repo-guard", self.md, "必须说明部署守卫的三道检查")

    def test_documents_test_baseline(self):
        """基线数会随时间增长，不该硬编码具体数字——校验格式即可。"""
        import re
        m = re.search(r"当前基线：\*\*(\d+) 条\*\*（([^）]+)）", self.md)
        self.assertIsNotNone(m, "文档里应有「当前基线：**N 条**（各套件分布）」")
        total = int(m.group(1))
        parts = dict(p.split() for p in m.group(2).split(" / "))
        self.assertEqual(sum(int(v) for v in parts.values()), total,
                         f"各套件之和 {sum(int(v) for v in parts.values())} != 总数 {total}")
        # 四套件都要列出
        for k in ("p20", "p1", "webui", "p2"):
            self.assertIn(k, parts, f"基线里缺 {k} 套件")

    def test_documents_pitfalls(self):
        """几个踩过的坑要留在文档里，避免重复犯。"""
        for kw in ("/pub/", "compat.status", "toast"):
            with self.subTest(kw=kw):
                self.assertIn(kw, self.md)
