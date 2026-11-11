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


# 类型兼容：ComfyUI 里这几对可以互给，不能一律判错。
#   IMAGE ↔ MASK  很多节点两者通吃
#   INT   ↔ FLOAT 数值互转
#   *     →  COMFY_*  动态类型接受任何输入（运行时才校验）
_COMPATIBLE_PAIRS = {
    frozenset({"IMAGE", "MASK"}),
    frozenset({"INT", "FLOAT"}),
}


def _type_name(spec) -> str:
    """输入声明的类型名。

    AUTOGROW / DYNAMICCOMBO 要**下钻到 template** 取真实类型 ——
    它们的顶层类型是 `COMFY_AUTOGROW_V3`，但每个子槽位要的是具体类型：

        ref_videos  →  template.required.ref_video : IMAGE   （帧序列，不是 VIDEO）

    踩过（2026-11-11）：顶层当 COMFY_* 一律放行，于是
    `ref_videos.ref_video_0` 接 `LoadVideo`（输出 VIDEO）检查不出来，
    真机才报 return_type_mismatch。
    """
    if not isinstance(spec, list) or not spec:
        return ""
    t = spec[0]
    if isinstance(t, str):
        if t in ("COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3") and len(spec) > 1:
            meta = spec[1] if isinstance(spec[1], dict) else {}
            tpl = meta.get("template") or {}
            req = ((tpl.get("input") or {}).get("required") or {})
            inner = next(iter(req.values()), None)
            if inner:
                return _type_name(inner)
        return t
    if isinstance(t, list):
        return "COMBO"
    return ""


def _types_compatible(got: str, want: str) -> bool:
    """目标类型可能是**联合**（`FLOAT,INT,BOOLEAN`）或**通配**（`*`）。

    踩过（2026-11-11）：按字符串直接比，于是 `INT` 对 `FLOAT,INT,BOOLEAN`
    被判成不兼容 —— 一趟扫出 33 处「错误」，全是假的。
    正确做法是把 want 按逗号拆开，任意一个匹配就算通过。
    """
    if want.startswith("COMFY_") or got.startswith("COMFY_"):
        return True
    if want in ("*", ""):
        return True
    for alt in (a.strip() for a in want.split(",")):
        if not alt:
            continue
        if alt == "*" or alt == got:
            return True
        if frozenset({got, alt}) in _COMPATIBLE_PAIRS:
            return True
    return False



def load_object_info(path: Path = OBJECT_INFO) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d.get("object_info") if isinstance(d.get("object_info"), dict) else d


def _meta(spec) -> dict:
    return spec[1] if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict) else {}


def dynamic_combo_required(spec, chosen: str) -> list[str]:
    """DYNAMICCOMBO 选中某个 key 后，**必须**出现的子参数名。

    为什么需要（2026-11-11 实测）：`TextGenerateLTX2Prompt.sampling_mode`
    是 DYNAMICCOMBO，图里写了 `"sampling_mode": "on"` 却没写它需要的
    `top_k/top_p/temperature/repetition_penalty/seed/min_p`。
    提交时 ComfyUI 报 6 个 `required_input_missing`，
    而 `graph_check` 当时判"通过"——因为它只检查"有没有以 `sampling_mode.`
    开头的键"，而这个节点根本不用点号写法，是**平铺**的。

    更关键的是：同一个字段 `on` 要 6 个、`off` 一个都不要。
    所以必须**按图里实际选的那个 key** 去展开，不能笼统判断。
    """
    meta = _meta(spec)
    options = meta.get("options")
    if not isinstance(options, list):
        return []
    for opt in options:
        if not isinstance(opt, dict) or str(opt.get("key")) != str(chosen):
            continue
        req = (opt.get("inputs") or {}).get("required") or {}
        return [str(k) for k in req]
    return []


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
    kind: str              # missing | dynamic | bad_value
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
    type_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unknown_class and not self.type_errors

    def by_class(self) -> dict[str, list[Missing]]:
        """按节点类归并 —— 同源工作流的问题会聚到一起，修一条顶多条。"""
        out: dict[str, list[Missing]] = {}
        for m in self.missing:
            out.setdefault(m.class_type, []).append(m)
        return out


def check(workflow_key: str, oi: dict, *, strict: bool = False,
          check_values: bool = False) -> Result:
    """从 registry 读工作流，只检查 graph 本身。"""
    from tools.p20 import registry

    wf = registry.get_workflow(workflow_key)
    graph = json.loads((ROOT / wf["graph"]).read_text(encoding="utf-8"))
    model = (wf.get("model") or {}).get("key") or wf.get("model_key") or ""
    return _check_graph(graph, oi, workflow_key, str(model), strict=strict,
                        check_values=check_values)


def _check_graph(graph: dict, oi: dict, workflow_key: str, model: str,
                 *, strict: bool = False, check_values: bool = False) -> Result:
    """核心判据。单独抽出是为了单测能直接灌假 graph + 假 objectinfo。

    check_values：是否校验「字段值在候选列表里」。
    默认关 —— 静态快照 object_info 不含运行时软链，开了会全线误报。
    用真机采的 object_info（`--live`）时打开。
    """
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
        # ---- 连线类型检查 ----
        # 静态检查以前只看"字段在不在"，查不出"类型对不对"。
        # 踩过（2026-11-11）：`ref_videos.ref_video_0` 要 IMAGE，
        # 我接了 `LoadVideo` 的输出 VIDEO —— 字段齐全、graph_check 全绿，
        # 真机才报 `return_type_mismatch`，一次 ¥0.09 建机费换一条错误信息。
        #
        # 判据：每个连线 [源节点, 输出索引] 取源节点的 output 列表对应类型，
        # 和目标输入声明的类型比对（少数类型允许互转，见 COMPATIBLE）。
        for fname, val in ins.items():
            if not (isinstance(val, list) and len(val) == 2):
                continue
            src_id, out_idx = str(val[0]), val[1]
            src = graph.get(src_id)
            if not isinstance(src, dict):
                continue
            src_cls = src.get("class_type")
            src_spec = oi.get(src_cls) or {}
            src_out = src_spec.get("output") or []
            if not isinstance(out_idx, int) or out_idx >= len(src_out):
                continue
            got = src_out[out_idx]
            # 目标声明的类型
            want = None
            # 子槽位（`ref_videos.ref_video_0`）要拆出组名去查 `ref_videos` 的声明
            base = fname.split(".")[0] if "." in fname else fname
            for grp in ("required", "optional"):
                fspec = ((spec.get("input") or {}).get(grp) or {}).get(base)
                if fspec:
                    want = _type_name(fspec)
                    break
            if not want or not isinstance(got, str):
                continue
            if got == want or _types_compatible(got, want):
                continue
            r.type_errors.append(
                f"{nid}.{fname} ← {src_id}({src_cls})[{out_idx}] "
                f"给出 {got}，但需要 {want}")
        req = (spec.get("input") or {}).get("required") or {}
        for fname, fspec in req.items():
            tname0 = fspec[0] if isinstance(fspec, list) and fspec else ""
            # DYNAMICCOMBO 的特殊之处：**主键写了不代表完事**。
            # 主键的值（如 sampling_mode="on"）会带来一组必填子参数，
            # 所以不能像普通字段那样"在 ins 里就跳过"，要往下查子参数。
            # 2026-11-11 踩到：TextGenerateLTX2Prompt 写了 sampling_mode="on"
            # 却没给 top_k/temperature 等 6 个，提交时被 ComfyUI 拒，
            # 而这里因为 `fname in ins` 直接 continue，判了"通过"。
            if tname0 == "COMFY_DYNAMICCOMBO_V3":
                # 三条独立规则，缺一不可：
                #
                # ① 有子键但缺主键 → 缺主键。
                #    ComfyUI 先按主键分派实现，没有主键会报
                #    "missing 1 required positional argument: 'resize_type'"。
                #    踩过：7 处 ResizeImageMaskNode 只写 resize_type.width/height。
                #
                # ② 主键写了、但选中的 key 要求的子参数没给 → 缺子参数。
                #    踩过（2026-11-11）：TextGenerateLTX2Prompt 写 sampling_mode="on"
                #    却一个子参数都没给，提交时被 ComfyUI 拒。
                #
                # ③ 主键和子键都没有 → dynamic（运行时才定，不算硬缺失）。
                #
                # 子参数有两种写法，都要认：点号 `resize_type.width`、平铺 `top_k`。
                has_sub = any(k.startswith(f"{fname}.") for k in ins)
                if has_sub and fname not in ins:
                    r.missing.append(Missing(nid, ct, f"{fname}（有子键缺主键）",
                                             "missing", tname0))
                    continue
                need = dynamic_combo_required(fspec, str(ins.get(fname) or ""))
                lacked = [n for n in need
                          if n not in ins and f"{fname}.{n}" not in ins]
                if lacked:
                    for n in lacked:
                        r.missing.append(Missing(
                            nid, ct, f"{fname}={ins.get(fname)} 需要 {n}",
                            "missing", tname0, need))
                elif fname not in ins:
                    r.dynamic.append(Missing(nid, ct, fname, "dynamic", tname0))
                continue
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
                # DYNAMICCOMBO 有个反直觉的要求：**主键和子键都要给**。
                # 只写 `resize_type.width` 而不写 `resize_type`，
                # ComfyUI 会报 "missing 1 required positional argument: 'resize_type'"
                # —— 因为它按主键分派到具体实现，子键只是参数。
                # 踩过：7 处 ResizeImageMaskNode 全是这个毛病。
                # 只有 DYNAMICCOMBO 需要主键；AUTOGROW 天生就用子键表达
                # （`values.a` 等价于 values 有一个名为 a 的槽），没有"主键"概念。
                if tname == "COMFY_DYNAMICCOMBO_V3" \
                        and any(k.startswith(f"{fname}.") for k in ins) \
                        and fname not in ins:
                    r.missing.append(Missing(nid, ct, f"{fname}（有子键缺主键）",
                                             "missing", tname))
                    continue
                # AUTOGROW / DYNAMICCOMBO 在 API 格式里是**带前缀的子键**：
                #   ComfyMathExpression.values  →  "values.a": 5
                #   ResizeImageMaskNode.resize_type → "resize_type.width": 512
                # 所以不能只看 `fname in ins`，要看有没有 `fname.` 开头的键。
                # 踩过：第一版漏了这一步，把已经写好的 values.a 报成"缺"。
                has_sub = any(k.startswith(f"{fname}.") for k in ins)
                # DYNAMICCOMBO 已在循环开头统一处理（要按选中的 key 展开子参数）。
                if not has_sub:
                    r.dynamic.append(Missing(nid, ct, fname, "dynamic", tname))
                continue
            if tname in LINK_TYPES:
                # 连接型没连线 = 图本身断了。单列，因为它不是"补个值"能解决的。
                r.missing.append(Missing(nid, ct, fname, "missing", f"{tname}(未连线)"))
                continue
            opts = combo_options(fspec) or []
            r.missing.append(Missing(nid, ct, fname, "missing", tname, opts))

        # 值校验：字段有值、是**纯枚举**型、候选列表非空，但值不在列表里。
        #
        # 为什么要加（2026-11-11 实测）：`LTXAVTextEncoderLoader.text_encoder`
        # 图里写 `gemma_3_12B_it_fp4_mixed.safetensors`，镜像里只有
        # int4_convrot / int8tensormixed / bf16。前面所有检查都通过，
        # 提交那一刻才被拒（`value_not_in_list`）。这类占「跑到一半才发现」的大头。
        #
        # 三个必须避开的误报源：
        #   ① DYNAMICCOMBO 的 options 是**对象数组**（{key, inputs}），
        #      拿它当字符串列表比必然全不中 → 直接跳过，它由上面单独处理
        #   ② 静态快照的 object_info 里没有我们运行时建的软链，
        #      拿它查会全线误报 → 只在 `--live`（真机快照）模式下启用
        #   ③ 连线（["12", 0]）不是值，跳过
        if check_values:
            for fname, val in ins.items():
                if fname not in req or isinstance(val, list):
                    continue
                # 占位符（`{{UPSCALE_MODEL}}` 等）是渲染时替换的，不是最终值。
                # 拿它比候选列表必然不中 —— 2026-11-11 因此把 verified 的
                # `detail_polish` 误判成"必失败"。
                if isinstance(val, str) and "{{" in val:
                    continue
                fspec = req[fname]
                tname = fspec[0] if isinstance(fspec, list) and fspec else ""
                if not isinstance(tname, str) or tname in DYNAMIC_TYPES:
                    continue
                opts = combo_options(fspec)
                if not opts or not all(isinstance(o, str) for o in opts):
                    continue
                if str(val) in opts:
                    continue
                r.missing.append(Missing(
                    nid, ct, f"{fname}={val!r} 不在候选里", "bad_value", tname,
                    opts[:12]))
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
    ap.add_argument("--object-info", metavar="FILE", dest="object_info",
                    help="用指定的 object_info 快照（默认 reports/p2/p21/evidence/objectinfo-v4.json）")
    ap.add_argument("--live", action="store_true",
                    help="同时校验字段值是否在候选列表里。**必须配真机快照**，"
                         "静态快照不含运行时软链会误报")
    ap.add_argument("--strict", action="store_true",
                    help="把带 default 的 required 字段也算缺（上机前彻底体检）")
    a = ap.parse_args(argv)

    from tools.p20 import registry
    oi = load_object_info(Path(a.object_info)) if a.object_info else load_object_info()

    if a.workflow:
        keys = [a.workflow]
    else:
        keys = []
        for w in registry.list_workflows():
            c = w.get("compat") or {}
            st = c.get("status") if isinstance(c, dict) else c
            if a.all or st == "experimental":
                keys.append(w["key"])

    results = [check(k, oi, strict=a.strict, check_values=a.live) for k in keys]

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
