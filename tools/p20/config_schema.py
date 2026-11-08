#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.0 配置契约：workflow / model / compat JSON 的纯标准库校验。

设计原则：
  1. 只校验契约里声明过的键，允许未知键（前向兼容，和 P1 schema.py 同口径）。
  2. 不碰数据库、不发网络请求——纯函数，方便单测与在 UI 侧复用。
  3. 报错必须能定位到「哪个文件、哪个字段」，不允许静默放过。

三份配置的 schema：
  p20.model/v1     模型：权重文件（角色→文件名）、默认超参、约束、资产检查规则
  p20.workflow/v1  工作流：图路径、模型槽位、参数→节点输入的绑定、输出、期望值
  p20.compat/v1    兼容性：人工确认的 verified / incompatible（其余由结构推导）
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

MODEL_SCHEMA = "p20.model/v1"
WORKFLOW_SCHEMA = "p20.workflow/v1"
COMPAT_SCHEMA = "p20.compat/v1"

KINDS = ("video", "image")
PARAM_TYPES = ("string", "int", "float", "bool", "image", "video", "audio", "enum")
COUNT_RE = re.compile(r"^\{\{[A-Z0-9_]+\}\}$")
MODEL_STATUS = ("verified", "experimental", "equivalent", "incompatible")
SETUP_PROFILES = ("h3", "qwen", "all", "none")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
PLACEHOLDER_RE = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


class ConfigError(Exception):
    """配置契约错误（携带完整错误清单）。"""

    def __init__(self, errors: list[str], path: str = ""):
        self.errors = list(errors)
        self.path = path
        head = f"{path}: " if path else ""
        super().__init__(head + "; ".join(self.errors[:8]) + (" …" if len(self.errors) > 8 else ""))


# ------------------------------------------------------------------ 基础工具
def load_json(path: Path) -> dict:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError([f"文件不存在：{path}"], str(path))
    except json.JSONDecodeError as e:
        raise ConfigError([f"JSON 解析失败：{e}"], str(path))
    if not isinstance(d, dict):
        raise ConfigError(["顶层必须是对象"], str(path))
    return d


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_obj(obj) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _check_type(name: str, value, typ: str, e: list[str], where: str) -> None:
    ok = {
        "string": lambda x: isinstance(x, str),
        "image": lambda x: isinstance(x, str) and bool(x.strip()),
        "video": lambda x: isinstance(x, str) and bool(x.strip()),
        "audio": lambda x: isinstance(x, str) and bool(x.strip()),
        "int": _is_int,
        "float": _is_num,
        "bool": lambda x: isinstance(x, bool),
        "enum": lambda x: isinstance(x, str),
    }.get(typ, lambda x: True)(value)
    if not ok:
        e.append(f"{where}.{name} 类型应为 {typ}，实际 {type(value).__name__}={value!r}")


def _check_match(rule, e: list[str], where: str) -> None:
    if not isinstance(rule, dict):
        e.append(f"{where}.match 应为对象（equals/contains/regex 三选一）")
        return
    keys = [k for k in ("equals", "contains", "regex") if k in rule]
    if len(keys) != 1:
        e.append(f"{where}.match 必须且只能有 equals/contains/regex 之一，实际 {keys}")
        return
    if not isinstance(rule[keys[0]], str) or not rule[keys[0]]:
        e.append(f"{where}.match.{keys[0]} 应为非空字符串")
        return
    if keys[0] == "regex":
        try:
            re.compile(rule["regex"])
        except re.error as ex:
            e.append(f"{where}.match.regex 非法：{ex}")


# ------------------------------------------------------------------ 图结构
def load_graph(path: Path) -> dict:
    d = load_json(path)
    if not d:
        raise ConfigError(["ComfyUI 图为空"], str(path))
    for nid, node in d.items():
        if not isinstance(node, dict):
            raise ConfigError([f"节点 {nid} 不是对象"], str(path))
        if not _nonempty_str(node.get("class_type")):
            raise ConfigError([f"节点 {nid} 缺 class_type"], str(path))
        if not isinstance(node.get("inputs", {}), dict):
            raise ConfigError([f"节点 {nid}.inputs 不是对象"], str(path))
    return d


def node_inputs(graph: dict, node_id: str, input_key: str) -> tuple[bool, object]:
    node = graph.get(node_id)
    if not isinstance(node, dict):
        return False, None
    ins = node.get("inputs")
    if not isinstance(ins, dict) or input_key not in ins:
        return False, None
    return True, ins[input_key]


def placeholder_sites(graph: dict) -> list[tuple[str, str, str]]:
    """返回所有占位符位置 [(node_id, input_key, token)]。"""
    out = []
    for nid, node in graph.items():
        ins = (node or {}).get("inputs") or {}
        if not isinstance(ins, dict):
            continue
        for k, v in ins.items():
            if isinstance(v, str):
                for m in PLACEHOLDER_RE.finditer(v):
                    out.append((nid, k, m.group(1)))
    return sorted(out)


def structure_signature(graph: dict) -> str:
    """工作流结构指纹：节点 class_type + 输入形状（哪些输入是连线），忽略常量与节点 ID。

    用途：兼容性推导。同一模型 + 同结构 = 已验证结论可传递（equivalent）；
    多一个 LoadImage / 多一个 first_frame 连线 → 结构不同，必须单独验证。
    """
    shape = []
    for _nid, node in graph.items():
        ins = (node or {}).get("inputs") or {}
        links = tuple(sorted(k for k, v in ins.items()
                             if isinstance(v, list) and len(v) == 2))
        shape.append((node.get("class_type"), links))
    return sha256_obj(sorted(shape))


def referenced_models(graph: dict) -> list[str]:
    """图里出现的模型文件名（仅用于诊断，不参与解析）。"""
    out = []
    for _nid, node in graph.items():
        for k, v in ((node or {}).get("inputs") or {}).items():
            if isinstance(v, str) and ("safetensors" in v or "ckpt" in v or "/" in v):
                if not v.startswith("{{") and k in ("unet_name", "clip_name", "vae_name", "ckpt_name"):
                    out.append(v)
    return sorted(set(out))


# ------------------------------------------------------------------ model
def validate_model(d: dict) -> list[str]:
    e: list[str] = []
    if d.get("schema") != MODEL_SCHEMA:
        e.append(f"schema 应为 {MODEL_SCHEMA}，实际 {d.get('schema')!r}")
    key = d.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key):
        e.append(f"key 非法：{key!r}（小写字母开头，[a-z0-9_]，2–64 位）")
    if not _nonempty_str(d.get("name")):
        e.append("name 缺失")
    if not _is_int(d.get("version")) or d.get("version", 0) < 1:
        e.append("version 应为 ≥1 的整数")
    if d.get("kind") not in KINDS:
        e.append(f"kind 应为 {KINDS}，实际 {d.get('kind')!r}")
    if "family" in d and not _nonempty_str(d.get("family")):
        e.append("family 若存在应为非空字符串")

    files = d.get("files")
    if not isinstance(files, dict) or not files:
        e.append("files 应为非空对象（角色 → 权重文件）")
        files = {}
    for role, f in files.items():
        w = f"files.{role}"
        if not ROLE_RE.match(str(role)):
            e.append(f"{w} 角色名非法")
        if not isinstance(f, dict):
            e.append(f"{w} 应为对象")
            continue
        for k in ("name", "loader", "input"):
            if not _nonempty_str(f.get(k)):
                e.append(f"{w}.{k} 缺失")
        if "extra" in f and not isinstance(f["extra"], dict):
            e.append(f"{w}.extra 应为对象")
        if "match" in f:
            _check_match(f["match"], e, w)

    primary = d.get("primary")
    if primary is not None:
        if primary not in files:
            e.append(f"primary={primary!r} 不在 files 里")
    elif files and "ckpt" not in files and "unet" not in files:
        e.append("primary 未指定，且 files 里没有 ckpt/unet 角色可作主文件")

    defaults = d.get("defaults", {})
    if not isinstance(defaults, dict):
        e.append("defaults 应为对象")
        defaults = {}
    for k, v in defaults.items():
        if not isinstance(v, (str, int, float, bool)):
            e.append(f"defaults.{k} 只能是标量（string/int/float/bool）")

    cons = d.get("constraints", {})
    if not isinstance(cons, dict):
        e.append("constraints 应为对象")
        cons = {}
    for name, c in cons.items():
        if not isinstance(c, dict):
            e.append(f"constraints.{name} 应为对象")
            continue
        for k in ("min", "max"):
            if k in c and not _is_num(c[k]):
                e.append(f"constraints.{name}.{k} 应为数字")
        if "multiple" in c and (not _is_num(c["multiple"]) or c["multiple"] <= 0):
            e.append(f"constraints.{name}.multiple 应为正数")
        if _is_num(c.get("min")) and _is_num(c.get("max")) and c["min"] > c["max"]:
            e.append(f"constraints.{name} min > max")

    setup = d.get("setup", {})
    if not isinstance(setup, dict):
        e.append("setup 应为对象")
        setup = {}
    if setup.get("profile", "all") not in SETUP_PROFILES:
        e.append(f"setup.profile 应为 {SETUP_PROFILES}")
    links = setup.get("links", [])
    if not isinstance(links, list):
        e.append("setup.links 应为数组")
    else:
        for i, ln in enumerate(links):
            if not isinstance(ln, dict) or not _nonempty_str(ln.get("link")) \
                    or not _nonempty_str(ln.get("target")):
                e.append(f"setup.links[{i}] 需要 link/target 两个非空字符串")

    assets = d.get("assets", [])
    if not isinstance(assets, list):
        e.append("assets 应为数组")
    else:
        for i, a in enumerate(assets):
            w = f"assets[{i}]"
            if not isinstance(a, dict):
                e.append(f"{w} 应为对象")
                continue
            for k in ("key", "loader", "input"):
                if not _nonempty_str(a.get(k)):
                    e.append(f"{w}.{k} 缺失")
            _check_match(a.get("match"), e, w)
    return e


def model_signature(d: dict) -> str:
    """模型文件签名：只认「角色 → 文件名 + loader/input/extra」，换权重文件即变。"""
    files = d.get("files") or {}
    sig = {role: {"name": (f or {}).get("name"), "loader": (f or {}).get("loader"),
                  "input": (f or {}).get("input"), "extra": (f or {}).get("extra") or {}}
           for role, f in files.items() if isinstance(f, dict)}
    return sha256_obj(sig)


# ------------------------------------------------------------------ workflow
def validate_workflow(d: dict, *, root: Path | None = None, check_graph: bool = True) -> list[str]:
    e: list[str] = []
    if d.get("schema") != WORKFLOW_SCHEMA:
        e.append(f"schema 应为 {WORKFLOW_SCHEMA}，实际 {d.get('schema')!r}")
    key = d.get("key")
    if not isinstance(key, str) or not KEY_RE.match(key):
        e.append(f"key 非法：{key!r}")
    if not _nonempty_str(d.get("name")):
        e.append("name 缺失")
    if not _is_int(d.get("version")) or d.get("version", 0) < 1:
        e.append("version 应为 ≥1 的整数")
    if d.get("kind") not in KINDS:
        e.append(f"kind 应为 {KINDS}，实际 {d.get('kind')!r}")
    if d.get("engine") != "comfyui":
        e.append("engine 目前只支持 comfyui")
    graph_rel = d.get("graph")
    if not _nonempty_str(graph_rel):
        e.append("graph 缺失")
        graph = {}
    else:
        graph_path = Path(graph_rel)
        if not graph_path.is_absolute() and root:
            graph_path = Path(root) / graph_rel
        if check_graph and not graph_path.exists():
            e.append(f"graph 文件不存在：{graph_rel}")
            graph = {}
        else:
            try:
                graph = load_graph(graph_path) if check_graph else {}
            except ConfigError as ex:
                e += [f"graph: {x}" for x in ex.errors]
                graph = {}

    model = d.get("model")
    if not isinstance(model, dict):
        e.append("model 缺失或不是对象")
        model = {}
    if not _nonempty_str(model.get("key")):
        e.append("model.key 缺失")
    slots = model.get("slots")
    if not isinstance(slots, dict) or not slots:
        e.append("model.slots 应为非空对象（角色 → 节点/输入）")
        slots = {}
    for role, s in slots.items():
        w = f"model.slots.{role}"
        if not ROLE_RE.match(str(role)):
            e.append(f"{w} 角色名非法")
        if not isinstance(s, dict) or not _nonempty_str(s.get("node")) \
                or not _nonempty_str(s.get("input")):
            e.append(f"{w} 需要 node/input")
            continue
        if graph:
            ok, _ = node_inputs(graph, s["node"], s["input"])
            if not ok:
                e.append(f"{w} 指向不存在的节点/输入：{s['node']}.{s['input']}")

    params = d.get("params")
    if not isinstance(params, dict) or not params:
        e.append("params 应为非空对象")
        params = {}
    covered: set[tuple[str, str]] = set()
    for name, p in params.items():
        w = f"params.{name}"
        if not KEY_RE.match(str(name)):
            e.append(f"{w} 参数名非法")
        if not isinstance(p, dict):
            e.append(f"{w} 应为对象")
            continue
        typ = p.get("type")
        if typ not in PARAM_TYPES:
            e.append(f"{w}.type 应为 {PARAM_TYPES}，实际 {typ!r}")
            continue
        if "required" in p and not isinstance(p["required"], bool):
            e.append(f"{w}.required 应为 bool")
        if "default" in p:
            try:
                _check_type("default", p["default"], typ, e, w)
            except Exception:
                pass
        for k in ("min", "max", "multiple"):
            if k in p and not _is_num(p[k]):
                e.append(f"{w}.{k} 应为数字")
        if typ == "enum":
            vals = p.get("values", p.get("enum"))
            if not isinstance(vals, list) or not vals \
                    or not all(isinstance(v, str) for v in vals):
                e.append(f"{w}.values 应为非空字符串数组（enum 类型的取值域）")
            else:
                if "default" in p and p["default"] not in vals:
                    e.append(f"{w}.default={p['default']!r} 不在 values 内")
                seen = {v for v in vals}
                if len(seen) != len(vals):
                    e.append(f"{w}.values 有重复项")
        elif "values" in p or "enum" in p:
            e.append(f"{w} 只有 type=enum 才允许 values/enum")
        if "description" in p and not isinstance(p["description"], str):
            e.append(f"{w}.description 应为字符串")
        if p.get("virtual"):
            continue
        bindings = p.get("bindings")
        if not isinstance(bindings, list) or not bindings:
            e.append(f"{w}.bindings 应为非空数组（或设 virtual=true）")
            continue
        for i, b in enumerate(bindings):
            wb = f"{w}.bindings[{i}]"
            if not isinstance(b, dict) or not _nonempty_str(b.get("node")) \
                    or not _nonempty_str(b.get("input")):
                e.append(f"{wb} 需要 node/input")
                continue
            covered.add((b["node"], b["input"]))
            if graph:
                ok, _ = node_inputs(graph, b["node"], b["input"])
                if not ok:
                    e.append(f"{wb} 指向不存在的节点/输入：{b['node']}.{b['input']}")

    out = d.get("output")
    if not isinstance(out, dict):
        e.append("output 缺失或不是对象")
        out = {}
    else:
        for k in ("node", "input"):
            if not _nonempty_str(out.get(k)):
                e.append(f"output.{k} 缺失")
        if out.get("kind") not in KINDS:
            e.append(f"output.kind 应为 {KINDS}")
        if not _nonempty_str(out.get("ext")):
            e.append("output.ext 缺失")
        pick = out.get("pick", ["images", "videos", "gifs"])
        if not isinstance(pick, list) or not all(isinstance(x, str) for x in pick):
            e.append("output.pick 应为字符串数组")
        if "count" in out:
            c = out["count"]
            if _is_int(c):
                if c < 1:
                    e.append("output.count 应为正整数")
            elif not (isinstance(c, str) and COUNT_RE.match(c)):
                e.append("output.count 应为正整数或 {{PARAM}} 形式")
            elif COUNT_RE.match(c):
                pname = c[2:-2].lower()
                pspec = (d.get("params") or {}).get(pname)
                if not pspec:
                    e.append(f"output.count 引用的参数不存在：{pname}")
                elif pspec.get("type") != "int":
                    e.append(f"output.count 只能引用 int 参数：{pname}")
        if graph and _nonempty_str(out.get("node")):
            if out["node"] not in graph:
                e.append(f"output.node 不存在：{out['node']}")
            covered.add((out["node"], str(out.get("input"))))

    # 图里的占位符必须被某个绑定/输出覆盖，否则渲染后必然残留
    if graph:
        for nid, inp, token in placeholder_sites(graph):
            if (nid, inp) not in covered:
                e.append(f"占位符 {{{{{token}}}}} 所在 {nid}.{inp} 没有任何绑定覆盖")

    constants = d.get("constants", [])
    if not isinstance(constants, list):
        e.append("constants 应为数组")
    else:
        for i, c in enumerate(constants):
            w = f"constants[{i}]"
            if not isinstance(c, dict) or not _nonempty_str(c.get("node")) \
                    or not _nonempty_str(c.get("input")) or "value" not in c:
                e.append(f"{w} 需要 node/input/value")
            elif graph and not node_inputs(graph, c["node"], c["input"])[0]:
                e.append(f"{w} 指向不存在的节点/输入：{c['node']}.{c['input']}")

    exp = d.get("expect_defaults", {})
    if not isinstance(exp, dict):
        e.append("expect_defaults 应为对象")
        exp = {}
    if "min_bytes" in exp and (not _is_int(exp["min_bytes"]) or exp["min_bytes"] <= 0):
        e.append("expect_defaults.min_bytes 应为正整数")
    for k in ("width", "height"):
        if k in exp and not (isinstance(exp[k], list) and all(_is_int(x) for x in exp[k])):
            e.append(f"expect_defaults.{k} 应为整数数组")
    if "duration_s" in exp and not (isinstance(exp["duration_s"], list) and len(exp["duration_s"]) == 2):
        e.append("expect_defaults.duration_s 应为 [下限,上限]")
    if "freeze" in exp and not isinstance(exp["freeze"], dict):
        e.append("expect_defaults.freeze 应为对象")
    if "images" in exp:
        v = exp["images"]
        if _is_int(v):
            if v < 1:
                e.append("expect_defaults.images 应为正整数")
        elif not (isinstance(v, str) and COUNT_RE.match(v)):
            e.append("expect_defaults.images 应为正整数或 {{PARAM}} 形式")
        else:
            pname = v[2:-2].lower()
            pspec = (d.get("params") or {}).get(pname)
            if not pspec:
                e.append(f"expect_defaults.images 引用的参数不存在：{pname}")
            elif pspec.get("type") != "int":
                e.append(f"expect_defaults.images 只能引用 int 参数：{pname}")

    limits = d.get("limits", {})
    if not isinstance(limits, dict):
        e.append("limits 应为对象")
    rg = d.get("required_gpu", [])
    if not isinstance(rg, list) or not rg:
        e.append("required_gpu 应为非空数组")
    return e


# ------------------------------------------------------------------ compat
def validate_compat(d: dict) -> list[str]:
    e: list[str] = []
    if d.get("schema") != COMPAT_SCHEMA:
        e.append(f"schema 应为 {COMPAT_SCHEMA}，实际 {d.get('schema')!r}")
    entries = d.get("entries")
    if not isinstance(entries, list):
        return e + ["entries 应为数组"]
    seen = set()
    for i, en in enumerate(entries):
        w = f"entries[{i}]"
        if not isinstance(en, dict):
            e.append(f"{w} 应为对象")
            continue
        for k in ("workflow", "model"):
            if not _nonempty_str(en.get(k)):
                e.append(f"{w}.{k} 缺失")
        status = en.get("status")
        if status not in ("verified", "incompatible"):
            e.append(f"{w}.status 只允许 verified/incompatible（其余由结构推导）")
        ev = en.get("evidence", [])
        if not isinstance(ev, list) or not all(isinstance(x, str) for x in ev):
            e.append(f"{w}.evidence 应为字符串数组")
        if status == "verified" and not ev:
            e.append(f"{w} verified 必须给出 evidence（真实成功作业 ID）")
        pair = (en.get("workflow"), en.get("model"))
        if pair in seen:
            e.append(f"{w} 重复的 workflow/model 组合：{pair}")
        seen.add(pair)
        if not isinstance(en.get("verified_at", ""), str):
            e.append(f"{w}.verified_at 应为字符串")
    return e


def load_and_validate(kind: str, path: Path, *, root: Path | None = None) -> dict:
    d = load_json(path)
    fn = {"model": validate_model, "workflow": validate_workflow, "compat": validate_compat}[kind]
    errs = fn(d) if kind != "workflow" else fn(d, root=root)
    if errs:
        raise ConfigError(errs, str(path))
    return d
