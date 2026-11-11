#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 ComfyUI API 图反推参数契约：给「用户上传自己的图」打地基。

## 这个文件解决什么

现在新增一条工作流要手写 `params`：每个参数的类型、默认值、绑到哪个节点
的哪个输入。节点 id 是 ComfyUI 导出时生成的随机数字串（`1349`、`src`、`ks`），
手填必然出错 —— 而错了的后果不是"报错"，是渲染时 `_set()` 抛异常，
那时实例已经开好、钱已经花了。

所以让机器读图，人只做确认。

## 反推得出什么

    媒体输入   LoadImage  → {type: image, bindings: [{node, input: image}], required: true}
               LoadVideo  → {type: video, ...}
               LoadAudio  → {type: audio, ...}
    提示词     顺着 sampler 的 positive/negative 往上追，找到真正吃文本的节点，
               按它在 positive 侧还是 negative 侧定名 `prompt` / `neg_prompt`
    采样参数   seed / steps / cfg / sampler_name / scheduler / denoise
    尺寸帧数   width / height / length（含 ComfyMathExpression 反算默认值）
    输出       SaveVideo / SaveImage → output + expect_defaults.ext

## 反推出的东西**为什么不能直接当真**

三处必须人确认，工具会明确标出来：

  ① **提示词哪个是正、哪个是负**：`CLIPTextEncode` 有 94 个，靠图连通性追；
     追到多个嫌疑对象时如实报「多条候选」而不是猜第一个。
     2026-11-11 的两张卡（qwen_layered / scail2）就是正负填反，出片糊但 PASS。
  ② **参数是否该暴露给用户**：内部中间量（`ComfyMathExpression.values.a`）
     不暴露；`strength` 这种要不要露是产品决定，工具给建议、标 `suggest`。
  ③ **默认值是否合理**：图里的值是**作者调试时留下的**（`egyptian_queen.png`），
     不是该给终端用户的默认值。媒体类一律标空 + required。

## 用法

    python3 -m tools.p20.wf_infer --graph workflows/pack/video_ltx2_3_i2v.api.json
    python3 -m tools.p20.wf_infer --graph X --json > params.json
    python3 -m tools.p20.wf_infer --graph X --key my_wf --emit-card   # 打印整张卡
"""
from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OBJECT_INFO = ROOT / "reports" / "p2" / "p21" / "evidence" / "objectinfo-linked.json"

# ---------------------------------------------------------------- 节点分类
MEDIA_LOADERS = {
    "LoadImage": "image",
    "LoadVideo": "video",
    "VHS_LoadVideo": "video",
    "LoadVideoUpload": "video",
    "LoadAudio": "audio",
}

OUTPUT_NODES = {
    "SaveVideo": ("video", "mp4", ["videos", "gifs"], "filename_prefix"),
    "VHS_VideoCombine": ("video", "mp4", ["videos", "gifs"], "filename_prefix"),
    "SaveImage": ("image", "png", ["images"], "filename_prefix"),
    "SaveImageAdvanced": ("image", "png", ["images"], "filename_prefix"),
    "SaveAnimatedWEBP": ("image", "webp", ["images"], "filename_prefix"),
}

# 产出 CONDITIONING 的 sampler 类：从这里开始往上追提示词
SAMPLERS = ("KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced")

# 吃文本的输入名
TEXT_INPUTS = ("text", "prompt", "text_a", "text_b", "positive", "negative")

# 采样参数：参数名 → (可能输入名, 类型)
SAMPLER_PARAMS = (
    ("seed", ("seed", "noise_seed"), "int"),
    ("steps", ("steps",), "int"),
    ("cfg", ("cfg",), "float"),
    ("sampler", ("sampler_name",), "enum"),
    ("scheduler", ("scheduler",), "enum"),
    ("denoise", ("denoise",), "float"),
)

# 权重输入名：这些字段的值是模型文件名，必须由 model.slots 提供
WEIGHT_INPUT_NAMES = frozenset({
    "ckpt_name", "unet_name", "vae_name", "clip_name", "clip_name1",
    "clip_name2", "lora_name", "model_name", "text_encoder",
    "control_net_name", "style_model_name", "upscale_model",
})

# 采样参数的说明文案（面向终端用户，不是开发者）
SAMPLER_DESC = {
    "seed": "随机种子。同一个种子 + 同一组参数能得到完全相同的结果；"
            "想复现某次出图就记下它。点「随机」换一个。",
    "steps": "采样步数。越多越细也越慢。轻量蒸馏模型 4–8 步就够，常规模型 20–30 步。",
    "cfg": "提示词遵循强度。越高越贴提示词也越容易过饱和；加速/蒸馏模型通常用 1。",
    "sampler": "采样算法。一般保持默认（euler / dpmpp_2m）。",
    "scheduler": "噪声调度。一般保持默认（simple / karras）。",
    "denoise": "重绘幅度。1.0 = 完全重画；0.2–0.4 = 保留原图结构做细节增强。",
}

# 「显式清零」节点：出现在某一侧说明**这一侧没有提示词**，不是"没追到"。
#
# `ConditioningZeroOut` = 条件清零，等价于「无条件反向」。图里这么画是刻意的，
# 说明作者不想让负向提示词起作用。此时要是还去追它上游的 CLIPTextEncode，
# 会得出「neg_prompt 应该绑 1318」的结论，而 1318 恰恰是**正向**那个 ——
# 结果是正负绑到同一节点（WF018），用户改哪个都只生效一个。
ZERO_OUT_NODES = frozenset({"ConditioningZeroOut", "ConditioningSetAreaStrength"})

# 不暴露给用户的输入名：内部中间量
INTERNAL_INPUTS = frozenset({
    "model", "clip", "vae", "samples", "latent", "latent_image", "conditioning",
    "positive", "negative", "images", "mask", "audio", "video", "image",
    "control_video", "control_masks", "clip_vision", "clip_vision_output",
    "sigmas", "guider", "sampler", "noise", "upscale_model", "av_latent",
    "video_latent", "audio_latent", "pixels", "pixel_samples",
})


def _is_link(v) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[1], int)


def _reachable_from_output(graph: dict) -> set[str]:
    """从保存节点反向可达的节点集合。

    为什么必须做：图里常带**旁支**——被 bypass 的旧链路、备份的采样器、
    作者试过的另一套 LoRA。2026-11-11 实测 `scail2` 那张图 75 个节点里
    只有 36 个是执行路径上的，不剪枝会把 14 个权重槽位全报出来，
    其中 7 个根本不会被求值 —— 人去核对这 7 个纯属浪费。
    """
    roots = [nid for nid, n in graph.items()
             if str((n or {}).get("class_type") or "") in OUTPUT_NODES]
    if not roots:
        return set(graph)
    seen: set[str] = set()
    stack = list(roots)
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        for v in ((graph.get(nid) or {}).get("inputs") or {}).values():
            if _is_link(v):
                stack.append(str(v[0]))
    return seen


def _looks_like_placeholder(v) -> bool:
    """`{{SEED}}` 这类是渲染期替换的占位符，不是能给用户看的默认值。

    踩过：`qwen_image_t2i` 的 seed 图里写的就是 `{{SEED}}`，
    反推时若原样当 default，UI 会渲染出一个字符串框给 int 参数。
    """
    return isinstance(v, str) and "{{" in v


def _role_for(class_type: str, input_name: str) -> str:
    """由「loader 类 + 权重输入名」定角色，避免同类不同用途撞车。

    为什么不能只看 input 名：`CLIPLoader.clip_name` 和
    `CLIPVisionLoader.clip_name` 同名，但一个是文本编码器、一个是视觉编码器，
    合成同一个角色会让模型卡缺一半文件（渲染时 apply_model 抛「缺槽位」）。
    """
    ct = class_type
    if ct == "CLIPVisionLoader":
        return "clip_vision"
    if ct in ("LTXAVTextEncoderLoader",):
        return "text_encoder"
    if ct == "LTXVAudioVAELoader":
        return "audio_vae"
    if ct in ("LatentUpscaleModelLoader",):
        return "upscale_latent"
    if ct in ("UpscaleModelLoader",):
        return "upscale"
    return {
        "ckpt_name": "ckpt", "unet_name": "unet", "vae_name": "vae",
        "clip_name": "clip", "lora_name": "lora", "model_name": "upscale",
        "text_encoder": "text_encoder", "control_net_name": "controlnet",
    }.get(input_name, input_name)


# 尺寸/帧数只从这些「真正定义 latent 形状」的节点取。
# ImageFromBatch.length 是**裁切长度**不是总帧数 —— 2026-11-11 实测
# scail2 图里 4 个 ImageFromBatch.length=1，直接取会把 81 帧的片反推成 1 帧。
SHAPE_NODES = (
    "EmptyLatentImage", "EmptySD3LatentImage", "EmptyFlux2LatentImage",
    "EmptyLTXVLatentVideo", "EmptyQwenImageLayeredLatentImage",
    "EmptyHunyuanLatentVideo", "EmptyMochiLatentVideo", "EmptyImage",
    "WanVaceToVideo", "WanImageToVideo", "MiniMaxH3ImageToVideo",
    "MiniMaxH3ReferenceToVideo", "LTXVImgToVideoInplace",
)


@dataclass
class Suggestion:
    """一个建议暴露的参数。"""
    name: str
    spec: dict
    source: str                      # 人话：从哪个节点推出来的
    confidence: str = "high"         # high / medium / low
    note: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "spec": self.spec, "source": self.source,
                "confidence": self.confidence, "note": self.note}


@dataclass
class Inference:
    params: dict = field(default_factory=dict)
    slots: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)
    expect: dict = field(default_factory=dict)
    suggestions: list[Suggestion] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    graph_nodes: int = 0
    live_nodes: int = 0

    def as_dict(self) -> dict:
        return {"params": self.params, "model_slots": self.slots,
                "output": self.output, "expect_defaults": self.expect,
                "suggestions": [s.as_dict() for s in self.suggestions],
                "warnings": self.warnings,
                "nodes": self.graph_nodes, "live_nodes": self.live_nodes}


# ---------------------------------------------------------------- 图遍历
def _trace_sides(graph: dict, start_nid: str, side: str,
                 depth: int = 0, seen: frozenset | None = None) -> tuple[str, str] | None:
    """从某节点沿 `side`（positive/negative）往上追，找到真正吃文本的那个节点。

    返回 (node_id, input_name) 或 None。

    ## 为什么不能「从 sampler 直接往上追」

    sampler 和文本编码器之间通常隔了 1~3 层透传节点，而且**透传节点不在
    sampler 的 direct input 里**：

        KSampler.positive → LTXVCropGuides → LTXVConditioning
                              .positive → CLIPTextEncode   ← 真正吃文本的是这个

        SamplerCustomAdvanced.guider → CFGGuider
                                        .positive → CLIPTextEncode

    第一版只从 sampler 的 `positive`/`negative` 追，遇到 `SamplerCustomAdvanced`
    这种「文本侧藏在 guider 里」的结构就一条都追不到 —— 实测 `ltx2_3_flf2v`
    反推出的参数里 prompt/neg_prompt 全丢，只剩 3 个参数。

    所以改成：**先穿过任意输入**找到最近的含 positive/negative 的节点，
    再按同名侧继续。穿过的过程要防环（`seen`）。
    """
    if depth > 20:
        return None
    seen = seen or frozenset()
    node = graph.get(start_nid)
    if not isinstance(node, dict) or start_nid in seen:
        return None
    seen = seen | {start_nid}
    ins = node.get("inputs") or {}

    # 同一个编码节点同时管正负两路（`TextEncodeQwenImage21` 的
    # `prompt` / `negative_prompt`，`TextEncodeMageFlowEdit` 同理）。
    #
    # 这类节点**不带 positive/negative 输入名**，而是按输出下标区分：
    #   output[0] = positive，output[1] = negative
    # 所以从 sampler 追过来时，节点里同时有 `prompt` 和 `negative_prompt`
    # 就要按 side 选对的那个输入名，不能两个都返回 `prompt` ——
    # 2026-11-11 踩过：`qwen_image_2_1_image_edit` 的正负被推成同一个
    # `prompt` 字段，判 WF017 说"都绑错了"，实际是追踪器自己分不清。
    for a_name, b_name in (("prompt", "negative_prompt"),
                           ("text", "negative_text")):
        if a_name in ins and b_name in ins:
            want = a_name if side == "positive" else b_name
            if not _is_link(ins.get(want)):
                return start_nid, want
            return None

    # 显式清零 → 这一侧没有文本，直接返回 None（不是继续往上找）
    if str(node.get("class_type") or "") in ZERO_OUT_NODES:
        return None

    # ComfySwitchNode：按 `switch` 的当前值选分支，**不许两条都追**。
    #
    # 踩过（2026-11-11）：`ltx2_3_i2v` 的提示词链是
    #     CLIPTextEncode.text ← ComfySwitchNode(switch=False)
    #                              ├ on_true  ← TextGenerateLTX2Prompt（AI 改写提示词）
    #                              └ on_false ← 硬编码的长文本
    # 不认 switch 的话会追到 on_true 的 TextGenerateLTX2Prompt，判定
    # 「正向提示词应该绑 AI 改写节点」—— 而作业实际用的是 on_false 的硬编码文本。
    # 照那个结论去改卡，等于让用户改了参数却不生效。
    if str(node.get("class_type") or "") == "ComfySwitchNode" or "switch" in ins:
        pick = "on_true" if ins.get("switch") in (True, "True", "true", 1) else "on_false"
        val = ins.get(pick)
        if _is_link(val):
            return _trace_sides(graph, str(val[0]), side, depth + 1, seen)
        if isinstance(val, str) and val:
            return start_nid, pick
        return None

    # 本节点自己就吃文本（常量，不是连线）？
    for name in TEXT_INPUTS:
        if name in ins and not _is_link(ins[name]):
            # `positive`/`negative` 作为**输入名**出现且是常量，只可能是在别的语境，
            # 这里只认真正承载文本的那几个名字
            if name in ("text", "prompt", "text_a", "text_b"):
                return start_nid, name

    # 有同名侧就先走同名侧（positive → positive）
    if side in ins and _is_link(ins[side]):
        hit = _trace_sides(graph, str(ins[side][0]), side, depth + 1, seen)
        if hit:
            return hit

    # 否则穿过其它连线（guider / conditioning 这类透传）
    for key, val in ins.items():
        if not _is_link(val):
            continue
        nxt = str(val[0])
        nnode = graph.get(nxt) or {}
        nins = nnode.get("inputs") or {}
        # 下一个节点同时有 positive/negative → 那是分叉点，必须按侧走，不能乱穿
        if "positive" in nins and "negative" in nins and side not in nins:
            continue
        hit = _trace_sides(graph, nxt, side, depth + 1, seen)
        if hit:
            return hit
    return None


def _sampler_roots(graph: dict, live: set[str]) -> list[str]:
    """找出「真正做采样」的节点，用于定位提示词入口。

    优先 SamplerCustomAdvanced / SamplerCustom（它们吃 guider，文本在 guider 里），
    没有再退回 KSampler 系（文本直接在 positive/negative 上）。
    """
    order = ("SamplerCustomAdvanced", "SamplerCustom",
             "KSamplerAdvanced", "KSampler")
    for want in order:
        hits = [nid for nid, n in graph.items()
                if nid in live and str((n or {}).get("class_type") or "") == want]
        if hits:
            return hits
    return []


def _text_entry(graph: dict, sampler_nid: str) -> dict[str, tuple[str, str]]:
    """给出这条图的 {positive|negative: (node, input)}。"""
    node = graph.get(sampler_nid) or {}
    ins = node.get("inputs") or {}
    found: dict[str, tuple[str, str]] = {}
    for side in ("positive", "negative"):
        # ① 直接挂在 sampler 上
        if side in ins and _is_link(ins[side]):
            hit = _trace_sides(graph, str(ins[side][0]), side)
            if hit:
                found[side] = hit
                continue
        # ② 挂在 guider 上（SamplerCustomAdvanced 的结构）
        guider = ins.get("guider")
        if _is_link(guider):
            gnode = graph.get(str(guider[0])) or {}
            gins = gnode.get("inputs") or {}
            if side in gins and _is_link(gins[side]):
                hit = _trace_sides(graph, str(gins[side][0]), side)
                if hit:
                    found[side] = hit
    return found


def _resolve_number(graph: dict, val, depth: int = 0) -> float | int | None:
    """把「常量 / ComfyMathExpression 表达式」解成数字。"""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        try:
            f = float(val)
            return int(f) if f.is_integer() else f
        except ValueError:
            return None
    if depth > 8 or not _is_link(val):
        return None
    node = graph.get(str(val[0]))
    if not isinstance(node, dict):
        return None
    ins = node.get("inputs") or {}
    if node.get("class_type") != "ComfyMathExpression":
        return None
    expr = str(ins.get("expression") or "")
    a = _resolve_number(graph, ins.get("values.a"), depth + 1)
    b = _resolve_number(graph, ins.get("values.b"), depth + 1)
    if a is None:
        return None
    return _eval_expr(expr, a, b)


# 只允许 a / b / 数字 / 四则运算。用 AST 求值而不是自造解析器：
# 表达式是导出方随便写的（`a/2`、`a * b + 1`、`(a-b)*2`），
# 枚举几种写法必然漏 —— 2026-11-11 第一版只认 `a + b` 等五种固定形式，
# `a/2`（ltx2_3_i2v 的 width）和 `a * b + 1`（length）全部解不出来，
# 三条工作流一个尺寸都没推出来。
_ALLOWED_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv)


def _eval_expr(expr: str, a, b):
    if not expr:
        return None
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    names = {"a": a, "b": b}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id not in names or names[node.id] is None:
                return None
        elif isinstance(node, (ast.Call, ast.Attribute, ast.Subscript, ast.Lambda)):
            return None
        elif isinstance(node, ast.BinOp) and not isinstance(node.op, _ALLOWED_OPS):
            return None
    try:
        v = eval(compile(tree, "<expr>", "eval"), {"__builtins__": {}}, names)  # noqa: S307
    except (TypeError, ZeroDivisionError, ValueError, NameError):
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return int(v) if float(v).is_integer() else float(v)


def _combo_options(oi: dict, class_type: str, input_name: str) -> list | None:
    spec = ((oi.get(class_type) or {}).get("input") or {})
    for bucket in ("required", "optional"):
        fs = (spec.get(bucket) or {}).get(input_name)
        if not fs:
            continue
        t = fs[0] if isinstance(fs, list) and fs else None
        if isinstance(t, list):
            return t
        meta = fs[1] if isinstance(fs, list) and len(fs) > 1 and isinstance(fs[1], dict) else {}
        opts = meta.get("options")
        if isinstance(opts, list) and opts and all(isinstance(o, str) for o in opts):
            return opts
    return None


def _default_of(oi: dict, class_type: str, input_name: str):
    spec = ((oi.get(class_type) or {}).get("input") or {})
    for bucket in ("required", "optional"):
        fs = (spec.get(bucket) or {}).get(input_name)
        if isinstance(fs, list) and len(fs) > 1 and isinstance(fs[1], dict):
            return fs[1].get("default")
    return None


# ---------------------------------------------------------------- 主流程
def infer(graph: dict, oi: dict | None = None, *,
          fps: float | None = None) -> Inference:
    """核心：从图推参数契约。纯函数，不写文件。"""
    oi = oi or {}
    out = Inference(graph_nodes=len(graph))
    params: dict = out.params

    # ---- ① 输出：决定 kind / ext / expect
    for nid, node in graph.items():
        ct = str((node or {}).get("class_type") or "")
        if ct not in OUTPUT_NODES:
            continue
        kind, ext, pick, prefix_input = OUTPUT_NODES[ct]
        out.output = {"node": str(nid), "input": prefix_input, "kind": kind,
                      "ext": ext, "pick": list(pick)}
        out.expect = {"ext": ext,
                      "min_bytes": 50000 if kind == "video" else 20000}
        break
    if not out.output:
        out.warnings.append("图里找不到保存节点（SaveVideo / SaveImage 等）—— "
                            "output 必须人手指定，否则产物收割不到")
    kind = out.output.get("kind")

    # 只认执行路径上的节点。旁支（bypass 的旧链路、作者试过的另一套 LoRA）
    # 不参与参数/槽位推断，否则会给出用户根本用不到的字段。
    live = _reachable_from_output(graph)
    out.live_nodes = len(live)

    # ---- ② 媒体输入
    for nid, node in graph.items():
        if nid not in live:
            continue
        ct = str((node or {}).get("class_type") or "")
        typ = MEDIA_LOADERS.get(ct)
        if not typ:
            continue
        ins = (node or {}).get("inputs") or {}
        # 该 loader 的输入名：LoadImage → image；LoadVideo → file
        inp = next((k for k in ("image", "file", "video", "audio") if k in ins), None)
        if not inp:
            continue
        base = {"image": "image", "video": "video", "audio": "audio"}[typ]
        name = base
        n = 2
        while name in params:                    # image / image_2 / image_3
            name = f"{base}_{n}"
            n += 1
        params[name] = {
            "type": typ,
            "required": True,
            "bindings": [{"node": str(nid), "input": inp}],
            "description": {
                "image": "输入图。这条工作流用它当身份/首帧/参考。",
                "video": "驱动视频。动作、节奏、镜头运动从这条素材继承。",
                "audio": "输入音频。音色/口型/节奏从这条素材继承。",
            }[typ],
        }
        # 图里的值是作者调试留下的素材名，不能当默认值 —— 媒体类必须由用户提供
        out.suggestions.append(Suggestion(
            name, {"type": typ}, f"{ct}（节点 {nid}）", "high",
            "图里原有值已丢弃（那是作者调试用的素材名，不是用户默认值）"))

    # ---- ③ 提示词：顺 sampler 追
    roots = _sampler_roots(graph, live)
    for nid in roots[:1]:
        for side, pname in (("positive", "prompt"), ("negative", "neg_prompt")):
            entry = _text_entry(graph, nid)
            hit = entry.get(side)
            if not hit:
                continue
            tnid, tinp = hit
            tnode = graph.get(tnid) or {}
            cur = ((tnode.get("inputs") or {}) or {}).get(tinp)
            # 已有同名参数 → 追加绑定（一个参数可绑多个节点）
            if pname in params:
                if {"node": tnid, "input": tinp} not in params[pname]["bindings"]:
                    params[pname]["bindings"].append({"node": tnid, "input": tinp})
                continue
            spec = {
                "type": "string",
                "required": side == "positive",
                "bindings": [{"node": tnid, "input": tinp}],
                "description": ("正向提示词：描述你想要的画面内容（主体、外貌、衣着、背景、风格）。"
                                if side == "positive" else
                                "负向提示词：不想出现的东西（糊、水印、畸形手、字幕等）。"
                                "留默认即可，不必改。"),
            }
            # 只保留**看起来像自然语言**的原值；占位符和调试素材名丢掉
            if isinstance(cur, str) and cur and "{{" not in cur and len(cur) > 8:
                spec["default"] = cur
            params[pname] = spec
            out.suggestions.append(Suggestion(
                pname, {"type": "string"}, f"{tnode.get('class_type')}（节点 {tnid}）",
                "medium",
                "正负侧靠图连通性追出来的，**请人确认对不对**："
                "把 prompt 和 neg_prompt 的默认值念一遍，方向反了出片必糊不报错"))
        break                                    # 只认第一组 sampler

    # ---- ④ 采样参数
    #
    # seed 不一定在 sampler 上：`SamplerCustomAdvanced` 的随机源是独立的
    # `RandomNoise.noise_seed`（两段式采样甚至有 2~3 个）。第一版只在
    # sampler 的输入里找 seed，`flf2v` / `ltx2_3_i2v` 一条都推不出来。
    for nid, node in graph.items():
        if nid not in live:
            continue
        if str((node or {}).get("class_type") or "") != "RandomNoise":
            continue
        ns = ((node or {}).get("inputs") or {}).get("noise_seed")
        if ns is None or _is_link(ns):
            continue
        params["seed"] = {
            "type": "int",
            "required": False,
            "default": 0 if _looks_like_placeholder(ns) else ns,
            "min": 0, "max": 2147483647,
            "description": SAMPLER_DESC["seed"],
            "bindings": [{"node": str(nid), "input": "noise_seed"}],
        }
        out.suggestions.append(Suggestion(
            "seed", {"type": "int"}, f"RandomNoise（节点 {nid}）", "high"))

    for nid, node in graph.items():
        if nid not in live:
            continue
        ct = str((node or {}).get("class_type") or "")
        if ct not in SAMPLERS:
            continue
        ins = (node or {}).get("inputs") or {}
        for pname, names, typ in SAMPLER_PARAMS:
            if pname in params:
                continue
            for nm in names:
                if nm not in ins:
                    continue
                val = ins[nm]
                if _is_link(val):
                    continue                     # 连线型不从 sampler 取
                spec = {"type": typ, "bindings": [{"node": str(nid), "input": nm}],
                        "description": SAMPLER_DESC[pname]}
                if typ == "enum":
                    opts = _combo_options(oi, ct, nm)
                    if opts:
                        spec["values"] = opts
                    elif _default_of(oi, ct, nm) is not None:
                        spec["default"] = _default_of(oi, ct, nm)
                else:
                    # `{{SEED}}` 是渲染期替换的占位符，不是能给用户看的默认值。
                    # 原样当 default 会让 int 参数渲染出字符串框（qwen_image_t2i 踩过）。
                    spec["default"] = 0 if _looks_like_placeholder(val) else val
                    if typ == "int":
                        spec["min"] = 0
                        spec["max"] = 2147483647
                params[pname] = spec
                out.suggestions.append(Suggestion(
                    pname, {"type": typ}, f"{ct}（节点 {nid}）", "high"))
                break
        break

    # ---- ⑤ 尺寸 / 帧数
    #
    # 只从 SHAPE_NODES 取。踩过：第一版扫所有带 `length` 的节点，
    # `scail2` 图里 4 个 `ImageFromBatch.length=1`（那是裁切长度）把
    # 81 帧的片反推成了 1 帧 —— 卡上写 frames 默认 1，用户不改就出 1 帧废片。
    #
    # 连线型的尺寸（`ComfyMathExpression: a/2`）也要能解：
    # `ltx2_3_i2v` 的 width/height/length 全是表达式连线，
    # 不解就一个都推不出来。
    for nid, node in graph.items():
        if nid not in live:
            continue
        ct = str((node or {}).get("class_type") or "")
        if ct not in SHAPE_NODES:
            continue
        ins = (node or {}).get("inputs") or {}
        for pname, names in (("width", ("width",)), ("height", ("height",)),
                             ("frames", ("length", "num_frames", "frames"))):
            if pname in params:
                continue
            for nm in names:
                if nm not in ins:
                    continue
                num = _resolve_number(graph, ins[nm])
                if num is None or not isinstance(num, (int, float)) or num <= 0:
                    continue
                spec = {"type": "int", "bindings": [{"node": str(nid), "input": nm}],
                        "default": int(num), "min": 1,
                        "description": {
                            "width": "输出宽度（像素）。多数模型要求是 16 的倍数。",
                            "height": "输出高度（像素）。多数模型要求是 16 的倍数。",
                            "frames": "总帧数。时长 = 帧数 ÷ 帧率。",
                        }[pname]}
                params[pname] = spec
                note = ""
                if _is_link(ins[nm]):
                    note = ("图上这个值来自表达式节点（如 a/2），已按当前常量反算；"
                            "改它会绕开表达式，请确认这是你要的效果")
                out.suggestions.append(Suggestion(
                    pname, {"type": "int"}, f"{ct}（节点 {nid}）",
                    "high" if not note else "medium", note))
                break

    # ---- ⑥ 模型槽位：按「loader 类 + 权重输入名」定角色
    #
    # 只用执行路径上的节点。`scail2` 图 75 个节点里 36 个可达，
    # 不剪枝会报 14 个槽位，其中 7 个永远不会被求值 —— 人去核对纯属浪费。
    # 先按角色优先级排序：同一个文件被多个 loader 引用时，保留**更主要**的那个角色。
    #
    # 踩过：`ltx2_3_i2v` 里 `CheckpointLoaderSimple.ckpt_name`（主权重）和
    # `LTXVAudioVAELoader.ckpt_name`（音频 VAE，同一个文件）都是
    # `ltx-2.3-22b-dev-fp8.safetensors`。按图遍历顺序去重会保留 audio_vae 而丢掉 ckpt，
    # 于是模型卡「缺 ckpt」—— 主权重没了，渲染必失败。
    ROLE_PRIORITY = {"ckpt": 0, "unet": 1, "vae": 2, "clip": 3, "lora": 4,
                     "text_encoder": 5, "clip_vision": 6, "audio_vae": 7,
                     "upscale_latent": 8, "upscale": 9, "controlnet": 10}
    taken: dict[str, int] = {}
    by_file: dict[str, str] = {}          # 文件名 → 已占用的角色（去重用）
    def _node_priority(kv) -> int:
        """节点里最「主要」的那个权重角色的优先级（数字越小越主要）。"""
        n = kv[1] or {}
        ct = str(n.get("class_type") or "")
        best = 99
        for inp in (n.get("inputs") or {}):
            if inp in WEIGHT_INPUT_NAMES:
                best = min(best, ROLE_PRIORITY.get(_role_for(ct, inp), 99))
        return best

    ordered = sorted(graph.items(), key=_node_priority)
    for nid, node in ordered:
        if nid not in live:
            continue
        ct = str((node or {}).get("class_type") or "")
        if "Load" not in ct and "Loader" not in ct:
            continue
        ins = (node or {}).get("inputs") or {}
        for inp, val in ins.items():
            if inp not in WEIGHT_INPUT_NAMES or _is_link(val):
                continue
            fname = str(val)
            # 同一个文件被多个 loader 引用 → 只留第一个，其余记警告。
            # `LTXAVTextEncoderLoader.ckpt_name` 和 `CheckpointLoaderSimple.ckpt_name`
            # 指的是**同一个主权重**（音频 VAE 和文本编码器都在那个 ckpt 里），
            # 报成两个槽位会让模型卡凭空缺一个文件，渲染时 apply_model 直接抛错。
            if fname in by_file:
                out.warnings.append(
                    f"{nid}.{inp} 与 {by_file[fname]} 引用同一个权重 {fname!r} —— "
                    f"按重复引用处理，不单列槽位（如需分别指定请手改）")
                continue
            base = _role_for(ct, inp)
            n = taken.get(base, 0)
            role = base if n == 0 else f"{base}_{n + 1}"
            taken[base] = n + 1
            by_file[fname] = role
            out.slots[role] = {"node": str(nid), "input": inp}

    # ---- ⑦ 期望判据
    if kind == "video":
        out.expect.setdefault("duration_s", [1.0, 120.0])
        # 能从图解出帧率和帧数就补精确窗口
        fps_val = fps
        for node in graph.values():
            if str((node or {}).get("class_type") or "") == "CreateVideo":
                v = _resolve_number(graph, ((node or {}).get("inputs") or {}).get("fps"))
                if isinstance(v, (int, float)) and v > 0:
                    fps_val = float(v)
                    break
        frames = (params.get("frames") or {}).get("default")
        if fps_val and isinstance(frames, int) and frames > 1:
            dur = frames / fps_val
            out.expect["duration_s"] = [round(max(0.2, dur - 0.3), 2),
                                        round(dur + 0.35, 2)]
            out.expect["fps"] = fps_val

    # ---- ⑧ 去重 + 清理
    for p in params.values():
        p.pop("bindings", None) if not p.get("bindings") else None
    return out


# ---------------------------------------------------------------- 入口
def _print_human(key: str, inf: Inference) -> None:
    print(f"### {key}  ({inf.graph_nodes} 个节点)")
    print()
    print("参数契约：")
    for name, spec in inf.params.items():
        b = ", ".join(f"{x['node']}.{x['input']}" for x in spec.get("bindings") or [])
        flag = " *必填" if spec.get("required") else ""
        print(f"  {name:12s} {spec.get('type'):7s}{flag:6s} ← {b}")
        if spec.get("default") is not None:
            d = repr(spec["default"])
            print(f"  {'':12s} default = {d[:70]}")
    print()
    print("模型槽位：")
    for role, s in inf.slots.items():
        print(f"  {role:14s} ← {s['node']}.{s['input']}")
    print()
    print("输出：", json.dumps(inf.output, ensure_ascii=False))
    print("期望：", json.dumps(inf.expect, ensure_ascii=False))
    if inf.warnings:
        print()
        for w in inf.warnings:
            print(f"  ! {w}")
    low = [s for s in inf.suggestions if s.confidence != "high"]
    if low:
        print()
        print("需要人确认的：")
        for s in low:
            print(f"  ? {s.name}（{s.confidence}）{s.note}")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wf_infer",
                                 description="从 API 图反推参数契约（¥0）")
    ap.add_argument("--graph", required=True, help="ComfyUI 导出的 API 图")
    ap.add_argument("--key", default="", help="工作流 key（输出卡时用）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit-card", action="store_true", dest="emit_card",
                    help="打印可直接落盘的卡骨架")
    ap.add_argument("--object-info", dest="object_info", metavar="FILE")
    a = ap.parse_args(argv)

    gp = Path(a.graph)
    if not gp.is_absolute():
        gp = ROOT / gp
    if not gp.is_file():
        print(f"✗ 图不存在：{gp}", file=sys.stderr)
        return 2
    graph = json.loads(gp.read_text(encoding="utf-8"))

    oi_path = Path(a.object_info) if a.object_info else OBJECT_INFO
    oi = {}
    if oi_path.is_file():
        try:
            oi = json.loads(oi_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            oi = {}

    inf = infer(graph, oi)
    key = a.key or gp.stem.replace(".api", "")

    if a.emit_card:
        kind = (inf.output or {}).get("kind") or "image"
        card = {
            "schema": "p20.workflow/v1",
            "key": key,
            "name": "",
            "version": 1,
            "kind": kind,
            "engine": "comfyui",
            "graph": str(gp.relative_to(ROOT)) if gp.is_relative_to(ROOT) else str(gp),
            "model": {"key": "", "slots": inf.slots},
            "params": inf.params,
            "output": inf.output,
            "expect_defaults": inf.expect,
            "limits": {"max_cost": 1.6, "max_minutes": 40},
            "required_gpu": ["4090", "4090_48G"],
            "description": "",
            "notes": "由 wf_infer 从图反推生成，**必须人审**："
                     "正负提示词、模型 key、name/description",
        }
        print(json.dumps(card, ensure_ascii=False, indent=1))
        return 0

    if a.json:
        print(json.dumps(inf.as_dict(), ensure_ascii=False, indent=1))
    else:
        _print_human(key, inf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
