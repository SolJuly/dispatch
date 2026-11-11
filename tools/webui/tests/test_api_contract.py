# -*- coding: utf-8 -*-
"""前端调用的每个接口，后端都必须真的存在。

## 为什么需要它

2026-11-11 用户截图：两条作业挂在「进行中的作业」里，没人管也没人解释。
查到最后是三个问题叠加，其中一个是——

    SPA 调 `GET /api/p2/dispatch`，后端**从来没有这个路由**（一直 404）。
    前端 `if (!d) { box.hidden = true; }` 把提示条整条隐藏了，
    于是「作业不会自己跑」这句最该说的话也一起消失了。

它不在 `docs/openapi.json` 契约里 —— 是 SPA 初版自己发明的。契约没覆盖到，
测试也没覆盖到，于是烂了两个月没人发现。

## 这里做什么

把 `web/dist/app.js` 里出现的 API 路径抽出来，逐个起真实 HTTP 服务打一遍。
路径参数（`/jobs/<id>`）用占位值替换，只要求"不是 404 路由缺失"。

注意区分两种 404：
  · **路由不存在** —— 后端压根没这条路径（本测试要抓的）
  · **资源不存在** —— 路由在，但那个 job_id/文件名查无此物（正常）

两者在 HTTP 上都可能是 404，所以对带参数的路径只验证"响应是 JSON 而不是
HTML 错误页"——后端所有 API 都返回 JSON，静态 404 页返回 HTML。
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJ = Path(__file__).resolve().parents[3]
APP_JS = PROJ / "web" / "dist" / "app.js"

ENV_KEYS = ("SHARE_ROOT", "SHARE_TOKEN_FILE", "SHARE_PORT", "SHARE_BASE",
            "P21_PUB_DIR", "UI_ROOT")

# 前端调用点：get('xxx') / post('xxx') / get(`/jobs/${id}/cancel`)
CALL_RE = re.compile(r"""(?:get|post|getBlob)\(\s*[`'"]([/][^`'"]*)[`'"]""")


def api_paths() -> set[str]:
    """从 app.js 抽出所有 /xxx 形式的 API 路径（不含模板变量的具体值）。"""
    txt = APP_JS.read_text(encoding="utf-8")
    out = set()
    for m in CALL_RE.finditer(txt):
        p = m.group(1)
        # 模板变量 → 占位；`/jobs/${encodeURIComponent(id)}/cancel` 这类
        p = re.sub(r"\$\{[^}]*\}", "PLACEHOLDER", p)
        out.add(p)
    return out


class TestApiContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        (root / "share").mkdir()
        (root / "pub").mkdir()
        cls._saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ.update({
            "SHARE_ROOT": str(root / "share"),
            "SHARE_TOKEN_FILE": str(root / ".share_token"),
            "SHARE_PORT": "0",
            "SHARE_BASE": "/files",
            "P21_PUB_DIR": str(root / "pub"),
        })
        os.environ.pop("UI_ROOT", None)
        (root / ".share_token").write_text("testtoken", encoding="utf-8")

        sys.modules.pop("fileserver", None)
        cls.mod = importlib.import_module("fileserver")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), cls.mod.Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        sys.modules.pop("fileserver", None)
        for k, v in cls._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cls._tmp.cleanup()

    def _get(self, path: str):
        url = f"http://127.0.0.1:{self.port}/{self.mod.TOKEN}/api/p2{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "")

    def _probe(self, path: str, method: str = "GET"):
        """按指定方法探测路径。POST 不带 body —— 只为看路由在不在。"""
        url = f"http://127.0.0.1:{self.port}/{self.mod.TOKEN}/api/p2{path}"
        data = b"{}" if method == "POST" else None
        req = urllib.request.Request(
            url, method=method, data=data,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers.get("Content-Type", "")

    def _get(self, path: str):
        return self._probe(path, "GET")

    def test_app_js_paths_are_extractable(self):
        """先确认抽取本身有效 —— 否则下面的测试是空转。"""
        paths = api_paths()
        self.assertGreaterEqual(len(paths), 5, f"只抽到 {len(paths)} 个路径：{paths}")
        self.assertIn("/jobs", paths)
        self.assertIn("/dispatch", paths)

    def test_dispatch_is_implemented(self):
        """dispatch 必须有真实实现（曾经 404 整整两个月）。

        它承载的信息很关键：**有没有调度器在跑**。没有的话 CREATED 作业
        永远不会自己启动，界面必须说出来。
        """
        code, body, ctype = self._get("/dispatch")
        self.assertEqual(code, 200, f"/dispatch 应返回 200，实际 {code}")
        self.assertIn("application/json", ctype)
        d = json.loads(body)
        for k in ("alive", "running", "slots", "note"):
            self.assertIn(k, d, f"/dispatch 缺字段 {k}")
        self.assertIsInstance(d["alive"], bool)
        self.assertIsInstance(d["slots"], int)

    def test_no_route_missing_404(self):
        """逐个打前端用到的路径，不许出现"路由不存在"。

        带 PLACEHOLDER 的路径用假 id：只要响应是 JSON，就说明路由存在
        （资源不存在是正常业务错误，不是契约缺失）。

        GET 探测失败的再试 POST：`/uploads` 这类只收 POST 的路由，用 GET 打
        会返回 404 页，但它是完好的 —— 那是**探测方法错**，不是路由缺失。
        """
        missing = []
        for p in sorted(api_paths()):
            if p.startswith("/f/") or p.startswith("/pub/"):
                continue                     # 静态资源，不归 /api/p2
            probe = p.replace("PLACEHOLDER", "x")
            ok = False
            for method in ("GET", "POST"):
                code, body, ctype = self._probe(probe, method)
                if not (code == 404 and "application/json" not in (ctype or "")):
                    ok = True
                    break
            if not ok:
                missing.append((p, probe))
        self.assertFalse(missing, f"这些前端调用的路径后端没有：{missing}")

    def test_openapi_contract_routes_all_exist(self):
        """docs/openapi.json 声明的路径也都得真的能访问。

        契约写了却没实现，和实现了没写契约一样糟 —— 前者骗读文档的人，
        后者骗写前端的人。dispatch 那次是后者。

        按契约里声明的方法探测，不能一律用 GET。
        **只校验 `x-status: implemented`** —— 标了 `planned` 的是有意未做，
        拿它当 bug 会逼着人去实现一个还没定方案的东西。
        """
        spec = json.loads((PROJ / "docs" / "openapi.json").read_text(encoding="utf-8"))
        missing = []
        for raw, ops in spec.get("paths", {}).items():
            if not raw.startswith("/api/p2/"):
                continue
            sub = re.sub(r"\{[^}]+\}", "x", raw[len("/api/p2"):])
            methods = [m for m, o in (ops or {}).items()
                       if m.lower() in ("get", "post", "put", "patch", "delete")
                       and (o or {}).get("x-status", "implemented") == "implemented"]
            if not methods:
                continue                     # 全是 planned，有意未做
            ok = False
            for m in methods:
                code, body, ctype = self._probe(sub, m.upper())
                if not (code == 404 and "application/json" not in (ctype or "")):
                    ok = True
                    break
            if not ok:
                missing.append((raw, methods))
        self.assertFalse(missing, f"契约声明但未实现：{missing}")


if __name__ == "__main__":
    unittest.main()
