#!/usr/bin/env python3
"""把 P2.1 计划材料发布到 nginx 静态公开目录 /srv/dsh-pub/p21/。

与 P2.0 的 publish_public.py 同一思路：材料复制进 nginx 已有 `location /pub/`
（alias /srv/dsh-pub/）的静态目录，不依赖任何常驻进程。

用法：python3 scripts/publish_p21.py [--check]
"""
from __future__ import annotations

import argparse
import html
import re
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "reports/p2/p21"
PUB = Path("/srv/dsh-pub/p21")
BASE = "https://123.207.74.155/pub/p21"

DOCS = [
    ("00-基线一致性核查.md", "00-基线一致性核查.html", "基线一致性核查",
     "分支/提交、测试复跑、工作区偏差、冻结动作"),
    ("02-5.1-基准文生图-设计与选型.md", "02-5.1-基准文生图-设计与选型.html", "5.1 基准文生图 设计与选型",
     "模型选型对比、10 参数覆盖矩阵、图变更、配置草案、测试计划"),
    ("01-P2.1-执行计划.md", "01-P2.1-执行计划.html", "P2.1 执行计划",
     "6 条验收映射、M0–M4 里程碑、20 连测方案、成本预算、风险登记册"),
    ("03-P0-P1-遗留问题排期.md", "03-P0-P1-遗留问题排期.html", "P0/P1 遗留问题排期",
     "13 项逐条评估阻塞性、修复窗口、验证方式"),
]

INDEX = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>P2.1 图片生成流水线 · 计划与设计</title>
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; padding:32px 20px 64px; background:#f6f7f9; color:#1b1f24;
  font:16px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
.wrap {{ max-width:1040px; margin:0 auto; }}
h1 {{ font-size:27px; margin:0 0 6px; }}
h2 {{ font-size:19px; margin:34px 0 12px; padding-bottom:8px; border-bottom:1px solid #e3e6ea; }}
.sub {{ color:#606a76; margin:0 0 20px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:12px; }}
.card {{ background:#fff; border:1px solid #e3e6ea; border-radius:12px; padding:14px 16px; }}
.card b {{ display:block; font-size:24px; line-height:1.25; }}
.card span {{ color:#606a76; font-size:13px; }}
table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid #e3e6ea;
  border-radius:12px; overflow:hidden; }}
th,td {{ text-align:left; padding:11px 12px; border-bottom:1px solid #eef1f4; font-size:14px; vertical-align:top; }}
th {{ background:#fafbfc; font-weight:600; color:#3b4450; }}
tr:last-child td {{ border-bottom:none; }}
a {{ color:#0b62d6; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.btn {{ display:inline-block; padding:5px 12px; border:1px solid #cfd6de; border-radius:8px;
  background:#fff; font-size:13px; white-space:nowrap; }}
.btn.primary {{ background:#0b62d6; border-color:#0b62d6; color:#fff; }}
.note {{ background:#fff8e6; border:1px solid #f2dfae; border-radius:10px; padding:12px 14px;
  font-size:14px; margin-top:18px; }}
footer {{ margin-top:36px; color:#8a929c; font-size:13px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>P2.1 图片生成流水线 · 计划与设计</h1>
  <p class="sub">基线 <code>feature-/creative-engine @ ca2a352</code> · 计划 v1 · 2026-11-02 提交，待确认后进入 M0</p>

  <div class="cards">
    <div class="card"><b>14</b><span>工作日（11-03 ~ 11-20）</span></div>
    <div class="card"><b>M0–M4</b><span>5 个里程碑，逐个有出口判据</span></div>
    <div class="card"><b>3 套</b><span>文生图 / 图生图 / 局部重绘</span></div>
    <div class="card"><b>20 连测</b><span>每套：成功率·耗时·显存·成本</span></div>
    <div class="card"><b>≤ ¥10</b><span>GPU 预算上限</span></div>
    <div class="card"><b>3 项</b><span>硬阻塞（M0/M1 内清零）</span></div>
  </div>

  <h2>文档</h2>
  <table>
    <tr><th style="width:250px">文档</th><th>内容</th><th style="width:190px">阅读</th></tr>
{doc_rows}
  </table>

  <h2>关键结论</h2>
  <table>
    <tr><th style="width:180px">问题</th><th>结论</th></tr>
    <tr><td>环境是否与交付一致</td><td>一致。60/60、42/42、P1 12 PASS/1 SKIP、深度校验 19/19、Registry 7 工作流/4 模型全部复跑通过；<b>唯一偏差是 P2.0 成果尚未提交且仓库无远端</b>，M0 第一件事就是提交并打 tag。</td></tr>
    <tr><td>5.1 选哪套</td><td><b>Qwen-Image 作质量基准（~35s/张）+ SDXL juggernaut 作速度与生态底座（3.0s/张）</b>；Flux 因冷启 60s、23.6GB 贴边 24G 显存不进基准；SD1.5 质量不达标。</td></tr>
    <tr><td>10 个参数差多少</td><td>v1 已有 7 个，缺 <b>sampler、scheduler、batch_size、模型选择</b> 共 4 项，v2 全部补齐。</td></tr>
    <tr><td>遗留问题是否阻塞</td><td>硬阻塞 3 项：P2.0 未提交、失败作业不写 ledger、写 API 未实现（原 P2 项被 P2.1 拉成关键路径）。均在 M0/M1 前 3 天解决，不影响 11-10 的 5.1 出口。</td></tr>
    <tr><td>5.4 做什么</td><td>本期只做 <b>LoRA / 图片放大 / 面部修复</b>三项；ControlNet、IP-Adapter、角色一致性、变体生成顺延 P2.2，避免挤掉 5.1–5.3 的稳定性。</td></tr>
  </table>

  <h2>证据（复跑原始输出）</h2>
  <table>
    <tr><th>文件</th><th>内容</th><th style="width:90px">查看</th></tr>
    <tr><td>tests.txt</td><td>P2.0 60 / P2 42 / P1 cases 汇总</td><td><a class="btn" href="evidence/tests.txt" target="_blank">打开</a></td></tr>
    <tr><td>validate.txt</td><td>p20ctl validate + jobctl validate --deep</td><td><a class="btn" href="evidence/validate.txt" target="_blank">打开</a></td></tr>
    <tr><td>git-baseline.txt</td><td>分支/HEAD/远端/工作区状态</td><td><a class="btn" href="evidence/git-baseline.txt" target="_blank">打开</a></td></tr>
  </table>

  <div class="note">本页与全部文档为 nginx 静态提供（<code>/pub/p21/</code>），不依赖临时后台进程。计划为 v1 草案，确认范围与预算后进入 M0。</div>
  <footer>由 <code>scripts/publish_p21.py</code> 生成 · {stamp}</footer>
</div>
</body>
</html>
"""


def ensure_rw() -> None:
    subprocess.run(["mount", "-o", "remount,rw", "/"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build() -> None:
    ensure_rw()
    sys.path.insert(0, str(ROOT / "scripts"))
    import md2html

    (PUB / "evidence").mkdir(parents=True, exist_ok=True)

    rows = []
    for md_name, html_name, title, desc in DOCS:
        src = SRC / md_name
        if not src.exists():
            print(f"  ! 缺失 {src}")
            continue
        text = src.read_text(encoding="utf-8")
        (PUB / md_name).write_text(text, encoding="utf-8")
        (PUB / html_name).write_text(md2html.render(text, title), encoding="utf-8")
        qu = quote(html_name)
        rows.append(
            f'    <tr><td>{html.escape(title)}</td><td>{html.escape(desc)}</td>'
            f'<td><a class="btn primary" href="{qu}" target="_blank">网页阅读</a> '
            f'<a class="btn" href="{quote(md_name)}" download>下载 .md</a></td></tr>'
        )

    for f in (SRC / "evidence").glob("*"):
        if f.is_file():
            (PUB / "evidence" / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    stamp = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M")
    (PUB / "index.html").write_text(
        INDEX.format(doc_rows="\n".join(rows), stamp=stamp), encoding="utf-8")

    for p in PUB.rglob("*"):
        if p.is_file():
            p.chmod(0o644)
        elif p.is_dir():
            p.chmod(0o755)

    print(f"[ok] 发布到 {PUB}")


def check() -> int:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    urls = ["", "index.html"] + [quote(h) for _, h, _, _ in DOCS] + [
        quote(m) for m, _, _, _ in DOCS
    ] + ["evidence/tests.txt", "evidence/validate.txt", "evidence/git-baseline.txt"]
    bad = 0
    for u in urls:
        url = f"{BASE}/{u}" if u else f"{BASE}/"
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=15) as r:
                print(f"  {r.status} {len(r.read()):>8,}  {url}")
                bad += 0 if r.status == 200 else 1
        except Exception as e:  # noqa: BLE001
            print(f"  ERR {e}  {url}")
            bad += 1
    print(f"[{'ok' if bad == 0 else 'fail'}] {len(urls) - bad}/{len(urls)} 可用")
    return 0 if bad == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if not a.check:
        build()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
