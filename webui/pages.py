# -*- coding: utf-8 -*-
"""控制台页面（服务端渲染）：创作台 / 任务列表 / 任务详情。

约定：所有函数接收 base（如 https://host/files/<token>）与 token，返回完整 HTML。
"""
from __future__ import annotations

import json

from . import api, components as C

NAV = [
    ("创作台", "/app/create", ""),
    ("视频", "/app/video", ""),
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


def _wf_picker(base: str, token: str, workflows: list[dict], current: str, *, page: str) -> str:
    opts = []
    for w in workflows:
        sel = " selected" if w["key"] == current else ""
        label = w["name"]
        if (w.get("compat") or "") not in ("verified", "equivalent") and "尚未开发" not in label:
            label += "（尚未开发）"
        opts.append(f'<option value="{C.esc(w["key"])}"{sel}>{C.esc(label)}</option>')
    return (f'<div class="field"><label for="wf">工作流</label>'
            f'<select id="wf" onchange="location.href=\'{base}/{token}{page}?workflow=\'+this.value">'
            f'{"".join(opts)}</select></div>')


def _grouped_fields(params: dict) -> str:
    from . import i18n as I
    buckets: dict[str, list[str]] = {}
    for name, spec in I.ordered_params(params):
        buckets.setdefault(I.group_of(name), []).append(C.field(name, spec))
    html = []
    for g in I.GROUP_ORDER:
        items = buckets.get(g)
        if not items:
            continue
        html.append(f'<fieldset class="grp"><legend>{C.esc(g)}</legend>{"".join(items)}</fieldset>')
    return "".join(html)


def _wf_head(wf: dict) -> str:
    from . import i18n as I
    text, cls = I.status_text(wf.get("compat"))
    desc = wf.get("description") or ""
    for tag in ("（✅ 可使用）", "（🚧 尚未开发）", "（⛔ 已替代，勿用）"):
        desc = desc.replace(tag, "")
    return (f'<div class="wf-meta"><span class="pill {cls}">{C.esc(text)}</span>'
            f'<code>{C.esc(wf["key"])}</code> v{C.esc(wf["version"])}</div>'
            f'<p class="desc">{C.esc(desc.strip())}</p>')


def _recent_html(base: str, token: str, limit: int = 8) -> str:
    recent = api.list_jobs(limit=limit)
    items = []
    for r in recent:
        art = r.get("first_artifact_url") or ""
        if art.endswith(".mp4"):
            thumb = f'<video src="{base}/{token}{art}" muted playsinline preload="metadata"></video>'
        elif art:
            thumb = f'<img src="{base}/{token}{art}" loading="lazy">'
        else:
            thumb = '<span class="ph">无图</span>'
        items.append(
            f'<a class="rec" href="{base}/{token}/app/jobs/{r["job_id"]}">{thumb}'
            f'<span class="recm">{C.badge(r["state"])}<b>{C.esc(r["workflow"])}</b>'
            f'<span class="muted">{C.esc(C.human_time(r["created_at"]))}</span></span></a>')
    return "".join(items) or '<p class="empty">还没有作业</p>'


def _create_body(base: str, token: str, wf: dict, kind: str) -> str:
    wf_json = json.dumps({"key": wf["key"], "params": wf["params"]}, ensure_ascii=False)
    page = "/app/create" if kind == "image" else "/app/video"
    warn = ""
    if kind == "video":
        warn = ('<div class="notice">视频作业按 GPU 时长计费，一条 5 秒视频约 ¥0.3–0.5；'
                '提交前请确认「立即执行」。</div>')
    return f"""
<div class="page-head">
  <h1>{'创作台' if kind == 'image' else '视频台'}</h1>
  <p class="sub">{'选工作流 → 填参数 → 提交，产物出现在右侧' if kind == 'image' else '文/图生视频，产物为 MP4'}</p>
</div>
{warn}
<div class="grid">
  <form class="card form" id="create-form">
    {_wf_picker(base, token, api.image_workflows() if kind == 'image' else api.video_workflows(), wf['key'], page=page)}
    {_wf_head(wf)}
    {_grouped_fields(wf["params"] or {})}
    <div class="actions">
      <label class="check"><input type="checkbox" id="run-now"> <span>提交后立即执行（占用 GPU，产生费用）</span></label>
      <div class="row">
        <button class="btn pri" id="submit-btn" type="submit">提交</button>
        <a class="btn" href="{base}/{token}/app/jobs">任务列表</a>
      </div>
      <div class="hint">参数默认值来自 registry；提交前服务端会再校验一次（含兼容性）。</div>
    </div>
  </form>
  <section class="card result">
    <h2>结果</h2>
    <div id="state" class="state muted">还没有提交作业</div>
    <div id="results" class="results"></div>
  </section>
</div>
<h2 class="sec">最近作业</h2>
<div class="recs">{_recent_html(base, token)}</div>
<script>window.__WF__ = {wf_json};</script>
"""


def create_page(base: str, token: str, workflow: str = "") -> str:
    wfs = api.image_workflows()
    if not wfs:
        body = ('<div class="card"><p class="empty">还没有 image 类工作流，'
                '先跑 <code>p20ctl import</code>。</p></div>')
        return shell(base, token, "/app/create", "创作台", body)
    t2i = next((w["key"] for w in wfs if w["key"] == "zimage_turbo_t2i_hq"), wfs[0]["key"])
    i2i = next((w["key"] for w in wfs if w["key"] == "zimage_turbo_img2img"), t2i)
    aliases = {"t2i": t2i, "i2i": i2i}
    requested = aliases.get(workflow or "", workflow or t2i)
    wf = next((w for w in wfs if w["key"] == requested), None) \
        or next(w for w in wfs if w["key"] == t2i)
    tabs = (f'<div class="tabs">'
            f'<a class="tab {"on" if wf["key"] == t2i else ""}" href="{base}/{token}/app/create?workflow=t2i">文生图</a>'
            f'<a class="tab {"on" if wf["key"] == i2i else ""}" href="{base}/{token}/app/create?workflow=i2i">图生图</a>'
            f'<a class="tab" href="{base}/{token}/app/video">视频</a></div>')
    body = tabs + _create_body(base, token, wf, "image")
    return shell(base, token, "/app/create", f"创作台 · {wf['key']}", body)


def video_page(base: str, token: str, workflow: str = "") -> str:
    wfs = api.video_workflows()
    if not wfs:
        body = '<div class="card"><p class="empty">还没有 video 类工作流。</p></div>'
        return shell(base, token, "/app/video", "视频台", body)
    requested = workflow or wfs[0]["key"]
    wf = next((w for w in wfs if w["key"] == requested), wfs[0])
    tabs = (f'<div class="tabs">'
            f'<a class="tab" href="{base}/{token}/app/create?workflow=t2i">文生图</a>'
            f'<a class="tab" href="{base}/{token}/app/create?workflow=i2i">图生图</a>'
            f'<a class="tab on" href="{base}/{token}/app/video">视频</a></div>')
    body = tabs + _create_body(base, token, wf, "video")
    return shell(base, token, "/app/video", f"视频台 · {wf['key']}", body)


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
