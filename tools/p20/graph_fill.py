#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图参数自动补全：把「必填但图里没写」的输入补上默认值。

## 适用范围（这是重点，别滥用）

**只补「有默认值」或「有唯一合理候选」的字段。** 具体：

    有 default      → 用 default（ComfyUI 自己也会用它，只是显式写出来更稳）
    是枚举、非空     → 用第一个候选（通常就是官方默认）
    是连线型        → **不补**（补不了，说明图断了，要人改）
    DYNAMICCOMBO   → 主键缺就补候选里的第一个 key，并递归补它的子参数

**不补的东西：**

  · 画布语义（`xx.png`、`positive` 这种字符串）—— 猜错了会静默出错片
  · 版本号/模型名 —— 补错了等于用错权重，成片里看不出来
  · 任何需要「理解工作流意图」的字段

所以补完必须人看一眼 `--dry-run` 的输出。这个工具的定位是
**「把 43 个显而易见的空字段填掉，把剩下的 5 个真问题露出来」**，
不是「自动修好一切」。

## 为什么需要

2026-11-11 的 `--live` 值校验扫出 10 条工作流、44 个问题。逐条手填要半天，
而这 44 个里绝大多数是「ComfyUI 有默认值、导出模板时丢了」——
补默认值既不改变行为，又能让静态检查安静下来，把真正的 5 个问题凸显出来。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 不自动补的输入名：这些字段的值决定"用哪个模型/哪个文件/什么内容"，
# 填错了不会报错，只会静默出错的结果。
NEVER_AUTOFILL = frozenset({
    "ckpt_name", "unet_name", "lora_name", "vae_name", "clip_name",
    "clip_name1", "clip_name2", "model_name", "text_encoder", "control_net_name",
    "style_model_name", "gligen_name", "upscale_model",
    "image", "video", "audio", "mask", "image_src", "image_2",
    "filename_prefix", "file", "directory",
    # 内容类：补空串会生成一张废片，而且作业会 PASS（文件合法、内容为空）。
    # 这不是"少个参数"，是"用户没写提示词"—— 必须让人填。
    "prompt", "text", "neg_prompt", "negative", "positive", "text_a", "text_b",
    "string_a", "string_b",
    # 注意 expression 不在此列：ComfyMathExpression 的默认表达式（'a + b'）
    # 是节点自带的语义，不是"用户内容"。补它和补 `steps=20` 一个性质。
    # 但 `value` / `string_*` 不同 —— 它们直接进画面/文本。
})


@dataclass
class Fill:
    node: str
    class_type: str
    input_name: str
    value: object
    why: str

    def as_dict(self) -> dict:
        return {"node": self.node, "class_type": self.class_type,
                "input": self.input_name, "value": self.value, "why": self.why}


@dataclass
class Plan:
    workflow: str
    graph_path: str
    fills: list[Fill] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"workflow": self.workflow, "graph": self.graph_path,
                "fills": [f.as_dict() for f in self.fills],
                "skipped": self.skipped}


def _meta(spec) -> dict:
    return spec[1] if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict) else {}


def _options(spec) -> list | None:
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return spec[0]
    meta = _meta(spec)
    opts = meta.get("options")
    if isinstance(opts, list) and opts and all(isinstance(o, str) for o in opts):
        return opts
    return None


def plan_workflow(workflow_key: str, oi: dict) -> Plan:
    """算「这条工作流该补哪些字段」。不写文件。"""
    from tools.p20 import registry as R

    wf = R.get_workflow(workflow_key)
    gp = Path(str(wf.get("graph") or ""))
    if not gp.is_absolute():
        gp = ROOT / gp
    plan = Plan(workflow=workflow_key, graph_path=str(gp.relative_to(ROOT)))
    if not gp.is_file():
        plan.skipped.append({"reason": f"图不存在：{gp}"})
        return plan

    graph = json.loads(gp.read_text(encoding="utf-8"))

    for nid, node in (graph or {}).items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        ct = node["class_type"]
        if ct not in oi:
            continue                      # 节点类本身缺失，不归这里管
        ins = node.setdefault("inputs", {})
        req = (oi[ct].get("input") or {}).get("required") or {}

        for fname, fspec in req.items():
            tname = fspec[0] if isinstance(fspec, list) and fspec else ""
            if isinstance(tname, list):
                tname = "COMBO"

            # ---- DYNAMICCOMBO：补主键 + 递归补子参数
            if tname == "COMFY_DYNAMICCOMBO_V3":
                opts = _meta(fspec).get("options") or []
                keys = [o.get("key") for o in opts if isinstance(o, dict)]
                if fname not in ins:
                    if not keys:
                        plan.skipped.append({"node": nid, "class": ct,
                                             "input": fname, "reason": "候选为空"})
                        continue
                    if fname in NEVER_AUTOFILL:
                        plan.skipped.append({"node": nid, "class": ct,
                                             "input": fname, "reason": "属不补名单"})
                        continue
                    ins[fname] = keys[0]
                    plan.fills.append(Fill(nid, ct, fname, keys[0],
                                           f"DYNAMICCOMBO 主键，取第一个候选"))
                # 子参数
                chosen = str(ins.get(fname) or "")
                sub = next((o.get("inputs") or {} for o in opts
                            if isinstance(o, dict) and str(o.get("key")) == chosen), {})
                for sk, sspec in (sub.get("required") or {}).items():
                    if sk in ins or f"{fname}.{sk}" in ins:
                        continue
                    v, why = _pick_value(sspec, sk)
                    if v is _SKIP:
                        plan.skipped.append({"node": nid, "class": ct,
                                             "input": f"{fname}.{sk}",
                                             "reason": "无法安全推断"})
                        continue
                    ins[sk] = v
                    plan.fills.append(Fill(nid, ct, sk, v, why))
                continue

            if fname in ins:
                continue                  # 已有值（含连线）
            if tname in ("COMFY_AUTOGROW_V3", "COMFY_MATCHTYPE_V3"):
                continue                  # 动态集合，跳
            v, why = _pick_value(fspec, fname)
            if v is _SKIP:
                plan.skipped.append({"node": nid, "class": ct, "input": fname,
                                     "reason": why})
                continue
            ins[fname] = v
            plan.fills.append(Fill(nid, ct, fname, v, why))

    return plan


class _Skip:
    def __repr__(self):
        return "<skip>"


_SKIP = _Skip()


def _pick_value(fspec, name: str):
    """为一个字段挑一个安全的值。返回 (值, 理由) 或 (_SKIP, 原因)。"""
    if name in NEVER_AUTOFILL:
        return _SKIP, "属不补名单（会改变输出内容/用错文件）"
    meta = _meta(fspec)
    tname = fspec[0] if isinstance(fspec, list) and fspec else ""

    # ① 有 default：最安全，直接用
    if "default" in meta:
        return meta["default"], "用 schema 的 default"

    # ② 枚举：取第一个候选（通常是官方默认）
    opts = _options(fspec)
    if opts:
        return opts[0], f"枚举，取第一个候选 {opts[0]!r}"

    # ③ 连线型：补不了
    if tname in ("MODEL", "CLIP", "VAE", "IMAGE", "LATENT", "CONDITIONING",
                 "AUDIO", "VIDEO", "MASK", "CONTROL_NET", "SAMPLER", "SIGMAS",
                 "GUIDER", "NOISE", "UPSCALE_MODEL", "CLIP_VISION"):
        return _SKIP, f"连线型（{tname}），图本身断了，需人工接"

    return _SKIP, f"既无 default 也非枚举（{tname}），不敢猜"


def apply_plan(plan: Plan, *, backup: bool = True) -> str:
    """把 plan 写回图文件。返回备份路径（没备份则空串）。"""
    gp = ROOT / plan.graph_path
    if not plan.fills:
        return ""
    bak = ""
    if backup:
        bak_path = gp.with_suffix(gp.suffix + ".bak")
        shutil.copy2(gp, bak_path)
        bak = str(bak_path.relative_to(ROOT))

    graph = json.loads(gp.read_text(encoding="utf-8"))
    for f in plan.fills:
        node = graph.get(f.node)
        if not isinstance(node, dict):
            continue
        node.setdefault("inputs", {})[f.input_name] = f.value
    gp.write_text(json.dumps(graph, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return bak


def main(argv=None) -> int:
    import argparse
    from tools.p2 import asset_preflight as AP

    ap = argparse.ArgumentParser(prog="graph_fill",
                                 description="补全图里「必填但没写」的字段（默认只演练）")
    ap.add_argument("workflows", nargs="*", help="工作流 key；不给则处理所有有问题的")
    ap.add_argument("--object-info", dest="object_info", metavar="FILE")
    ap.add_argument("--apply", action="store_true", help="真的写回图文件")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    oi = AP.load_object_info(a.object_info) if a.object_info else AP.load_object_info()
    if not oi:
        print("✗ 需要 object_info 证据")
        return 2

    keys = a.workflows
    if not keys:
        from tools.p20 import graph_check as G, registry as R
        keys = []
        for wf in R.list_workflows():
            gp = Path(str(wf.get("graph") or ""))
            if not gp.is_absolute():
                gp = ROOT / gp
            if not gp.is_file():
                continue
            g = json.loads(gp.read_text(encoding="utf-8"))
            r = G._check_graph(g, oi, wf["key"], "", check_values=True)
            if r.missing:
                keys.append(wf["key"])

    plans = [plan_workflow(k, oi) for k in keys]
    total = sum(len(p.fills) for p in plans)
    skipped = sum(len(p.skipped) for p in plans)

    if a.json:
        print(json.dumps({"plans": [p.as_dict() for p in plans],
                          "fills": total, "skipped": skipped},
                         ensure_ascii=False, indent=1))
    else:
        for p in plans:
            if not p.fills and not p.skipped:
                continue
            print(f"### {p.workflow}  ({p.graph_path})")
            for f in p.fills:
                print(f"   + {f.node:6s} {f.class_type}.{f.input_name} = {f.value!r}"
                      f"   ({f.why})")
            for s in p.skipped:
                print(f"   - {s.get('node','?'):6s} {s.get('class','?')}."
                      f"{s.get('input','?')}   跳过：{s.get('reason')}")
        print()
        print(f"可补 {total} 处 · 跳过 {skipped} 处")

    if a.apply:
        for p in plans:
            bak = apply_plan(p)
            if bak:
                print(f"  已写回 {p.graph_path}（备份 {bak}）")
        print("\n下一步：python3 -m tools.p20.p20ctl import && "
              "python3 -m tools.p2.asset_preflight")
    else:
        print("（演练模式，加 --apply 才写回）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
