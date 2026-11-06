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
    ("图库", "/app/gallery", ""),
    ("工作流与模型", "/app/registry", ""),
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
  <span class="brand">控制台</span>
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
    est_json = _est_payload(wf)
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
    {_budget_block(wf)}
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
<script>window.__WF__ = {wf_json}; window.__EST__ = {est_json};</script>
"""



def _est_payload(wf: dict) -> str:
    from . import budget as B
    secs, rf, rw, rh = B.MEASURED.get(wf.get("key"), B.DEFAULT_REF)
    return json.dumps({"seconds": secs, "frames": rf, "width": rw, "height": rh,
                       "price": B.PRICE_PER_HOUR, "boot": B.BOOT_COST}, ensure_ascii=False)


def _budget_block(wf: dict) -> str:
    from . import budget as B
    est = B.estimate(wf, wf.get("params") or {})
    return (f'<div class="budget"><div class="field"><label for="budget">预算上限（元）</label>'
            f'<input id="budget" type="number" step="0.1" min="0" value="{B.DEFAULT_BUDGET:.1f}">'
            f'<div class="hint" id="est-hint">预估 <b id="est-text">{C.esc(est["hint"])}</b>'
            f'{"（实测基准）" if est["measured"] else "（经验估算）"}；改帧数/尺寸会实时更新，超过上限会拒绝提交。</div></div></div>')


PAGE_SIZE = 48


def gallery_page(base: str, token: str, workflow: str = "", page: int = 1) -> str:
    page = max(1, int(page or 1))
    rows = api.list_jobs(limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE, workflow=workflow)
    groups: dict[str, list[str]] = {}
    for r in rows:
        art = r.get("first_artifact_url") or ""
        if art.endswith(".mp4"):
            media = f'<video src="{base}/{token}{art}" muted playsinline preload="metadata"></video>'
        elif art:
            media = f'<img src="{base}/{token}{art}" loading="lazy">'
        else:
            media = '<span class="ph">无图</span>'
        day = (r.get("created_at") or "")[:10] or "未知日期"
        groups.setdefault(day, []).append(
            f'<a class="gcard" href="{base}/{token}/app/jobs/{r["job_id"]}">{media}'
            f'<span class="gmeta">{C.badge(r["state"])}<b>{C.esc(r["workflow"])}</b>'
            f'<span class="muted">{C.esc(C.human_time(r["created_at"]))}</span></span></a>')
    cards = [(d, "".join(v)) for d, v in sorted(groups.items(), reverse=True)]
    wfs = sorted({r["workflow"] for r in rows if r["workflow"]})
    opts = ''.join(f'<option value="{C.esc(w)}"{" selected" if w == workflow else ""}>{C.esc(w)}</option>'
                   for w in wfs)
    filt = (f'<form class="row" method="get" action="{base}/{token}/app/gallery" style="margin-bottom:14px">'
            f'<select name="workflow" style="width:280px"><option value="">全部工作流</option>{opts}</select>'
            f'<button class="btn" type="submit">筛选</button></form>')
    blocks = "".join(f'<h3 class="day">{C.esc(d)}</h3><div class="gallery">{g}</div>' for d, g in cards) \
        or '<p class="empty">还没有产物</p>'
    prev_link = (f'<a class="btn" href="{base}/{token}/app/gallery?workflow={C.esc(workflow)}&page={page-1}">上一页</a>'
                 if page > 1 else "")
    next_link = (f'<a class="btn" href="{base}/{token}/app/gallery?workflow={C.esc(workflow)}&page={page+1}">下一页</a>'
                 if len(rows) == PAGE_SIZE else "")
    body = (f'<div class="page-head"><h1>图库</h1><p class="sub">按日期分组，每页 {PAGE_SIZE} 个作业；点开看详情</p></div>'
            f'{filt}{blocks}<div class="row" style="margin-top:16px">{prev_link}{next_link}</div>')
    return shell(base, token, "/app/gallery", "图库", body)


def registry_page(base: str, token: str) -> str:
    from tools.p20 import registry as reg
    st = reg.stats()
    wfs = reg.list_workflows()
    rows = []
    for w in sorted(wfs, key=lambda x: (0 if (x.get("compat") or {}).get("status") in ("verified", "equivalent") else 1, x["key"])):
        text, cls = _status_of(w)
        rows.append(f'<tr><td>{C.esc(w.get("name"))}</td><td><code>{C.esc(w["key"])}</code></td>'
                    f'<td class="nowrap">{"图片" if w.get("kind") == "image" else "视频"}</td>'
                    f'<td class="nowrap"><span class="pill {cls}">{text}</span></td>'
                    f'<td><code>{C.esc(w.get("model_key"))}</code></td></tr>')
    models = reg.list_models()
    mrows = [f'<tr><td><code>{C.esc(m["key"])}</code></td><td>{C.esc(m.get("name"))}</td>'
             f'<td>{C.esc(m.get("kind"))}</td><td>{len(m.get("files") or {})}</td></tr>'
             for m in sorted(models, key=lambda x: x["key"])]
    body = f"""
<div class="page-head"><h1>工作流与模型</h1>
<p class="sub">共 {st.get('workflows')} 条工作流 · {st.get('models')} 个模型 · 可用 {(st.get('compat') or {}).get('verified', 0)} 条</p></div>
<div class="card"><h2>工作流</h2>
<table><tr><th>名称</th><th>key</th><th>类型</th><th>状态</th><th>模型</th></tr>{''.join(rows)}</table></div>
<div class="card" style="margin-top:16px"><h2>模型</h2>
<table><tr><th>key</th><th>名称</th><th>类型</th><th>文件数</th></tr>{''.join(mrows)}</table></div>"""
    return shell(base, token, "/app/registry", "工作流与模型", body)


def _status_of(w: dict) -> tuple[str, str]:
    from . import i18n as I
    status = (w.get("compat") or {}).get("status")
    return I.status_text(status)

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
  <button class="btn" id="dl-selected" type="button">下载选中作业的图</button>
  <button class="btn" id="rerun-selected" type="button">重跑选中（只建作业）</button>
  <button class="btn danger" id="delete-selected" type="button">删除选中</button>
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
    <div class="row" style="margin-top:10px">
      <button class="btn danger" id="delete-job" data-job="{C.esc(job_id)}" type="button">删除此作业</button>
      <span class="hint">默认移到 .trash，可恢复；彻底删除用命令行 <code>jobctl rm --purge</code></span>
    </div>
    <h2 style="margin-top:16px">参数快照</h2>
    <pre style="overflow:auto;font-size:12px">{C.esc(params)}</pre>
  </div>
</div>"""
    return shell(base, token, "/app/jobs", f"作业 {job_id}", body)
