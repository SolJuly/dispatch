#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 comfyui_workflows_json.zip（官方模板包）里**还没入库**的工作流导入 registry。

输入（包内）：
  workflows/api/<name>.api.json     扁平 API 图（可直接 POST /prompt）
  workflows/build_report.json       每个模板的 input_slots / status
  index.json                        playbooks（标题/许可/显存/商用标记）

输出（仓库内）：
  workflows/pack/<name>.api.json            图（原样复制，不改一个字节）
  registry/workflows/pack_<name>.json       p20.workflow/v1 配置（参数绑定来自 input_slots）
  registry/models/<model_key>.json          p20.model/v1（同主权重合并成一个模型）
  registry/compat.json                      追加 experimental/unverified 条目

刻意不做的事：
  · 不猜参数：只绑定 input_slots 里声明的 node.input；其余输入原样保留
  · 不冒充已验证：新导入一律 experimental（要 verified 需要真机跑一次并写 evidence）
  · 不动已有文件：同名 key / model key 已存在就跳过并计入 skipped
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REG_WF = ROOT / "registry" / "workflows"
REG_MD = ROOT / "registry" / "models"
REG_COMPAT = ROOT / "registry" / "compat.json"
GRAPH_DIR = ROOT / "workflows" / "pack"

VIDEO_OUT = {"SaveVideo", "SaveWEBM", "VHS_VideoCombine", "SaveAnimatedWEBP", "CreateVideo"}
IMAGE_OUT = {"SaveImage", "SaveImageAdvanced", "PreviewImage", "SaveImageWebsocket", "SaveAnimatedPNG"}
NAME_INPUTS = {
    "ckpt_name": ("ckpt", "CheckpointLoaderSimple"),
    "unet_name": ("unet", "UNETLoader"),
    "clip_name": ("clip", "CLIPLoader"),
    "clip_name1": ("clip1", "DualCLIPLoader"),
    "clip_name2": ("clip2", "DualCLIPLoader"),
    "vae_name": ("vae", "VAELoader"),
    "model_name": ("upscale", "UpscaleModelLoader"),
    "lora_name": ("lora", "LoraLoader"),
}


def slug(s: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    if s and s[0].isdigit():
        s = "m_" + s
    return s[:limit]


def out_kind(graph: dict) -> tuple[str, str | None, str | None]:
    """返回 (kind, 输出节点 id, 输出输入键)。"""
    vid, img = [], []
    for nid, node in graph.items():
        ct = node.get("class_type") or ""
        ins = node.get("inputs") or {}
        has_prefix = any(k in ins for k in ("filename_prefix", "prefix", "filename"))
        if ct in VIDEO_OUT or (has_prefix and "Video" in ct and "Save" in ct):
            vid.append((nid, node))
        elif ct in IMAGE_OUT or (has_prefix and "Save" in ct):
            img.append((nid, node))
    cands = vid or img
    if not cands:
        return ("", None, None)
    kind = "video" if vid else "image"
    nid, node = cands[0]
    ins = node.get("inputs") or {}
    key = next((k for k in ("filename_prefix", "prefix", "filename") if k in ins), None)
    if key is None:
        key = next((k for k in ("filename_prefix", "prefix", "filename") if k in ins), list(ins)[0] if ins else None)
    return (kind, nid, key)


ROLE_OF_CLASS = {
    "CheckpointLoaderSimple": "ckpt",
    "UNETLoader": "unet",
    "CLIPLoader": "clip",
    "DualCLIPLoader": "clip",
    "VAELoader": "vae",
    "CLIPVisionLoader": "clip_vision",
    "UpscaleModelLoader": "upscale",
    "LoraLoader": "lora",
    "LoraLoaderModelOnly": "lora",
    "LatentUpscaleModelLoader": "upscale",
    "ModelPatchLoader": "model_patch",
}


def scan_models(graph: dict) -> dict[str, list[tuple[str, str, str]]]:
    """角色 → [(node, input, filename)]。

    同一张图里同一个权重文件可能被多个 loader 引用（Wan2.2 双专家、两段式采样），
    按「文件 + 类」去重；同角色不同文件才编号（unet / unet_2）。
    """
    found: dict[str, list[tuple[str, str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    unnamed: list[str] = []
    for nid, node in graph.items():
        ct = node.get("class_type") or ""
        base = ROLE_OF_CLASS.get(ct)
        ins = node.get("inputs") or {}
        if not base:
            continue
        # 该类对应哪个文件名输入
        key = next((k for k, (role, loader) in NAME_INPUTS.items()
                    if role == base or (base == "lora" and role == "lora")), None)
        if key is None:
            if any(k.endswith("_name") for k in ins):
                unnamed.append(f"{nid}:{ct}")
            continue
        v = ins.get(key)
        if not isinstance(v, str) or not v.strip():
            continue
        if (ct, v) in seen:
            continue
        seen.add((ct, v))
        role = base
        n = 1
        while role in found and found[role][0][2] != v:
            n += 1
            role = f"{base}_{n}"
        found.setdefault(role, []).append((nid, key, v))
    return found


def params_from_slots(slots: list[dict]) -> tuple[dict, list[str]]:
    """input_slots → 参数契约。

    包里所有文本槽的 label 都是「提示词 / 编辑指令」，没有正/负之分，所以：
      · 先看默认文本像不像负面提示词（中英关键词打分）；
      · 都不像时按惯例「第一个=正向、第二个=负向」；
      · 多个 seed 槽（Wan2.2 双专家用两个 KSamplerAdvanced）合并成一个 seed，绑定多个节点。
    """
    params: dict = {}
    skipped: list[str] = []
    prompts: list[dict] = []
    seeds: list[dict] = []
    images: list[dict] = []

    NEG_ZH = ("过曝", "静态", "模糊", "低质量", "最差", "字幕", "残缺", "畸形", "丑陋", "灰暗")
    NEG_EN = ("low quality", "worst quality", "blurry", "watermark", "deformed", "bad anatomy")

    for s in slots or []:
        kind = (s.get("kind") or "").lower()
        node, inp = str(s.get("node")), s.get("input")
        if not node or not inp:
            continue
        if kind == "prompt":
            prompts.append(s)
        elif kind == "seed":
            seeds.append(s)
        elif kind in ("asset", "image", "mask", "video"):
            images.append(s)
        else:
            skipped.append(f"{node}.{inp}({kind or '?'})")

    def is_neg(text: str) -> bool:
        t = (text or "").lower()
        return any(k in t for k in NEG_ZH) or any(k in t for k in NEG_EN)

    neg_idx = next((i for i, s in enumerate(prompts) if is_neg(str(s.get("default") or ""))), None)
    if neg_idx is None and len(prompts) > 1:
        neg_idx = 1
    pos_n = 0
    for i, s in enumerate(prompts):
        if i == neg_idx:
            name = "neg_prompt"
        else:
            pos_n += 1
            name = "prompt" if pos_n == 1 else f"prompt_{pos_n}"
        if name in params:
            name += "_2"
        default = str(s.get("default") or "")[:300]
        params[name] = {
            "type": "string",
            "required": name == "prompt",
            "default": default if name != "neg_prompt" else default,
            "bindings": [{"node": str(s["node"]), "input": s["input"]}],
        }

    if seeds:
        params["seed"] = {
            "type": "int", "min": 0, "max": 2 ** 31 - 1, "default": 0,
            "bindings": [{"node": str(s["node"]), "input": s["input"]} for s in seeds],
        }

    for i, s in enumerate(images):
        name = "image" if i == 0 else f"image_{i + 1}"
        params[name] = {
            "type": "image", "required": True,
            "bindings": [{"node": str(s["node"]), "input": s["input"]}],
        }
    return params, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", default="/tmp/wfzip")
    ap.add_argument("--apply", action="store_true", help="真的写文件（默认只 dry-run）")
    a = ap.parse_args()
    pack = Path(a.pack)
    api_dir = pack / "workflows" / "api"
    report = json.loads((pack / "workflows" / "build_report.json").read_text(encoding="utf-8"))
    index = json.loads((pack / "index.json").read_text(encoding="utf-8"))
    playbooks = {p.get("official_template"): p for p in index.get("playbooks") or []}
    items = {i["name"]: i for i in report.get("items") or []}

    existing_wf = {p.stem for p in REG_WF.glob("*.json")}
    existing_md = {p.stem for p in REG_MD.glob("*.json")}
    known_model_keys = set(existing_md)

    created, skipped, warned = [], [], []
    model_cache: dict[str, dict] = {}

    for f in sorted(api_dir.glob("*.api.json")):
        name = f.name.replace(".api.json", "")
        key = f"pack_{slug(name)}"[ :64]
        if key in existing_wf:
            skipped.append((name, "registry 已有同名 key"))
            continue
        graph = json.loads(f.read_text(encoding="utf-8"))
        kind, onode, oinput = out_kind(graph)
        if not kind or not onode or not oinput:
            skipped.append((name, "识别不到输出节点（SaveImage/SaveVideo）"))
            continue
        models = scan_models(graph)
        if not models:
            skipped.append((name, "图里没有可识别的权重加载器"))
            continue

        # 主模型：unet 优先（VACE/VOID 这类图里 ckpt 往往是 SAM3 辅助模型）
        primary_role = next((r for r in ("unet", "ckpt") if r in models), None)
        if not primary_role:
            skipped.append((name, "没有 ckpt/unet 主权重"))
            continue
        pnode, pinput, pfile = models[primary_role][0]
        mkey = slug(Path(pfile).stem, 36) or f"{key}_model"
        if mkey in known_model_keys:          # 已被别的工作流占用 → 加本工作流后缀，避免权重集串味
            mkey = f"{mkey}_{slug(key.replace('pack_', ''), 24)}"[:64]
        known_model_keys.add(mkey)
        model_conf = {
            "schema": "p20.model/v1", "key": mkey,
            "name": f"{Path(pfile).stem}",
            "version": 1, "kind": kind, "family": slug(Path(pfile).stem, 20),
            "primary": primary_role,
            "files": {}, "defaults": {}, "constraints": {},
            "setup": {"profile": "all", "links": []},
        }
        for role, hits in models.items():
            nid, inp, fname = hits[0]
            ct = (graph[nid].get("class_type") or "")
            model_conf["files"].setdefault(role, {
                "name": fname, "loader": ct, "input": inp,
                "match": {"contains": Path(fname).stem},
            })
        slots = {role: {"node": hits[0][0], "input": hits[0][1]} for role, hits in models.items()}
        params, unsupported = params_from_slots(items.get(name, {}).get("input_slots") or [])
        if not params:
            skipped.append((name, "input_slots 为空，无法生成参数契约"))
            continue
        pb = playbooks.get(name) or {}
        notes = []
        if pb:
            notes.append(f"{pb.get('id')} · {pb.get('title')}")
            if pb.get("license"):
                notes.append(f"许可：{pb['license']}")
            if pb.get("commercial_ok") is not None:
                notes.append(f"商用：{'可' if pb['commercial_ok'] else '需授权'}")
            if pb.get("min_vram_gb"):
                notes.append(f"≥{pb['min_vram_gb']}GB 显存")
        notes.append("官方模板导入，未本地真机验证（compat=experimental）")
        if unsupported:
            notes.append("未绑定槽位：" + ", ".join(unsupported[:6]))

        wf = {
            "schema": "p20.workflow/v1", "key": key,
            "name": (pb.get("title") or name) + f"（{name}）",
            "version": 1, "kind": kind, "engine": "comfyui",
            "graph": f"workflows/pack/{name}.api.json",
            "model": {"key": mkey, "slots": slots},
            "params": params,
            "output": {"node": onode, "input": oinput, "kind": kind,
                       "ext": "mp4" if kind == "video" else "png",
                       "pick": ["videos", "gifs"] if kind == "video" else ["images"]},
            "expect_defaults": ({"ext": "mp4", "min_bytes": 50000} if kind == "video"
                                else {"ext": "png", "min_bytes": 20000}),
            "limits": {"max_minutes": 40, "max_cost": 1.6} if kind == "video"
                      else {"max_minutes": 20, "max_cost": 0.8},
            "required_gpu": ["4090", "4090_48G"],
            "notes": " · ".join(notes),
        }
        model_cache[mkey] = model_conf
        created.append((name, key, mkey, kind, len(params)))

        if a.apply:
            GRAPH_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, GRAPH_DIR / f.name)
            (REG_WF / f"{key}.json").write_text(json.dumps(wf, ensure_ascii=False, indent=2), encoding="utf-8")

    if a.apply:
        for mkey, conf in model_cache.items():
            if mkey in existing_md:
                continue
            (REG_MD / f"{mkey}.json").write_text(json.dumps(conf, ensure_ascii=False, indent=2), encoding="utf-8")
        # 注意：compat.json 只记录人工 confirmed 的 verified/incompatible；
        # experimental 由 registry 按"模型+图结构"自动推导，不能手写条目（schema 会拒）。
    print(f"可导入 {len(created)} 个，跳过 {len(skipped)} 个"
          f"{'' if a.apply else '（dry-run，未写文件）'}")
    for name, key, mkey, kind, n in created:
        print(f"  + {key:44} kind={kind:5} model={mkey:32} params={n}")
    if skipped:
        print("跳过：")
        for name, why in skipped:
            print(f"  - {name:52} {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
