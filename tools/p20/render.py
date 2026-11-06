#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.0 参数映射：把「已解析的参数 + 模型文件」写进 ComfyUI API 图。

与 P1 `_render_workflow()` 的区别（这是解耦的核心）：
  P1：代码里写死 5 个占位符 {{PROMPT}}/{{NEG_PROMPT}}/{{SEED}}/{{FIRST_FRAME}}/{{OUTPUT_PREFIX}}，
      换一个节点名/换一个参数就得改 run.py。
  P2.0：节点/输入全部来自 registry/workflows/*.json 的 bindings；模型文件名来自
      registry/models/*.json 的 files。run.py 只调用 render_job()，不再认识任何节点。

渲染顺序（后者覆盖前者）：
  1. 图里静态常量
  2. workflow.constants（配置化覆盖，如 fps/format）
  3. 模型槽位（role → 节点/输入 + extra，如 CLIPLoader.type）
  4. 参数绑定（一个参数可绑多个节点/输入，如 h3 的 width/height/length）
  5. 输出前缀（job_id）

硬校验：
  - 绑定指向的节点/输入必须存在
  - 渲染后不允许残留 {{...}}（否则说明配置漏绑）
  - 模型槽位必须被模型文件填满（registry.resolve 已先查一遍）
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from . import config_schema as cs
from . import registry

PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")


class RenderError(Exception):
    """渲染失败（配置/图不匹配）。"""


def _deep(d):
    return copy.deepcopy(d)


def _set(graph: dict, node: str, input_key: str, value, *, where: str) -> None:
    n = graph.get(node)
    if not isinstance(n, dict):
        raise RenderError(f"{where} 指向不存在的节点：{node}")
    ins = n.setdefault("inputs", {})
    if not isinstance(ins, dict):
        raise RenderError(f"{where} 节点 {node}.inputs 不是对象")
    ins[input_key] = value


def apply_model(graph: dict, wf: dict, model: dict) -> dict:
    """把模型文件写进工作流声明的槽位；校验槽位齐全。"""
    slots = (wf.get("model") or {}).get("slots") or {}
    files = model.get("files") or {}
    missing = [r for r in slots if r not in files]
    if missing:
        raise RenderError(f"模型 {model.get('key')} 缺少槽位：{missing}")
    for role, slot in slots.items():
        f = files[role]
        name = f.get("name")
        if not name:
            raise RenderError(f"模型 {model.get('key')} 的 files.{role}.name 为空")
        _set(graph, slot["node"], slot["input"], name, where=f"model.slots.{role}")
        for k, v in (f.get("extra") or {}).items():
            _set(graph, slot["node"], k, v, where=f"model.slots.{role}.extra.{k}")
    return graph


def apply_constants(graph: dict, wf: dict) -> dict:
    for c in wf.get("constants") or []:
        _set(graph, c["node"], c["input"], c["value"], where="constants")
    return graph


def apply_params(graph: dict, wf: dict, params: dict) -> dict:
    schema = wf.get("params") or {}
    for name, spec in schema.items():
        if name not in params:
            continue
        for b in spec.get("bindings") or []:
            _set(graph, b["node"], b["input"], params[name], where=f"params.{name}")
    return graph


def apply_output(graph: dict, wf: dict, prefix: str) -> dict:
    out = wf.get("output") or {}
    if out.get("node"):
        _set(graph, out["node"], out["input"], prefix, where="output")
    return graph


def check_placeholders(graph: dict) -> None:
    left = []
    for nid, node in graph.items():
        for k, v in ((node or {}).get("inputs") or {}).items():
            if isinstance(v, str):
                for m in PLACEHOLDER_RE.finditer(v):
                    left.append(f"{nid}.{k}={m.group(0)}")
    if left:
        raise RenderError("渲染后仍有未绑定占位符：" + ", ".join(left[:6]))


def render(wf: dict, model: dict, params: dict, prefix: str, *,
           graph: dict | None = None) -> tuple[dict, dict]:
    """纯函数：返回 (渲染后的图, 元信息)，不读写数据库。"""
    g = _deep(graph if graph is not None else (wf.get("graph_obj") or {}))
    if not g:
        raise RenderError(f"工作流 {wf.get('key')} 没有图")
    apply_constants(g, wf)
    apply_model(g, wf, model)
    apply_params(g, wf, params)
    apply_output(g, wf, prefix)
    check_placeholders(g)
    meta = {
        "workflow_key": wf.get("key"),
        "workflow_version": wf.get("version"),
        "model_key": model.get("key"),
        "model_version": model.get("version"),
        "params": dict(params),
        "prefix": prefix,
        "nodes": len(g),
        "graph_sha256": cs.sha256_obj(g),
        "template_sha256": wf.get("graph_sha256"),
    }
    return g, meta


def render_job(job: dict, *, db_path: Path = registry.DB_PATH) -> tuple[dict, dict]:
    """执行器入口：从 job.model.registry 取配置并渲染。legacy job 不调用本函数。"""
    reg = ((job.get("model") or {}).get("registry") or {})
    if not reg.get("workflow_key"):
        raise RenderError("job 没有 model.registry.workflow_key，不能走 registry 渲染")
    wk = reg["workflow_key"]
    mk = reg.get("model_key") or (job.get("model") or {}).get("key")
    wf = registry.get_workflow(wk, version=reg.get("workflow_version"), db_path=db_path)
    model = registry.get_model(mk, version=reg.get("model_version"), db_path=db_path)
    # job 创建时已过兼容性门禁；这里再校验一次参数/槽位，防止有人手改 job.json
    res = registry.resolve(wk, model_key=mk, params=job.get("params") or {},
                           allow_experimental=True, db_path=db_path,
                           workflow_version=reg.get("workflow_version"),
                           model_version=reg.get("model_version"))
    graph, meta = render(wf, model, res["params"], job["job_id"], graph=wf.get("graph_obj"))
    meta.update({
        "compat": res["compat"],
        "defaults_applied": res["defaults_applied"],
        "registry_fingerprint": registry.stats(db_path).get("fingerprint"),
    })
    return graph, meta


def read_params(graph: dict, wf: dict) -> tuple[dict, list[str]]:
    """从渲染后的图回读参数（多绑定时要求一致）。返回 (参数, 告警)。"""
    out, warns = {}, []
    for name, spec in (wf.get("params") or {}).items():
        vals = []
        for b in spec.get("bindings") or []:
            ok, v = cs.node_inputs(graph, b["node"], b["input"])
            if not ok:
                warns.append(f"{name}: 绑定 {b['node']}.{b['input']} 不存在")
                continue
            vals.append(v)
        if not vals:
            continue
        first = vals[0]
        for v in vals[1:]:
            try:
                same = float(v) == float(first)
            except (TypeError, ValueError):
                same = v == first
            if not same:
                warns.append(f"{name}: 多绑定值不一致 {first!r} vs {v!r}")
        out[name] = first
    return out, warns


def check_params(job: dict, graph: dict, wf: dict) -> tuple[bool | None, dict]:
    """回读比对：渲染图里的值必须与 job.params 一致（params 复验项）。"""
    got, warns = read_params(graph, wf)
    params = job.get("params") or {}
    diffs, compared, skipped = [], [], []
    for name, wf_val in got.items():
        if name not in params:
            skipped.append(name)
            continue
        compared.append(name)
        job_val = params[name]
        try:
            same = float(wf_val) == float(job_val)
        except (TypeError, ValueError):
            same = wf_val == job_val
        if not same:
            diffs.append(f"{name}: workflow={wf_val!r} / job={job_val!r}")
    ok = None if not compared else (not diffs)
    return ok, {"compared": compared, "skipped": skipped, "diffs": diffs, "warnings": warns}


def _resolve_count(spec, params: dict) -> int | None:
    """{{BATCH_SIZE}} → params["batch_size"]；int 原样返回；解析不了返回 None。"""
    if isinstance(spec, bool):
        return None
    if isinstance(spec, int):
        return spec
    if isinstance(spec, str):
        m = re.fullmatch(r"\{\{([A-Z0-9_]+)\}\}", spec)
        if m:
            v = params.get(m.group(1).lower())
            return int(v) if isinstance(v, int) and not isinstance(v, bool) else None
    return None


def _fps_of(wf: dict) -> float | None:
    """工作流声明的帧率：顶层 fps，或 expect_defaults.fps。"""
    for src in (wf, wf.get("expect_defaults") or {}):
        v = src.get("fps") if isinstance(src, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
    return None


def _frames_of(wf: dict, params: dict, fps: float) -> int | None:
    """本次请求的等效帧数：frames 直接用；duration（秒）按 fps 换算（Wan 口径 n=fps*d+1）。"""
    ps = wf.get("params") or {}
    if "frames" in ps:
        v = params.get("frames")
        if isinstance(v, int) and not isinstance(v, bool):
            return int(v)
    if "duration" in ps:
        v = params.get("duration")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(round(float(v) * fps)) + 1
    return None


def _recalibrate_duration(wf: dict, params: dict, exp: dict) -> None:
    """帧数/时长被请求覆盖时重算 duration_s。

    原先 duration_s 直接抄工作流默认值（按默认帧数标定），一旦作业改了 frames/duration，
    期望窗口还停在默认那几秒，产物必然判 FAIL —— 这是 h3 241 帧作业被误杀的根因。
    """
    if "duration_s" not in exp:
        return
    fps = _fps_of(wf)
    if not fps:
        return
    ps = wf.get("params") or {}
    if "frames" in ps:
        v = params.get("frames")
        if not isinstance(v, int) or isinstance(v, bool) or v == (ps["frames"] or {}).get("default"):
            return
    elif "duration" in ps:
        v = params.get("duration")
        if not isinstance(v, (int, float)) or isinstance(v, bool) \
                or v == (ps["duration"] or {}).get("default"):
            return
    else:
        return
    n = _frames_of(wf, params, fps)
    if not n:
        return
    dur = n / fps
    exp["duration_s"] = [round(max(0.2, dur - 0.3), 2), round(dur + 0.35, 2)]


def expect_for(wf: dict, params: dict) -> dict:
    """由工作流默认期望 + 解析后的尺寸/帧数，生成 job.json 的 expect。"""
    exp = dict(wf.get("expect_defaults") or {})
    # 尺寸以本次请求为准（宽度/高度是参数时，配置里的 expect 只是默认值）
    for k in ("width", "height"):
        if k in params:
            exp[k] = [params[k]]
    # 输出可能经过超分/放大（例如 Z-Image HQ 的 2x 细化）：
    # expect_scale 只影响校验期望，不改变提交给 ComfyUI 的 width/height。
    scale = wf.get("expect_scale") or {}
    if isinstance(scale, dict):
        for k in ("width", "height"):
            mult = scale.get(k)
            if k not in params or not isinstance(mult, (int, float)) or mult == 1:
                continue
            try:
                exp[k] = [max(1, int(round(float(params[k]) * float(mult))))]
            except (TypeError, ValueError):
                pass
    out = wf.get("output") or {}
    exp.setdefault("ext", out.get("ext", "mp4"))
    # 帧数/时长被覆盖时，duration_s 必须跟着算，不能沿用默认帧数的窗口
    _recalibrate_duration(wf, params, exp)

    # 多图：expect.images 或 output.count 可以是 {{PARAM}}，这里解析成具体张数
    if "images" in exp:
        n = _resolve_count(exp["images"], params)
        if n is None:
            exp.pop("images", None)
        else:
            exp["images"] = n
    if "images" not in exp and "count" in out:
        n = _resolve_count(out["count"], params)
        if n is not None:
            exp["images"] = n
    if wf.get("kind") == "video" and "freeze" not in exp:
        exp["freeze"] = {"ratio_max": 0.20, "longest_max_s": 1.0}
    return exp


def build_payload(graph: dict, client_id: str = "p20") -> dict:
    return {"prompt": graph, "client_id": client_id}


def payload_json(graph: dict, client_id: str = "p20") -> str:
    return json.dumps(build_payload(graph, client_id), ensure_ascii=False)
