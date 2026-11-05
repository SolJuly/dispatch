# -*- coding: utf-8 -*-
"""控制台页面渲染测试：结构、参数表单、转义、空态。"""
from __future__ import annotations

import unittest

from webui import api, components as C, pages


class TestComponents(unittest.TestCase):
    def test_escape_script(self):
        out = C.field("prompt", {"type": "string", "default": "<script>alert(1)</script>"})
        self.assertNotIn("<script>alert(1)</script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_enum_select_marks_default(self):
        out = C.field("sampler", {"type": "enum", "values": ["euler", "dpmpp_2m"],
                                  "default": "dpmpp_2m"})
        self.assertIn('<option value="dpmpp_2m" selected>', out)
        self.assertNotIn('<option value="euler" selected>', out)

    def test_number_bounds(self):
        out = C.field("batch_size", {"type": "int", "min": 1, "max": 4, "default": 1})
        self.assertIn('min="1"', out)
        self.assertIn('max="4"', out)

    def test_badge_classes(self):
        self.assertIn("pill ok", C.badge("DONE"))
        self.assertIn("pill bad", C.badge("FAILED_RUN"))
        self.assertIn("pill run", C.badge("RUNNING"))


class TestCreatePage(unittest.TestCase):
    def test_renders_form_and_params(self):
        html = pages.create_page("/files", "tok")
        self.assertIn("创作台", html)
        self.assertIn('id="create-form"', html)
        self.assertIn('data-param="prompt"', html)
        self.assertIn('data-param="sampler"', html)
        self.assertIn('data-param="steps"', html)
        self.assertIn("window.__WF__", html)
        self.assertIn("/files/tok/static/app.js", html)

    def test_img2img_tab_has_upload(self):
        html = pages.create_page("/files", "tok", "sdxl_img2img")
        self.assertIn('type="file"', html)
        self.assertIn('data-upload="image"', html)
        self.assertIn('data-param="denoise"', html)

    def test_inpaint_tab_has_mask_canvas(self):
        html = pages.create_page("/files", "tok", "sdxl_inpaint")
        self.assertIn('data-mask="mask"', html)
        self.assertIn("data-mask-invert", html)
        self.assertIn("data-mask-clear", html)

    def test_recent_jobs_strip(self):
        html = pages.create_page("/files", "tok")
        self.assertIn("最近作业", html)

    def test_switch_workflow(self):
        wfs = api.image_workflows()
        self.assertGreaterEqual(len(wfs), 2)
        other = next(w for w in wfs if w["key"] != wfs[0]["key"])
        html = pages.create_page("/files", "tok", other["key"])
        self.assertIn(f"创作台 · {other['key']}", html)

    def test_unknown_workflow_falls_back(self):
        html = pages.create_page("/files", "tok", "does_not_exist")
        self.assertIn("创作台", html)


class TestJobsPage(unittest.TestCase):
    def test_empty_state(self):
        orig = api.list_jobs
        api.list_jobs = lambda **kw: []
        try:
            html = pages.jobs_page("/files", "tok")
        finally:
            api.list_jobs = orig
        self.assertIn("还没有作业", html)

    def test_rows_and_escaping(self):
        orig = api.list_jobs
        api.list_jobs = lambda **kw: [{
            "job_id": "p1-20260101T0000-aaaaaa", "state": "DONE", "workflow": "qwen_image_t2i",
            "workflow_version": 2, "model": "qwen_image", "created_at": "2026-01-01T00:00:00+08:00",
            "artifacts": 2, "prompt": "<img src=x onerror=alert(1)>",
        }]
        try:
            html = pages.jobs_page("/files", "tok")
        finally:
            api.list_jobs = orig
        self.assertIn("p1-20260101T0000-aaaaaa", html)
        self.assertIn("pill ok", html)
        self.assertIn("job-check", html)
        self.assertIn("dl-selected", html)
        self.assertNotIn("<img src=x", html)


if __name__ == "__main__":
    unittest.main()
