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
        self.assertTrue(d["artifacts"][0]["url"].endswith("/pub/jobs/p1-20260101T0000-aaaaaa/01-a.png"))

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
