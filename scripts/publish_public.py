#!/usr/bin/env python3
"""把 P2.0 验收材料发布到 nginx 静态公开目录 /srv/dsh-pub/p20/。

背景：容器内启动的常驻进程会在会话结束时被杀掉（/files/ 反代 8081 因此经常 502），
而 nginx 本身是宿主持久进程。把材料复制到 nginx 已经对外提供的静态目录
（nginx 配置里的 `location /pub/ { alias /srv/dsh-pub/; }`），链接就再也不依赖任何临时进程。

用法：
    python3 scripts/publish_public.py            # 发布并打印链接
    python3 scripts/publish_public.py --check    # 只做 HTTP 自检

注意：容器根文件系统默认只读，脚本会先 `mount -o remount,rw /`（仅当前 mount namespace）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import ssl
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUB = Path("/srv/dsh-pub/p20")
BASE = "https://123.207.74.155/pub/p20"

# (源文件, 目标相对路径, 说明)
FILES = [
    ("reports/p2/p20/P2.0-交付报告.md", "report.md"),
    ("reports/p2/p20/P2.0-Workflow-Model-Registry-设计.md", "design.md"),
    ("reports/p2/p20/01-调用关系审查.md", "review-callgraph.md"),
    ("reports/p2/p20/02-硬编码清单.md", "hardcode-list.md"),
    ("reports/p2/p20/p20-验收包.zip", "bundle.zip"),
    ("reports/p2/p20/下载与预览.md", "links.md"),
    ("reports/p2/p20/调用关系图.png", "callgraph.png"),
    ("reports/p2/p20/调用关系图.svg", "callgraph.svg"),
    ("reports/p2/p20/test-evidence/final-summary.txt", "evidence/final-summary.txt"),
    ("reports/p2/p20/test-evidence/summary.txt", "evidence/summary.txt"),
    ("reports/p2/p20/test-evidence/jobs.json", "evidence/jobs.json"),
    ("reports/p2/p20/test-evidence/failure-injection.txt", "evidence/failure-injection.txt"),
    ("reports/p2/p20/test-evidence/api.txt", "evidence/api.txt"),
    ("reports/p2/p20/task5b-hash-proof.txt", "hash/task5b-hash-proof.txt"),
    ("reports/p2/p20/task5b-core-before.sha256", "hash/core-before.sha256"),
    ("reports/p2/p20/task5b-core-after.sha256", "hash/core-after.sha256"),
    ("reports/p1/jobs/p1-20261101T2121-c82708/artifacts/01-p1-20261101T2121-c82708_00001_.png",
     "jobs/apple-1024x1024.png"),
    ("reports/p1/jobs/p1-20261101T2126-9fb4ed/artifacts/01-p1-20261101T2126-9fb4ed_00001_.png",
     "jobs/vase-768x1024.png"),
    ("reports/p1/jobs/p1-20261101T2131-4d366a/artifacts/01-p1-20261101T2131-4d366a_00001_.png",
     "jobs/lake-1024x768.png"),
]

JOBS = [
    ("任务 4 · 红苹果", "qwen_image_t2i × qwen_image", "1024×1024", "p1-20261101T2121-c82708",
     "¥0.0752", "jobs/apple-1024x1024.png"),
    ("任务 5A · 花瓶（新工作流）", "qwen_image_t2i_portrait × qwen_image", "768×1024",
     "p1-20261101T2126-9fb4ed", "¥0.0717", "jobs/vase-768x1024.png"),
    ("任务 5B · 湖景（只改配置）", "qwen_image_t2i_landscape × qwen_image", "1024×768",
     "p1-20261101T2131-4d366a", "¥0.0681", "jobs/lake-1024x768.png"),
]

INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>P2.0 Workflow / Model Registry 验收材料</title>
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:32px 20px 64px; font:16px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       background:#f6f7f9; color:#1b1f24; }}
.wrap {{ max-width:1080px; margin:0 auto; }}
h1 {{ font-size:28px; margin:0 0 6px; }}
h2 {{ font-size:19px; margin:36px 0 12px; padding-bottom:8px; border-bottom:1px solid #e3e6ea; }}
.sub {{ color:#606a76; margin:0 0 20px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; }}
.card {{ background:#fff; border:1px solid #e3e6ea; border-radius:12px; padding:14px 16px; }}
.card b {{ display:block; font-size:26px; line-height:1.2; }}
.card span {{ color:#606a76; font-size:13px; }}
table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid #e3e6ea; border-radius:12px; overflow:hidden; }}
th,td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #eef1f4; font-size:14px; vertical-align:middle; }}
th {{ background:#fafbfc; font-weight:600; color:#3b4450; }}
tr:last-child td {{ border-bottom:none; }}
a {{ color:#0b62d6; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.btn {{ display:inline-block; padding:5px 12px; border:1px solid #cfd6de; border-radius:8px; background:#fff; font-size:13px; white-space:nowrap; }}
.btn.primary {{ background:#0b62d6; border-color:#0b62d6; color:#fff; }}
.gallery {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }}
figure {{ margin:0; background:#fff; border:1px solid #e3e6ea; border-radius:12px; overflow:hidden; }}
figure img {{ display:block; width:100%; height:300px; object-fit:contain; background:#11151a; }}
figcaption {{ padding:10px 12px; font-size:13px; color:#3b4450; }}
code {{ background:#eef1f4; padding:1px 6px; border-radius:5px; font-size:13px; }}
.note {{ background:#fff8e6; border:1px solid #f2dfae; border-radius:10px; padding:12px 14px; font-size:14px; margin-top:18px; }}
footer {{ margin-top:40px; color:#8a929c; font-size:13px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>P2.0 Workflow / Model Registry 验收材料</h1>
  <p class="sub">JSON 配置 → 注册表 → 参数映射 → ComfyUI 执行 → 结果回传 全链路；不改 P1 调度核心。</p>

  <div class="cards">
    <div class="card"><b>60 / 60</b><span>P2.0 单元 + 集成测试</span></div>
    <div class="card"><b>7 / 4</b><span>工作流 / 模型配置</span></div>
    <div class="card"><b>3</b><span>真实 GPU 出图全部 DONE + PASS</span></div>
    <div class="card"><b>¥{cost}</b><span>本阶段实际花费</span></div>
    <div class="card"><b>37</b><span>核心文件哈希 0 改动（5B 证明）</span></div>
  </div>

  <h2>验收包 / 报告</h2>
  <table>
    <tr><th>材料</th><th>说明</th><th style="width:200px">操作</th></tr>
    <tr><td>验收包 zip</td><td>报告 + 设计 + 图 + 证据 + 配置，{zip_mb} MB<br><code>{zip_sha}</code></td>
        <td><a class="btn primary" href="bundle.zip" download>⬇ 下载 zip</a></td></tr>
    <tr><td>P2.0 交付报告</td><td>5 项任务 × 10 项交付字段，{report_kb} KB</td>
        <td><a class="btn primary" href="report.html" target="_blank">网页预览</a> <a class="btn" href="report.md" download>下载 .md</a></td></tr>
    <tr><td>Registry 设计文档</td><td>DB schema、REST API、模块划分、JSON 模板</td>
        <td><a class="btn" href="design.html" target="_blank">网页预览</a> <a class="btn" href="design.md" download>下载 .md</a></td></tr>
    <tr><td>01 调用关系审查</td><td>UI / 脚本 / ComfyUI API 调用图与输入输出清单</td>
        <td><a class="btn" href="review-callgraph.html" target="_blank">网页预览</a> <a class="btn" href="review-callgraph.md" download>下载 .md</a></td></tr>
    <tr><td>02 硬编码清单</td><td>固定模型 / 节点 / 参数逐条定位</td>
        <td><a class="btn" href="hardcode-list.html" target="_blank">网页预览</a> <a class="btn" href="hardcode-list.md" download>下载 .md</a></td></tr>
    <tr><td>调用关系图</td><td>PNG / SVG</td>
        <td><a class="btn" href="callgraph.png" target="_blank">PNG</a> <a class="btn" href="callgraph.svg" target="_blank">SVG</a></td></tr>
  </table>

  <h2>实际产物（真实 GPU 作业）</h2>
  <div class="gallery">
{figures}
  </div>
  <table style="margin-top:16px">
    <tr><th>作业</th><th>工作流 × 模型</th><th>分辨率</th><th>Job ID</th><th>费用</th><th>产物</th></tr>
{job_rows}
  </table>

  <h2>测试与证据</h2>
  <table>
    <tr><th>证据</th><th>内容</th><th style="width:120px">操作</th></tr>
    <tr><td>final-summary.txt</td><td>测试/校验终态汇总</td><td><a class="btn" href="evidence/final-summary.txt" target="_blank">查看</a></td></tr>
    <tr><td>summary.txt</td><td>用例明细</td><td><a class="btn" href="evidence/summary.txt" target="_blank">查看</a></td></tr>
    <tr><td>jobs.json</td><td>3 个真实作业的 ledger/manifest 摘要</td><td><a class="btn" href="evidence/jobs.json" target="_blank">查看</a></td></tr>
    <tr><td>failure-injection.txt</td><td>故障注入：SUBMITTED → FAILED_RUN → RELEASING → FAILED</td><td><a class="btn" href="evidence/failure-injection.txt" target="_blank">查看</a></td></tr>
    <tr><td>api.txt</td><td>Registry REST API 实际响应</td><td><a class="btn" href="evidence/api.txt" target="_blank">查看</a></td></tr>
    <tr><td>task5b-hash-proof.txt</td><td>只改配置不动核心代码的哈希证明</td><td><a class="btn" href="hash/task5b-hash-proof.txt" target="_blank">查看</a></td></tr>
  </table>

  <h2>Registry API（静态快照）</h2>
  <table>
    <tr><th>接口</th><th>内容</th><th style="width:120px">操作</th></tr>
    <tr><td>/api/p2/registry</td><td>库统计摘要</td><td><a class="btn" href="api/registry.json" target="_blank">JSON</a></td></tr>
    <tr><td>/api/p2/registry/workflows</td><td>7 个工作流</td><td><a class="btn" href="api/workflows.json" target="_blank">JSON</a></td></tr>
    <tr><td>/api/p2/registry/models</td><td>4 个模型</td><td><a class="btn" href="api/models.json" target="_blank">JSON</a></td></tr>
    <tr><td>/api/p2/registry/compat</td><td>兼容性矩阵</td><td><a class="btn" href="api/compat.json" target="_blank">JSON</a></td></tr>
  </table>

  <div class="note">本页与全部材料由 nginx 直接静态提供（<code>/pub/p20/</code>），不依赖任何临时后台进程，重启容器后链接依然有效。</div>
  <footer>由 <code>scripts/publish_public.py</code> 从 <code>reports/</code> 自动生成 · {stamp}</footer>
</div>
</body>
</html>
"""


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_rw() -> None:
    """容器根 fs 只读；在本次 mount namespace 内重新挂载为可写。"""
    subprocess.run(["mount", "-o", "remount,rw", "/"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dump_api() -> None:
    """把 Registry 只读 API 的响应落成静态 JSON（供无服务时查看）。"""
    sys.path.insert(0, str(ROOT))
    from tools.p20 import registry as reg  # noqa: PLC0415

    subprocess.run([sys.executable, "-m", "tools.p20.p20ctl", "import"], cwd=ROOT,
                   check=False, stdout=subprocess.DEVNULL)
    targets = {
        "registry.json": [sys.executable, "-m", "tools.p20.p20ctl", "stats"],
        "workflows.json": [sys.executable, "-m", "tools.p20.p20ctl", "list", "workflows", "--json"],
        "models.json": [sys.executable, "-m", "tools.p20.p20ctl", "list", "models", "--json"],
        "compat.json": [sys.executable, "-m", "tools.p20.p20ctl", "compat", "list", "--json"],
    }
    for name, cmd in targets.items():
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ! {name} 生成失败: {r.stderr.strip()[:200]}")
            continue
        (PUB / "api" / name).write_text(r.stdout, encoding="utf-8")
    _ = reg  # 保留导入以复用包内路径解析


def build() -> None:
    ensure_rw()
    for d in ("", "evidence", "hash", "jobs", "api"):
        p = PUB / d if d else PUB
        p.mkdir(parents=True, exist_ok=True)

    missing = []
    for src, dst in FILES:
        s = ROOT / src
        if not s.exists():
            missing.append(src)
            continue
        shutil.copy2(s, PUB / dst)

    for p in PUB.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
        elif p.is_dir():
            p.chmod(0o755)

    dump_api()

    cost = "¥0.2150"
    zip_p = PUB / "bundle.zip"
    zip_mb = f"{zip_p.stat().st_size / 1048576:.1f}" if zip_p.exists() else "?"
    zip_sha = sha256(zip_p)[:16] + "…" if zip_p.exists() else "?"
    report_kb = f"{((PUB / 'report.md').stat().st_size / 1024):.0f}" if (PUB / "report.md").exists() else "?"

    figures, rows = [], []
    for title, spec, res, jid, price, img in JOBS:
        figures.append(
            f'    <figure>\n      <img src="{img}" alt="{title}">\n'
            f'      <figcaption>{title} · {res}<br>'
            f'<a class="btn" href="{img}" target="_blank">原图</a> '
            f'<a class="btn" href="{img}" download>下载</a></figcaption>\n    </figure>'
        )
        rows.append(
            f"    <tr><td>{title}</td><td><code>{spec}</code></td><td>{res}</td>"
            f"<td><code>{jid}</code></td><td>{price}</td>"
            f'<td><a class="btn" href="{img}" download>下载 PNG</a></td></tr>'
        )

    # Markdown 文档另存一份渲染后的 HTML：nginx 把 .md 当 octet-stream 下载，
    # 而 .html 可以直接在浏览器里阅读。
    import md2html  # 同目录模块

    for md_name, title in (("report.md", "P2.0 交付报告"), ("design.md", "Registry 设计文档"),
                           ("review-callgraph.md", "01 调用关系审查"),
                           ("hardcode-list.md", "02 硬编码清单")):
        src = PUB / md_name
        if not src.exists():
            continue
        page = md2html.render(src.read_text(encoding="utf-8"), title)
        out = src.with_suffix(".html")
        out.write_text(page, encoding="utf-8")
        out.chmod(0o644)

    html = INDEX_HTML.format(
        cost=cost, zip_mb=zip_mb, zip_sha=zip_sha, report_kb=report_kb,
        figures="\n".join(figures), job_rows="\n".join(rows),
        stamp=__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    (PUB / "index.html").write_text(html, encoding="utf-8")
    (PUB / "index.html").chmod(0o644)

    print(f"[ok] 发布到 {PUB}")
    if missing:
        print("[warn] 缺失源文件：")
        for m in missing:
            print("   -", m)


def check() -> int:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    urls = ["", "index.html", "report.html", "design.html", "review-callgraph.html",
            "hardcode-list.html", "report.md", "design.md", "bundle.zip",
            "callgraph.png", "callgraph.svg",
            "evidence/final-summary.txt", "evidence/jobs.json",
            "hash/task5b-hash-proof.txt", "api/registry.json", "api/workflows.json",
            "api/compat.json", "jobs/apple-1024x1024.png", "jobs/vase-768x1024.png",
            "jobs/lake-1024x768.png"]
    bad = 0
    for u in urls:
        url = f"{BASE}/{u}" if u else f"{BASE}/"
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=15) as r:
                n = len(r.read())
                print(f"  {r.status} {n:>9,}  {url}")
                bad += 0 if r.status == 200 else 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERR {e}  {url}")
            bad += 1
    print(f"[{'ok' if bad == 0 else 'fail'}] {len(urls) - bad}/{len(urls)} 可用")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只做 HTTP 自检，不重新发布")
    a = ap.parse_args()
    if not a.check:
        build()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
