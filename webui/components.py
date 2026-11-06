# -*- coding: utf-8 -*-
"""页面组件：纯函数，输入数据 → HTML 片段（全部转义）。"""
from __future__ import annotations

import datetime as dt
import html

from . import i18n


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def badge(state: str) -> str:
    s = (state or "").upper()
    if s == "DONE":
        cls = "ok"
    elif s.startswith("FAILED") or s in ("ABORTED", "FAILED"):
        cls = "bad"
    elif s in ("CREATED",):
        cls = "idle"
    else:
        cls = "run"
    return f'<span class="pill {cls}">{esc(s or "?")}</span>'


def compat_badge(status: str) -> str:
    cls = {"verified": "ok", "equivalent": "warn", "experimental": "bad",
           "incompatible": "bad"}.get(status or "", "idle")
    return f'<span class="pill {cls}">{esc(status or "unknown")}</span>'


def _input(name: str, spec: dict, value) -> str:
    typ = spec.get("type")
    attrs = f'data-param="{esc(name)}"'
    if typ == "image" and name == "mask":
        return (
            f'<div class="maskwrap"><canvas class="maskcanvas" data-mask="{esc(name)}" '
            f'width="512" height="512"></canvas></div>'
            f'<div class="row" style="margin-top:6px">'
            f'<span class="hint">笔刷</span>'
            f'<input type="range" data-mask-size min="6" max="80" value="28" style="width:120px">'
            f'<button class="btn" type="button" data-mask-clear>清空</button>'
            f'<button class="btn" type="button" data-mask-invert>反选</button>'
            f'<span class="hint" data-mask-state>白色=重绘区域</span></div>'
            f'<input type="file" data-upload="{esc(name)}" accept="image/png" style="margin-top:6px">'
            f'<input type="hidden" {attrs} value="{esc(value or "")}">'
            f'<div class="hint" data-preview="{esc(name)}">也可以直接上传黑白蒙版 PNG</div>')
    if typ == "image":
        return (f'<input type="file" data-upload="{esc(name)}" accept="image/png,image/jpeg,image/webp">'
                f'<input type="hidden" {attrs} value="{esc(value or "")}">'
                f'<div class="hint" data-preview="{esc(name)}">上传后自动生成内容哈希文件名</div>')
    if name in ("audio", "video") or typ in ("audio", "video"):
        acc = "audio/*" if (typ == "audio" or name == "audio") else "video/*"
        return (f'<input type="file" data-upload="{esc(name)}" accept="{acc}">'
                f'<input type="hidden" {attrs} value="{esc(value or "")}">'
                f'<div class="hint" data-preview="{esc(name)}">上传素材后自动生成文件名</div>')
    if typ == "enum":
        vals = spec.get("values") or []
        cur = value if value is not None else spec.get("default")
        opts = "".join(
            f'<option value="{esc(v)}"{" selected" if v == cur else ""}>{esc(v)}</option>'
            for v in vals)
        return f'<select {attrs}>{opts}</select>'
    if typ == "bool":
        cur = bool(value if value is not None else spec.get("default"))
        return (f'<label class="switch"><input type="checkbox" {attrs}'
                f'{" checked" if cur else ""}><span></span></label>')
    if typ in ("int", "float"):
        step = "1" if typ == "int" else "0.01"
        cur = value if value is not None else spec.get("default", "")
        rng = ""
        if "min" in spec:
            rng += f' min="{spec["min"]}"'
        if "max" in spec:
            rng += f' max="{spec["max"]}"'
        return f'<input type="number" step="{step}" {attrs}{rng} value="{esc(cur)}">'
    if name in ("prompt", "neg_prompt"):
        cur = value if value is not None else spec.get("default", "")
        rows = 4 if name == "prompt" else 2
        return f'<textarea rows="{rows}" {attrs} placeholder="{esc(spec.get("description", ""))}">{esc(cur)}</textarea>'
    cur = value if value is not None else spec.get("default", "")
    return f'<input type="text" {attrs} value="{esc(cur)}">'


def field(name: str, spec: dict, value=None) -> str:
    label = esc(i18n.param_label(name))
    key = "" if i18n.param_label(name) == name else f' <code class="key">{esc(name)}</code>'
    hint = ""
    bits = [i18n.type_label(spec.get("type", ""))]
    if "default" in spec:
        bits.append(f"默认 {spec['default']}")
    if spec.get("min") is not None or spec.get("max") is not None:
        bits.append(f"{spec.get('min', '')}–{spec.get('max', '')}")
    if bits:
        hint = f'<div class="hint">{esc(" · ".join(str(b) for b in bits))}</div>'
    return (f'<div class="field"><label for="p-{esc(name)}">{label}{key}</label>'
            f'{_input(name, spec, value)}{hint}</div>')


def fmt_bytes(n: int | None) -> str:
    n = int(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1:.1f} {unit}" if unit == "GB" else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n} B"


def money(v) -> str:
    try:
        return f"¥{float(v):.3f}" if v is not None else "—"
    except (TypeError, ValueError):
        return "—"


def human_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        t = dt.datetime.fromisoformat(iso)
    except ValueError:
        return esc(iso)
    return t.astimezone().strftime("%m-%d %H:%M:%S")


def kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows)
    return f'<table class="kv">{body}</table>'
