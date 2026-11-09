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


class TestMediaFields(unittest.TestCase):
    """媒体字段（image / video）都要有上传器 + 预览。

    回归背景：动作迁移要同时传人物图（image）和驱动视频（image_2，type=video），
    但 renderForm 只写了 `if (spec.type === 'image')` 分支，
    type='video' 直接掉进最后的 <input type="text"> ——
    用户看到「只有上传图像的界面」，驱动视频只能手打文件名。
    后端其实早就支持视频上传（sniff_upload 认 mp4/mov/webm/avi）。
    """

    def setUp(self):
        self.js = (WEB / "dist" / "app.js").read_text(encoding="utf-8")
        self.css = (WEB / "dist" / "app.css").read_text(encoding="utf-8")

    def test_video_type_has_uploader(self):
        """type==='video' 必须走和 image 同一个上传器，不能掉进文本框。"""
        # mediaCtl 里按 spec.type 区分，两者统一入口
        self.assertIn("spec.type === 'image' || spec.type === 'video'", self.js)
        self.assertIn("function mediaCtl(", self.js)

    def test_video_accept_attribute(self):
        """视频输入框要收 mp4/mov/webm/m4v。"""
        self.assertIn("video/mp4", self.js)
        self.assertIn(".mov", self.js)
        self.assertIn(".webm", self.js)

    def test_preview_tag_follows_actual_file_type(self):
        """预览标签按**实际文件扩展名**决定，不是字段声明类型。

        用户可能给 image 字段传 mp4（后端按内容判型，不会拒），
        这时用 <img> 显示 mp4 又是黑块 —— 和列表页同一个坑。
        """
        self.assertIn("isVideoFile", self.js)
        self.assertIn("/\\.(mp4|mov|webm|m4v|avi)$/i", self.js)

    def test_upload_has_clear_button(self):
        """上传后要能移除，否则选错文件只能重开页面。"""
        self.assertIn("data-clear", self.js)
        self.assertIn("delete S.uploads[name]", self.js)

    def test_onchange_not_overwritten(self):
        """onchange 只能赋值一次。

        回归：原来连续两次 `box.onchange = ...`，第一份（普通字段回写）
        被第二份覆盖，导致文本/数字输入改了不进 S.form。
        """
        import re
        m = re.search(r"function renderForm\(params\)\s*\{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m)
        n = m.group(1).count("box.onchange =")
        self.assertEqual(n, 1, f"renderForm 里 box.onchange 被赋值 {n} 次")

    def test_video_preview_css(self):
        self.assertIn("video.upthumb", self.css)
        self.assertIn(".up[data-kind=video]", self.css)


class TestFileInputFieldName(unittest.TestCase):
    """文件框必须能被正确识别出是哪个字段。

    回归背景（用户实测）：上传了图和视频，提交却报
    「params.image 必填但未提供；params.image_2 必填但未提供」。

    根因：<input type="file"> 只有 id="f_<name>"，没有 name 属性；
    onchange 里写 e.target.name.replace(...) → e.target.name 是 undefined →
    TypeError → doUpload 从未执行 → S.uploads 一直是空的。
    界面看起来「上传成功了」（选了文件），实际一个字节都没发出去。

    修法：文件框加 data-field，onchange 从 dataset 取（并保留 id 兜底）。
    不要给它加 name 属性 —— 那会被 collect()/回写逻辑当成文本字段，
    把假路径写进 S.form。
    """

    def setUp(self):
        self.js = (WEB / "dist" / "app.js").read_text(encoding="utf-8")

    def test_file_input_has_data_field(self):
        import re
        m = re.search(r'<input type="file"[^>]*>', self.js)
        self.assertIsNotNone(m, "找不到文件输入框模板")
        self.assertIn("data-field=", m.group(0),
                      "文件框必须有 data-field，否则不知道是哪个参数")

    def test_file_input_has_no_name_attr(self):
        """有 name 会被当成文本字段回写 S.form。"""
        import re
        m = re.search(r'<input type="file"[^>]*>', self.js)
        self.assertNotIn('name="', m.group(0),
                         "文件框不该有 name 属性（会被当文本字段处理）")

    def test_onchange_uses_dataset_not_name(self):
        import re
        m = re.search(r"box\.onchange = e => \{(.*?)\n  \};", self.js, re.S)
        self.assertIsNotNone(m, "找不到 onchange 处理器")
        body = m.group(1)
        self.assertIn("dataset.field", body, "应从 data-field 取字段名")
        self.assertNotIn("e.target.name.replace", body,
                         "不能再对 e.target.name 调 replace（file 框没有 name）")

    def test_upload_result_goes_to_uploads_map(self):
        """上传结果必须写进 S.uploads（submitJob 从这里取文件名）。"""
        import re
        m = re.search(r"async function doUpload\(field, file\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("S.uploads[", body)
        self.assertIn("renderForm(", body, "上传后要重绘表单才能显示预览")

    def test_submit_reads_from_uploads(self):
        """submitJob 的媒体字段必须从 S.uploads 取，不能靠 S.form 里的巧合。"""
        import re
        m = re.search(r"async function submitJob\(\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("S.uploads[name]", body)
        self.assertIn("spec.type === 'image' || spec.type === 'video'", body)


class TestResultCostAndVideo(unittest.TestCase):
    """详情页必须显示本轮花费；多路视频不能同时预载。

    问题一：跑完一轮不知道花了多少。
      cost 在 GET /api/p2/jobs/<id> 的**顶层**（{hourly, minutes, amount}），
      不在 job 里（job.cost 是 None，早期读错过一次）。原详情页完全没有费用区。

    问题二：多个产物视频同页时平板播放卡死。
      浏览器/移动端硬件解码器同时解多路 h264 会争抢，
      表现就是「播一两秒卡住」。改为只让第一个 preload=metadata，其余 none。
    """

    def setUp(self):
        self.js = (WEB / "dist" / "app.js").read_text(encoding="utf-8")
        self.css = (WEB / "dist" / "app.css").read_text(encoding="utf-8")

    def test_cost_card_exists(self):
        self.assertIn("function costCard(", self.js)
        self.assertIn("costCard(S.detail.cost", self.js)

    def test_cost_read_from_top_level(self):
        """必须读 S.detail.cost（顶层），不能读 job.cost（那是 None）。

        注：只检查「有没有从这里读」，不检查字符串是否出现过——
        注释里解释「不在 job 里」会提到 job.cost，那是正常的。
        """
        self.assertIn("costCard(S.detail.cost, job)", self.js)
        # 不能有 actual 读取 job.cost 的表达式（赋值/传参）
        import re
        self.assertIsNone(re.search(r"\bjob\.cost\b(?!\s*是)", self.js),
                          "不应从 job.cost 取费用")

    def test_cost_shows_amount_and_minutes(self):
        import re
        m = re.search(r"function costCard\(cost, job\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("c.amount", body)
        self.assertIn("c.minutes", body)
        self.assertIn("c.hourly", body)

    def test_video_preload_only_first(self):
        """第一个视频 preload=metadata，其余 none。"""
        import re
        m = re.search(r"function artifactCard\(a, jobId, index = 0\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m, "artifactCard 应接收 index")
        body = m.group(1)
        self.assertIn("index === 0", body)
        self.assertIn("'metadata'", body)
        self.assertIn("'none'", body)

    def test_artifact_card_passed_index(self):
        self.assertIn("arts.map((a, i) => artifactCard(a, job.job_id, i))", self.js)

    def test_video_error_shows_hint(self):
        """播放失败要给可操作提示，不能留黑框。"""
        self.assertIn("MediaError", self.js)
        self.assertIn("addEventListener('error'", self.js)

    def test_cost_css_present(self):
        self.assertIn(".costcard", self.css)
        self.assertIn(".big-money", self.css)


class TestResultQualityCard(unittest.TestCase):
    """详情页要显示产物质量分（P5：把 tools/p2/quality.py 的分数接进 UI）。

    背景：质量分只落在 manifest.json 的 quality 字段里，UI 上看不到。用户跑完
    只能看到「文件成功」，看不到「这 87 分 A 级」或「这条 32 分 D 级、糊成一片」。

    判据分两层：
      · 正/负向行为由 `web/tests/quality_card.test.js` 真跑 qualityCard() 验（见下面
        的 test_quality_card_js），包括「没数据整张卡不渲染」这种负向断言；
      · 这里只钉住结构与样式锚点，以及那条最重要的约束 —— 质量分**不是门禁**。
    """

    def setUp(self):
        self.js = (WEB / "dist" / "app.js").read_text(encoding="utf-8")
        self.css = (WEB / "dist" / "app.css").read_text(encoding="utf-8")

    def test_quality_card_js(self):
        """跑 web/tests/quality_card.test.js（21 条，含负向断言）。"""
        script = TESTS / "quality_card.test.js"
        self.assertTrue(script.is_file(), f"缺测试脚本：{script}")
        r = subprocess.run([NODE, str(script)], cwd=str(WEB),
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, f"前端质量卡单测失败：\n{r.stdout}\n{r.stderr}")
        self.assertIn("0 failed", r.stdout)

    def test_quality_card_function_exists(self):
        self.assertIn("function qualityCard(", self.js)

    def test_quality_read_from_toplevel(self):
        """必须读 S.detail.quality（与 cost 并列的顶层字段），不能读 job.quality。"""
        self.assertIn("qualityCard(S.detail.quality)", self.js)
        import re
        self.assertIsNone(re.search(r"\bjob\.quality\b", self.js),
                          "quality 在顶层，不该从 job 里取")

    def test_quality_card_sits_next_to_cost_card(self):
        import re
        self.assertIsNotNone(
            re.search(r"costCard\(S\.detail\.cost, job\)\}\s*\$\{qualityCard\(", self.js),
            "质量卡应紧跟在成本卡之后渲染")

    def test_no_quality_no_card(self):
        """没有 quality 数据时返回空串（整张卡不渲染），不能吐空壳。"""
        import re
        m = re.search(r"function qualityCard\(q\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m, "找不到 qualityCard")
        body = m.group(1)
        self.assertIn("return ''", body, "无数据必须返回空串")
        self.assertIn("q.overall", body, "以 overall 是否存在作为判据")

    def test_grade_colors_defined(self):
        """A/B/C/D 四档颜色都要有，D 不能跟 A 同色。"""
        for sel in (".qbadge.a", ".qbadge.b", ".qbadge.c", ".qbadge.d",
                    ".qcard", ".qissues", ".qscore"):
            with self.subTest(sel=sel):
                self.assertIn(sel, self.css, f"CSS 缺少 {sel}")

    def test_report_mode_is_not_a_gate(self):
        """质量分只展示，不拦截：report 模式要在卡上明说不影响作业结论。

        这是本轮的硬约束 —— 现存 30 条 verified 都是按 verify.py 七项判的，
        把质量分设成默认门禁等于全部重判一次。UI 里不能出现"阻止/拦截"这类入口。
        """
        self.assertIn("仅记录", self.js)
        self.assertIn("不影响作业结论", self.js)
        for word in ("质量门禁未通过", "blockedByQuality", "qualityBlock"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.js)


class TestParamDescriptions(unittest.TestCase):
    """参数说明必须常驻显示，且每个参数都要有说明。

    问题：用户说「不知道这个工作流它是怎么样的，需要在填参数的位置加备注」。
    现状是 description 只塞进 textarea 的 placeholder —— 一填内容就看不见了，
    而「驱动视频（MP4/MOV/WebM）」这种信息恰恰在填的时候最需要。
    另外 328 个参数里只有 55 个（17%）有 description。
    """

    def setUp(self):
        self.js = (WEB / "dist" / "app.js").read_text(encoding="utf-8")
        self.css = (WEB / "dist" / "app.css").read_text(encoding="utf-8")

    def test_field_desc_function_exists(self):
        self.assertIn("function fieldDesc(", self.js)

    def test_desc_rendered_outside_placeholder(self):
        """说明要独立成 <p class="fdesc">，不能只靠 placeholder。"""
        self.assertIn('class="fdesc', self.js)
        m = __import__("re").search(
            r"function fieldDesc\(spec\) \{(.*?)\n\}", self.js, __import__("re").S)
        self.assertIsNotNone(m)
        self.assertIn("<p class=\"fdesc", m.group(1))

    def test_desc_survives_typing(self):
        """placeholder 不该承载完整说明（输入后消失）。"""
        m = __import__("re").search(r"ctl = `<textarea[^`]*`", self.js)
        self.assertIsNotNone(m)
        # textarea 的 placeholder 里不应再出现长说明的标志性措辞
        self.assertNotIn("写好细节", m.group(0))

    def test_long_desc_collapsible(self):
        """长说明（带算例的 trim_seconds）要能折叠，否则挤爆表单。"""
        import re
        m = re.search(r"function fieldDesc\(spec\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(m)
        body = m.group(1)
        # 模板里按长度加 long 类
        self.assertIn("long ?", body.replace("'", ""), "应按长度决定是否加 long 类")
        self.assertIn("d.length >", body)
        # CSS 三种态都要有
        for sel in (".fdesc.long{", ".fdesc.long::after", ".fdesc.long.open{"):
            with self.subTest(sel=sel):
                self.assertIn(sel, self.css)

    def test_missing_desc_is_marked(self):
        """没写说明的参数要明确标出，不能留白让人猜。"""
        self.assertIn("（无说明）", self.js)

    def test_media_field_desc_not_inside_flex_row(self):
        """媒体字段的说明不能被塞进 .up 这个 flex 行里（会被挤成一长条）。"""
        m = __import__("re").search(r"function mediaCtl\(name, spec\) \{(.*?)\n\}", self.js, __import__("re").S)
        self.assertIsNotNone(m)
        self.assertNotIn("fieldDesc(", m.group(1),
                         "mediaCtl 内不该插 fdesc —— 由 renderForm 统一插在 .upwrap 之后")
        self.assertIn("upwrap", m.group(1))

    def test_registry_all_params_have_desc(self):
        """registry 里 100% 的参数都要有 description。"""
        import json
        wfdir = WEB.parent / "registry" / "workflows"
        if not wfdir.is_dir():
            self.skipTest("registry 目录不在预期位置")
        total = has = 0
        missing = []
        for f in wfdir.glob("*.json"):
            d = json.loads(f.read_text(encoding="utf-8"))
            for n, v in (d.get("params") or {}).items():
                total += 1
                if (v or {}).get("description"):
                    has += 1
                else:
                    missing.append(f"{d.get('key')}.{n}")
        self.assertGreater(total, 0)
        self.assertEqual(has, total,
                         f"{len(missing)} 个参数缺说明，例如：{missing[:6]}")


class TestOpenApiContract(unittest.TestCase):
    """契约（docs/openapi.json）必须与实现一致。

    回归背景：2026-11-08 检查发现 5 个端点照契约写必然 404：
      /api/p2/status        实现只有 /api/status（少 p2 段）→ 已补
      /api/p2/presets       GET 补空集；POST 标为计划中
      /api/p2/share/{id}    P2.2 计划 → 标注 x-implemented: false
      /api/p2/queue/jobs/{id}  旧契约残留，与 jobs/{id} 重复 → 删除
      /api/p2/jobs/{id}/artifacts/{name}  P2.2 计划 → 标注
      /api/p2/jobs/{job_id}/rerun  实现是批量 /jobs/rerun → 契约对齐
    """

    @classmethod
    def setUpClass(cls):
        import json
        cls.spec = json.loads((PROJ / "docs" / "openapi.json").read_text(encoding="utf-8"))
        cls.paths = cls.spec["paths"]

    def test_no_stale_queue_jobs_detail(self):
        """queue/jobs/{id} 旧契约残留必须已删除（它和 jobs/{id} 完全重复）。"""
        self.assertNotIn("/api/p2/queue/jobs/{job_id}", self.paths)

    def test_planned_endpoints_are_marked(self):
        """未实现的端点必须显式标 x-status: planned（仓库原有口径）。"""
        planned = []
        for path, ops in self.paths.items():
            for m, op in ops.items():
                if m in ("get", "post", "put", "delete") and op.get("x-status") == "planned":
                    planned.append(path)
        # 至少这三处已知未实现要被标出来
        for p in ("/api/p2/share/{job_id}",
                  "/api/p2/jobs/{job_id}/artifacts/{name}",
                  "/api/p2/presets"):
            self.assertIn(p, planned, f"{p} 应标为未实现")

    def test_rerun_path_matches_implementation(self):
        """rerun 的路径必须与 fileserver 实际提供的一致（批量）。"""
        self.assertIn("/api/p2/jobs/rerun", self.paths)
        self.assertNotIn("/api/p2/jobs/{job_id}/rerun", self.paths)
        fs = (PROJ / "fileserver.py").read_text(encoding="utf-8")
        self.assertIn('parts[4] == "rerun"', fs, "实现里应有批量 rerun")

    def test_status_and_presets_exist_in_impl(self):
        """status 与 presets 在实现里必须有分支。"""
        fs = (PROJ / "fileserver.py").read_text(encoding="utf-8")
        self.assertIn('sub == ["status"]', fs)
        self.assertIn('sub == ["presets"]', fs)

    def test_status_flag_present_on_all(self):
        """每个操作都要有 x-status（全库统一口径，便于脚本比对与 mock 生成）。"""
        for path, ops in self.paths.items():
            for m, op in ops.items():
                if m in ("get", "post", "put", "delete"):
                    with self.subTest(path=path, method=m):
                        self.assertIn("x-status", op,
                                      f"{m.upper()} {path} 缺 x-status")
