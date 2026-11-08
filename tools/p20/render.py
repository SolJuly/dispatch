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

import ast
import copy
import functools
import hashlib
import json
import math
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


ROOT = Path(__file__).resolve().parents[2]

_EXPR_FUNCS = {"round": round, "int": int, "abs": abs, "max": max, "min": min,
               "float": float, "floor": math.floor, "ceil": math.ceil}
# 白名单只放行「纯算术 + 位运算 + 比较 + and/or」：
# 54 张图里真实存在 floor/ceil、`a & ~1`、`a > 2048`、`(x==1) and y`
# （2026-11-08 审计：原来的白名单拒掉了 14 个表达式里的 6 个，导致这些工作流
# 推不出 duration_s）。刻意**不放行** ast.IfExp / ast.Lambda / 属性访问 / 下标，
# 也不放行任何非白名单函数名 —— 这三类才是能绕出去的口子。
_EXPR_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Name, ast.Load,
    ast.Call, ast.BoolOp, ast.Compare,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.FloorDiv, ast.USub, ast.UAdd,
    ast.BitAnd, ast.BitOr, ast.BitXor, ast.Invert, ast.LShift, ast.RShift,
    ast.And, ast.Or, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def _eval_num_expr(expr: str, vals: dict) -> float | None:
    """只放行算术/位运算/比较的表达式求值（ComfyMathExpression）。

    返回 float；bool 结果按 0/1 处理（`a > 2048` 这类在模板里用于开关）。
    任何不在白名单里的节点（IfExp、Lambda、属性、下标、未知函数）一律拒绝，
    宁可推导不出时长，也不执行来路不明的表达式。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, _EXPR_NODES):
            return None
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _EXPR_FUNCS:
                return None
        if isinstance(node, ast.Name) and node.id not in vals and node.id not in _EXPR_FUNCS:
            return None
    try:
        # 位运算（& ~ | << >>）只接受 int：上游 _resolve_num 一律返回 float，
        # 直接把 5.0 喂给 `a & ~1` 会 TypeError → 整条表达式被判「求解失败」。
        # 整数值的 float 先转 int，非整数值保留 float（算术仍按浮点走）。
        env = {k: (int(v) if isinstance(v, float) and v.is_integer() else v)
               for k, v in vals.items()}
        got = eval(compile(tree, "<expr>", "eval"),
                   {"__builtins__": {}, **_EXPR_FUNCS}, env)
    except Exception:
        return None
    if isinstance(got, bool):
        return 1.0 if got else 0.0
    if isinstance(got, (int, float)):
        return float(got)
    return None


def _resolve_num(graph: dict, val, depth: int = 0) -> float | None:
    """把「常量 / 连线到常量或 ComfyMathExpression」的输入解析成数值。"""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if not isinstance(val, list) or len(val) != 2 or depth > 6:
        return None
    node = graph.get(str(val[0])) or {}
    ins = node.get("inputs") or {}
    ct = node.get("class_type", "")
    if ct == "ComfyMathExpression":
        vals = {}
        for k, v in ins.items():
            if not str(k).startswith("values."):
                continue
            got = _resolve_num(graph, v, depth + 1)
            if got is None:
                return None
            vals[str(k).split(".", 1)[1]] = got
        return _eval_num_expr(str(ins.get("expression") or ""), vals)
    for k in ("value", "int", "number", "float"):
        v = ins.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


@functools.lru_cache(maxsize=128)
def _graph_json(path: str, _sig: tuple) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _file_sig(path: Path) -> tuple:
    """缓存键：内容 sha1 + size。

    不能只用 st_mtime：本环境（以及某些容器文件系统）上，同一秒内重写文件
    mtime_ns / ctime_ns / size 都可能完全不变（实测：内容 24→25 同长度改写，
    三个时间戳与 size 全部相同）。那样缓存会返回旧图，duration_s 按旧 fps 算，
    好产物被时长门禁误杀（2026-11-08 审计复现）。
    改成读内容算 sha1：54 个工作流图每个几十 KB，一次哈希比"误判一次作业"便宜得多。
    """
    data = path.read_bytes()
    return (hashlib.sha1(data).hexdigest(), len(data))


def _load_graph(wf: dict) -> dict | None:
    rel = wf.get("graph") or wf.get("source_path") or ""
    if not rel:
        return None
    path = Path(rel)
    if not path.is_absolute():
        path = ROOT / path
    try:
        return _graph_json(str(path), _file_sig(path))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _graph_timing(wf: dict) -> tuple[float | None, float | None]:
    """从 API 图读出「默认帧数 / 帧率」，两者都唯一可解才返回。

    为什么需要：21 个视频工作流只声明了 min_bytes，帧数/时长期望是空的
    （verify._check_frames 直接 skipped），出 1 秒也能判 PASS。
    有循环批处理（ImageFromBatch 大窗）或多路 latent 的工作流帧数随输入变，
    这里直接放弃推导，交给 params 覆盖或人工配置。
    """
    graph = _load_graph(wf)
    if not isinstance(graph, dict) or not graph:
        return None, None
    if any(n.get("class_type") in ("ImageFromBatch",)
           and isinstance((n.get("inputs") or {}).get("length"), int)
           and (n["inputs"]["length"] or 0) >= 1000 for n in graph.values()):
        return None, None

    fps_vals, frame_vals, latent_nodes = set(), set(), 0
    for n in graph.values():
        if n.get("class_type") != "CreateVideo":
            continue
        v = _resolve_num(graph, (n.get("inputs") or {}).get("fps"))
        if v:
            fps_vals.add(round(v, 3))
    if len(fps_vals) != 1:
        return None, None
    fps = fps_vals.pop()

    for n in graph.values():
        ct = n.get("class_type", "")
        ins = n.get("inputs") or {}
        if "Video" not in ct and "Latent" not in ct:
            continue
        for key in ("length", "frames", "num_frames"):
            if key not in ins:
                continue
            v = _resolve_num(graph, ins[key])
            if v is None:
                v = _default_math_frames(graph, ins[key], fps)
            if v and 1 < v < 2048:
                frame_vals.add(int(v))
                latent_nodes += 1
            break
    if len(frame_vals) != 1 or latent_nodes > 2:
        return None, None
    frames = frame_vals.pop()
    if not fps or frames <= 1:
        return None, None
    return frames / fps, fps


def _default_math_frames(graph: dict, val, fps: float) -> float | None:
    """expression 没落盘时按模板惯例反推帧数：Wan 口径 n = fps × 秒 + 1。

    只在「values.a 是秒数、values.b 缺省或等于帧率」时启用，其余一律放弃。
    """
    if not isinstance(val, list) or len(val) != 2:
        return None
    node = graph.get(str(val[0])) or {}
    if node.get("class_type") != "ComfyMathExpression":
        return None
    ins = node.get("inputs") or {}
    if ins.get("expression"):
        return None
    a = _resolve_num(graph, ins.get("values.a"))
    b = _resolve_num(graph, ins.get("values.b"))
    if not a or a <= 0 or a > 120:
        return None
    if b is not None and abs(b - fps) > 0.01:
        return None
    return a * fps + 1


def _fps_of(wf: dict) -> float | None:
    """工作流声明的帧率：顶层 fps，或 expect_defaults.fps。"""
    for src in (wf, wf.get("expect_defaults") or {}):
        v = src.get("fps") if isinstance(src, dict) else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
    _dur, fps = _graph_timing(wf)
    return float(fps) if fps else None


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
    # 图里能推出默认帧数/帧率的工作流，补一条时长窗口（21 个视频工作流原本没有）
    if wf.get("kind") == "video" and "duration_s" not in exp:
        dur, _fps = _graph_timing(wf)
        if dur:
            exp["duration_s"] = [round(max(0.2, dur - 0.6), 2), round(dur + 0.8, 2)]
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
        # 生成类沿用标定硬门禁；素材驱动类只拦「整片冻结」，静态素材不再误杀
        exp["freeze"] = dict(FREEZE_MATERIAL_DRIVEN if _is_material_driven(wf)
                             else FREEZE_GENERATIVE)
    return exp


def _is_material_driven(wf: dict) -> bool:
    """输出运动性继承自输入素材的工作流（静态素材 → 静态片是合法结果）。

    freeze-calibration.md 的 0.20 阈值是在「文/图生视频」样本上标定的，用来抓
    时间维崩坏（人物走出画/后段崩/全片静止）。但动作迁移、超分、插帧、抠像、
    视频重绘这类工作流的输出由输入视频驱动：用户传一段静止素材，产出静止是
    对的，套 0.2 硬门禁会把合法作业判成 FAILED_VERIFY（2026-11-07 animate2
    蒸馏版真实事故：纯色源图 primary ratio=0.716 → 整单 FAIL）。

    判据只用注册表事实，不猜工作流名字：
      1) params 里有 type=video 的输入；或
      2) API 图里有 LoadVideo / VHS_LoadVideo 节点。
    """
    for v in (wf.get("params") or {}).values():
        if isinstance(v, dict) and v.get("type") == "video":
            return True
    graph = _load_graph(wf)
    if isinstance(graph, dict):
        for n in graph.values():
            ct = (n or {}).get("class_type", "")
            if isinstance(ct, str) and ct.startswith(("LoadVideo", "VHS_LoadVideo")):
                return True
    return False


# 生成类：沿用标定的硬门禁；素材驱动类：只拦整片冻结，中度静止降级为告警
FREEZE_GENERATIVE = {"ratio_max": 0.20, "longest_max_s": 1.0}
FREEZE_MATERIAL_DRIVEN = {"mode": "full_freeze_only",
                          "ratio_max": 0.20, "longest_max_s": 1.0}


def build_payload(graph: dict, client_id: str = "p20") -> dict:
    return {"prompt": graph, "client_id": client_id}


def payload_json(graph: dict, client_id: str = "p20") -> str:
    return json.dumps(build_payload(graph, client_id), ensure_ascii=False)
