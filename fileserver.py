#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简文件分享服务（平板友好）：目录浏览 / 在线预览 / 单文件下载 / 打包下载 / 上传。

只暴露 SHARE_ROOT 目录，token 走路径首段，防止被随意扫到。
启动: SHARE_ROOT=/root/智云视频工作流/share SHARE_PORT=8081 python3 fileserver.py
"""
from __future__ import annotations

import html
import io
import json
import subprocess
import sys
import mimetypes
import os
import re
import secrets
import time
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("SHARE_ROOT", Path(__file__).resolve().parent / "share")).resolve()
PORT = int(os.environ.get("SHARE_PORT", "8081"))
TOKEN_FILE = Path(os.environ.get("SHARE_TOKEN_FILE", ROOT.parent / ".share_token"))
BIND = os.environ.get("SHARE_BIND", "127.0.0.1")
MAX_UPLOAD = int(os.environ.get("SHARE_MAX_UPLOAD_MB", "200")) * 1024 * 1024
BASE = os.environ.get("SHARE_BASE", "/files")  # nginx 反代的路径前缀
PROJ = ROOT.parent
ARCH_SVG = PROJ / "reports" / "architecture.svg"
ARCH_TS = {"t": 0.0}
LOG_FILES = {
    "lab.log": PROJ / "logs" / "lab.log",
    "watchdog.log": PROJ / "logs" / "watchdog.log",
    "status2.log": PROJ / "logs" / "status2.log",
    "watch.log": PROJ / ".compshare" / "watch.log",
    "ledger.jsonl": PROJ / ".compshare" / "ledger.jsonl",
    "lab.state": PROJ / "lab" / "state.json",
}
sys.path.insert(0, str(PROJ))

TEXT_EXT = {".md", ".py", ".sh", ".txt", ".json", ".yml", ".yaml", ".cfg", ".ini", ".log", ".csv", ".env"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
CODE_EXT = {".py", ".sh", ".json", ".yml", ".yaml", ".cfg", ".ini", ".txt", ".csv", ".log"}


def load_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    t = secrets.token_hex(4)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    os.chmod(TOKEN_FILE, 0o600)
    return t


TOKEN = load_token()

# ------------------------------------------------------------------ 构建标识
# 线上 /files/ 曾被一个残留的旧进程占着（对外的 /app 页面 404，但看起来"服务还在"）。
# /api/p2/version 让人一眼判断"这个后端到底是哪一版、支持哪些路由"，
# 而不是对着旧响应调 UI。
API_VERSION = "p2.1"
API_CONTRACT = "openapi-3.1"
CAPABILITIES = ["console", "registry", "jobs", "uploads", "zip", "version"]
STARTED_AT = time.time()
_BUILD: dict = {}


def _iso(ts: float) -> str:
    s = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))
    return f"{s[:-2]}:{s[-2:]}" if len(s) > 5 else s


def build_info() -> dict:
    """启动后读一次 git 信息（git 不可用也不影响服务）。"""
    if _BUILD:
        return _BUILD
    info: dict = {"commit": None, "branch": None, "dirty": None, "dirty_files": None}
    try:
        def _git(*args: str) -> tuple[int, str]:
            try:
                r = subprocess.run(["git", "-C", str(PROJ), *args], capture_output=True,
                                   text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                return 1, ""
            return r.returncode, (r.stdout or "").strip()

        rc, sha = _git("rev-parse", "--short", "HEAD")
        if rc == 0 and sha:
            info["commit"] = sha
            _, br = _git("rev-parse", "--abbrev-ref", "HEAD")
            info["branch"] = br or None
            _, st = _git("status", "--porcelain")
            files = [ln for ln in st.splitlines() if ln.strip()]
            info["dirty"] = bool(files)
            info["dirty_files"] = len(files)
    except Exception:  # noqa: BLE001 - 仅展示用
        pass
    _BUILD.update(info)
    return _BUILD


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def size_of(p: Path) -> str:
    try:
        b = p.stat().st_size
    except OSError:
        return "-"
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b/1024:.1f} KB"
    return f"{b/1024/1024:.1f} MB"


def mtime_of(p: Path) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))
    except OSError:
        return "-"


def icon_of(p: Path) -> str:
    ext = p.suffix.lower()
    if p.is_dir():
        return "📁"
    if ext in IMG_EXT:
        return "🖼️"
    if ext == ".md":
        return "📘"
    if ext in (".py", ".sh"):
        return "⚙️"
    if ext == ".zip":
        return "🗜️"
    if ext in (".pyc",):
        return "·"
    return "📄"


def inline_md(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
    return s


def md_to_html(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    list_open = None

    def close_list():
        nonlocal list_open
        if list_open:
            out.append(f"</{list_open}>")
            list_open = None

    while i < len(lines):
        line = lines[i]
        # 代码块
        if line.lstrip().startswith("```"):
            close_list()
            buf = []
            i += 1
            while i < len(lines) and not lines[i].lstrip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre class='code'><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue
        # 表格
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]) \
                and "-" in lines[i + 1]:
            close_list()
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = ["<div class='tw'><table><thead><tr>"]
            t += [f"<th>{inline_md(h)}</th>" for h in header]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline_md(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_list()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline_md(m.group(2))}</h{lvl}>")
            i += 1
            continue
        # 分隔线
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            close_list()
            out.append("<hr>")
            i += 1
            continue
        # 列表
        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            if list_open != "ul":
                close_list()
                out.append("<ul>")
                list_open = "ul"
            out.append(f"<li>{inline_md(m.group(1))}</li>")
            i += 1
            continue
        m = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if m:
            if list_open != "ol":
                close_list()
                out.append("<ol>")
                list_open = "ol"
            out.append(f"<li>{inline_md(m.group(1))}</li>")
            i += 1
            continue
        # 引用
        if line.startswith(">"):
            close_list()
            out.append(f"<blockquote>{inline_md(line.lstrip('> ').rstrip())}</blockquote>")
            i += 1
            continue
        # 空行
        if not line.strip():
            close_list()
            i += 1
            continue
        close_list()
        out.append(f"<p>{inline_md(line)}</p>")
        i += 1
    close_list()
    return "\n".join(out)


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box;-webkit-tap-highlight-color:rgba(80,160,255,.2)}
body{margin:0;background:#0f1115;color:#e6e8ee;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
 "PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;padding-bottom:40px}
a{color:#6cb6ff;text-decoration:none}
header{position:sticky;top:0;z-index:9;display:flex;gap:10px;align-items:center;padding:12px 14px;
 background:rgba(15,17,21,.94);backdrop-filter:blur(8px);border-bottom:1px solid #23262e}
header .t{font-weight:600;font-size:17px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 14px;border-radius:10px;background:#1b2333;
 color:#dbe6ff;border:1px solid #2b3550;font-size:15px;white-space:nowrap}
.btn:active{background:#243050}
.btn.p{background:#2f6fed;border-color:#2f6fed;color:#fff}
main{padding:14px;max-width:900px;margin:0 auto}
.row{display:flex;align-items:center;gap:12px;padding:13px 12px;border-radius:12px;background:#161a22;
 margin-bottom:9px;border:1px solid #1f242e}
.row .ic{font-size:22px;width:26px;text-align:center}
.row .meta{flex:1;min-width:0}
.row .nm{font-weight:600;font-size:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .sub{color:#8b93a7;font-size:13px;margin-top:2px}
.row .dl{font-size:14px;padding:8px 12px;border-radius:9px;background:#213047;color:#cfe0ff}
.hint{color:#8b93a7;font-size:13px;margin:16px 4px}
ul.md,ol.md{padding-left:22px}
h1{font-size:22px;margin:.6em 0 .4em}h2{font-size:19px;margin:1.2em 0 .4em;padding-top:.4em;border-top:1px solid #23262e}
h3{font-size:17px;margin:1em 0 .3em}
pre.code{background:#0b0e13;border:1px solid #232a36;border-radius:10px;padding:12px;overflow:auto;
 font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre}
code{background:#1c2230;padding:1px 5px;border-radius:5px;font-size:14px}
pre code{background:none;padding:0}
.tw{overflow:auto;border:1px solid #232a36;border-radius:10px;margin:10px 0}
table{border-collapse:collapse;font-size:14px;min-width:100%}
th,td{padding:8px 11px;border-bottom:1px solid #232a36;text-align:left;white-space:nowrap}
th{background:#18202d;font-weight:600}
blockquote{margin:10px 0;padding:8px 12px;border-left:3px solid #2f6fed;background:#151b26;color:#b9c2d4}
hr{border:0;border-top:1px solid #23262e;margin:18px 0}
.meta-bar{display:flex;gap:10px;flex-wrap:wrap;color:#8b93a7;font-size:13px;margin:0 4px 12px}
img.pv{max-width:100%;border-radius:12px;border:1px solid #232a36}
form.up{border:1px dashed #2b3550;border-radius:12px;padding:14px;margin-top:18px}
input[type=file]{width:100%;color:#aab3c5;padding:8px 0}
button.submit{margin-top:10px;width:100%;padding:12px;border-radius:10px;border:0;background:#2f6fed;color:#fff;font-size:16px}
.badge{font-size:12px;color:#8b93a7;border:1px solid #2b3550;border-radius:999px;padding:1px 8px}
"""


def page(title: str, body: str, back: str | None = None, extra_btn: str = "",
         refresh: int | None = None) -> bytes:
    head = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">{f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ''}<title>{html.escape(title)}</title>
<style>{CSS}</style></head><body><header>
{'<a class="btn" href="' + back + '">← 返回</a>' if back else '<span style="font-size:20px">📂</span>'}
<span class="t">{html.escape(title)}</span>{extra_btn}</header><main>{body}</main></body></html>"""
    return head.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "share/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("[share] " + fmt % args, flush=True)

    # ---------------- 工具
    def _send(self, code: int, body: bytes, ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code: int, msg: str):
        self._send(code, page(f"{code}", f"<p class='hint'>{html.escape(msg)}</p>",
                              back=f"{BASE}/{TOKEN}/"))

    def _safe(self, rel: str) -> Path | None:
        rel = urllib.parse.unquote(rel).lstrip("/")
        p = (ROOT / rel).resolve()
        if p == ROOT or str(p).startswith(str(ROOT)):
            return p
        return None

    def _token_ok(self, first: str) -> bool:
        return secrets.compare_digest(first or "", TOKEN)

    # ---------------- 路由
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        parts = [x for x in u.path.split("/") if x != ""]
        q = urllib.parse.parse_qs(u.query)
        if not parts:  # / -> 跳 token 目录
            return self._send(302, b"", headers={"Location": f"{BASE}/{TOKEN}/"})
        tok, rest = parts[0], parts[1:]
        if not self._token_ok(tok):
            return self._err(404, "链接不对：缺少正确的访问 token。")
        # 列表
        if not rest:
            return self._index()
        kind = rest[0]
        if kind == "app":
            return self._app_page(rest[1:], q)
        if kind == "static":
            return self._static_asset(rest[1:])
        if kind == "zip":
            return self._zip()
        if kind == "monitor" and not rest[1:]:
            return self._monitor_page()
        if kind == "arch" and not rest[1:]:
            return self._arch_page()
        if kind == "cost" and not rest[1:]:
            return self._cost_page()
        if kind == "risk" and not rest[1:]:
            return self._risk_page()
        if kind == "diagram" and not rest[1:]:
            return self._arch_page()
        if kind == "api" and len(rest) >= 2:
            if rest[1] == "p2":
                # 版本探测：旧后端没有这条路由（404），前端据此识别"线上是旧进程"
                if rest[2:3] == ["version"]:
                    return self._api_version()
                # 兼容旧契约：队列视角的 jobs 挪到 /api/p2/queue/jobs*
                if rest[2:5] == ["queue", "jobs"]:
                    return self._api_p2(["jobs"] + rest[5:], q)
                if rest[2:3] == ["jobs"]:
                    return self._api_jobs(rest[3:], q)
                if rest[2:3] == ["registry"]:
                    return self._api_registry(rest[3:], q)
                return self._api_p2(rest[2:], q)
            if rest[1] == "status":
                return self._send(200, json.dumps(self._api_status(), ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            if rest[1] == "arch":
                return self._send(200, self._api_arch().encode(), "image/svg+xml; charset=utf-8")
            if rest[1] == "cost":
                return self._send(200, json.dumps(self._api_cost(), ensure_ascii=False,
                                                  default=str).encode(),
                                  "application/json; charset=utf-8")
            if rest[1] == "risk":
                p3 = ROOT / "reports" / "risk" / "latest.json"
                data = p3.read_bytes() if p3.exists() else b'{"checks":[]}'
                return self._send(200, data, "application/json; charset=utf-8")
            if rest[1] == "log":
                q2 = urllib.parse.parse_qs(u.query)
                name = (q2.get("name") or ["lab.log"])[0]
                lines = int((q2.get("lines") or ["200"])[0] or 200)
                text, err = self._api_log(name, lines)
                return self._send(200, (text or err).encode("utf-8"), "text/plain; charset=utf-8")
        if kind in ("architecture.png", "architecture.svg") and not rest[1:]:
            p2 = ROOT / "reports" / kind
            if p2.exists():
                data = p2.read_bytes()
                fn = kind
                disp = ("attachment; filename=\"%s\"; filename*=UTF-8''%s"
                        % (fn, urllib.parse.quote(fn)))
                return self._send(200, data, mimetypes.guess_type(fn)[0] or
                                  "application/octet-stream", {"Content-Disposition": disp})
        if kind in ("f", "raw") and len(rest) >= 2:
            rel = "/".join(rest[1:])
            return self._file(rel, inline=(kind == "raw" or q.get("raw") == ["1"]),
                              force_dl=q.get("dl") == ["1"])
        if kind == "upload":
            return self._upload_page(q.get("ok", [""])[0])
        return self._err(404, "路径不存在")

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        parts = [x for x in u.path.split("/") if x != ""]
        if len(parts) >= 2 and self._token_ok(parts[0]) and parts[1] == "upload":
            return self._receive_upload()
        if len(parts) == 6 and self._token_ok(parts[0]) and parts[1:4] == ["api", "p2", "jobs"] \
                and parts[5] == "cancel":
            return self._api_jobs([parts[4], "cancel"], {})
        if len(parts) == 4 and self._token_ok(parts[0]) and parts[1:4] == ["api", "p2", "uploads"]:
            return self._api_upload()
        if len(parts) == 4 and self._token_ok(parts[0]) and parts[1:4] == ["api", "p2", "jobs"]:
            return self._api_job_create()
        return self._err(404, "路径不存在")

    # ---------------- 页面
    def _index(self):
        dirs, files = [], []
        for p in sorted(ROOT.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name.startswith(".") or p.name == "__pycache__":
                continue
            (dirs if p.is_dir() else files).append(p)
        rows = []
        for p in dirs + files:
            link = f"{BASE}/{TOKEN}/f/{urllib.parse.quote(p.name)}"
            dl = f"{link}?dl=1"
            rows.append(
                f"<div class='row'><a class='ic' href='{link}'>{icon_of(p)}</a>"
                f"<a class='meta' href='{link}'><div class='nm'>{html.escape(p.name)}</div>"
                f"<div class='sub'>{'目录' if p.is_dir() else size_of(p)} · {mtime_of(p)}</div></a>"
                f"<a class='dl' href='{dl}'>下载</a></div>"
            )
        total = sum(p.stat().st_size for p in files)
        body = (
            f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px'>"
            f"<a class='btn p' href='{BASE}/{TOKEN}/monitor'>📊 监控</a>"
            f"<a class='btn' href='{BASE}/{TOKEN}/cost'>💰 成本记录</a>"
            f"<a class='btn' href='{BASE}/{TOKEN}/risk'>🛡️ 风险监控</a>"
            f"<a class='btn' href='{BASE}/{TOKEN}/arch'>🗺️ 实时架构图</a>"
            f"<a class='btn' href='{BASE}/{TOKEN}/f/reports/lab/gallery-inline.html'>🖼️ 画廊</a>"
            f"<a class='btn' href='{BASE}/{TOKEN}/f/reports/lab/REPORT.md'>📋 数据表</a>"
            "</div>" + ("".join(rows) or "<p class='hint'>空目录</p>")
        )
        body += (f"<div class='hint'>共 {len(files)} 个文件 · {human(total)} · "
                 f"点文件名在线预览，点「下载」保存到平板</div>")
        body += (f"<div class='hint'><a href='{BASE}/{TOKEN}/'>↻ 刷新列表</a>"
                 f"　（我刚生成的新文件会出现在这里）</div>")
        body += f"""<div style="display:flex;gap:10px;margin-top:6px">
<a class="btn p" style="flex:1;justify-content:center" href="{BASE}/{TOKEN}/zip">🗜️ 打包下载全部</a>
<a class="btn" style="flex:1;justify-content:center" href="{BASE}/{TOKEN}/upload">⬆️ 上传文件给我</a>
</div>
<div class="hint">这是服务器上工作目录的只读副本。要看/改源文件，直接在对话里说。</div>"""
        extra = f'<a class="btn p" href="{BASE}/{TOKEN}/zip">全部 zip</a>'
        self._send(200, page("文件 · 智云视频工作流", body, extra_btn=extra))

    def _file(self, rel: str, inline: bool, force_dl: bool):
        p = self._safe(rel)
        if p is None or not p.exists():
            return self._err(404, "文件不存在")
        if p.is_dir():
            rows = []
            for c in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if c.name.startswith(".") or c.name == "__pycache__":
                    continue
                link = f"{BASE}/{TOKEN}/f/{urllib.parse.quote(rel.strip('/') + '/' + c.name)}"
                rows.append(f"<div class='row'><a class='ic' href='{link}'>{icon_of(c)}</a>"
                            f"<a class='meta' href='{link}'><div class='nm'>{html.escape(c.name)}</div>"
                            f"<div class='sub'>{size_of(c)} · {mtime_of(c)}</div></a>"
                            f"<a class='dl' href='{link}?dl=1'>下载</a></div>")
            parent = "/".join(rel.strip("/").split("/")[:-1])
            back = f"{BASE}/{TOKEN}/f/{urllib.parse.quote(parent)}" if parent else f"{BASE}/{TOKEN}/"
            return self._send(200, page(f"📁 {rel}", "".join(rows) or "<p class='hint'>空目录</p>",
                                        back=back))
        ext = p.suffix.lower()
        if force_dl or (ext not in TEXT_EXT and ext not in IMG_EXT) or not inline:
            if force_dl or (ext not in TEXT_EXT and ext not in IMG_EXT):
                data = p.read_bytes()
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                fn = p.name
                disp = ("attachment; filename=\"%s\"; filename*=UTF-8''%s"
                        % (fn.encode("ascii", "ignore").decode() or "file", urllib.parse.quote(fn)))
                return self._send(200, data, ctype, {"Content-Disposition": disp})
        if ext in IMG_EXT or inline:
            data = p.read_bytes()
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            return self._send(200, data, ctype)
        # 文本预览
        raw = p.read_text(encoding="utf-8", errors="replace")
        if ext == ".md":
            content = f"<article>{md_to_html(raw)}</article>"
        else:
            content = f"<pre class='code'><code>{html.escape(raw)}</code></pre>"
        dl = f"{BASE}/{TOKEN}/f/{urllib.parse.quote(rel)}?dl=1"
        parent = "/".join(rel.strip("/").split("/")[:-1])
        back = f"{BASE}/{TOKEN}/f/{urllib.parse.quote(parent)}" if parent else f"{BASE}/{TOKEN}/"
        body = (f"<div class='meta-bar'><span>{size_of(p)}</span><span>{mtime_of(p)}</span>"
                f"<span class='badge'>{ext or 'file'}</span></div>{content}")
        extra = f'<a class="btn p" href="{dl}">下载</a>'
        self._send(200, page(p.name, body, back=back, extra_btn=extra))

    def _zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in ROOT.rglob("*"):
                if p.is_dir() or "__pycache__" in p.parts or p.name.startswith("."):
                    continue
                if p.parent.name == "_incoming":
                    continue
                z.write(p, p.relative_to(ROOT).as_posix())
        data = buf.getvalue()
        fn = "智云视频工作流-tools.zip"
        disp = ("attachment; filename=\"cs-tools.zip\"; filename*=UTF-8''%s"
                % urllib.parse.quote(fn))
        self._send(200, data, "application/zip", {"Content-Disposition": disp})

    # ---------------- P2.0 Registry 只读接口
    # ---------------- 图片控制台（M1 骨架）
    def _app_page(self, rest: list[str], q: dict):
        from webui import api as webapi, pages as webpages

        base = BASE
        page = rest[0] if rest else "create"
        try:
            if page in ("", "create"):
                body = webpages.create_page(base, TOKEN, (q.get("workflow") or [""])[0])
            elif page == "jobs" and len(rest) == 1:
                body = webpages.jobs_page(base, TOKEN, (q.get("state") or [""])[0],
                                          (q.get("q") or [""])[0])
            elif page == "jobs" and len(rest) == 2:
                body = webpages.detail_page(base, TOKEN, rest[1])
            elif page in ("gallery", "registry"):
                body = webpages.shell(base, TOKEN, f"/app/{page}", page,
                                      f"<div class='card'><p class='empty'>{page} 页面在 M3/M4 交付</p></div>")
            else:
                return self._err(404, "页面不存在")
        except webapi.ApiError as e:
            return self._err(e.code, e.msg)
        return self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def _static_asset(self, rest: list[str]):
        from webui import assets as webassets

        name = rest[0] if rest else ""
        if name == "app.css":
            body, ct = webassets.CSS.encode("utf-8"), "text/css; charset=utf-8"
        elif name == "app.js":
            body, ct = webassets.JS.encode("utf-8"), "application/javascript; charset=utf-8"
        else:
            return self._err(404, "资源不存在")
        return self._send(200, body, ct, {"Cache-Control": "public, max-age=60"})

    def _api_jobs(self, rest: list[str], q: dict):
        from webui import api as webapi

        def _json(obj, code=200):
            return self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        try:
            if rest == ["zip"]:
                ids = [x for x in ((q.get("ids") or [""])[0]).split(",") if x]
                data = webapi.zip_artifacts(ids)
                return self._send(200, data, "application/zip",
                                  {"Content-Disposition": 'attachment; filename="jobs.zip"'})
            if not rest:
                items = webapi.list_jobs(
                    limit=int((q.get("limit") or ["20"])[0] or 20),
                    state_prefix=(q.get("state") or [""])[0],
                    workflow=(q.get("workflow") or [""])[0],
                    q=(q.get("q") or [""])[0])
                return _json({"items": items, "count": len(items)})
            jid = rest[0]
            if len(rest) == 1:
                return _json(webapi.get_job(jid))
            if rest[1] == "cancel" and len(rest) == 2:
                return _json(webapi.cancel_job(jid))
            return _json({"error": "路径不存在"}, 404)
        except webapi.ApiError as e:
            return _json({"error": e.msg}, e.code)
        except Exception as e:  # noqa: BLE001
            return _json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api_upload(self):
        """multipart 上传输入图（img2img / inpaint），返回内容寻址文件名。"""
        from webui import api as webapi

        def _json(obj, code=200):
            return self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        if "boundary=" not in ctype or length <= 0:
            return _json({"error": "需要 multipart 上传"}, 400)
        if length > 25 * 1048576:
            return _json({"error": "请求体过大"}, 413)
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        raw = self.rfile.read(length if length < 25 * 1048576 else 25 * 1048576)
        for chunk in raw.split(b"--" + boundary.encode()):
            if b"\r\n\r\n" not in chunk:
                continue
            head, data = chunk.split(b"\r\n\r\n", 1)
            if b"filename=" not in head:
                continue
            m = re.search(rb'filename="([^"]*)"', head)
            name = m.group(1).decode("utf-8", "ignore") if m else "upload.png"
            data = data[:-2] if data.endswith(b"\r\n") else data
            try:
                info = webapi.save_upload(name, data)
            except webapi.ApiError as e:
                return _json({"error": e.msg}, e.code)
            return _json(info, 201)
        return _json({"error": "没有文件字段"}, 400)

    def _api_job_create(self):
        from webui import api as webapi

        def _json(obj, code=200):
            return self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 1_000_000:
                return _json({"error": "请求体为空或过大"}, 400)
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(body, dict):
                return _json({"error": "请求体应为 JSON 对象"}, 400)
            data = webapi.create_job(str(body.get("workflow") or ""),
                                     body.get("params") or {},
                                     run=bool(body.get("run")))
            return _json(data, 201)
        except webapi.ApiError as e:
            return _json({"error": e.msg}, e.code)
        except json.JSONDecodeError:
            return _json({"error": "JSON 解析失败"}, 400)
        except Exception as e:  # noqa: BLE001
            return _json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api_version(self):
        """GET /api/p2/version —— 前端启动时探测后端版本。

        为什么要有：线上 /files/ 可能指向一个还没更新的旧进程。前端打到旧进程时，
        /app/* 和 /api/p2/registry 会 404，但其它接口仍返回旧形状的数据，很容易被
        误当成"新后端有 bug"。这里一次性返回构建标识 + 能力清单，前端据此提示。
        """
        reg_stats: dict = {}
        try:
            from tools.p20 import registry as reg
            reg_stats = reg.stats()
        except Exception as e:  # noqa: BLE001
            reg_stats = {"error": f"{type(e).__name__}: {e}"}
        payload = {
            "service": "dsh-share",
            "ok": True,
            "api": API_VERSION,
            "contract": API_CONTRACT,
            "build": build_info(),
            "started_at": _iso(STARTED_AT),
            "uptime_s": int(time.time() - STARTED_AT),
            "pid": os.getpid(),
            "bind": f"{BIND}:{PORT}",
            "base": BASE,
            "root": str(ROOT),
            "capabilities": CAPABILITIES,
            "features": {"console": True, "registry": True, "jobs": True,
                         "uploads": True, "zip": True, "version": True},
            "registry": reg_stats,
            "docs": {"openapi": "/pub/api/openapi.json",
                     "handoff": "/pub/p21/07-API契约与Mock.html"},
        }
        self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def _api_registry(self, sub: list[str], q: dict):
        """Workflow/Model Registry 只读 API（UI 用）。

        GET /api/p2/registry                     统计 + 目录摘要
        GET /api/p2/registry/workflows           工作流列表
        GET /api/p2/registry/workflows/<key>     工作流详情（参数/绑定/兼容性）
        GET /api/p2/registry/models              模型列表
        GET /api/p2/registry/models/<key>        模型详情
        GET /api/p2/registry/compat              兼容性矩阵
        GET /api/p2/registry/resolve?workflow=&model=&params=<urlencoded JSON>
        GET /api/p2/registry/stats               库统计
        """
        def _json(obj, code=200):
            return self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")

        try:
            from tools.p20 import registry as reg, render as p20render, config_schema as cs
        except Exception as e:
            return _json({"error": f"P20 模块不可用: {e}"}, 500)

        def _wf_summary(w: dict) -> dict:
            return {"key": w.get("key"), "name": w.get("name"), "version": w.get("version"),
                    "kind": w.get("kind"), "model_key": w.get("model_key"),
                    "graph_sha256": w.get("graph_sha256"), "compat": w.get("compat"),
                    "params": sorted((w.get("params") or {}).keys()),
                    "nodes": len(w.get("graph_obj") or {})}

        def _wf_detail(w: dict) -> dict:
            d = {k: v for k, v in w.items() if k != "graph"}
            d["nodes"] = len(w.get("graph_obj") or {})
            d["params"] = w.get("params") or {}
            return d

        try:
            if not sub or sub == ["stats"]:
                return _json({"stats": reg.stats(), "workflows": len(reg.list_workflows()),
                              "models": len(reg.list_models())})
            if sub[0] == "workflows":
                if len(sub) == 1:
                    return _json({"total": len(reg.list_workflows()),
                                  "workflows": [_wf_summary(w) for w in reg.list_workflows()]})
                key = sub[1]
                try:
                    w = reg.get_workflow(key)
                except reg.RegistryError as e:
                    return _json({"error": str(e)}, 404)
                return _json({"workflow": _wf_detail(w),
                              "compat": reg.compat_for(key, (w.get("model") or {}).get("key"))})
            if sub[0] == "models":
                if len(sub) == 1:
                    rows = []
                    for m in reg.list_models():
                        rows.append({"key": m.get("key"), "name": m.get("name"),
                                     "version": m.get("version"), "kind": m.get("kind"),
                                     "family": m.get("family"),
                                     "files": {r: f.get("name") for r, f in
                                               (m.get("files") or {}).items()}})
                    return _json({"total": len(rows), "models": rows})
                key = sub[1]
                try:
                    return _json({"model": reg.get_model(key)})
                except reg.RegistryError as e:
                    return _json({"error": str(e)}, 404)
            if sub[0] == "compat":
                return _json({"matrix": reg.compat_matrix()})
            if sub[0] == "resolve":
                wkey = (q.get("workflow") or [None])[0]
                if not wkey:
                    return _json({"error": "缺少 workflow 参数"}, 400)
                mkey = (q.get("model") or [None])[0]
                raw = (q.get("params") or ["{}"])[0] or "{}"
                try:
                    params = json.loads(raw)
                except json.JSONDecodeError:
                    return _json({"error": "params 不是合法 JSON"}, 400)
                allow = (q.get("allow_experimental") or ["0"])[0] in ("1", "true", "yes")
                try:
                    r = reg.resolve(wkey, model_key=mkey, params=params,
                                    allow_experimental=allow)
                    graph, meta = p20render.render(r["workflow"], r["model"], r["params"], "preview",
                                                   graph=r["workflow"].get("graph_obj"))
                except (reg.RegistryError, cs.ConfigError, p20render.RenderError) as e:
                    return _json({"error": str(e)}, 400)
                out = {"workflow": r["workflow"]["key"], "model": r["model_key"],
                       "compat": r["compat"], "params": r["params"],
                       "defaults_applied": r["defaults_applied"],
                       "expect": p20render.expect_for(r["workflow"], r["params"]),
                       "render": {k: v for k, v in meta.items() if k != "params"}}
                if (q.get("graph") or ["0"])[0] in ("1", "true"):
                    out["graph"] = graph
                return _json(out)
            return _json({"error": f"未知 registry 路径: {sub}"}, 404)
        except Exception as e:
            return _json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---------------- 监控接口
    def _api_p2(self, sub: list[str], q: dict):
        """P2 只读 API（给 UI）：jobs / jobs/<id> / audit / events / queue。

        全部 GET、只读、沿用 token 鉴权；轮询用 since 游标（audit id / events seq）。
        """
        def _json(obj):
            return self._send(200, json.dumps(obj, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")

        try:
            from tools.p2 import jobqueue as qq
            from tools.p1 import store as p1store
        except Exception as e:
            return self._err(500, f"P2 模块不可用: {e}")

        def _job_view(it: dict) -> dict:
            jid = it["job_id"]
            j = p1store.read_json(p1store.job_file(jid)) or {}
            man = p1store.read_json(p1store.job_file(jid, "manifest.json")) or {}
            return {
                "job_id": jid,
                "queue_state": it.get("state"),
                "job_state": j.get("state"),
                "model": (j.get("model") or {}).get("key") or it.get("model_key"),
                "kind": j.get("kind"),
                "attempts": it.get("attempts"),
                "defers": it.get("defers"),
                "instance_id": it.get("instance_id"),
                "exit_code": it.get("exit_code"),
                "cost": it.get("cost"),
                "gpu_minutes": it.get("gpu_minutes"),
                "params_hash": it.get("params_hash"),
                "verdict": man.get("verdict"),
                "checks": man.get("checks") or {},
                "artifacts": [a.get("rel") for a in (man.get("artifacts") or [])],
                "enqueued_at": it.get("enqueued_at"),
                "started_at": it.get("started_at"),
                "ended_at": it.get("ended_at"),
                "last_error": (it.get("last_error") or "")[:300],
            }

        # /api/p2/queue
        if sub == ["queue"]:
            return _json({"stats": qq.stats(),
                          "items": [{k: it.get(k) for k in ("job_id", "state", "model_key", "attempts",
                                                            "defers", "cost", "exit_code", "enqueued_at")}
                                    for it in qq.items()]})

        # /api/p2/jobs  |  /api/p2/jobs/<id>
        if sub and sub[0] == "jobs":
            if len(sub) == 1:
                return _json({"jobs": [_job_view(it) for it in qq.items()],
                              "total": qq.stats()["total"]})
            jid = sub[1]
            rows = [it for it in qq.items() if it["job_id"] == jid]
            if not rows:
                return self._err(404, f"作业不存在: {jid}")
            ev = []
            jdir = p1store.job_dir(jid)
            try:
                with open(jdir / "events.jsonl", encoding="utf-8") as f:
                    ev = [json.loads(x) for x in f.read().splitlines() if x.strip()]
            except FileNotFoundError:
                pass
            return _json({"job": _job_view(rows[0]), "events": ev})

        # /api/p2/audit?job=&since=&limit=
        if sub == ["audit"]:
            since = int((q.get("since") or ["0"])[0] or 0)
            limit = int((q.get("limit") or ["200"])[0] or 200)
            job_id = (q.get("job") or [None])[0]
            rows = qq.audit_since(since, job_id, min(max(limit, 1), 1000))
            return _json({"rows": rows, "next": (rows[-1]["id"] if rows else since)})

        # /api/p2/events?job=&since=  （job 状态机的增量事件，供 UI 轮询）
        if sub == ["events"]:
            job_id = (q.get("job") or [None])[0]
            since = int((q.get("since") or ["0"])[0] or 0)
            if not job_id:
                return self._err(400, "缺少 job 参数")
            jdir = p1store.job_dir(job_id)
            try:
                with open(jdir / "events.jsonl", encoding="utf-8") as f:
                    evs = [json.loads(x) for x in f.read().splitlines() if x.strip()]
            except FileNotFoundError:
                return self._err(404, f"作业不存在: {job_id}")
            evs = [e for e in evs if int(e.get("seq") or 0) > since]
            return _json({"events": evs, "next": (evs[-1]["seq"] if evs else since)})

        return self._err(404, "未知的 P2 API 路径")

    def _api_status(self):
        out = {"ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "balance": None, "instances": [], "stock": {}, "lab": {}, "services": {},
               "errors": []}
        for svc in ("cs-files", "nginx"):   # cs-lab 已退役（P1/P2 取代），不再监控
            try:
                r = subprocess.run(["systemctl", "is-active", svc], capture_output=True,
                                   text=True, timeout=5)
                out["services"][svc] = r.stdout.strip() or "unknown"
            except Exception:
                out["services"][svc] = "unknown"
        try:
            from compshare.cs import CS, load_config
            cs = CS()
            b = cs.balance()
            out["balance"] = b.get("AmountAvailable")
            now = time.time()
            for i in cs.instances():
                st = i.get("StartTime") or i.get("CreateTime") or now
                hourly = float(i.get("_hourly") or 0)
                running = i.get("State") in ("Running", "Rebooting")
                out["instances"].append({
                    "id": i.get("UHostId"), "name": i.get("Name"), "state": i.get("State"),
                    "state_cn": i.get("_state_cn"), "gpu": i.get("GpuType"),
                    "count": i.get("GPU"), "cpu": i.get("CPU"),
                    "mem_gb": int((i.get("Memory") or 0) // 1024),
                    "hourly": hourly, "ip": cs.public_ip(i),
                    "ssh": cs.ssh_of(i),
                    "uptime_h": round((now - float(st)) / 3600, 2) if running else 0,
                    "cost": round(hourly * (now - float(st)) / 3600, 2) if running else 0,
                    "stop_at": i.get("SchedulerStopTime") or 0,
                })
            inv = cs.inventory_raw().get("GpuInventory") or {}
            for scope, label in (("Exclusive", "独占"), ("Spot", "抢占")):
                d = {}
                for zid, gpus in (inv.get(scope) or {}).items():
                    for g, n in (gpus or {}).items():
                        if n:
                            d.setdefault(g, 0)
                            d[g] += n
                out["stock"][label] = d
        except Exception as e:
            out["errors"].append(f"API: {type(e).__name__}: {e}")
        try:
            p2p = PROJ / "reports" / "p2" / "status2.json"
            dd = json.loads(p2p.read_text(encoding="utf-8")) if p2p.exists() else {}
            by = ((dd.get("queue") or {}).get("by_state") or {})
            out["p2"] = {
                "jobs": dd.get("jobs") or {},
                "cost": float(dd.get("cost_total") or 0),
                "queued": (by.get("queued") or {}).get("n", 0),
                "running": (by.get("claimed") or {}).get("n", 0) + (by.get("running") or {}).get("n", 0),
                "done": (by.get("done") or {}).get("n", 0),
                "terminal_failed": (by.get("terminal_failed") or {}).get("n", 0),
                "reclaimed": (by.get("reclaimed") or {}).get("n", 0),
                "items": (dd.get("items") or [])[-8:],
            }
        except Exception as e:
            out["errors"].append(f"p2: {e}")
            out["p2"] = {}
        return out

    def _api_arch(self):
        """实时重算架构图（20s 内复用），返回 SVG 文本。"""
        try:
            if time.time() - ARCH_TS["t"] > 20 or not ARCH_SVG.exists():
                subprocess.run([sys.executable, str(PROJ / "tools" / "arch.py"), "--no-sync"],
                               capture_output=True, timeout=60)
                ARCH_TS["t"] = time.time()
            svg = ARCH_SVG.read_text(encoding="utf-8")
            svg = svg[svg.index("<svg"):].replace('width="1280" height="900"',
                                                 'width="100%" height="auto"')
            return svg
        except Exception as e:
            return f"<div style='color:#f88'>架构图生成失败: {html.escape(str(e))}</div>"

    def _ledger(self) -> list[dict]:
        rows = []
        p = PROJ / ".compshare" / "ledger.jsonl"
        if not p.exists():
            return rows
        for line in p.read_text(encoding="utf-8").splitlines()[-5000:]:
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        return rows

    def _api_cost(self):
        out = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time(),
               "balance": None, "today": 0.0, "week": 0.0, "total": 0.0,
               "groups": [], "by_gpu": {}, "by_model": {}, "days": {}, "recent": []}
        try:
            from compshare.cs import CS
            out["balance"] = CS().balance().get("AmountAvailable")
        except Exception as e:
            out["errors"] = [str(e)]
        led = self._ledger()
        today = time.strftime("%Y-%m-%d")
        week_ago = time.time() - 7 * 86400
        for r in led:
            ev = r.get("event")
            if ev == "balance":
                d = str(r.get("at", ""))[:10]
                if d:
                    out["days"].setdefault(d, {"start": r.get("balance"), "end": r.get("balance"),
                                               "cost": 0.0})
                    out["days"][d]["end"] = r.get("balance")
                continue
            c = r.get("cost")
            if ev in ("lab_group",) and c is not None:
                c = float(c or 0)
                at = str(r.get("at", ""))[:10]
                out["total"] += c
                if at == today:
                    out["today"] += c
                if r.get("ts", 0) >= week_ago:
                    out["week"] += c
                if at in out["days"]:
                    out["days"][at]["cost"] += c
                g = r.get("gpu") or "?"
                out["by_gpu"][g] = out["by_gpu"].get(g, 0) + c
            elif ev == "cost" and c is not None:
                c = float(c or 0)
                out["total"] += c
                at = str(r.get("at", ""))[:10]
                if at == today:
                    out["today"] += c
                if r.get("ts", 0) >= week_ago:
                    out["week"] += c
                if at in out["days"]:
                    out["days"][at]["cost"] += c
        # 按机器（并补齐账本里还没有的历史数据）
        ledger_gids = {r.get("gid") for r in led if r.get("event") == "lab_group"}
        try:
            st = json.loads((PROJ / "lab" / "state.json").read_text(encoding="utf-8"))
            for gid, g in (st.get("groups") or {}).items():
                w = g.get("workloads") or {}
                cost_g = round(float(g.get("cost_estimate") or 0), 3)
                if gid not in ledger_gids and cost_g:
                    out["total"] += cost_g
                    out["week"] += cost_g
                    started = int(g.get("started") or 0)
                    if time.strftime("%Y-%m-%d", time.localtime(started)) == today:
                        out["today"] += cost_g
                        out["days"].setdefault(today, {"start": None, "end": None,
                                                       "cost": 0.0})["cost"] += cost_g
                    gg = g.get("gpu") or "?"
                    out["by_gpu"][gg] = out["by_gpu"].get(gg, 0) + cost_g
                out["groups"].append({
                    "id": gid, "gpu": g.get("gpu"), "zone": g.get("zone"),
                    "status": g.get("status"), "boot": g.get("t_to_running_s"),
                    "cost": cost_g,
                    "ok": sum(1 for x in w.values() if x.get("status") == "ok"),
                    "bad": sum(1 for x in w.values() if x.get("status") == "failed"),
                    "wall_min": g.get("wall_min"),
                })
                for wn, x in w.items():
                    if x.get("status") != "ok":
                        continue
                    m = wn.split("_")[0]
                    b = out["by_model"].setdefault(m, {"n": 0, "warm": 0.0, "warm_n": 0,
                                                       "gpus": []})
                    b["n"] += 1
                    if x.get("t_warm_s"):
                        b["warm"] += float(x["t_warm_s"]); b["warm_n"] += 1
                    if g.get("gpu") not in b["gpus"]:
                        b["gpus"].append(g.get("gpu"))
        except Exception as e:
            out.setdefault("errors", []).append(str(e))
        for m, b in out["by_model"].items():
            b["avg_warm"] = round(b["warm"] / b["warm_n"], 2) if b["warm_n"] else None
            b.pop("warm", None)
        out["recent"] = led[-40:][::-1]
        return out

    def _api_log(self, name: str, lines: int = 200):
        p = LOG_FILES.get(name)
        if not p or not p.exists():
            return "", f"没有这个日志: {name}"
        try:
            r = subprocess.run(["tail", "-n", str(max(1, min(lines, 2000))), str(p)],
                               capture_output=True, text=True, timeout=10)
            return r.stdout, ""
        except Exception as e:
            return "", str(e)

    # ---------------- 页面（服务端渲染，JS 只负责自动刷新）
    def _tbl(self, headers, rows):
        h = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
            for r in rows)
        return f"<div class='tw'><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>"

    def _monitor_page(self):
        d = self._api_status()
        root = f"{BASE}/{TOKEN}"
        kpi = "".join([
            f"<div><b>{html.escape(str(d.get('balance')))}</b><span>余额 ¥</span></div>",
            f"<div><b>{len(d.get('instances') or [])}</b><span>实例</span></div>",
            f"<div><b>{(d.get('p2') or {}).get('queued', 0)}</b><span>队列待跑</span></div>",
            f"<div><b>{(d.get('p2') or {}).get('terminal_failed', 0)}</b><span>终态失败</span></div>",
        ] + [f"<div><b style='font-size:14px'>{html.escape(str(v))}</b><span>{html.escape(k)}</span></div>"
             for k, v in (d.get("services") or {}).items()])

        inst_rows = [[i.get("id"), i.get("name"), i.get("state_cn"), f"{i.get('gpu')}x{i.get('count')}",
                      f"{i.get('cpu')}C/{i.get('mem_gb')}G", i.get("hourly"), f"{i.get('uptime_h')}h",
                      f"¥{i.get('cost')}", i.get("ip") or "-"] for i in (d.get("instances") or [])]
        inst = self._tbl(["实例", "名称", "状态", "卡", "规格", "¥/h", "已跑", "已花", "公网IP"],
                         inst_rows) if inst_rows else "<div class='hint'>当前没有实例（等货中）</div>"

        stock = " · ".join(f"{k} → " + (" / ".join(f"{g}:{n}" for g, n in v.items()) or "全 0")
                           for k, v in (d.get("stock") or {}).items())
        p2 = d.get("p2") or {}
        p2_rows = [[it.get("job_id"), it.get("state"), it.get("model") or "-",
                    f"¥{float(it.get('cost') or 0):.3f}",
                    (it.get("verdict") or "-"),
                    "".join("✓" if v else "✗" for v in (it.get("checks") or {}).values())]
                   for it in (p2.get("items") or [])]
        lab = self._tbl(["作业", "队列状态", "模型", "成本", "判定", "复验"],
                        p2_rows) if p2_rows else "<div class='hint'>队列为空（batchctl submit 后可在此看到）</div>"

        log_text, _ = self._api_log("watchdog.log", 200)
        body = f"""
<div class='hint' id='diag'>快照 {html.escape(d.get('at', ''))} · 页面每 20 秒自动重载</div>
<div class='kpi'>{kpi}</div>
<h2>实例</h2>{inst}
<h2>库存</h2><div class="hint">{html.escape(stock or '无现货')}</div>
<h2>P2 队列（最近 8 项 · 数据源 status2.json）</h2>{lab}
<h2>日志（watchdog.log 尾部）</h2>
<pre class='code' style='max-height:46vh;overflow:auto;white-space:pre-wrap'>{html.escape(log_text[-20000:])}</pre>
<div class='hint'>完整数据：<a href="{root}/f/reports/p2/status2.json">status2.json</a> ·
 <a href="{root}/f/reports/p2/P2-联调与对账报告.md">联调与对账报告</a> ·
 <a href="{root}/cost">成本记录</a> · <a href="{root}/risk">风险监控</a> ·
 <a href="{root}/arch">实时架构图</a></div>
<h2>架构图（每 20 秒重算）</h2>
<div style='border:1px solid #222833;border-radius:12px;overflow:auto;background:#0d1013'>{self._api_arch()}</div>
"""
        self._send(200, page("监控 · 智云实例", body, back=f"{root}/", refresh=20,
                             extra_btn=f"<a class='btn' href='{root}/cost'>成本</a>"
                                       f"<a class='btn' href='{root}/arch'>架构图</a>"))

    def _arch_page(self):
        root = f"{BASE}/{TOKEN}"
        body = f"""<div class='hint'>每 20 秒重新生成一次（服务端按当前实例/库存/进度重算）。</div>
<div style='border:1px solid #222833;border-radius:12px;overflow:auto;background:#0d1013'>
{self._api_arch()}</div>
<div class='hint' style='display:flex;gap:8px;flex-wrap:wrap;align-items:center'>
 <a class="btn" href="{root}/f/reports/architecture.png?dl=1">⬇ 下载 PNG</a>
 <a class="btn" href="{root}/f/reports/architecture.svg?dl=1">⬇ 下载 SVG</a>
 <a class="btn" href="{root}/monitor">📊 监控</a>
 <a class="btn" href="{root}/cost">💰 成本</a></div>
"""
        self._send(200, page("架构图 · 实时", body, back=f"{root}/", refresh=20,
                             extra_btn=f"<a class='btn' href='{root}/cost'>成本</a>"))

    def _risk_page(self):
        root = f"{BASE}/{TOKEN}"
        p = ROOT / "reports" / "risk" / "latest.json"
        d = {}
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                d = {}
        if not d:
            body = ("<div class='hint'>还没有扫描结果。等下一次定时扫描（每 10 分钟），"
                    "或在控制机执行 <code>python3 tools/riskmon.py</code>。</div>")
            return self._send(200, page("风险监控", body, back=f"{root}/"))
        s = d.get("summary") or {}
        icon = {"high": "🔴", "medium": "🟡", "info": "⚪", "ok": "🟢"}
        cn = {"high": "高危", "medium": "关注", "info": "提示", "ok": "正常"}
        kpi = "".join([
            f"<div><b>{s.get('high', 0)}</b><span>高危</span></div>",
            f"<div><b>{s.get('medium', 0)}</b><span>关注</span></div>",
            f"<div><b>{s.get('info', 0)}</b><span>提示</span></div>",
            f"<div><b>{s.get('ok', 0)}</b><span>正常</span></div>",
            f"<div><b style='font-size:15px'>{html.escape(cn.get(d.get('worst'), '—'))}</b>"
            f"<span>最坏级别</span></div>",
        ])
        rows = [[c.get("id"),
                 f"{icon.get(c.get('level'), '')} {cn.get(c.get('level'), c.get('level'))}",
                 c.get("name"), c.get("value"), c.get("detail"),
                 c.get("action") or "-"] for c in (d.get("checks") or [])]
        body = f"""
<div class='hint'>扫描时间 {html.escape(str(d.get('ts', '')))} · 每 60 秒自动重载 · 规则 {len(rows)} 条，由 tools/riskmon.py 定义</div>
<div class='kpi'>{kpi}</div>
{self._tbl(["ID", "级别", "项目", "当前值", "说明", "处置"], rows)}
<div class='hint'>告警明细 <a href="{root}/f/reports/risk/alerts.md">alerts.md</a> ·
 原始 JSON <a href="{root}/f/reports/risk/latest.json">latest.json</a> ·
 立即扫描：控制机执行 <code>python3 tools/riskmon.py</code></div>
"""
        self._send(200, page("风险监控", body, back=f"{root}/", refresh=60,
                             extra_btn=f"<a class='btn' href='{root}/cost'>成本</a>"))

    def _cost_page(self):
        d = self._api_cost()
        root = f"{BASE}/{TOKEN}"
        budget = 8.0
        left = max(0.0, budget - float(d.get("total") or 0))
        bal = d.get("balance")
        kpi = "".join([
            f"<div><b>¥{html.escape(str(bal))}</b><span>账户余额</span></div>",
            f"<div><b>¥{float(d.get('today') or 0):.2f}</b><span>今日花费</span></div>",
            f"<div><b>¥{float(d.get('week') or 0):.2f}</b><span>近 7 天</span></div>",
            f"<div><b>¥{float(d.get('total') or 0):.2f}</b><span>实验累计</span></div>",
            f"<div><b>¥{left:.2f}</b><span>预算剩余 / ¥{budget:g}</span></div>",
        ])
        g_rows = [[g.get("id"), g.get("gpu"), g.get("zone"), g.get("status"),
                   g.get("boot") or "-", g.get("ok"), g.get("bad"), g.get("wall_min") or "-",
                   f"¥{g.get('cost')}"] for g in
                  sorted(d.get("groups") or [], key=lambda x: -(x.get("cost") or 0))]
        gpu_rows = [[k, f"¥{v:.2f}"] for k, v in sorted((d.get("by_gpu") or {}).items())]
        model_rows = [[k, v.get("n"), v.get("avg_warm") if v.get("avg_warm") is not None else "-",
                       " / ".join(v.get("gpus") or [])] for k, v in
                      sorted((d.get("by_model") or {}).items())]
        days = sorted((d.get("days") or {}).items())
        mx = max([float(v.get("cost") or 0) for _, v in days] + [0.001])
        bars = ""
        for day, v in days:
            cost = float(v.get("cost") or 0)
            w = int(100 * cost / mx)
            bars += (f"<div style='display:flex;gap:8px;align-items:center;margin:4px 0'>"
                     f"<span style='width:92px;color:#8b93a7;font-size:12px'>{html.escape(day)}</span>"
                     f"<span style='flex:1;background:#1b2230;border-radius:6px;overflow:hidden;height:16px'>"
                     f"<span style='display:block;height:16px;width:{w}%;background:#2f6fed'></span></span>"
                     f"<span style='width:74px;text-align:right;font-size:12px'>¥{cost:.2f}</span>"
                     f"<span style='width:96px;text-align:right;font-size:12px;color:#6b7385'>"
                     f"余 {html.escape(str(v.get('end')))}</span></div>")
        led_rows = [[r.get("at"), r.get("event"), r.get("gid") or r.get("uhost") or r.get("name") or "-",
                     f"¥{r['cost']:.2f}" if r.get("cost") is not None else "-",
                     f"¥{r['balance']}" if r.get("balance") is not None else "-"]
                    for r in (d.get("recent") or [])]
        body = f"""
<div class='hint'>快照 {html.escape(d.get('at', ''))} · 每 20 秒自动重载 · 数据源 ledger.jsonl + lab/state.json</div>
<div class='kpi'>{kpi}</div>
<h2>按机器</h2>{self._tbl(["机器","卡型","可用区","状态","就绪s","成功","失败","时长min","花费"], g_rows)}
<h2>按卡型</h2>{self._tbl(["卡型","花费"], gpu_rows)}
<h2>按模型</h2>{self._tbl(["模型","样本","平均预热s","卡型"], model_rows)}
<h2>每日花费</h2>{bars or "<div class='hint'>还没有余额快照</div>"}
<h2>账本最近事件</h2>{self._tbl(["时间","事件","实例/机器","花费","余额"], led_rows)}
<div class='hint'><a href="{root}/monitor">监控</a> · <a href="{root}/arch">架构图</a> ·
 <a href="{root}/f/reports/lab/REPORT.md">数据表</a> ·
 预算在 <code>lab_runner.py --budget</code> 与 <code>csctl up --daily-budget</code> 里</div>
"""
        self._send(200, page("成本记录", body, back=f"{root}/", refresh=20,
                             extra_btn=f"<a class='btn' href='{root}/monitor'>监控</a>"))

    def _upload_page(self, ok: str):
        msg = "<p class='hint'>✅ 上传成功，已保存到 _incoming/</p>" if ok else ""
        body = msg + f"""
<div class="hint">选好文件后点上传，会存到服务器 <code>share/_incoming/</code>，我这边随时能看。</div>
<form class="up" method="post" action="{BASE}/{TOKEN}/upload" enctype="multipart/form-data">
  <input type="file" name="file" multiple>
  <button class="submit" type="submit">上传</button>
</form>"""
        self._send(200, page("上传文件", body, back=f"{BASE}/{TOKEN}/"))

    def _receive_upload(self):
        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length") or 0)
        if "boundary=" not in ctype or length <= 0:
            return self._err(400, "上传格式不支持")
        if length > MAX_UPLOAD:
            return self._err(413, f"文件太大（上限 {MAX_UPLOAD // 1048576}MB）")
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"')
        raw = self.rfile.read(length)
        dest = ROOT / "_incoming"
        dest.mkdir(exist_ok=True)
        saved = 0
        for chunk in raw.split(b"--" + boundary.encode()):
            if b"\r\n\r\n" not in chunk:
                continue
            head, data = chunk.split(b"\r\n\r\n", 1)
            if b"filename=" not in head:
                continue
            m = re.search(rb'filename="([^"]*)"', head)
            if not m:
                continue
            name = m.group(1).decode("utf-8", "ignore").replace("/", "_").replace("\\", "_") or "upload.bin"
            data = data[:-2] if data.endswith(b"\r\n") else data
            if len(data) > MAX_UPLOAD:
                continue
            if not re.match(r"^[\w.\-\u4e00-\u9fa5 ()（）]+$", name):
                name = re.sub(r"[^\w.\-]", "_", name)
            (dest / name).write_bytes(data)
            saved += 1
        self._send(302, b"", headers={"Location": f"{BASE}/{TOKEN}/upload?ok=1" if saved
                                      else f"{BASE}/{TOKEN}/upload"})


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[share] root={ROOT} bind={BIND}:{PORT} url={BASE}/{TOKEN}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
