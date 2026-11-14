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

# 参考图：p4/p5 都描述同一个银发异色瞳少女，
# 所以用同一张图（由 qwen_image_t2i 生成，见 reports/p2/p21/inputs/in-silverice.png）
REF_IMG = "in-silverice.png"

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
VARIANTS = {
    "baseline": {
        "note": "改动前：shift 5/1 · euler/simple · int8 VAE · 20 步",
        "graph": lambda g: (apply_shift(g, 5.0, 1.0),
                            apply_sampler(g, "euler", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_int8_convrot-v2.safetensors")),
    },
    "fixed": {
        "note": "改动后：shift 12/3 · res_multistep · fp16 VAE · 20 步",
        "graph": lambda g: (apply_shift(g, 12.0, 3.0),
                            apply_sampler(g, "res_multistep", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_fp16.safetensors")),
    },
    "shift12": {
        "note": "只改 shift 12/3（其余同 baseline）",
        "graph": lambda g: (apply_shift(g, 12.0, 3.0),
                            apply_sampler(g, "euler", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_int8_convrot-v2.safetensors")),
    },
    "vae16": {
        "note": "只改 fp16 VAE（其余同 baseline）",
        "graph": lambda g: (apply_shift(g, 5.0, 1.0),
                            apply_sampler(g, "euler", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_fp16.safetensors")),
    },
    "steps32": {
        "note": "改动后 + 32 步（社区：32 步音质/运动更好，只多 15-20% 时间）",
        "graph": lambda g: (apply_shift(g, 12.0, 3.0),
                            apply_sampler(g, "res_multistep", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_fp16.safetensors"),
                            apply_steps(g, 32)),
    },
    "resmulti": {
        "note": "只改 res_multistep（其余同 baseline）",
        "graph": lambda g: (apply_shift(g, 5.0, 1.0),
                            apply_sampler(g, "res_multistep", "simple"),
                            apply_vae(g, "minimax_h3_video_vae_int8_convrot-v2.safetensors")),
    },
}


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
    ap.add_argument("--width", type=int, default=768)
    ap.add_argument("--height", type=int, default=512)
    ap.add_argument("--dry-run", action="store_true", help="只建 spec 不跑")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)

    if a.list:
        print("变体：")
        for k, v in VARIANTS.items():
            print(f"  {k:10s} {v['note']}")
        print("\n提示词：", ", ".join(sorted(PROMPTS)))
        return 0

    if a.variant not in VARIANTS:
        print(f"✗ 没有变体 {a.variant}", file=sys.stderr)
        return 1

    # 参考图类提示词必须用对应的工作流，否则 image/image_2 参数根本不存在
    wf = a.workflow or IMAGE_PROMPTS.get(a.prompt) or "h3_t2v_121f"

    spec, gpath = make_spec(wf, PROMPTS[a.prompt], a.variant, a.width, a.height)
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
