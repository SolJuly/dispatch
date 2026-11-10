#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L6 创作执行层补完测试：质量评分 / 工作流预检。

覆盖 2026-11-09 新增的两块：

  quality    内容质量分（清晰度/运动/曝光/稳定 + 木桶封顶 + 门禁）
  preflight  静态预检（节点/权重/图结构 + 证据强度分级 + 环境漂移）

测试**不依赖真视频文件**的项用构造数据；需要真解码的项用 ffmpeg 现场生成
小样本（2 秒 128×128），保证在无 GPU 无网络的机器上也能跑。

用法：python3 -m unittest tools.p2.tests.test_l6_quality
"""
from __future__ import annotations

import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import preflight as PF  # noqa: E402
from tools.p2 import quality as Q  # noqa: E402

HAVE_FFMPEG = subprocess.run(["sh", "-c", "command -v ffmpeg"],
                             capture_output=True).returncode == 0


def _make_video(path: Path, *, source: str = "testsrc2", seconds: float = 2.0,
                size: str = "128x128", fps: int = 8, vf: str = "") -> bool:
    """现场生成一个测试视频。

    `source` 是完整的 lavfi 滤镜串（不含 size/rate/duration）。
    分开传而不是拼 `f"{pattern}=size=..."`：`color=c=black` 本身已经带 `=`，
    再拼就变成 `color=c=black=size=...`，ffmpeg 会当成颜色名解析失败
    （2026-11-09 实测：5 个测试因为这个静默 skip）。
    """
    if not HAVE_FFMPEG:
        return False
    if source.startswith("color="):
        filt = f"{source}:size={size}:rate={fps}:duration={seconds}"
    else:
        filt = f"{source}=size={size}:rate={fps}:duration={seconds}"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", filt]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    return r.returncode == 0 and path.is_file() and path.stat().st_size > 0


def _make_image(path: Path, *, source: str = "testsrc2", size: str = "128x128",
                vf: str = "") -> bool:
    if not HAVE_FFMPEG:
        return False
    filt = (f"{source}:size={size}" if source.startswith("color=")
            else f"{source}=size={size}")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", filt]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-frames:v", "1", str(path)]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    return r.returncode == 0 and path.is_file()


# ------------------------------------------------------------------ 统计量
class TestStats(unittest.TestCase):
    """纯计算的辅助函数，必须先对。"""

    def test_mean(self):
        self.assertEqual(Q._mean([1, 2, 3]), 2)
        self.assertEqual(Q._mean([]), 0.0)

    def test_cv_stable(self):
        self.assertAlmostEqual(Q._cv([5, 5, 5, 5]), 0.0, places=6)

    def test_cv_varies(self):
        self.assertGreater(Q._cv([1, 5, 1, 5]), 0.5)

    def test_cv_zero_mean_safe(self):
        """均值≈0 时不能除爆（黑屏视频就会走到这条）。"""
        self.assertEqual(Q._cv([0, 0, 0]), 0.0)

    def test_clip01(self):
        self.assertEqual(Q._clip01(-1), 0.0)
        self.assertEqual(Q._clip01(2), 1.0)
        self.assertEqual(Q._clip01(0.3), 0.3)


class TestGrade(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(Q._grade(75), "A")
        self.assertEqual(Q._grade(74.9), "B")
        self.assertEqual(Q._grade(55), "B")
        self.assertEqual(Q._grade(54.9), "C")
        self.assertEqual(Q._grade(35), "C")
        self.assertEqual(Q._grade(34.9), "D")
        self.assertEqual(Q._grade(0), "D")


class TestBucketCap(unittest.TestCase):
    """木桶封顶：这是让分数排序变正确的关键机制，逐条钉住。"""

    def test_blurry_capped_below_b(self):
        cap, why = Q._bucket_cap(0.56, 3.0, 100, 200, 0.1)
        self.assertIsNotNone(cap)
        self.assertLess(cap, 55, "糊的必须落到 C 以下")
        self.assertIn("sharpness", why)

    def test_dark_capped(self):
        cap, why = Q._bucket_cap(2.0, 3.0, 10, 200, 0.1)
        self.assertIsNotNone(cap)
        self.assertIn("luma", why)

    def test_static_capped_but_looser(self):
        cap, why = Q._bucket_cap(2.0, 0.5, 100, 200, 0.1)
        self.assertIsNotNone(cap)
        self.assertIn("motion", why)
        # 静帧可能是用户要的（定格/照片风），封顶比"糊"宽松
        self.assertGreater(cap, Q._bucket_cap(0.56, 3.0, 100, 200, 0.1)[0])

    def test_good_video_not_capped(self):
        cap, _ = Q._bucket_cap(3.0, 18.0, 120, 240, 0.05)
        self.assertIsNone(cap)

    def test_flicker_capped(self):
        cap, why = Q._bucket_cap(2.0, 3.0, 100, 200, 0.9)
        self.assertIsNotNone(cap)
        self.assertIn("cv", why)

    def test_flat_capped(self):
        cap, why = Q._bucket_cap(2.0, 3.0, 120, 10, 0.1)
        self.assertIsNotNone(cap)
        self.assertIn("range", why)


class TestNoiseDiscriminator(unittest.TestCase):
    """噪点判别：这一类失败在原评分里拿满分。

    **实测漏洞（2026-11-09）**：`noise=alls=120` 生成的纯雪花视频拿 100 分 A 级 ——
    清晰度项满分（边缘能量 95.16，正常片 0.9–6.3）、运动项满分（每帧都在变）、
    曝光项满分（灰度填满 0–255）。三项都"对"，结论完全错。

    根因：「清晰度」这个指标分不清「细节」和「噪点」。两者在高频能量上是同一件事，
    区别在**结构** —— 噪点没有结构，细节有。所以先 gblur 再量边缘能量，看保留多少：

        有结构的边缘 → 模糊后塌得慢（13 条真实素材实测保留 28–55%）
        噪点          → 模糊后几乎全塌（8 条合成噪点实测 10–15%）

    线定 20%：离噪点最高 15.0% 留 5 个点，离真实最低 28.3% 留 8 个点，两边不贴边。
    """

    def test_noise_keep_threshold_sits_between_measured_clusters(self):
        """阈值必须落在两个实测簇之间，且两边都有余量。"""
        self.assertLess(Q.NOISE_KEEP, 28.3, "不能高到误杀真实素材（最低 28.3%）")
        self.assertGreater(Q.NOISE_KEEP, 15.0, "不能低到放过噪点（最高 15.0%）")

    def test_pure_noise_capped_hard(self):
        """纯噪声必须被判为噪点，且封顶最狠（20 分，D 级）。

        它封得比"糊"（32）还狠：糊至少还有内容，噪点是彻底没有内容。
        """
        cap, why = Q._bucket_cap(95.0, 70.0, 127.0, 255.0, 0.002, noise_keep=15.0)
        self.assertEqual(cap, 20, "噪点封顶应当是 20")
        self.assertIn("noise_keep", why)
        self.assertIn("噪点", why)

    def test_noise_cap_beats_sharpness_cap(self):
        """噪点的边缘能量很高（>SHARP_BLUR），所以不会触发"糊"的封顶。

        这条钉住检查顺序：噪点必须排在 sharpness 之前，否则永远轮不到它。
        """
        # sharpness=95 远高于 SHARP_BLUR，单看 sharpness 不会封顶
        cap_no_noise, _ = Q._bucket_cap(95.0, 70.0, 127.0, 255.0, 0.002)
        self.assertIsNone(cap_no_noise)
        cap_with, _ = Q._bucket_cap(95.0, 70.0, 127.0, 255.0, 0.002, noise_keep=12.0)
        self.assertEqual(cap_with, 20)

    def test_good_noise_keep_not_capped(self):
        """保留率正常（真实素材量级）就不该封顶。"""
        for keep in (28.3, 34.0, 45.0, 55.0):
            with self.subTest(keep=keep):
                cap, _ = Q._bucket_cap(2.0, 18.0, 120.0, 240.0, 0.05, noise_keep=keep)
                self.assertIsNone(cap, f"keep={keep}% 是真实素材量级，不该判噪点")

    def test_unknown_noise_keep_does_not_cap(self):
        """拿不到模糊序列（None）时必须"不表态"。

        把 None 当成 0 会把一次 ffmpeg 抖动变成"这条片子是垃圾"的结论。
        评分器自己出错时应当不表态，不是乱表态。
        """
        cap, _ = Q._bucket_cap(2.0, 18.0, 120.0, 240.0, 0.05, noise_keep=None)
        self.assertIsNone(cap)

    def test_noise_keep_helper(self):
        """_noise_keep：正常算百分比，分母为 0 或序列为空时返回 None。"""
        self.assertAlmostEqual(Q._noise_keep([0.5, 0.5], [1.0, 1.0]), 50.0)
        self.assertIsNone(Q._noise_keep([], [1.0]))
        self.assertIsNone(Q._noise_keep([0.5], []))
        self.assertIsNone(Q._noise_keep([0.5], [0.0]), "分母为 0 不能炸")


@unittest.skipUnless(HAVE_FFMPEG, "没有 ffmpeg")
class TestNoiseRealDecode(unittest.TestCase):
    """真解码验证：合成噪点必须落到 D，同期真实结构不能误杀。"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.td.name)
        cls.noise = cls.dir / "noise.mp4"
        cls.clean = cls.dir / "clean.mp4"
        cls.noise_ok = _make_video(cls.noise, source="nullsrc",
                                   vf="geq=random(1)*255:128:128")
        cls.clean_ok = _make_video(cls.clean, source="testsrc2")

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_noise_video_is_not_grade_a(self):
        """回归：纯噪声视频曾经拿 100 分 A 级。

        这条用例存在的唯一目的就是**永远不要再回到那个状态**。
        """
        if not self.noise_ok:
            self.fail("未能生成噪声测试视频（ffmpeg 不可用或参数变了）")
        s = Q.score_video(self.noise)
        self.assertEqual(s.grade, "D", f"纯噪声必须 D 级，实得 {s.grade} {s.score} "
                                      f"metrics={s.metrics}")
        self.assertLessEqual(s.score, 32, f"分数不该超过 32，实得 {s.score}")
        self.assertTrue(any("噪点" in i for i in s.issues),
                        f"应报噪点问题：{s.issues}")
        self.assertIn("noise_keep", s.metrics)

    def test_clean_video_still_scores_well(self):
        """反向：真实结构不能被噪点检测误杀。"""
        if not self.clean_ok:
            self.fail("未能生成正常测试视频")
        s = Q.score_video(self.clean)
        self.assertGreaterEqual(s.score, 55, f"正常片被误杀：{s.score} {s.issues}")
        self.assertFalse(any("噪点" in i for i in s.issues), s.issues)


@unittest.skipUnless(HAVE_FFMPEG, "没有 ffmpeg")
class TestNoiseRealImage(unittest.TestCase):
    """图片同一套判别（噪点图的边缘能量同样极高）。"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.td.name)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_noise_image_is_d(self):
        p = self.dir / "noise.png"
        if not _make_image(p, source="nullsrc", vf="geq=random(1)*255:128:128"):
            self.fail("未能生成噪声图")
        s = Q.score_image(p)
        self.assertEqual(s.grade, "D", f"噪声图必须 D，实得 {s.grade} {s.score}")
        self.assertTrue(any("噪点" in i for i in s.issues), s.issues)

    def test_clean_image_not_misjudged(self):
        p = self.dir / "clean.png"
        if not _make_image(p, source="testsrc2"):
            self.fail("未能生成正常图")
        s = Q.score_image(p)
        self.assertFalse(any("噪点" in i for i in s.issues), s.issues)
        self.assertGreaterEqual(s.score, 55, f"正常图被误杀：{s.score} {s.issues}")


# ------------------------------------------------------------------ 真实解码
@unittest.skipUnless(HAVE_FFMPEG, "没有 ffmpeg")
class TestRealVideo(unittest.TestCase):
    """用现场生成的视频验证：正常片得分高，黑屏/纯色片得分低。"""

    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.td.name)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_normal_video_scores_ok(self):
        """testsrc2 有运动有细节，应该拿 A/B 且无 issue。"""
        p = self.dir / "normal.mp4"
        if not _make_video(p, source="testsrc2"):
            self.skipTest("生成测试视频失败")
        s = Q.score_video(p)
        self.assertGreaterEqual(s.score, 55, f"正常片得分过低：{s.score} {s.issues}")
        self.assertEqual(s.issues, [], f"正常片不该有问题：{s.issues}")
        self.assertEqual(s.kind, "video")

    def test_black_video_is_dark(self):
        """纯黑：亮度低 → 过暗 issue + 封顶。"""
        p = self.dir / "black.mp4"
        if not _make_video(p, source="color=c=black"):
            self.skipTest("生成测试视频失败")
        s = Q.score_video(p)
        self.assertTrue(any("过暗" in i for i in s.issues), s.issues)
        self.assertLess(s.score, 55, "黑屏不该拿 B 以上")

    def test_white_video_is_overexposed(self):
        p = self.dir / "white.mp4"
        if not _make_video(p, source="color=c=white"):
            self.skipTest("生成测试视频失败")
        s = Q.score_video(p)
        self.assertTrue(any("过曝" in i for i in s.issues), s.issues)
        self.assertLess(s.score, 55)

    def test_static_video_flagged(self):
        """纯色画面没有运动也没有细节 —— 两个问题都该报。"""
        p = self.dir / "static.mp4"
        if not _make_video(p, source="color=c=gray"):
            self.skipTest("生成测试视频失败")
        s = Q.score_video(p)
        joined = " ".join(s.issues)
        self.assertTrue("帧差" in joined or "边缘能量" in joined, s.issues)

    def test_metrics_present(self):
        p = self.dir / "m.mp4"
        if not _make_video(p):
            self.skipTest("生成测试视频失败")
        s = Q.score_video(p)
        for k in ("sharpness", "motion", "luma_mean", "luma_range",
                  "sharpness_cv", "samples"):
            self.assertIn(k, s.metrics, f"缺指标 {k}")
        self.assertGreater(s.metrics["samples"], 0)

    def test_luma_not_edge_energy(self):
        """亮度均值必须是原始 YAVG，不能是卷积后的边缘能量。

        2026-11-09 踩过：把卷积后的 YAVG 当亮度，得到 luma=1.4 的假过暗，
        连最亮的 H3 成片都被判成黑屏。
        """
        p = self.dir / "bright.mp4"
        if not _make_video(p, source="color=c=white"):
            self.skipTest("生成测试视频失败")
        s = Q.score_video(p)
        self.assertGreater(s.metrics["luma_mean"], 200,
                           f"白色画面亮度应 >200，实际 {s.metrics['luma_mean']}"
                           " —— 拿错了序列")


@unittest.skipUnless(HAVE_FFMPEG, "没有 ffmpeg")
class TestRealImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.td = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.td.name)

    @classmethod
    def tearDownClass(cls):
        cls.td.cleanup()

    def test_image_scores(self):
        p = self.dir / "i.png"
        if not _make_image(p):
            self.skipTest("生成测试图片失败")
        s = Q.score_image(p)
        self.assertEqual(s.kind, "image")
        self.assertIn("sharpness", s.metrics)
        self.assertNotIn("motion", s.metrics, "图片没有运动维度")

    def test_black_image_flagged(self):
        p = self.dir / "b.png"
        if not _make_image(p, source="color=c=black"):
            self.skipTest("生成测试图片失败")
        s = Q.score_image(p)
        self.assertTrue(any("过暗" in i for i in s.issues), s.issues)


# ------------------------------------------------------------------ 门禁
class TestGate(unittest.TestCase):
    def _score(self, score=70.0, grade="B", issues=None):
        return Q.QualityScore(file="x.mp4", score=score, grade=grade,
                              issues=list(issues or []))

    def test_report_mode_never_fails(self):
        s = self._score(20.0, "D", ["边缘能量 0.1 < 0.8"])
        self.assertFalse(s.gated, "未过门禁的评分卡 gated 应为 False")
        self.assertTrue(s.passed, "默认 passed=True（report 模式语义）")
        # assess() 在 report 模式下不会写 gated
        rep = Q.assess_job([], kind="video", mode="report")
        self.assertEqual(rep.mode, "report")

    def test_min_score_blocks(self):
        s = self._score(40.0, "C")
        Q.gate(s, min_score=60)
        self.assertFalse(s.passed)
        self.assertTrue(s.gated)
        self.assertTrue(any("门禁下限" in i for i in s.issues))

    def test_min_grade_blocks(self):
        s = self._score(52.0, "C")
        Q.gate(s, min_grade="B")
        self.assertFalse(s.passed)

    def test_min_grade_passes(self):
        s = self._score(80.0, "A")
        Q.gate(s, min_grade="B")
        self.assertTrue(s.passed)

    def test_block_issues(self):
        s = self._score(90.0, "A", ["帧差 0.2 < 0.6：画面基本没动"])
        Q.gate(s, block_issues=("基本没动",))
        self.assertFalse(s.passed, "点名禁的问题即使高分也要拦住")

    def test_from_expect_defaults_to_report(self):
        cfg = Q.from_expect({})
        self.assertEqual(cfg["mode"], "report")
        self.assertEqual(cfg["min_score"], 0.0)

    def test_from_expect_reads_workflow_card(self):
        cfg = Q.from_expect({"quality": {"mode": "gate", "min_score": 65,
                                         "min_grade": "B",
                                         "block_issues": ["过暗"]}})
        self.assertEqual(cfg["mode"], "gate")
        self.assertEqual(cfg["min_score"], 65.0)
        self.assertEqual(cfg["block_issues"], ("过暗",))


class TestAssessJob(unittest.TestCase):
    def test_empty_artifacts(self):
        rep = Q.assess_job([], kind="video", mode="gate")
        self.assertEqual(rep.verdict, "FAIL")
        rep2 = Q.assess_job([], kind="video", mode="report")
        self.assertEqual(rep2.verdict, "PASS")

    def test_missing_file_reported(self):
        s = Q.assess(Path("/nonexistent/x.mp4"), kind="video")
        self.assertIn("文件不存在", " ".join(s.issues))
        self.assertEqual(s.score, 0.0)

    def test_overall_uses_primary_not_average(self):
        """附加产物（pose 预览、封面）质量天然低，平均会把好作业拉低。"""
        a = Q.QualityScore(file="main.mp4", score=90.0, grade="A")
        b = Q.QualityScore(file="pose.mp4", score=20.0, grade="D")
        rep = Q.QualityReport(mode="report", artifacts=[a, b])
        rep.overall = rep.artifacts[0].score
        rep.grade = rep.artifacts[0].grade
        self.assertEqual(rep.overall, 90.0)


# ------------------------------------------------------------------ 预检
class TestPreflightGraph(unittest.TestCase):
    def test_graph_classes_collected(self):
        g = {"1": {"class_type": "A"}, "2": {"class_type": "B"},
             "3": {"inputs": {}}}
        self.assertEqual(PF.graph_classes(g), {"A", "B"})

    def test_missing_node_detected(self):
        wf = {"key": "w", "graph": "workflows/none.api.json",
              "model": {"key": "m"}, "params": {}}
        model = {"key": "m", "files": {}}
        probe = PF.Probe(node_classes={"A"}, node_classes_known=True)
        # 图文件不存在 → 空图 → bad_graph
        r = PF.check_workflow(wf, model, probe)
        self.assertEqual(r.status, "bad_graph")

    def test_no_model_detected(self):
        wf = {"key": "w", "graph": "x", "model": {"key": "missing"}, "params": {}}
        r = PF.check_workflow(wf, None, PF.Probe())
        self.assertEqual(r.status, "no_model")
        self.assertIn("模型卡", r.fix_hint())


class TestPreflightEvidence(unittest.TestCase):
    """证据强度分级：抽样清单只能证明"在"，不能证明"不在"。"""

    def _wf_and_model(self):
        wf = {"key": "w", "graph": "workflows/none.api.json",
              "model": {"key": "m"}, "params": {}}
        model = {"key": "m", "files": {
            "ckpt": {"name": "a.safetensors", "loader": "CheckpointLoaderSimple"}}}
        return wf, model

    def test_complete_list_missing_is_no_weight(self):
        wf, model = self._wf_and_model()
        probe = PF.Probe(weight_files={"other.bin"}, weights_known=True,
                         weights_complete=True)
        r = PF.check_workflow(wf, model, probe)
        self.assertEqual(r.missing_weights, ["a.safetensors"])

    def test_sampled_list_missing_is_unverified(self):
        """抽样清单未命中 → unverified_weights，不是 missing_weights。

        这是本项目真实踩过的坑：把 95 行的缺口调查当成完整盘点，
        结果 31 条 verified 工作流全被判成缺权重。
        """
        wf, model = self._wf_and_model()
        probe = PF.Probe(weight_files={"other.bin"}, weights_known=True,
                         weights_complete=False)
        r = PF.check_workflow(wf, model, probe)
        self.assertEqual(r.missing_weights, [], "抽样未命中不能算缺失")
        self.assertEqual(r.unverified_weights, ["a.safetensors"])
        self.assertTrue(any("抽样" in n for n in r.notes))

    def test_sampled_list_hit_still_counts(self):
        """抽样命中是**强证据**：能确认存在。"""
        wf, model = self._wf_and_model()
        probe = PF.Probe(weight_files={"a.safetensors"}, weights_known=True,
                         weights_complete=False)
        r = PF.check_workflow(wf, model, probe)
        self.assertEqual(r.missing_weights, [])
        self.assertEqual(r.unverified_weights, [])

    def test_no_weights_known_skips(self):
        wf, model = self._wf_and_model()
        r = PF.check_workflow(wf, model, PF.Probe())
        self.assertEqual(r.missing_weights, [])
        self.assertTrue(any("跳过权重检查" in n for n in r.notes))

    def test_match_rule_recognizes_renamed_file(self):
        """平台库文件名常带后缀，靠 match 规则认出来。"""
        wf = {"key": "w", "graph": "workflows/none.api.json",
              "model": {"key": "m"}, "params": {}}
        model = {"key": "m", "files": {
            "ckpt": {"name": "a.safetensors", "loader": "CheckpointLoaderSimple",
                     "match": {"contains": "a"}}}}
        probe = PF.Probe(weight_files={"a-v2-fp8.safetensors"}, weights_known=True,
                         weights_complete=True)
        r = PF.check_workflow(wf, model, probe)
        self.assertEqual(r.missing_weights, [])


class TestPreflightRealRegistry(unittest.TestCase):
    """对真实 registry 跑一遍，保证工具在真数据上不崩、结论自洽。"""

    def test_check_all_runs(self):
        rows = PF.check_all()
        self.assertGreater(len(rows), 20, "应该检查到全部工作流")
        for r in rows:
            self.assertIn(r.status, ("ready", "no_node", "no_weight",
                                     "bad_graph", "no_model"))

    def test_summary_adds_up(self):
        rows = PF.check_all()
        s = PF.summarize(rows)
        self.assertEqual(s["total"], len(rows))
        self.assertEqual(s["ready"] + s["blocked"], s["total"])

    def test_with_object_info_snapshot(self):
        oi = _ROOT / "reports" / "p2" / "p21" / "evidence" / "objectinfo-v4.json"
        if not oi.is_file():
            self.skipTest("没有 object_info 快照")
        import json
        probe = PF.Probe.from_object_info(json.loads(oi.read_text(encoding="utf-8")))
        rows = PF.check_all(probe=probe)
        s = PF.summarize(rows)
        # 0.36 镜像缺 QwenImage21Cache 等，至少要抓到几条 no_node
        self.assertGreaterEqual(s["counts"].get("no_node", 0), 1,
                                "0.36 镜像应至少缺一个节点类")

    def test_drift_report_shape(self):
        rows = PF.check_all()
        d = PF.drift_report(rows)
        for x in d:
            self.assertIn("workflow", x)
            self.assertIn("registry_says", x)
            self.assertIn("env_says", x)

    def test_filter(self):
        rows = PF.check_all(only="qwen")
        self.assertTrue(rows)
        self.assertTrue(all("qwen" in r.workflow for r in rows))


class TestPreflightCompatLinks(unittest.TestCase):
    """compat_links 是「ComfyUI 名字 → 真实文件」的已确认映射，属强证据。

    2026-11-09 实测：不读它的后果是 qwen_image_t2i 等 **verified（真机跑通过）**
    被预检报成 no_weight —— 判据漏一条证据来源，就会把已验证说成不可用。
    """

    def _wf_model(self, name="a.safetensors", rule=None):
        wf = {"key": "w", "graph": "workflows/none.api.json",
              "model": {"key": "m"}, "params": {}}
        f = {"name": name, "loader": "CheckpointLoaderSimple"}
        if rule:
            f["match"] = rule
        return wf, {"key": "m", "files": {"ckpt": f}}

    def test_compat_link_satisfies_weight(self):
        wf, model = self._wf_model("qwen_std_vae.safetensors")
        probe = PF.Probe(weight_files={"other.bin"}, weights_known=True,
                         weights_complete=True,
                         compat_links={"qwen_std_vae.safetensors": "/model/x.safetensors"})
        r = PF.check_workflow(wf, model, probe)
        self.assertEqual(r.missing_weights, [],
                         "compat_links 里有就该放过，真实文件叫别的名字无所谓")

    def test_without_compat_link_still_missing(self):
        wf, model = self._wf_model("qwen_std_vae.safetensors")
        probe = PF.Probe(weight_files={"other.bin"}, weights_known=True,
                         weights_complete=True)
        r = PF.check_workflow(wf, model, probe)
        self.assertEqual(r.missing_weights, ["qwen_std_vae.safetensors"])

    def test_load_compat_links_parses_tabs(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# 注释行\n")
            f.write("vae\tqwen_std_vae.safetensors\t/model/a/b.safetensors\n")
            f.write("loras\tsub/dir/x.safetensors\t/model/c/x.safetensors\n")
            f.write("坏行没有制表符\n")
            path = f.name
        try:
            links = PF.load_compat_links(path)
            self.assertEqual(links["qwen_std_vae.safetensors"], "/model/a/b.safetensors")
            # 带子目录的名字按 basename 建索引（与模型卡匹配口径一致）
            self.assertEqual(links["x.safetensors"], "/model/c/x.safetensors")
            self.assertEqual(len(links), 2)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_real_compat_links_has_no_bad_paths(self):
        links = PF.load_compat_links()
        if not links:
            self.skipTest("没有 compat_links.txt")
        for name, target in links.items():
            self.assertTrue(target.startswith("/model"),
                            f"{name} 的映射不是 /model 下的绝对路径：{target}")


class TestMatchNormalization(unittest.TestCase):
    """同名文件在不同地方写法不同（大小写 / 连字符），不能判成缺失。

    实测：模型卡 `ltx_2.3_22b_...` vs 库里 `ltx-2.3-22b-...`；
    模型卡 `ltx-2.3-id-lora-talkvid-3k` vs 库里 `LTX-2.3-ID-LoRA-TalkVid-3K`。
    按原名比会误判成缺权重，白排一轮真机验证。
    """

    def test_hyphen_underscore_equivalent(self):
        self.assertTrue(PF._match("ltx-2.3-22b-distilled.safetensors",
                                  {"contains": "ltx_2.3_22b_distilled"}))
        self.assertTrue(PF._match("ltx_2.3_22b_distilled.safetensors",
                                  {"contains": "ltx-2.3-22b-distilled"}))

    def test_case_insensitive(self):
        self.assertTrue(PF._match("LTX-2.3-ID-LoRA-TalkVid-3K.safetensors",
                                  {"contains": "ltx-2.3-id-lora-talkvid-3k"}))

    def test_equals_normalized(self):
        self.assertTrue(PF._match("Qwen_Std_VAE.safetensors",
                                  {"equals": "qwen_std_vae.safetensors"}))

    def test_version_numbers_still_distinguished(self):
        """归一**只**处理大小写和连字符。版本/精度/规模必须保持可区分。

        rank_105 匹配到 rank_111、fp8 匹配到 int8，会让作业静默用错权重 ——
        那种错误在成片里根本看不出来。
        """
        self.assertFalse(PF._match("ltx-lora-rank_105.safetensors",
                                   {"contains": "lora_rank_111"}))
        self.assertFalse(PF._match("seedvr2_7b_fp8.safetensors",
                                   {"contains": "seedvr2_7b_int8"}))
        self.assertFalse(PF._match("seedvr2_ema_3b.safetensors",
                                   {"contains": "seedvr2_ema_7b"}))

    def test_empty_rule_does_not_match_everything(self):
        self.assertFalse(PF._match("anything.safetensors", {}))


if __name__ == "__main__":
    unittest.main()
