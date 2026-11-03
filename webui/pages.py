# -*- coding: utf-8 -*-
"""控制台页面（服务端渲染）：创作台 / 任务列表 / 任务详情。

约定：所有函数接收 base（如 https://host/files/<token>）与 token，返回完整 HTML。
"""
from __future__ import annotations

import json

from . import api, components as C

NAV = [
    ("创作台", "/app/create", ""),
    ("任务", "/app/jobs", ""),
    ("图库", "/app/gallery", "M3"),
    ("工作流与模型", "/app/registry", "M4"),
]


def shell(base: str, token: str, active: str, title: str, body: str,
          head_extra: str = "", body_class: str = "") -> str:
    links = []
    for name, path, badge in NAV:
        url = f"{base}/{token}{path}"
        on = "on" if path.split("?")[0] == active else ""
        off = "off" if badge else ""
        tag = f'<span class="pill idle">{badge}</span>' if badge else ""
        links.append(f'<a class="{on} {off}" href="{url}">{C.esc(name)}{tag}</a>')
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{C.esc(title)}</title>
<link rel="stylesheet" href="{base}/{token}/static/app.css">
{head_extra}
</head><body class="{body_class}">
<header class="top">
  <span class="brand">图片控制台</span>
  <nav class="app">{''.join(links)}</nav>
  <span class="right"><a href="{base}/{token}/">文件</a> · <a href="{base}/{token}/monitor">监控</a></span>
</header>
<main>{body}</main>
<script>window.__BASE__ = "{base}/{token}";</script>
<script src="{base}/{token}/static/app.js"></script>
</body></html>"""


def _wf_picker(base: str, token: str, workflows: list[dict], current: str) -> str:
    opts = []
    for w in workflows:
        sel = " selected" if w["key"] == current else ""
        label = f'{w["key"]} v{w["version"]} · {w.get("compat") or "?"}'
        opts.append(f'<option value="{C.esc(w["key"])}"{sel}>{C.esc(label)}</option>')
    return (f'<div class="field"><label for="wf">工作流</label>'
            f'<select id="wf" onchange="location.href=\'{base}/{token}/app/create?workflow=\'+this.value">'
            f'{"".join(opts)}</select></div>')


def create_page(base: str, token: str, workflow: str = "") -> str:
    wfs = api.image_workflows()
    aliases = {"t2i": next((w["key"] for w in wfs if "img2img" not in w["key"]), ""),
               "i2i": next((w["key"] for w in wfs if "img2img" in w["key"]), "")}
    workflow = aliases.get(workflow, workflow)
    wfs = ([w for w in wfs if "img2img" in w["key"] or True] if workflow else wfs)
    if not wfs:
        body = '<div class="card"><p class="empty">还没有 image 类工作流，先跑 <code>p20ctl import</code>。</p></div>'
        return shell(base, token, "/app/create", "创作台", body)
    wf = next((w for w in wfs if w["key"] == workflow), wfs[0])
    fields = "".join(C.field(name, spec) for name, spec in (wf["params"] or {}).items())
    wf_json = json.dumps({"key": wf["key"], "params": wf["params"]}, ensure_ascii=False)
    recent = api.list_jobs(limit=8)
    rec_items = []
    for r in recent:
        thumb = (f'<img src="{base}/{token}{r["first_artifact_url"]}" loading="lazy">'
                 if r.get("first_artifact_url") else '<span class="ph">无图</span>')
        rec_items.append(
            f'<a class="rec" href="{base}/{token}/app/jobs/{r["job_id"]}">{thumb}'
            f'<span class="recm">{C.badge(r["state"])}<b>{C.esc(r["workflow"])}</b>'
            f'<span class="muted">{C.esc(C.human_time(r["created_at"]))}</span></span></a>')
    recent_html = "".join(rec_items) or '<p class="empty">还没有作业</p>'
    body = f"""
<h1>创作台</h1>
<div class="row" style="margin-bottom:14px">
  <a class="btn {'pri' if 'img2img' not in wf['key'] else ''}" href="{base}/{token}/app/create?workflow=t2i">文生图</a>
  <a class="btn {'pri' if 'img2img' in wf['key'] else ''}" href="{base}/{token}/app/create?workflow=i2i">图生图</a>
  <a class="btn" href="#" onclick="return false" title="M3">局部重绘 · M3</a>
</div>
<div class="grid">
  <form class="card" id="create-form">
    <h2>工作流与参数</h2>
    {_wf_picker(base, token, wfs, wf["key"])}
    {fields}
    <div class="field"><label class="row"><input type="checkbox" id="run-now" style="width:auto">
      <span>提交后立即执行（会占用 GPU，产生费用）</span></label></div>
    <div class="row">
      <button class="btn pri" id="submit-btn" type="submit">提交</button>
      <a class="btn" href="{base}/{token}/app/jobs">任务列表</a>
    </div>
    <div class="hint">参数默认值来自 registry；提交前服务端会再校验一次（含兼容性）。</div>
  </form>
  <section class="card">
    <h2>结果</h2>
    <div id="state" class="state muted">还没有提交作业</div>
    <div id="results" class="results"></div>
  </section>
</div>
<h2 style="margin-top:26px;font-size:16px">最近作业</h2>
<div class="recs">{recent_html}</div>
<script>window.__WF__ = {wf_json};</script>
"""
    return shell(base, token, "/app/create", f"创作台 · {wf['key']}", body)


def jobs_page(base: str, token: str, state: str = "", q: str = "") -> str:
    rows = api.list_jobs(limit=50, state_prefix=state, q=q)
    trs = []
    for r in rows:
        jid = r["job_id"]
        trs.append(
            f'<tr><td><input type="checkbox" class="job-check" value="{jid}" style="width:auto"></td>'
            f'<td><a href="{base}/{token}/app/jobs/{jid}"><code>{C.esc(jid)}</code></a></td>'
            f'<td>{C.badge(r["state"])}</td><td>{C.esc(r["workflow"])} v{C.esc(r["workflow_version"])}</td>'
            f'<td>{C.esc(r["model"])}</td><td>{r["artifacts"]} 张</td>'
            f'<td>{C.esc(C.human_time(r["created_at"]))}</td>'
            f'<td><a href="{base}/{token}/app/jobs/{jid}">详情</a></td></tr>')
    table = (f'<table><tr><th style="width:36px"></th><th>作业</th><th>状态</th><th>工作流</th><th>模型</th>'
             f'<th>产物</th><th>创建</th><th></th></tr>{"".join(trs)}</table>') if trs else \
        '<div class="card"><p class="empty">还没有作业，去创作台提交一个。</p></div>'
    filters = f"""
<div class="row" style="margin-bottom:12px">
  <button class="btn" id="dl-selected" type="button">⬇ 下载选中作业的图</button>
  <span class="hint">勾选左侧复选框；产物取自已发布的静态副本。</span>
</div>
<form class="row" method="get" action="{base}/{token}/app/jobs" style="margin-bottom:14px">
  <select name="state" style="width:180px">
    <option value="">全部状态</option>
    {''.join(f'<option value="{s}"{" selected" if s == state else ""}>{s}</option>' for s in ("DONE", "RUNNING", "FAILED", "CREATED"))}
  </select>
  <input name="q" value="{C.esc(q)}" placeholder="job_id / prompt 关键词" style="max-width:280px">
  <button class="btn" type="submit">筛选</button>
</form>"""
    return shell(base, token, "/app/jobs", "任务", f"<h1>任务</h1>{filters}{table}")


def detail_page(base: str, token: str, job_id: str) -> str:
    data = api.get_job(job_id)
    job, arts = data["job"], data["artifacts"]
    reg = (job.get("model") or {}).get("registry") or {}
    kv = C.kv_table([
        ("作业", f'<code>{C.esc(job_id)}</code>'),
        ("状态", C.badge(job.get("state"))),
        ("工作流", f'{C.esc(reg.get("workflow_key"))} v{C.esc(reg.get("workflow_version"))}'),
        ("模型", f'{C.esc(reg.get("model_key"))} v{C.esc(reg.get("model_version"))}'),
        ("params_hash", f'<code>{C.esc((job.get("params_hash") or "")[:16])}</code>'),
        ("创建时间", C.esc(C.human_time(job.get("created_at")))),
    ])
    steps = (job.get("steps") or {})
    tl = "".join(f'<li><b>{C.esc(k)}</b> {C.esc(v.get("at") if isinstance(v, dict) else v)}</li>'
                 for k, v in steps.items())
    params = json.dumps(job.get("params") or {}, ensure_ascii=False, indent=2)
    cards = "".join(
        f'<figure class="thumb"><a href="{C.esc(a["url"])}" target="_blank">'
        f'<img src="{C.esc(a["url"])}" alt="{C.esc(a["name"])}"></a>'
        f'<div class="meta"><span>{C.esc(a["name"])}</span>'
        f'<a href="{C.esc(a["url"])}" download>下载</a></div></figure>'
        for a in arts)
    body = f"""
<h1>作业详情</h1>
<div class="grid">
  <div class="card">{kv}
    <h2 style="margin-top:14px">状态时间线</h2>
    <ul class="timeline">{tl or '<li class="muted">无</li>'}</ul>
  </div>
  <div class="card"><h2>产物 {len(arts)} 个</h2>
    <div class="results">{cards or '<p class="empty">还没有产物</p>'}</div>
    <h2 style="margin-top:16px">参数快照</h2>
    <pre style="overflow:auto;font-size:12px">{C.esc(params)}</pre>
  </div>
</div>"""
    return shell(base, token, "/app/jobs", f"作业 {job_id}", body)
