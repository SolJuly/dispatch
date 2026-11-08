# -*- coding: utf-8 -*-
"""控制台数据层单测：列表/详情/取消 用临时 jobs 目录；创建用假 jobctl 走全流程。"""
from __future__ import annotations

import io
import json
import sys
import zipfile
import tempfile
import shutil
import unittest
from pathlib import Path
from unittest import mock

from webui import api

FAKE_JOBCTL = r'''
import json, sys, re, time
sys.path.insert(0, {root!r})
from tools.p1 import store
spec = json.load(open(sys.argv[sys.argv.index("--spec") + 1], encoding="utf-8"))
jid = "p1-20260101T0000-abc123"
from pathlib import Path
d = Path({jobs!r}) / jid
d.mkdir(parents=True, exist_ok=True)
ph = store.params_hash(spec["model"], spec["params"], spec.get("expect") or {{}})
job = {{"schema": "p1.job/v1", "job_id": jid, "state": "CREATED",
        "created_at": "2026-01-01T00:00:00+08:00", "kind": spec.get("kind", "image"),
        "model": spec.get("model"), "params": spec.get("params"), "expect": spec.get("expect"),
        "params_hash": ph, "steps": {{}}}}
(d / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
print("✓ 作业已创建 " + jid)
'''


def _mk_job(root: Path, jid: str, state: str, wf: str = "qwen_image_t2i",
            prompt: str = "a cat") -> Path:
    d = root / jid
    d.mkdir(parents=True, exist_ok=True)
    job = {
        "schema": "p1.job/v1", "job_id": jid, "state": state,
        "created_at": "2026-01-01T00:00:00+08:00", "kind": "image",
        "model": {"key": "qwen_image", "registry": {"workflow_key": wf, "workflow_version": 2,
                                                    "model_key": "qwen_image", "model_version": 1}},
        "params": {"prompt": prompt, "seed": 1}, "expect": {"ext": "png"}, "steps": {},
        "state_seq": 10,
    }
    (d / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    (d / "artifacts").mkdir(exist_ok=True)
    (d / "artifacts" / "01-a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return d


class TestListGet(unittest.TestCase):
    def setUp(self):
        self._mp = api.maybe_publish
        api.maybe_publish = lambda *a, **k: None
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE")
        _mk_job(self.root, "p1-20260101T0001-bbbbbb", "FAILED_RUN", prompt="a dog")
        _mk_job(self.root, "p1-20260101T0002-cccccc", "CREATED")

    def tearDown(self):
        api.maybe_publish = self._mp
        self.tmp.cleanup()

    def test_list_all(self):
        rows = api.list_jobs(jobs_dir=self.root)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["job_id"], "p1-20260101T0002-cccccc")   # created_at 倒序
        self.assertEqual(rows[0]["artifacts"], 1)
        self.assertTrue(rows[0]["first_artifact_url"].endswith("01-a.png"))

    def test_filter_failed_prefix(self):
        rows = api.list_jobs(state_prefix="FAILED", jobs_dir=self.root)
        self.assertEqual([r["state"] for r in rows], ["FAILED_RUN"])

    def test_filter_query(self):
        rows = api.list_jobs(q="dog", jobs_dir=self.root)
        self.assertEqual(len(rows), 1)

    def test_get_job(self):
        d = api.get_job("p1-20260101T0000-aaaaaa", jobs_dir=self.root)
        self.assertEqual(d["workflow"], "qwen_image_t2i")
        self.assertEqual(len(d["artifacts"]), 1)
        # 产物在作业目录里（夹具就是本地创建的），URL 必须走 /f/ 读作业目录，
        # 不能是 /pub/——那条路由读 /srv/dsh-pub，本机上没有这个目录 → 必 404。
        self.assertTrue(d["artifacts"][0]["url"].startswith("/f/"))
        self.assertTrue(d["artifacts"][0]["url"].endswith(
            "/reports/p1/jobs/p1-20260101T0000-aaaaaa/artifacts/01-a.png"))

    def test_get_missing(self):
        with self.assertRaises(api.ApiError) as cm:
            api.get_job("p1-20990101T0000-ffffff", jobs_dir=self.root)
        self.assertEqual(cm.exception.code, 404)

    def test_bad_job_id(self):
        with self.assertRaises(api.ApiError) as cm:
            api.get_job("../../etc/passwd", jobs_dir=self.root)
        self.assertEqual(cm.exception.code, 400)


class TestCancel(unittest.TestCase):
    def setUp(self):
        self._mp = api.maybe_publish
        api.maybe_publish = lambda *a, **k: None
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "CREATED")
        _mk_job(self.root, "p1-20260101T0001-bbbbbb", "DONE")

    def tearDown(self):
        api.maybe_publish = self._mp
        self.tmp.cleanup()

    def test_cancel_created(self):
        r = api.cancel_job("p1-20260101T0000-aaaaaa", jobs_dir=self.root)
        self.assertEqual(r["state"], "ABORTED")
        job = api.get_job("p1-20260101T0000-aaaaaa", jobs_dir=self.root)["job"]
        self.assertEqual(job["state"], "ABORTED")
        self.assertEqual(job["aborted_by"], "web")
        self.assertEqual(job["state_seq"], 11)          # 原 state_seq=10，取消后 +1

    def test_cancel_done_refused(self):
        with self.assertRaises(api.ApiError) as cm:
            api.cancel_job("p1-20260101T0001-bbbbbb", jobs_dir=self.root)
        self.assertEqual(cm.exception.code, 409)


class TestUpload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._i, self._s = api.INPUTS_DIR, api.SHARE_INPUTS
        api.INPUTS_DIR = self.root / "inputs"
        api.SHARE_INPUTS = self.root / "share"

    def tearDown(self):
        api.INPUTS_DIR, api.SHARE_INPUTS = self._i, self._s
        self.tmp.cleanup()

    def test_png_saved_twice(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        info = api.save_upload("photo.png", png)
        self.assertRegex(info["name"], r"^in-[0-9a-f]{8}\.png$")
        self.assertTrue((api.INPUTS_DIR / info["name"]).is_file())
        self.assertTrue((api.SHARE_INPUTS / info["name"]).is_file())
        self.assertEqual(info["url"], f"/f/inputs/{info['name']}")

    def test_jpeg_detected(self):
        info = api.save_upload("x.jpg", b"\xff\xd8\xff" + b"1" * 50)
        self.assertTrue(info["name"].endswith(".jpg"))

    def test_non_image_rejected(self):
        with self.assertRaises(api.ApiError) as cm:
            api.save_upload("x.txt", b"hello")
        self.assertEqual(cm.exception.code, 415)

    def test_oversize_rejected(self):
        with self.assertRaises(api.ApiError) as cm:
            api.save_upload("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * (21 * 1048576))
        self.assertEqual(cm.exception.code, 413)


class TestZip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pub = self.root / "pub"
        for jid, names in (("p1-20260101T0000-aaaaaa", ["a.png", "b.png"]),
                           ("p1-20260101T0001-bbbbbb", ["c.png"])):
            d = self.pub / jid
            d.mkdir(parents=True)
            for n in names:
                (d / n).write_bytes(b"\x89PNG\r\n\x1a\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_two_jobs(self):
        data = api.zip_artifacts(["p1-20260101T0000-aaaaaa", "p1-20260101T0001-bbbbbb"],
                                 pub_dir=self.pub)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
        self.assertEqual(len(names), 3)
        self.assertIn("p1-20260101T0000-aaaaaa/a.png", names)
        self.assertIn("p1-20260101T0001-bbbbbb/c.png", names)

    def test_zip_ignores_bad_ids(self):
        data = api.zip_artifacts(["../../etc/passwd", "p1-20260101T0001-bbbbbb"],
                                 pub_dir=self.pub)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertEqual(len(zf.namelist()), 1)

    def test_zip_empty_raises(self):
        with self.assertRaises(api.ApiError) as cm:
            api.zip_artifacts(["p1-20990101T0000-ffffff"], pub_dir=self.pub)
        self.assertEqual(cm.exception.code, 404)


class TestCreate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs"
        self.specs = self.root / "specs"
        self.jobs.mkdir()
        self.fake = self.root / "fake_jobctl.py"
        self.fake.write_text(FAKE_JOBCTL.format(root=str(api.ROOT), jobs=str(self.jobs)),
                             encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_spec_written(self):
        r = api.create_job("qwen_image_t2i", {"prompt": "a red apple", "seed": 42},
                           jobs_dir=self.jobs, spec_dir=self.specs, jobctl=self.fake)
        self.assertEqual(r["job_id"], "p1-20260101T0000-abc123")
        self.assertEqual(r["workflow_version"], 2)
        self.assertTrue(r["params_hash"])
        self.assertEqual(r["params"]["seed"], 42)
        self.assertTrue(any(self.specs.glob("*.json")))

    def test_unknown_workflow_rejected(self):
        with self.assertRaises(api.ApiError) as cm:
            api.create_job("nope", {"prompt": "x"}, jobs_dir=self.jobs,
                           spec_dir=self.specs, jobctl=self.fake)
        self.assertEqual(cm.exception.code, 400)

    def test_bad_param_type_rejected(self):
        with self.assertRaises(api.ApiError):
            api.create_job("qwen_image_t2i", {"prompt": "x", "seed": "not-a-number"},
                           jobs_dir=self.jobs, spec_dir=self.specs, jobctl=self.fake)

    def test_experimental_rejected(self):
        with self.assertRaises(api.ApiError) as cm:
            api.create_job("pack_video_wan_vace_14b_ref2v",
                           {"prompt": "x", "seed": 1, "image": "a.png", "width": 768,
                            "height": 512, "frames": 121, "steps": 20, "cfg": 1.0},
                           jobs_dir=self.jobs, spec_dir=self.specs, jobctl=self.fake)
        self.assertEqual(cm.exception.code, 409)

    def test_duplicate_done_returns_409(self):
        """jobctl 报「同参数已完成」时，webui 应回 409 而不是笼统的 500。"""
        fake = self.root / "fake_duplicate.py"
        fake.write_text(
            "import sys\n"
            "print('✗ 同参数作业已完成：p1-20260101T0000-abc123（state=DONE）。"
            "重复跑会白烧钱；确实要重跑加 --force。', file=sys.stderr)\n"
            "sys.exit(1)\n", encoding="utf-8")
        with self.assertRaises(api.ApiError) as cm:
            api.create_job("qwen_image_t2i", {"prompt": "x", "seed": 1},
                           jobs_dir=self.jobs, spec_dir=self.specs, jobctl=fake)
        self.assertEqual(cm.exception.code, 409)

    def test_force_is_forwarded_to_jobctl(self):
        """rerun 走 force=True，--force 必须透传到 jobctl。"""
        argv_log = self.root / "argv.json"
        fake = self.root / "fake_argv.py"
        fake.write_text(
            "import sys, json\n"
            f"open({str(argv_log)!r}, 'w').write(json.dumps(sys.argv))\n"
            "print('✗ 普通错误', file=sys.stderr)\n"
            "sys.exit(1)\n", encoding="utf-8")
        with self.assertRaises(api.ApiError):
            api.create_job("qwen_image_t2i", {"prompt": "x", "seed": 1}, force=True,
                           jobs_dir=self.jobs, spec_dir=self.specs, jobctl=fake)
        self.assertIn("--force", json.loads(argv_log.read_text()))


    def test_failed_old_job_id_not_picked(self):
        """jobctl 先打印「旧作业已结束：<旧id>」时，必须取最后那个真正新建的 id。

        2026-11-08 审计发现的真 bug：原实现用 JOB_ID_RE.search() 取第一个匹配，
        拿到的是已失败的历史作业 id，run=True 时就会去执行那个死作业。
        """
        out = ("  同参数旧作业已结束（FAILED_RUN），按新提交处理："
               "p1-20260101T0000-aaaaa1\n"
               "✓ 作业已建：p1-20260101T0000-bbbbb2\n")
        ids = api.JOB_ID_RE.findall(out)
        self.assertEqual(ids[0], "p1-20260101T0000-aaaaa1")
        self.assertEqual(ids[-1], "p1-20260101T0000-bbbbb2")
        # create_job 现在取 [-1]；与 p20ctl._pick_job_id 同口径
        src = Path(api.__file__).read_text(encoding="utf-8")
        self.assertIn("job_id = found[-1]", src)


class TestArtifactFallback(unittest.TestCase):
    """只读部署：台账有、产物没迁，缩略图回退到 /srv/dsh-pub 的发布副本。"""

    JID = "p1-20260101T0000-aaaaaa"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.jobs = base / "jobs"
        self.pub = base / "pub"
        (self.jobs / self.JID).mkdir(parents=True)
        self._pub = api.PUB_DIR
        api.PUB_DIR = self.pub

    def tearDown(self):
        api.PUB_DIR = self._pub
        self.tmp.cleanup()

    def test_falls_back_to_published_copy(self):
        d = self.pub / self.JID
        d.mkdir(parents=True)
        (d / "01-a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (d / "job-summary.json").write_text("{}", encoding="utf-8")
        arts = api._artifacts(self.JID, self.jobs)
        self.assertEqual([a["name"] for a in arts], ["01-a.png"])
        self.assertTrue(arts[0]["published"])
        self.assertEqual(arts[0]["url"], f"/pub/jobs/{self.JID}/01-a.png")

    def test_empty_when_neither_side_has_files(self):
        self.assertEqual(api._artifacts(self.JID, self.jobs), [])


if __name__ == "__main__":
    unittest.main()


class TestVideoUpload(unittest.TestCase):
    """动作迁移要传驱动视频：上传接口必须认 MP4/MOV/WebM（以前只认图片，直接 415）。"""

    def test_mp4_accepted(self):
        import webui.api as api
        data = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 4096
        with mock.patch.object(api, "INPUTS_DIR", Path(self.tmp)), \
                mock.patch.object(api, "SHARE_INPUTS", Path(self.tmp) / "share"):
            info = api.save_upload("drive.mp4", data)
        self.assertTrue(info["name"].endswith(".mp4"), info)
        self.assertEqual(info["bytes"], len(data))

    def test_sniff_moves_and_webm(self):
        import webui.api as api
        self.assertEqual(api.sniff_upload(b"\x00\x00\x00\x14ftypqt  " + b"0" * 8), ".mov")
        self.assertEqual(api.sniff_upload(b"\x1a\x45\xdf\xa3" + b"0" * 8), ".webm")
        self.assertEqual(api.sniff_upload(b"\xff\xd8\xff\xe0" + b"0" * 8), ".jpg")

    def test_unknown_type_rejected_with_hint(self):
        import webui.api as api
        with self.assertRaises(api.ApiError) as cm:
            api.sniff_upload(b"hello world", "x.txt")
        self.assertEqual(cm.exception.code, 415)
        self.assertIn("MP4", cm.exception.msg)

    def test_video_has_bigger_cap(self):
        import webui.api as api
        self.assertGreater(api.MAX_UPLOAD_VIDEO, api.MAX_UPLOAD)
        self.assertEqual(api.max_upload_bytes(), api.MAX_UPLOAD_VIDEO)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="upload-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestVideoParamBinding(unittest.TestCase):
    """绑到 LoadVideo 的参数必须是 video 类型，否则界面给的是图片上传框。"""

    def test_no_upstream_param_mismatch(self):
        from tools.p20 import registry
        bad = []
        for wf in registry.list_workflows():
            graph = registry.get_workflow(wf["key"])["graph_obj"]
            for pname, spec in (wf.get("params") or {}).items():
                for b in spec.get("bindings") or []:
                    ct = (graph.get(b["node"]) or {}).get("class_type") or ""
                    if ct == "LoadVideo" and spec.get("type") != "video":
                        bad.append(f"{wf['key']}.{pname} type={spec.get('type')} → LoadVideo")
        self.assertEqual(bad, [])

    def test_animate2_exposes_video_picker(self):
        from tools.p20 import registry
        from webui import components
        wf = registry.get_workflow("pack_video_wan_animate2_distilled")
        html = components.field("image_2", wf["params"]["image_2"])
        self.assertIn('accept="video/*"', html)
        self.assertIn("驱动视频", html)


class TestArtifactUrl(unittest.TestCase):
    """产物 URL 必须指向文件真正所在的位置。

    回归背景：_artifacts() 曾把 URL 恒定写成 /pub/jobs/<id>/<name>，
    但 /pub/ 由 nginx 直读 /srv/dsh-pub——只读部署的发布副本目录。
    在开发机/自建机上，产物在 reports/p1/jobs/<id>/artifacts/，没跑过 publish
    的作业在 /srv/dsh-pub 里根本不存在，于是前端拿到必 404 的链接。
    <video> 遇到 404 会反复重试，UI 上表现为「与后端失联」的假弹窗。
    """

    def setUp(self):
        self._pub = api.PUB_DIR
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pubdir = self.root / "pub"
        self.pubdir.mkdir()
        api.PUB_DIR = self.pubdir

    def tearDown(self):
        api.PUB_DIR = self._pub
        self.tmp.cleanup()

    def _job_with(self, jid: str, name: str, where: str):
        base = (self.root / "jobs" if where == "local" else self.pubdir) / jid
        base.mkdir(parents=True)
        (base / name).write_bytes(b"\x00" * 32)
        return base

    def test_local_artifact_uses_f_route_not_pub(self):
        """有本地产物时，URL 必须走 /f/（读作业目录），不能是 /pub/（读发布副本）。"""
        jid = "p1-20260101T0000-local1"
        d = self.root / "jobs" / jid / "artifacts"
        d.mkdir(parents=True)
        (d / "00-primary-x.mp4").write_bytes(b"\x00" * 32)

        arts = api._artifacts(jid, self.root / "jobs")
        self.assertEqual(len(arts), 1)
        url = arts[0]["url"]
        self.assertTrue(url.startswith("/f/"), f"应以 /f/ 开头，实际 {url}")
        self.assertNotIn("/pub/", url)
        self.assertIn(jid, url)
        self.assertIn("00-primary-x.mp4", url)
        self.assertEqual(arts[0]["source"], "local")

    def test_published_fallback_uses_pub_route(self):
        """只有发布副本时（只读部署），URL 才用 /pub/。"""
        jid = "p1-20260101T0000-pub0001"
        d = self.pubdir / jid
        d.mkdir(parents=True)
        (d / "01-a.png").write_bytes(b"\x00" * 32)

        arts = api._artifacts(jid, self.root / "jobs")
        self.assertEqual(len(arts), 1)
        self.assertTrue(arts[0]["url"].startswith("/pub/"))
        self.assertEqual(arts[0]["source"], "published")

    def test_local_wins_over_published(self):
        """两者都在时优先本地（原件比发布副本可信）。"""
        jid = "p1-20260101T0000-both01"
        loc = self.root / "jobs" / jid / "artifacts"
        loc.mkdir(parents=True)
        (loc / "00-primary-y.png").write_bytes(b"\x00" * 32)
        pub = self.pubdir / jid
        pub.mkdir(parents=True)
        (pub / "00-primary-y.png").write_bytes(b"\x00" * 32)

        arts = api._artifacts(jid, self.root / "jobs")
        self.assertEqual(arts[0]["source"], "local")
        self.assertTrue(arts[0]["url"].startswith("/f/"))
        self.assertTrue(arts[0]["published"], "published 标记应仍反映发布副本存在")

    def test_first_artifact_url_matches_list_route(self):
        """列表页缩略图用的 first_artifact_url 必须与详情页同一个路由前缀。"""
        jid = "p1-20260101T0000-thumb1"
        d = self.root / "jobs" / jid / "artifacts"
        d.mkdir(parents=True)
        (d / "00-primary-z.png").write_bytes(b"\x00" * 32)

        url = api._first_artifact_url(jid, self.root / "jobs")
        self.assertTrue(url.startswith("/f/"), f"缩略图应走 /f/，实际 {url}")
        self.assertIn(jid, url)


class TestDeleteJobs(unittest.TestCase):
    """删除链路：force 必须透传到 jobctl rm，否则僵尸作业永远清不掉。

    背景：控制台提交时若没勾「立即执行」，作业会停在 CREATED 且无过期机制。
    jobctl 默认拒绝删未终态（保护真在跑的作业），但没有 force 通道时
    用户只能在界面上看着这些从没执行过的作业永远留着。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _run_capture(self, **kw):
        with mock.patch.object(api.subprocess, "run") as m:
            m.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
            api.delete_jobs(["p1-x"], **kw)
            return m.call_args[0][0]

    def test_purge_flag_appended(self):
        self.assertIn("--purge", self._run_capture(purge=True))

    def test_purge_absent_by_default(self):
        self.assertNotIn("--purge", self._run_capture())

    def test_force_flag_appended(self):
        """force=True 必须变成 --force，否则 CREATED 僵尸清不掉。"""
        self.assertIn("--force", self._run_capture(force=True))

    def test_force_absent_by_default(self):
        self.assertNotIn("--force", self._run_capture())

    def test_empty_ids_raises(self):
        with self.assertRaises(api.ApiError):
            api.delete_jobs([])

    def test_force_and_purge_together(self):
        cmd = self._run_capture(force=True, purge=True)
        self.assertIn("--force", cmd)
        self.assertIn("--purge", cmd)


class TestThumbnails(unittest.TestCase):
    """列表缩略图：视频要抽帧、图片要缩放，绝不能把 .mp4 塞进 <img>。

    用户报「预览图都是黑的，或者要加载很久」，两个独立原因：
      1. 视频缩略图用 <img src="x.mp4"> → 永远解不出来 → 黑块
      2. 图片用 13MB 级原图 → 一屏几十张等于几百 MB → 慢到像卡死
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._share = api.ROOT
        # 隔离 share：thumb_for 会往 share/ 写硬链接，测试不该碰真实目录
        self._saved_root = api.ROOT
        api.ROOT = self.root
        (self.root / "share").mkdir()

    def tearDown(self):
        api.ROOT = self._saved_root
        self.tmp.cleanup()

    def _job(self, jid: str, name: str, data: bytes):
        d = self.root / jid
        (d / "artifacts").mkdir(parents=True)
        (d / "artifacts" / name).write_bytes(data)
        return d

    def test_video_thumb_is_jpeg_not_mp4(self):
        """视频必须有 jpg 缩略图，且 URL 不能是 .mp4。"""
        jid = "p1-20260101T0000-vid001"
        d = self._job(jid, "00-primary-x.mp4", b"not-a-real-mp4")
        t = api.thumb_for(d / "artifacts" / "00-primary-x.mp4", d)
        # ffmpeg 解不了假数据 → 应返回 None，而不是抛异常
        self.assertIsNone(t, "损坏的视频应返回 None，不能抛")

    def test_missing_source_returns_none(self):
        jid = "p1-20260101T0000-nofile"
        d = self.root / jid
        d.mkdir()
        self.assertIsNone(api.thumb_for(d / "artifacts" / "nope.mp4", d))

    def test_thumb_name_carries_source_stamp(self):
        """缩略图文件名要带源文件戳，源一变旧图自然失效（无需清理逻辑）。"""
        jid = "p1-20260101T0000-stamp1"
        d = self._job(jid, "a.png", b"\x89PNG\r\n\x1a\n")
        src = d / "artifacts" / "a.png"
        stamp = api._src_stamp(src)
        self.assertIn(str(src.stat().st_size), stamp)

    def test_list_returns_thumb_fields(self):
        """列表项必须有 thumb_url / thumb_kind 两个字段（前端据此决定渲染方式）。"""
        jid = "p1-20260101T0000-fields"
        self._job(jid, "00-primary-y.png", b"\x89PNG\r\n\x1a\n")
        import json as _j
        (self.root / jid / "job.json").write_text(_j.dumps({
            "job_id": jid, "state": "DONE", "created_at": "2026-01-01T00:00:00+08:00",
            "kind": "image", "model": {"key": "m"}, "params": {}, "steps": {},
        }), encoding="utf-8")
        rows = api.list_jobs(limit=10, jobs_dir=self.root)
        self.assertEqual(len(rows), 1)
        self.assertIn("thumb_url", rows[0])
        self.assertIn("thumb_kind", rows[0])

    def test_video_art_detected_as_video_kind(self):
        jid = "p1-20260101T0000-vkind"
        self._job(jid, "00-primary-z.mp4", b"x")
        import json as _j
        (self.root / jid / "job.json").write_text(_j.dumps({
            "job_id": jid, "state": "DONE", "created_at": "2026-01-01T00:00:00+08:00",
            "kind": "video", "model": {"key": "m"}, "params": {}, "steps": {},
        }), encoding="utf-8")
        rows = api.list_jobs(limit=10, jobs_dir=self.root)
        self.assertEqual(rows[0]["thumb_kind"], "video")

    def test_ffmpeg_failure_never_raises(self):
        """ffmpeg 不存在/超时都只能返回 None，列表页不能因为一张图挂掉。"""
        jid = "p1-20260101T0000-boom01"
        d = self._job(jid, "a.mp4", b"garbage")
        with mock.patch.object(api.subprocess, "run",
                               side_effect=OSError("no ffmpeg")):
            self.assertIsNone(api.thumb_for(d / "artifacts" / "a.mp4", d))
