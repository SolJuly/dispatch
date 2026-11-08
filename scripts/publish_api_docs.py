#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 OpenAPI 契约发布到 /pub/api/（含一份静态接口清单页 + Mock 用法）。

用法：python3 scripts/publish_api_docs.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "openapi.json"
PUB = Path("/srv/dsh-pub/api")
BASE = "https://123.207.74.155/pub/api"

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>P2.1 后端接口契约</title>
<style>
:root{{color-scheme:light dark}}
body{{margin:0;background:#f6f7f9;color:#1b1f24;font:15px/1.65 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:1080px;margin:0 auto;padding:28px 18px 64px}}
h1{{font-size:24px;margin:0 0 6px}}h2{{font-size:18px;margin:28px 0 10px;padding-bottom:6px;border-bottom:1px solid #e3e6ea}}
.sub{{color:#606a76;margin:0 0 18px}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e6ea;border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #eef1f4;font-size:13.5px;vertical-align:top}}
th{{background:#fafbfc;color:#3b4450}}
tr:last-child td{{border-bottom:none}}
code{{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12.5px}}
.ok{{color:#0f7b3f;font-weight:600}}.plan{{color:#9a6b00;font-weight:600}}
a{{color:#0b62d6;text-decoration:none}}a:hover{{text-decoration:underline}}
pre{{background:#11151a;color:#e6edf3;padding:12px 14px;border-radius:10px;overflow:auto;font-size:13px}}
.note{{background:#fff8e6;border:1px solid #f2dfae;border-radius:10px;padding:12px 14px;margin-top:16px}}
</style></head><body><main>
<h1>P2.1 后端接口契约</h1>
<p class="sub">OpenAPI 3.1 · <a href="openapi.json">openapi.json</a> · 可直接导入 Swagger Editor / Postman / Apifox</p>
<div class="note">
<b>鉴权</b>：token 在路径里（<code>/files/&lt;token&gt;/api/p2/...</code>），不是 Header/Cookie。<br>
<b>Mock</b>：<code>python3 scripts/mock_api.py</code> → <code>http://127.0.0.1:8099/files/23e00ec2/</code>，建作业/取消/上传有状态。<br>
<b>标注</b>：<span class="ok">已实现</span> 可直接联调；<span class="plan">规划中</span> 先用 Mock，联调前会补。
</div>
<h2>接口清单</h2>
<table>
<tr><th style="width:70px">方法</th><th>路径</th><th>说明</th><th style="width:90px">状态</th></tr>
{rows}
</table>
<h2>前端接入顺序建议</h2>
<pre>1. GET  /api/p2/registry/workflows        拿工作流 + 参数契约，渲染表单/下拉
2. POST /api/p2/uploads                   图生图/局部重绘：先传图拿 in-&lt;sha8&gt;.png
3. POST /api/p2/jobs                      {{workflow, params, run:false}} 先只建作业
4. GET  /api/p2/jobs/{{id}}                 轮询状态与产物（terminal=true 停止）
5. artifact.url                           直接用静态地址展示/下载（/pub/jobs/...）
6. GET  /api/p2/jobs?state=&amp;q=&amp;limit=     任务列表；zip?ids= 批量下载</pre>
<h2>示例数据</h2>
<p>每个接口的示例都在 <code>openapi.json</code> 的 <code>examples.default.value</code> 里；Mock 服务直接读它。
真实数据可对比 <a href="../bench/">连测产物</a> 与 <a href="../jobs/index.json">作业索引</a>。</p>
</main></body></html>
"""


def main() -> int:
    subprocess.run(["mount", "-o", "remount,rw", "/"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PUB.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SPEC, PUB / "openapi.json")
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    rows = []
    for path, item in sorted(spec["paths"].items()):
        for method, op in item.items():
            if method not in ("get", "post"):
                continue
            cls = "ok" if op["x-status"] == "implemented" else "plan"
            label = "已实现" if op["x-status"] == "implemented" else "规划中"
            rows.append(f'<tr><td><code>{method.upper()}</code></td>'
                        f'<td><code>{path}</code></td><td>{op["summary"]}</td>'
                        f'<td><span class="{cls}">{label}</span></td></tr>')
    (PUB / "index.html").write_text(PAGE.format(rows="\n".join(rows)), encoding="utf-8")
    for p in PUB.iterdir():
        p.chmod(0o644)
    print(f"[ok] {PUB} · {len(rows)} 条接口 · {BASE}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
