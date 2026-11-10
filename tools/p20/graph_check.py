#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流 graph 完整性检查：找出「API 格式下会导致 400」的缺失输入。

## 为什么需要

ComfyUI 的 UI 导出成 API 格式（`workflows/*.api.json`）时会**丢掉一批输入**：

  · 有默认值的输入（如 `batch_size`）—— 这个没关系，ComfyUI 服务端会补；
  · **没有默认值的输入** —— 这个会让 `/prompt` 直接返回 400 `required_input_missing`。

第二种就是 2026-11-10 实测踩到的：
`pack_image_qwen_image_edit_2511_int8` 一次缺 5 个输入（prompt ×2、
reference_latents_method ×2、SaveImageAdvanced 的 format 子字段），
每条要花 ¥0.10 建机才能发现一次，非常不划算。

这个工具把这类问题**在上机之前**查出来，¥0。

## 判据（三段分类，顺序不能换）

    has_default   required 里第二项 dict 带 default → 服务端会补，**不算缺**
    dynamic       COMFY_AUTOGROW_V3 / DYNAMICCOMBO_V3 / MATCHTYPE_V3
                  → API 格式下键名是运行时生成的（可能带子键），
                    静态查不出，**标记为「需实测」而不是「缺」**
    missing       既没默认值、又不是动态类型 → **真的缺，必须补**

把 dynamic 单独归类很重要：第一版把 `ComfyMathExpression.values`
（`COMFY_AUTOGROW_V3`）算成「缺」，8 条工作流报了 53 个问题，
其中大部分是误报 —— 照那个清单修会把能跑的工作流改坏。

## 用法

    python3 tools/p20/graph_check.py                    # 全部 experimental
    python3 tools/p20/graph_check.py --group            # 按"同源组"归并
    python3 tools/p20/graph_check.py --workflow KEY     # 只查一条
    python3 tools/p20/graph_check.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OBJECT_INFO = ROOT / "reports" / "p2" / "p21" / "evidence" / "objectinfo-v4.json"

# API 格式下键名运行时生成的类型。静态检查必须放过它们，否则全是误报。
DYNAMIC_TYPES = frozenset({
    "COMFY_AUTOGROW_V3",      # ComfyMathExpression.values / BatchImagesNode.images
    "COMFY_DYNAMICCOMBO_V3",  # ResizeImageMaskNode.resize_type（选 key 后带子字段）
    "COMFY_MATCHTYPE_V3",     # ResizeImageMaskNode.input
})

# 连接型（必须由别的节点喂），出现在 missing 里说明连线断了，不是"忘了填值"
LINK_TYPES = frozenset({
    "IMAGE", "LATENT", "MODEL", "CLIP", "VAE", "CONDITIONING",
    "MASK", "AUDIO", "VIDEO", "CONTROL_NET", "CLIP_VISION",
})


def load_object_info(path: Path = OBJECT_INFO) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d.get("object_info") if isinstance(d.get("object_info"), dict) else d


def _meta(spec) -> dict:
    return spec[1] if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict) else {}


def combo_options(spec) -> list | None:
    """取 COMBO 的候选值。三种写法都见过：meta.options / meta 本身是 list / spec[0] 是 list。"""
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return spec[0]
    if len(spec) > 1:
        m = spec[1]
        if isinstance(m, dict) and "options" in m:
            return m["options"]
        if isinstance(m, list):
            return m
    return None


@dataclass
class Missing:
    node: str
    class_type: str
    field_name: str
    kind: str              # missing | dynamic
    type_name: str = ""
    options: list = field(default_factory=list)


@dataclass
class Result:
    workflow: str
    model: str
    nodes: int
    missing: list[Missing] = field(default_factory=list)
    dynamic: list[Missing] = field(default_factory=list)
    unknown_class: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unknown_class

    def by_class(self) -> dict[str, list[Missing]]:
        """按节点类归并 —— 同源工作流的问题会聚到一起，修一条顶多条。"""
        out: dict[str, list[Missing]] = {}
        for m in self.missing:
            out.setdefault(m.class_type, []).append(m)
        return out


def check(workflow_key: str, oi: dict, *, strict: bool = False) -> Result:
    """从 registry 读工作流，只检查 graph 本身。"""
    from tools.p20 import registry

    wf = registry.get_workflow(workflow_key)
    graph = json.loads((ROOT / wf["graph"]).read_text(encoding="utf-8"))
    model = (wf.get("model") or {}).get("key") or wf.get("model_key") or ""
    return _check_graph(graph, oi, workflow_key, str(model), strict=strict)


def _check_graph(graph: dict, oi: dict, workflow_key: str, model: str,
                 *, strict: bool = False) -> Result:
    """核心判据。单独抽出是为了单测能直接灌假 graph + 假 objectinfo。"""
    r = Result(workflow=workflow_key, model=model, nodes=len(graph))

    for nid, node in graph.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        ct = node["class_type"]
        ins = node.get("inputs") or {}
        spec = oi.get(ct)
        if not spec:
            r.unknown_class.append(f"{nid}:{ct}")
            continue
        req = (spec.get("input") or {}).get("required") or {}
        for fname, fspec in req.items():
            if fname in ins:
                continue
            m = _meta(fspec)
            tname = fspec[0] if isinstance(fspec, list) and fspec else ""
            tname = tname if isinstance(tname, str) else "COMBO"
            # 带 default 的 required 字段：**默认放过，--strict 时算缺**。
            #
            # 实测两种都出现过（2026-11-10）：
            #   · `qwen_image_edit_2511` 补完后跑通，其中 `CFGNorm.strength`
            #     虽然有默认值，但图里没写它 —— 我补了，所以没验证到"能不能省"。
            #   · `flux2_klein` 里三个写法完全相同的 ImageScaleToTotalPixels，
            #     ComfyUI 只对**执行路径上真被求值**的那个报 `megapixels` 缺失，
            #     另两个没报。
            #
            # 结论：default 能不能省取决于节点是否被求值，静态判断不了。
            # 所以默认按"可省"（否则 18/24 条假红），需要上机前彻底体检时用 --strict。
            if "default" in m and not strict:
                continue
            if tname in DYNAMIC_TYPES:
                # AUTOGROW / DYNAMICCOMBO 在 API 格式里是**带前缀的子键**：
                #   ComfyMathExpression.values  →  "values.a": 5
                #   ResizeImageMaskNode.resize_type → "resize_type.width": 512
                # 所以不能只看 `fname in ins`，要看有没有 `fname.` 开头的键。
                # 踩过：第一版漏了这一步，把已经写好的 values.a 报成"缺"。
                has_sub = any(k.startswith(f"{fname}.") for k in ins)
                if not has_sub:
                    r.dynamic.append(Missing(nid, ct, fname, "dynamic", tname))
                continue
            if tname in LINK_TYPES:
                # 连接型没连线 = 图本身断了。单列，因为它不是"补个值"能解决的。
                r.missing.append(Missing(nid, ct, fname, "missing", f"{tname}(未连线)"))
                continue
            opts = combo_options(fspec) or []
            r.missing.append(Missing(nid, ct, fname, "missing", tname, opts))
    return r


def groups(results: list[Result]) -> dict[str, list[str]]:
    """把工作流按「缺的节点类集合」归并 —— 同源模板修一条顶多条。"""
    out: dict[str, list[str]] = {}
    for r in results:
        if r.ok and not r.dynamic:
            continue
        sig = ",".join(sorted(r.by_class()))
        if not sig:
            sig = "(仅动态输入)"
        out.setdefault(sig, []).append(r.workflow)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="工作流 graph 完整性检查（¥0）")
    ap.add_argument("--workflow", help="只查一个 workflow_key")
    ap.add_argument("--group", action="store_true", help="按同源组归并展示")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="查全部 54 条（默认只看 experimental）")
    ap.add_argument("--strict", action="store_true",
                    help="把带 default 的 required 字段也算缺（上机前彻底体检）")
    a = ap.parse_args(argv)

    from tools.p20 import registry
    oi = load_object_info()

    if a.workflow:
        keys = [a.workflow]
    else:
        keys = []
        for w in registry.list_workflows():
            c = w.get("compat") or {}
            st = c.get("status") if isinstance(c, dict) else c
            if a.all or st == "experimental":
                keys.append(w["key"])

    results = [check(k, oi, strict=a.strict) for k in keys]

    if a.json:
        print(json.dumps([{
            "workflow": r.workflow, "model": r.model, "ok": r.ok,
            "missing": [m.__dict__ for m in r.missing],
            "dynamic": [m.__dict__ for m in r.dynamic],
            "unknown_class": r.unknown_class,
        } for r in results], ensure_ascii=False, indent=2))
        return 0

    if a.group:
        g = groups(results)
        print(f"共 {len(results)} 条工作流，归并为 {len(g)} 组问题\n")
        for i, (sig, wfs) in enumerate(sorted(g.items(), key=lambda kv: -len(kv[1])), 1):
            print(f"── 第 {i} 组（{len(wfs)} 条，修一顶{len(wfs)}）")
            for w in wfs:
                print(f"     · {w}")
            print(f"     缺的节点类: {sig}\n")
        clean = [r.workflow for r in results if r.ok and not r.dynamic]
        if clean:
            print(f"✓ 无缺失（{len(clean)} 条）: {', '.join(clean)}")
        return 0

    bad = 0
    for r in results:
        if r.ok and not r.dynamic:
            print(f"✓ {r.workflow}")
            continue
        bad += 1 if not r.ok else 0
        mark = "✗" if not r.ok else "?"
        print(f"{mark} {r.workflow}  [{r.model}]  {r.nodes} 节点")
        for m in r.missing:
            opts = ""
            if m.options:
                short = [o.get("key") if isinstance(o, dict) else o for o in m.options][:6]
                opts = f"  候选={short}"
            print(f"     {m.node} {m.class_type}.{m.field_name} "
                  f"type={m.type_name}{opts}")
        for m in r.dynamic:
            print(f"     {m.node} {m.class_type}.{m.field_name} "
                  f"[{m.type_name} 运行时动态，需实测]")
        for u in r.unknown_class:
            print(f"     {u} [节点类不在镜像]")
    print(f"\n{len(results)} 条中 {bad} 条有硬缺失")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
