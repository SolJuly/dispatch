#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H3 画质实验：对比不同参数组合，用**肉眼**判定而不是纸面分数。

## 为什么单独写这个脚本

`quality.py` 只能抓黑帧、静帧、空画面这类硬故障。它量不出：
  · 画面对比度、色彩饱和度
  · 皮肤/布料的细节质感
  · 运镜是否自然
  · 有没有 3D 塑料感

要回答"画质有没有变好"，只能：**同一提示词、同一 seed，换参数跑，肉眼看**。
所以这里不做打分，只负责：建作业 → 跑 → 把产物按变体命名，方便并排对比。

## 变体怎么定义

每个变体是一组「改哪个参数、改成什么」。改的是 job.json 里的字段，
不改 registry（避免互相污染）。这样同一份工作流能跑出多个对照。

## 用法

    python3 tools/p2/h3_quality_lab.py --list
    python3 tools/p2/h3_quality_lab.py --variant baseline --prompt p1
    python3 tools/p2/h3_quality_lab.py --variant shift12 --prompt p1
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JOBCTL = ROOT / "tools" / "p1" / "jobctl.py"
RUNPY = ROOT / "tools" / "p1" / "run.py"
SPEC_DIR = ROOT / "reports" / "p20" / "specs" / "h3lab"

SEED = 424242

# ---- 五条测试提示词（用户提供，原文照抄）-------------------------------
PROMPTS = {
    "p1": (
        "Cinematic, high-fantasy action sequence featuring a beautiful woman with "
        "long flowing white hair wearing an elaborate, shimmering white silk kimono "
        "adorned with gold floral patterns. She wields a katana that glows with "
        "intense red energy. The scene begins with her standing poised in a dark, "
        "atmospheric hall filled with blurred figures in the background. She swings "
        "the glowing sword, and the camera cuts to a dynamic low-angle shot of her "
        "feet landing heavily on stone, causing dust and debris to explode outward. "
        "Close-up shots capture the motion blur of the blade slicing through the air. "
        "The sequence culminates in a dramatic wide shot where she is enveloped in "
        "swirling red energy trails and sparks as she engages in combat with a "
        "shadowy figure. The lighting is moody and dramatic, highlighting the "
        "contrast between the bright white fabric, the silver hair, and the vivid "
        "crimson magical effects. 4k resolution, highly detailed, masterpiece, "
        "slow-motion elements mixed with fast-paced action, epic fantasy aesthetic."
    ),
    "p2": (
        "Cinematic close-up shot, photorealistic 4k video. A young Asian girl with "
        "long dark hair and bangs, wearing a traditional Japanese navy blue sailor "
        "school uniform (seifuku), stands outdoors facing a blurred figure in the "
        "foreground left. The scene captures an emotional moment: she initially "
        "looks down with her eyes closed, holding back tears, her expression pained "
        "and vulnerable. She slowly lifts her head, opening her eyes which are "
        "filled with moisture and sadness, looking up at the person in front of her "
        "with a mix of resignation and hope. Soft, natural daylight illuminates her "
        "face, highlighting subtle skin textures and the gloss of her lips. Shallow "
        "depth of field keeps the background of trees and pavement softly blurred, "
        "focusing entirely on her facial micro-expressions. High-quality drama "
        "aesthetic, slow motion feel, poignant atmosphere."
    ),
    "p4": (
        "Reference-based character: silver-haired anime girl with pale flawless "
        "skin, heterochromatic eyes — one crimson-red jewel eye, one cyan-blue "
        "jewel eye, delicate pink lips, wrapped in translucent shattered ice with "
        "visible crack lines. Camera slowly orbits around her frozen face, ice "
        "shards catching glancing light, dust motes floating in cold air, her gaze "
        "shifting slightly toward the lens. Soft volumetric moonlight, blue-cyan "
        "and faint warm red accent lighting, subsurface scattering on ice, shallow "
        "focus, cinematic bokeh, melancholic serene mood, high-fidelity anime 3D "
        "realism, detailed frost and refraction, fluid graceful movement, 24fps, "
        "5 seconds."
    ),
    "p5": (
        "Extreme close-up of a silver-haired girl encased in translucent cracked "
        "ice, her heterochromatic eyes glowing crimson-red and cyan-blue beneath "
        "the frozen surface. Subtle breathing motion: fine frost particles slowly "
        "drifting inside the ice, hair strands gently swaying, faint inner light "
        "pulsing softly across her irises. Slow dolly-in push toward her face, ice "
        "cracks glinting with cold refracted highlights, tiny bubbles rising. Pale "
        "moonlight rim light, soft cool blue palette, crystalline subsurface "
        "scattering, shallow depth of field, dreamy ethereal atmosphere, cinematic "
        "anime realism, hyper-detailed ice texture, smooth cinematic motion, 24fps, "
        "5 seconds."
    ),
    "p3": (
        "A photorealistic, close-up portrait video of a beautiful young woman with "
        "long dark brown hair and soft makeup. She has a distinct small mole under "
        "her right eye and is wearing a cozy white fuzzy sweater. The video begins "
        "with her resting her chin on her hand, looking directly at the camera with "
        "a gentle, subtle smile. As the frames progress, she slowly lowers her hand "
        "out of frame while maintaining eye contact. Her expression shifts slightly "
        "to a more serene, contemplative look as she tilts her head gently. The "
        "lighting is soft, warm, and diffused, creating a dreamy and intimate "
        "atmosphere with highlights in her eyes. The background is blurred (bokeh), "
        "suggesting an indoor setting near a window. The camera movement is minimal, "
        "featuring a very slow, subtle zoom-in or push-in effect that enhances the "
        "intimacy of the shot. High definition, 4k, cinematic depth of field, highly "
        "detailed skin texture, soft focus edges."
    ),
}

# 负向提示词：**必须为空**（2026-11-15 实测）。
#
# 唯一成功过的 h3_t2v 作业（p1-20261113T2316-93978b，同一提示词，71.4 分）
# 用的是 neg_prompt="" + cfg=2。而默认配置 cfg=1.0 + 一段中文负向词
# 跑出的是**全黑视频**（YMIN=YAVG=YMAX=16，124 帧全黑）——
# verify 报 size/black/freeze 三项全败。
#
# 原因是 H3 是 distilled 模型：cfg=1.0 时负向分支不参与计算，
# 而 MinimaxH3ImageToVideo 的 negative 节点仍然要吃一个 prompt，
# 喂一段长中文进去会把条件张量带偏，最终解码出黑屏。
NEG = ""


# ---- 动作提示词（2026-11-15：用户指出静态画面看不出差别）----
# 设计原则：**大幅度、连续、有物理反馈**。
# 静态/微动作的片子，几条 LoRA 出来几乎一样 —— 因为差异体现在
# 运动模糊、布料褶皱、肌肉形变、光影随动这些地方，画面不动就看不到。
ACT_PROMPTS = {
    "a1": (
        "Cinematic action shot, photorealistic. A young woman with long black hair "
        "in a white tank top and dark cargo pants sprints across a wet rooftop at "
        "night, then leaps over a ventilation duct, lands in a deep crouch that "
        "sends water splashing outward, and immediately pivots to her feet while "
        "drawing a pistol. Rain streaks through the frame, neon signs reflect off "
        "the wet concrete, fabric whips and clings to her body from the motion. "
        "Handheld camera tracks her run, then whips around as she lands. "
        "High shutter speed with visible motion blur on the limbs, dramatic "
        "rim lighting from the neon, high detail skin texture, 4k, 24fps."
    ),
    "a2": (
        "Dynamic martial arts sequence, cinematic realism. A martial artist in a "
        "black gi performs a spinning roundhouse kick, then flows into a low sweep "
        "and rises into a rapid three-punch combination. His body rotates fully, "
        "the gi fabric snaps and trails through the air with each movement, sweat "
        "flies off his hair on the spin. Camera orbits him at chest height then "
        "drops low for the sweep. Hard directional key light from the left creates "
        "strong shadows across defined muscles, dust rises from the mat, "
        "hyper-detailed fabric texture and skin, 4k, 24fps."
    ),
    "a3": (
        "High-speed dance sequence, cinematic photorealistic. A woman in a flowing "
        "red dress performs a fast whirling dance, spinning multiple times with her "
        "arms extended, the dress flaring into a wide circle and snapping back, her "
        "hair whipping across her face. She drops into a deep backbend then rises "
        "sharply. Camera circles her in a smooth arc, then pushes in as she spins. "
        "Warm side lighting rakes across the moving fabric, catching every fold and "
        "highlight, motion blur on the trailing dress edges, "
        "ultra-detailed fabric weave and skin pores, 4k, 24fps."
    ),
}

# 动作提示词并入 PROMPTS（放在 ACT_PROMPTS 定义之后）
PROMPTS.update(ACT_PROMPTS)

# 参考图：p4/p5 都描述同一个银发异色瞳少女，
# 所以用同一张图（由 qwen_image_t2i 生成，见 reports/p2/p21/inputs/in-silverice.png）
REF_IMG = "in-silverice.png"

# 社区加速 LoRA（阿里 PAI 的 PDD 并行解码蒸馏，8 步档）。
# 机器上 ComfyUI 版已在 /model/comfyui/models/loras/minimax/ 下，
# 文件名带 _pruned_comfy 后缀，说明已按 ComfyUI 的键名重排过，可直接 Load。
# FL2VA 用于文本/首尾帧驱动；Ref2VA 用于参考图驱动。
# **必须带子目录前缀 minimax/**（2026-11-15 实测踩到）：
# 文件物理路径是 models/loras/minimax/xxx.safetensors，
# ComfyUI 的 LoraLoaderModelOnly 列的是相对 loras/ 的路径，
# 少了 minimax/ 会被判 value_not_in_list：
#     prompt_outputs_failed_validation → node_errors.lora
PDD_LORA_FL2VA = "minimax/MiniMax-H3-FL2VA-Acc-8Step_pruned_comfy.safetensors"

# PDD LoRA 是阿里按**官方 FL2VA 结构**训的蒸馏权重。
# 当前卡用的 DasiwaHybridV2 是第三方混合模型（改过结构/键名），
# 硬套官方 LoRA 会被 ComfyUI 判 value_not_in_list 或静默不生效。
# 所以用官方 pruned int8 模型配它。
OFFICIAL_FL2VA = "minimax/10Eros_Max_h3_fl2va_pruned_int8_convrot.safetensors"

# 画质/风格 LoRA（都在 /model/comfyui/models/loras/minimax/ 下）
QUALITY_LORAS = {
    "realism": "minimax/h3-realism-people-t2v-i2v-r2v-触发词r34l1sm.safetensors",
    "cinema":  "minimax/电影感H3-loraV1.0正式版.safetensors",
    "davham":  "minimax/davham_cinema-h3-Euler-simple-0.8.safetensors",
}
PDD_LORA_REF2VA = "minimax/MiniMax-H3-Ref2VA-Acc-8Step_pruned_comfy.safetensors"

# 哪些提示词需要参考图 → 用哪个工作流
IMAGE_PROMPTS = {"p4": "pack_video_minimax_h3_r2v", "p5": "h3_i2v_121f"}


def apply_shift(graph: dict, video: float, audio: float) -> None:
    for n in graph.values():
        if n.get("class_type") == "MiniMaxH3Shift" or \
           n.get("class_type") == "MiniMaxH3SigmaShift":
            n["inputs"]["shift_video"] = video
            n["inputs"]["shift_audio"] = audio


def apply_sampler(graph: dict, name: str, sched: str) -> None:
    for n in graph.values():
        if n.get("class_type") == "KSampler":
            n["inputs"]["sampler_name"] = name
            n["inputs"]["scheduler"] = sched


def apply_unet(graph: dict, name: str) -> None:
    for n in graph.values():
        if n.get("class_type") == "UNETLoader":
            n["inputs"]["unet_name"] = name


def apply_style_lora(graph: dict, name: str, strength: float = 1.0) -> None:
    """在 PDD 加速 LoRA **之后**再串一个画质/风格 LoRA。

    链路变成：unet → lora(PDD) → lora_style → SigmaShift

    为什么能串：`LoraLoaderModelOnly` 只动 model 权重（不碰 clip），
    两个串起来等于对同一份基础权重连续做两次低秩修正 —— 这是 ComfyUI
    的标准用法，多个 LoRA 叠加就是这样。
    """
    lora_node = graph.get("lora")
    if lora_node is None:
        raise RuntimeError("先 apply_lora(PDD)，再串画质 LoRA")
    graph["lora_style"] = {
        "_meta": {"title": "style LoRA"},
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"lora_name": name, "strength_model": strength,
                   "model": ["lora", 0]},
    }
    for n in graph.values():
        if n.get("class_type") in ("MiniMaxH3SigmaShift", "MiniMaxH3Shift"):
            n["inputs"]["model"] = ["lora_style", 0]


def drop_style_lora(graph: dict) -> None:
    """摘掉画质 LoRA，链路回到 lora(PDD) → shift。"""
    if "lora_style" not in graph:
        return
    for n in graph.values():
        if n.get("class_type") in ("MiniMaxH3SigmaShift", "MiniMaxH3Shift"):
            if n["inputs"].get("model") == ["lora_style", 0]:
                n["inputs"]["model"] = ["lora", 0]
    graph.pop("lora_style", None)


def apply_lora(graph: dict, name: str, strength: float = 1.0) -> None:
    """把 PDD 加速 LoRA 接进链路：UNETLoader → LoraLoaderModelOnly → SigmaShift。

    为什么接在这里而不是替换 unet：LoraLoaderModelOnly 是**旁挂**权重，
    不改基础模型文件，卸载后立刻回 20 步基线，天然可回滚。
    """
    node = graph.get("lora")
    if node is None:
        lora_id = "lora"
        graph[lora_id] = {
            "_meta": {"title": "PDD Acc LoRA"},
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": name, "strength_model": strength,
                       "model": ["unet", 0]},
        }
    else:
        graph["lora"]["inputs"]["lora_name"] = name
        graph["lora"]["inputs"]["strength_model"] = strength
    for n in graph.values():
        if n.get("class_type") in ("MiniMaxH3SigmaShift", "MiniMaxH3Shift"):
            n["inputs"]["model"] = ["lora", 0]


def drop_lora(graph: dict) -> None:
    """摘掉 LoRA，链路回到 unet → shift。用于 baseline 对照。"""
    if "lora" not in graph:
        return
    for n in graph.values():
        if n.get("class_type") in ("MiniMaxH3SigmaShift", "MiniMaxH3Shift"):
            if n["inputs"].get("model") == ["lora", 0]:
                n["inputs"]["model"] = ["unet", 0]
    graph.pop("lora", None)


def apply_savevideo_crf(graph: dict, crf: float) -> None:
    """把 SaveVideo 切到「显式重编码 + 指定 crf」。

    为什么这么写（2026-11-15 从 object_info 挖出来的）：
    `format` / `codec` 都是 COMFY_DYNAMICCOMBO_V3 —— 选了某个 key 之后，
    它的**子参数**是平铺在 inputs 里的（不是点号路径）：

        format="mp4" → 需要 codec
        codec="h264" → 需要 encoding
        encoding="re-encode" → 需要 crf

    原来的写法是 `format=mp4 codec=h264`（没有 encoding），
    等于走 auto 保留上游流 —— 实测码率只有 37–2187 kb/s。
    显式 re-encode + crf=12 才能拿到接近视觉无损的码率。
    """
    for n in graph.values():
        if n.get("class_type") != "SaveVideo":
            continue
        ins = n["inputs"]
        ins["format"] = "mp4"
        ins["codec"] = "h264"
        ins["encoding"] = "re-encode"
        ins["crf"] = float(crf)


def apply_cfg(graph: dict, cfg: float) -> None:
    for n in graph.values():
        if n.get("class_type") == "KSampler":
            n["inputs"]["cfg"] = cfg


def apply_steps(graph: dict, steps: int) -> None:
    for n in graph.values():
        if n.get("class_type") == "KSampler":
            n["inputs"]["steps"] = steps


def apply_vae(graph: dict, name: str) -> None:
    for n in graph.values():
        if n.get("class_type") == "VAELoader":
            n["inputs"]["vae_name"] = name


# ---- 变体表 -------------------------------------------------------------
# 每个变体 = 一组图/参数改动。baseline 是改动前的原始配置，
# 用来和修好之后的版本并排看。

# 变体对 **params** 的覆盖。必须同时改 params 和 graph：
# render 的 apply_params 会按卡上 schema 把 params 写进图，
# 只改 graph 会被这一步覆盖回去。
# 2026-11-15 实测两次踩到：想跑 8 步实际跑 20 步；想跑 crf=2 实际 crf=12。
VARIANTS_PARAMS = {
    "pdd8":    {"steps": 8},
    "pdd8off": {"steps": 8},
    "pdd4":    {"steps": 4},
    "steps32": {"steps": 32},
    "crf2":    {"steps": 8, "crf": 2.0},
    "crf12":   {"steps": 8, "crf": 12.0},
    "res1024": {"steps": 8},
    "nolora":  {"steps": 8},
    "realism": {"steps": 8},
    "cinema":  {"steps": 8},
    "davham":  {"steps": 8},
}


def _pdd(g, steps: int, crf: float | None = None):
    """PDD LoRA + 指定步数（+ 可选 crf）。多数变体都基于这个组合。"""
    apply_shift(g, 12.0, 3.0)
    apply_sampler(g, "res_multistep", "simple")
    apply_vae(g, "minimax_h3_video_vae_fp16.safetensors")
    apply_lora(g, PDD_LORA_FL2VA, 1.0)
    apply_steps(g, steps)
    if crf is not None:
        apply_savevideo_crf(g, crf)


VARIANTS = {
    "baseline": {
        "note": "改动前：shift 5/1 · euler/simple · int8 VAE · 20 步",
        "graph": lambda g: (drop_lora(g),
                            apply_shift(g, 5.0, 1.0),
                            apply_sampler(g, "euler", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_int8_convrot-v2.safetensors")),
    },
    "fixed": {
        "note": "改动后：shift 12/3 · res_multistep · fp16 VAE · 20 步（无 LoRA）",
        "graph": lambda g: (drop_lora(g),
                            apply_shift(g, 12.0, 3.0),
                            apply_sampler(g, "res_multistep", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_fp16.safetensors")),
    },
    "shift12": {
        "note": "只改 shift 12/3（其余同 baseline）",
        "graph": lambda g: (drop_lora(g),
                            apply_shift(g, 12.0, 3.0),
                            apply_sampler(g, "euler", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_int8_convrot-v2.safetensors")),
    },
    "vae16": {
        "note": "只改 fp16 VAE（其余同 baseline）",
        "graph": lambda g: (drop_lora(g),
                            apply_shift(g, 5.0, 1.0),
                            apply_sampler(g, "euler", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_fp16.safetensors")),
    },
    "resmulti": {
        "note": "只改 res_multistep（其余同 baseline）",
        "graph": lambda g: (drop_lora(g),
                            apply_shift(g, 5.0, 1.0),
                            apply_sampler(g, "res_multistep", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_int8_convrot-v2.safetensors")),
    },
    "steps32": {
        "note": "改动后 + 32 步（社区：32 步音质/运动更好，只多 15-20% 时间）",
        "graph": lambda g: (drop_lora(g),
                            apply_shift(g, 12.0, 3.0),
                            apply_sampler(g, "res_multistep", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_fp16.safetensors"),
                            apply_steps(g, 32)),
    },
    "pdd8off": {
        "note": "PDD Acc-8Step LoRA + 8 步（保质提速，当前默认）",
        "graph": lambda g: _pdd(g, 8),
    },
    "pdd4": {
        "note": "PDD LoRA + 4 步（极限提速，看画质掉多少）",
        "graph": lambda g: _pdd(g, 4),
    },
    "crf12": {
        "note": "PDD 8 步 + crf=12（近视觉无损，默认）",
        "graph": lambda g: _pdd(g, 8, 12),
    },
    "crf2": {
        "note": "PDD 8 步 + crf=2（接近无损，验证码率上限）",
        "graph": lambda g: _pdd(g, 8, 2),
    },
    "res1024": {
        "note": "PDD 8 步 + crf12 @ 1024×1024（短边超 768，验证是否掉画质）",
        "graph": lambda g: _pdd(g, 8, 12),
    },
    "nolora": {
        "note": "对照组：无画质 LoRA（PDD 8 步 + crf12，与下面三条同基线）",
        "graph": lambda g: _pdd(g, 8, 12),
    },
    "realism": {
        "note": "写实人像 LoRA（触发词 r34l1sm）",
        "graph": lambda g: (_pdd(g, 8, 12),
                            apply_style_lora(g, QUALITY_LORAS["realism"], 1.0)),
    },
    "cinema": {
        "note": "电影感 LoRA V1.0",
        "graph": lambda g: (_pdd(g, 8, 12),
                            apply_style_lora(g, QUALITY_LORAS["cinema"], 1.0)),
    },
    "davham": {
        "note": "davham 电影感 LoRA（原作者建议配 euler/simple 0.8）",
        "graph": lambda g: (_pdd(g, 8, 12),
                            apply_style_lora(g, QUALITY_LORAS["davham"], 0.8)),
    },
}


# H3 官方硬约束（UnifiedForge/h3.c + FlowForgeLab 8GB 工作流两份来源一致）：
#   短边 <= 768、面积 <= 768x1344、宽高都是 32 的倍数。
# 超过短边 768 画质反而下降 —— 训练区间就在 768p。
H3_SHORT_MAX = 768
H3_AREA_MAX = 768 * 1344


def _snap(v: int) -> int:
    return max(32, int(round(v / 32.0)) * 32)


def fit_resolution(ref_w: int, ref_h: int, mode: str, wf: int, hf: int) -> tuple[int, int]:
    """把参考图比例压进 H3 的合规区间，尽量取最大像素。

    为什么必须保持参考图比例（2026-11-15 实测踩到）：
    p5 的参考图是 1024x1024（1:1），却按 768x512（3:2）出片 ——
    人物被横向拉扁，用户一眼就看出来"比例不对"。

    为什么上限是短边 768 而不是往大里堆：
    H3 是 768p 模型，短边超过 768 会离开训练区间，画质反而下降
    （FlowForgeLab 8GB 工作流里明确记着这个）。
    """
    if wf and hf:
        return wf, hf
    if mode == "square768":
        return 768, 768
    if mode == "square1024":
        # 故意越过短边 768 的限制：验证"H3 是 768p 模型、超了会掉画质"
        # 这个社区说法在我们这套管线里成不成立。
        return 1024, 1024
    if mode == "hd":
        return 1024, 768
    if mode == "same":
        return _snap(ref_w), _snap(ref_h)

    # max：保持比例，短边钉到 768，再按面积上限收
    ar = ref_w / ref_h
    if ref_w >= ref_h:          # 横版或正方：高是短边
        h = H3_SHORT_MAX
        w = _snap(h * ar)
    else:                       # 竖版：宽是短边
        w = H3_SHORT_MAX
        h = _snap(w / ar)
    # 面积超限就等比缩
    if w * h > H3_AREA_MAX:
        scale = (H3_AREA_MAX / (w * h)) ** 0.5
        w, h = _snap(w * scale), _snap(h * scale)
    return w, h


def _ref_size() -> tuple[int, int]:
    """读参考图宽高（PNG 头，不依赖 PIL）。"""
    p = ROOT / "reports" / "p2" / "p21" / "inputs" / REF_IMG
    try:
        head = p.read_bytes()[:33]
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            import struct
            w, h = struct.unpack(">II", head[16:24])
            return int(w), int(h)
    except Exception:
        pass
    return 1024, 1024


_SPEED_MODE = ""


def make_spec(wf_key: str, prompt: str, variant: str, wf: int, hf: int):
    from tools.p2 import lifecycle
    from tools.p20 import p20ctl as p20cli
    from tools.p20 import registry

    # 参数必须按**每个工作流自己的契约**给，不能一套打通。
    # pack_video_minimax_h3_r2v 只接受 image / image_2 / prompt / seed ——
    # 它没有 width/height/frames/cfg/neg_prompt（尺寸和图都来自参考图本身）。
    # 多给一个参数会被 resolve 直接拒绝。
    params: dict = {"prompt": prompt, "seed": SEED}
    if wf_key == "pack_video_minimax_h3_r2v":
        params["image"] = REF_IMG
        params["image_2"] = REF_IMG
    else:
        params.update({
            "neg_prompt": NEG,
            "width": wf,
            "height": hf,
            "frames": 121,
            # cfg=2 是唯一被实测验证过的值（p1-20261113T2316-93978b 用的就是 2）。
            # 卡里的默认 1.0 会出全黑，见 NEG 的注释。
            "cfg": 2.0,
        })
        if wf_key == "h3_i2v_121f":
            params["image"] = REF_IMG
    # 变体对 params 的覆盖**必须在 resolve 之前**：
    # steps/cfg 都是卡上 schema 的字段，resolve 会按 schema 校验并生成 spec，
    # 之后再改 params 不会进 spec；只改 graph 又会被 render 的 apply_params
    # 覆盖回去（2026-11-15 实测：想跑 8 步，实际跑了 20 步）。
    # 优先级：--speed 显式指定 > 变体自带的 params 覆盖 > 卡默认值。
    # --speed 走的是卡的 presets 展开（render.expand_presets），
    # 所以这里**不能**再塞 steps/lora_strength —— 会被展开前的显式值盖住。
    if _SPEED_MODE:
        params["speed_mode"] = _SPEED_MODE
        params.pop("steps", None)
        params.pop("lora_strength", None)
    else:
        for _k, _v in (VARIANTS_PARAMS.get(variant) or {}).items():
            if _k in (registry.get_workflow(wf_key).get("params") or {}):
                params[_k] = _v

    res = registry.resolve(wf_key, params=params, allow_experimental=True)
    spec = p20cli._make_spec(res, job_key=wf_key, task_ref="H3-画质实验")
    tc, src = lifecycle.normalize_task_class("short")
    spec.update(task_class=tc, task_class_source=src)

    # 变体改动直接写进 spec 的 model.workflow（graph 是路径，先读出来改再落盘）
    graph_path = ROOT / spec["model"]["workflow"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    VARIANTS[variant]["graph"](graph)
    # cfg 统一 2.0：卡里默认 1.0 会出全黑（见 NEG 注释）。
    # 对所有变体都设，保证只有被研究的那个变量在变。
    apply_cfg(graph, 2.0)
    # neg_prompt 为空时渲染出来是 ""，但图里节点期望有输入，填空串即可


    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    # 用 variant 前缀命名 graph，让作业目录里能看出是哪个变体
    new_graph = SPEC_DIR / f"graph-{variant}-{wf_key}.json"
    new_graph.write_text(json.dumps(graph, ensure_ascii=False, indent=1), encoding="utf-8")
    spec["model"]["workflow"] = str(new_graph.relative_to(ROOT))
    return spec, new_graph


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="H3 画质实验")
    ap.add_argument("--list", action="store_true", help="列出变体与提示词")
    ap.add_argument("--variant", default="fixed")
    ap.add_argument("--prompt", default="p1", choices=sorted(PROMPTS))
    ap.add_argument("--workflow", default="",
                    help="工作流 key；留空时按提示词自动选"
                         "（p4→r2v、p5→i2v、其余 t2v）")
    ap.add_argument("--width", type=int, default=0,
                    help="留空则按参考图比例算（i2v 保持首帧比例）")
    ap.add_argument("--height", type=int, default=0)
    ap.add_argument("--speed", default="",
                    help="直接指定 speed_mode（quality/balanced/fast），"
                         "优先于 --variant 的 steps/lora 设定")
    ap.add_argument("--res", default="max",
                    choices=["max", "square768", "square1024", "same", "hd"],
                    help="分辨率档：max=保持原比例的最高合规档；"
                         "square768=768×768；same=参考图原始尺寸（可能超限）；"
                         "hd=1024×768（横版）")
    ap.add_argument("--dry-run", action="store_true", help="只建 spec 不跑")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)

    if a.list:
        print("变体：")
        for k, v in VARIANTS.items():
            print(f"  {k:10s} {v['note']}")
        print("\n提示词：", ", ".join(sorted(PROMPTS)))
        return 0

    global _SPEED_MODE
    _SPEED_MODE = a.speed
    if a.variant not in VARIANTS:
        print(f"✗ 没有变体 {a.variant}", file=sys.stderr)
        return 1

    # 参考图类提示词必须用对应的工作流，否则 image/image_2 参数根本不存在
    wf = a.workflow or IMAGE_PROMPTS.get(a.prompt) or "h3_t2v_121f"

    rw, rh = _ref_size()
    w, h = fit_resolution(rw, rh, a.res, a.width, a.height)
    print(f"参考图 {rw}x{rh} → 输出 {w}x{h}"
          f"（{w*h/1e6:.2f}MP，短边 {min(w,h)}）")
    spec, gpath = make_spec(wf, PROMPTS[a.prompt], a.variant, w, h)
    print(f"工作流 {wf}")
    print(f"变体 {a.variant}：{VARIANTS[a.variant]['note']}")
    print(f"提示词 {a.prompt}（{len(PROMPTS[a.prompt])} 字符）")
    print(f"graph → {gpath.relative_to(ROOT)}")

    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    spec_path = SPEC_DIR / f"spec-{a.variant}-{a.prompt}.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    p = subprocess.run([sys.executable, str(JOBCTL), "new", "--spec",
                        str(spec_path), "--task", "H3-画质实验", "--force"],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=600)
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        print(f"✗ 建作业失败：{out.strip()[-500:]}", file=sys.stderr)
        return 1
    ids = re.findall(r"p1-\d{8}T\d{4}-[0-9a-f]{6}", out)
    if not ids:
        print(f"✗ 取不到 job_id：{out[-400:]}", file=sys.stderr)
        return 1
    jid = ids[-1]
    print(f"作业 {jid}")

    if a.dry_run:
        print("--dry-run，未执行")
        return 0
    if not a.yes:
        try:
            if input("开始跑？约 ¥0.3 [y/N] ").strip().lower() not in ("y", "yes"):
                print("已取消")
                return 0
        except EOFError:
            pass

    log = ROOT / "reports" / "p1" / "jobs" / jid / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as fh:
        fh.write(f"\n--- h3lab {a.variant}/{a.prompt} ---\n".encode())
    subprocess.run([sys.executable, str(RUNPY), jid, "--batch", "--no-keep",
                    "--from-pool", "--wait-lock", "14400"],
                   cwd=str(ROOT), timeout=3600)
    job = json.loads((ROOT / "reports" / "p1" / "jobs" / jid / "job.json")
                     .read_text(encoding="utf-8"))
    print(f"状态 {job.get('state')}")
    arts = list((ROOT / "reports" / "p1" / "jobs" / jid / "artifacts").glob("*.mp4"))
    if arts:
        dst = ROOT / "reports" / "lab" / "h3lab"
        dst.mkdir(parents=True, exist_ok=True)
        target = dst / f"{a.variant}-{a.prompt}.mp4"
        target.write_bytes(arts[0].read_bytes())
        print(f"产物 → {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
