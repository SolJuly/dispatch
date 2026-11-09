#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 docs/openapi.json 的 examples 起一个本地 Mock 服务，供前端并行开发。

特点：
  · 零依赖（stdlib），CORS 全开；
  · GET 返回 spec 里的示例；POST 走"有状态假服务"：建作业/取消/上传会真的改变内存状态；
  · 路径与线上一致：`http://127.0.0.1:8099/files/{TOKEN}/api/p2/...`

用法：
    python3 scripts/mock_api.py [--port 8099]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import re
import struct
import threading
import zipfile
import io
import zlib
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
TOKEN = SPEC.get("x-mock", {}).get("token", "{TOKEN}")
TZ = timezone(timedelta(hours=8))
PORT = 8099
STARTED_AT = ""
STARTED_TS = time.time()

STATE_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def seed() -> None:
    ex = SPEC["components"]["schemas"]["JobDetail"]["examples"][0]
    jid = ex["job"]["job_id"]
    JOBS[jid] = {"detail": json.loads(json.dumps(ex)), "state": ex["job"]["state"]}


seed()


def png_bytes(w: int = 64, h: int = 64) -> bytes:
    """生成一张灰色 PNG（Mock 产物用）。"""
    raw = b"".join(b"\x00" + bytes([180, 180, 185]) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def job_summary(detail: dict) -> dict:
    j = detail["job"]
    arts = detail.get("artifacts") or []
    return {
        "job_id": j["job_id"], "state": j["state"],
        "workflow": detail.get("workflow") or j.get("workflow", ""),
        "workflow_version": (j.get("model", {}).get("registry") or {}).get("workflow_version", 1),
        "model": (j.get("model") or {}).get("key", ""),
        "created_at": j.get("created_at"), "kind": j.get("kind"),
        "prompt": (j.get("params") or {}).get("prompt", ""),
        "artifacts": len(arts),
        "first_artifact_url": arts[0]["url"] if arts else "",
    }


def match_spec(method: str, target: str) -> tuple[int, object] | None:
    """在 spec 里找 method+path 的示例；供 Mock 与单测共用。"""
    for tpl, item in SPEC["paths"].items():
        if method not in item:
            continue
        pat = "^" + re.sub(r"\{[^}]+\}", "[^/]+", tpl) + "$"
        if not re.match(pat, target):
            continue
        resp = item[method]["responses"]
        for code in ("200", "201", "202"):
            if code in resp:
                content = (resp[code] or {}).get("content") or {}
                js = content.get("application/json")
                if js:
                    ex = (js.get("examples") or {}).get("default", {}).get("value")
                    if ex is not None:
                        return int(code), ex
                return int(code), b"binary"
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "P21Mock/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        print(f"[mock] {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # ---------------- 基础
    def _send(self, code: int, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _route(self):
        path = self.path.split("?", 1)[0]
        parts = [x for x in path.split("/") if x]
        # /files/<token>/api/...
        if len(parts) >= 3 and parts[0] == "files" and parts[1] == TOKEN:
            return parts[2:]
        return None

    def do_OPTIONS(self):
        self._send(204, b"")

    # ---------------- GET
    def do_GET(self):
        r = self._route()
        if r is None:
            return self._json({"error": "路径不存在（Mock 只支持 /files/<token>/...）"}, 404)
        if r[:2] == ["api", "p2"]:
            return self._api_get(r[2:])
        if r[0] == "pub":  # 产物静态地址：给一张占位图
            return self._send(200, png_bytes(), "image/png")
        return self._json({"error": "Mock 未实现该路径", "path": r}, 404)

    def _api_get(self, sub: list[str]) -> None:
        with STATE_LOCK:
            if sub == ["version"]:
                # 动态返回，前端看 features 一眼确认"这是 Mock，不是线上"
                return self._json({
                    "service": "mock", "ok": True, "api": "p2.1", "contract": "openapi-3.1",
                    "build": {"commit": "mock", "branch": None, "dirty": None, "dirty_files": None},
                    "started_at": STARTED_AT or now(), "uptime_s": int(time.time() - STARTED_TS),
                    "pid": os.getpid(), "bind": f"127.0.0.1:{PORT}", "base": "/files",
                    "root": str(ROOT), "capabilities": ["console", "registry", "jobs",
                                                       "uploads", "zip", "version"],
                    "features": {"console": False, "registry": True, "jobs": True,
                                 "uploads": True, "zip": True, "version": True},
                    "registry": {"workflows": 13, "models": 6},
                    "docs": {"openapi": "/pub/api/openapi.json",
                             "handoff": "/pub/p21/07-API契约与Mock.html"},
                })
            if sub == ["jobs"]:
                items = sorted((job_summary(v["detail"]) for v in JOBS.values()),
                               key=lambda x: x.get("created_at") or "", reverse=True)
                return self._json({"items": items, "count": len(items)})
            if sub == ["jobs", "zip"]:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    for jid in JOBS:
                        zf.writestr(f"{jid}/mock.png", png_bytes())
                return self._send(200, buf.getvalue(), "application/zip",
                                  {"Content-Disposition": 'attachment; filename="jobs.zip"'})
            if sub[:1] == ["jobs"] and len(sub) == 2:
                v = JOBS.get(sub[1])
                if not v:
                    return self._json({"error": f"作业不存在：{sub[1]}"}, 404)
                return self._json(v["detail"])
            if sub == ["registry"]:
                return self._json({"workflows": 13, "models": 6,
                                   "compat": {"verified": 10, "equivalent": 2, "experimental": 1}})
            if sub == ["queue"]:
                return self._json(SPEC["components"]["schemas"]["QueueStats"]["examples"][0])
            if sub in (["status"], ["registry", "stats"]):
                return self._json({"total": len(JOBS), "running": 0, "done": len(JOBS), "failed": 0})
        # 其余 GET 一律回 spec 示例
        return self._from_spec("get", sub)

    def _from_spec(self, method: str, sub: list[str]) -> None:
        target = "/" + "/".join(["api", "p2"] + sub)
        for tpl, item in SPEC["paths"].items():
            if method not in item:
                continue
            pat = "^" + re.sub(r"\\{[^}]+\\}", "[^/]+", tpl) + "$"
            if re.match(pat, target):
                resp = item[method]["responses"]
                for code in ("200", "201", "202"):
                    if code in resp:
                        content = (resp[code] or {}).get("content") or {}
                        js = content.get("application/json")
                        if js:
                            ex = (js.get("examples") or {}).get("default", {}).get("value")
                            if ex is not None:
                                return self._json(ex, int(code))
                        return self._send(200, b"mock", "application/octet-stream")
        return self._json({"error": "Mock 未覆盖该接口（补 docs/openapi.json 的示例即可）",
                           "path": target}, 404)

    # ---------------- POST
    def do_POST(self):
        r = self._route()
        if r is None or r[:2] != ["api", "p2"]:
            return self._json({"error": "路径不存在"}, 404)
        sub = r[2:]
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if sub == ["jobs"]:
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "JSON 解析失败"}, 400)
            if not body.get("workflow") or not isinstance(body.get("params"), dict):
                return self._json({"error": "workflow/params 必填"}, 400)
            jid = "p1-" + datetime.now(TZ).strftime("%Y%m%dT%H%M%S") + "-" + hashlib.md5(raw).hexdigest()[:6]
            params = body["params"]
            detail = {
                "job": {"job_id": jid, "state": "CREATED", "created_at": now(),
                        "kind": "image",
                        "model": {"key": "sdxl_juggernaut",
                                  "workflow": "workflows/mock.api.json",
                                  "registry": {"workflow_key": body["workflow"],
                                               "workflow_version": 1,
                                               "model_key": "sdxl_juggernaut",
                                               "model_version": 1}},
                        "params": params, "params_hash": "sha256:mock", "steps": {},
                        "state_seq": 1},
                "artifacts": [], "workflow": body["workflow"], "terminal": False,
            }
            with STATE_LOCK:
                JOBS[jid] = {"detail": detail, "state": "CREATED"}
            return self._json({"job_id": jid, "state": "CREATED", "run": bool(body.get("run")),
                               "workflow": body["workflow"], "workflow_version": 1,
                               "model": "sdxl_juggernaut", "params_hash": "sha256:mock",
                               "params": params, "expect": {"ext": "png", "min_bytes": 20000}}, 201)

        if len(sub) == 3 and sub[0] == "jobs" and sub[2] == "cancel":
            with STATE_LOCK:
                v = JOBS.get(sub[1])
                if not v:
                    return self._json({"error": f"作业不存在：{sub[1]}"}, 404)
                if v["state"] != "CREATED":
                    return self._json({"error": f"作业状态为 {v['state']}，不能取消"}, 409)
                v["state"] = "ABORTED"
                v["detail"]["job"]["state"] = "ABORTED"
                v["detail"]["terminal"] = True
            return self._json({"job_id": sub[1], "state": "ABORTED"})

        if sub == ["uploads"]:
            data = b""
            ctype = self.headers.get("Content-Type", "")
            if "boundary=" in ctype:
                boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
                for chunk in raw.split(b"--" + boundary):
                    if b"\r\n\r\n" in chunk:
                        data = chunk.split(b"\r\n\r\n", 1)[1]
                        data = data[:-2] if data.endswith(b"\r\n") else data
                        break
            if not data:
                return self._json({"error": "没有文件字段"}, 400)
            ext = ".png" if data.startswith(b"\x89PNG") else ".jpg" if data.startswith(b"\xff\xd8") else ""
            if not ext:
                return self._json({"error": "只支持 PNG / JPEG / WebP"}, 415)
            name = "in-" + hashlib.sha256(data).hexdigest()[:8] + ext
            return self._json({"name": name, "bytes": len(data), "url": f"/f/inputs/{name}"}, 201)

        return self._from_spec("post", sub)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    global PORT, STARTED_AT
    a = ap.parse_args()
    PORT = a.port
    STARTED_AT = now()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"[mock] http://{a.host}:{a.port}/files/{TOKEN}/  （Ctrl-C 退出）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
