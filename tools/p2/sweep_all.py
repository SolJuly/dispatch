#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量工作流实测：把 registry 里**预检 ready** 的工作流各跑一次。

## 为什么要有这个脚本

`registry` 里 54 条工作流，此前只有 39 条有兼容性证据，剩下 15 条从没在
真机上跑过；而已验证的那批也不等于"现在还跑得通"——图改过、软链动过、
镜像升过，都可能让老证据失效。

要回答"这 54 条到底哪些能用"，唯一可信的办法是**每条真跑一次**。
手工点 41 次不现实（参数名写错、素材配错、重复花钱），所以集中在这里：
参数表是**声明式**的，跑之前可以 `--plan` 全部预演（¥0）。

## 分层

    --tier ready    只跑预检 ready 的（默认，41 条）
    --tier all      连缺权重的也跑（13 条会开机后 FAILED_ASSET，只烧建机费）
    --tier image    只跑图像
    --tier video    只跑视频

## 花费（全部来自实测单价）

    图像  ¥0.101/次  × 21 ≈ ¥2.1
    视频  ¥0.270/次  × 20 ≈ ¥5.4
    ────────────────────────────
    合计 ≈ ¥7.5（失败重试另算；预热池命中可再省 ~60%）

## 用法

    python3 tools/p2/sweep_all.py --plan              # 只预演，¥0
    python3 tools/p2/sweep_all.py --tier image        # 只跑图像
    python3 tools/p2/sweep_all.py --only zimage_turbo_t2i
    python3 tools/p2/sweep_all.py --tier ready --submit-only   # 只入队不执行
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INPUTS = ROOT / "reports" / "p2" / "p21" / "inputs"
SPEC_DIR = ROOT / "reports" / "p20" / "specs" / "sweep"


def dt_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S")
JOBCTL = ROOT / "tools" / "p1" / "jobctl.py"

# ---- 素材（按内容哈希命名）---------------------------------------------
APPLE = "in-a6891d7b.png"          # 1024² 苹果静物
GIRL_BAMBOO = "in-ff45f4b6.png"    # 768×1024 竹林和服人像
GIRL_CREEK = "in-d9b9cba6.png"     # 1620×1080 溪边白衣人像
SQ_A = "in-5935a044.png"           # 1024²
SQ_B = "in-867e2ac9.png"           # 768×1024
BIG = "in-c69017f5.png"            # 大图，放大类用
MASK = "mask-apple.png"            # 与 APPLE 配套的蒙版
VID_720 = "in-22a23224-t4-16fps.mp4"   # 720×1280 16fps 4s
WAV = "s2v-audio.wav"              # 音频
SEED = 20261114

P_IMG = "a ripe red apple on a weathered wooden table by a sunlit window, " \
        "soft morning light, shallow depth of field, highly detailed, photographic"
P_PORTRAIT = "a young woman in a cream kimono standing in a bamboo forest, " \
             "soft natural light, sharp facial features, detailed fabric, photographic"
P_VID = "The camera slowly pushes in as she turns her head toward the light, " \
        "gentle breeze moving the fabric"
NEG = "blurry, low quality, watermark, distorted, deformed"

# ---- 41 条 ready 工作流的参数表 -----------------------------------------
# 参数名必须与 registry schema 完全一致：写错一个字母会在 resolve 阶段被拒
# （¥0，但白等一次）。required 项必填，其余走卡里的默认值。
JOBS: list[dict] = [
    # ================= 图像 21 条 =================
    {"workflow": "zimage_turbo_t2i", "tier": "image",
     "note": "文生图基线 1024（8 步）", "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "zimage_turbo_t2i_hq", "tier": "image",
     "note": "文生图 HQ", "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "zimage_turbo_batch4", "tier": "image",
     "note": "批量 4 图", "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "zimage_turbo_img2img", "tier": "image",
     "note": "图生图", "params": {"image": APPLE, "prompt": P_IMG, "seed": SEED}},
    {"workflow": "zimage_turbo_controlnet_canny", "tier": "image",
     "note": "ControlNet 边缘", "params": {"image": APPLE, "prompt": P_IMG, "seed": SEED}},
    {"workflow": "sdxl_t2i", "tier": "image",
     "note": "SDXL 文生图", "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "sdxl_t2i_lora", "tier": "image",
     "note": "SDXL + LoRA", "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "sdxl_img2img", "tier": "image",
     "note": "SDXL 图生图", "params": {"image": APPLE, "prompt": P_IMG, "seed": SEED}},
    {"workflow": "sdxl_inpaint", "tier": "image",
     "note": "SDXL 局部重绘（image + mask）",
     "params": {"image": APPLE, "mask": MASK, "prompt": P_IMG, "seed": SEED}},
    {"workflow": "flux2_klein_4b_fast", "tier": "image",
     "note": "FLUX.2 Klein 文生图",
     "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "qwen_image_t2i", "tier": "image",
     "note": "Qwen 文生图", "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "qwen_image_t2i_landscape", "tier": "image",
     "note": "Qwen 文生图 横版", "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "qwen_image_t2i_portrait", "tier": "image",
     "note": "Qwen 文生图 竖版", "params": {"prompt": P_PORTRAIT, "seed": SEED}},
    {"workflow": "qwen_image_edit_2509", "tier": "image",
     "note": "Qwen 图像编辑",
     "params": {"image": GIRL_BAMBOO, "prompt":
                "same person, same pose, detailed kimono fabric, sharp facial "
                "features, natural bamboo forest lighting", "seed": SEED}},
    {"workflow": "pack_image_qwen_image_edit_2511_int8", "tier": "image",
     "note": "Qwen 编辑 int8",
     "params": {"image": GIRL_BAMBOO, "prompt":
                "same person, same pose, detailed kimono fabric, sharp facial "
                "features, natural bamboo forest lighting",
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_image_qwen_image_2_1_image_edit", "tier": "image",
     "note": "Qwen 2.1 双图编辑",
     "params": {"image": APPLE, "image_2": SQ_A, "prompt":
                "place the apple from picture 1 on the wooden table from picture 2, "
                "natural light, realistic shadows", "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_image_qwen_image_2_1_background_removal", "tier": "image",
     "note": "Qwen 2.1 抠图",
     "params": {"image": APPLE,
                "prompt": "remove the background, keep the apple, clean alpha edge",
                "seed": SEED}},
    {"workflow": "pack_image_qwen_image_layered", "tier": "image",
     "note": "分层输出 RGBA",
     "params": {"image": APPLE, "prompt": P_IMG, "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_image_flux2_klein_image_edit_4b_distilled", "tier": "image",
     "note": "FLUX.2 Klein 双图编辑",
     "params": {"image": APPLE, "image_2": SQ_A, "prompt":
                "stylize the apple with warm golden light, keep the shape",
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "seedvr2_3b_upscale", "tier": "image",
     "note": "SeedVR2 3B 放大（单图）", "params": {"image": GIRL_BAMBOO, "seed": SEED}},
    {"workflow": "detail_polish", "tier": "image",
     "note": "低 denoise 重绘 + 4x 放大",
     "params": {"image": GIRL_BAMBOO, "prompt": P_PORTRAIT,
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_utility_z_image_turbo_2k_upscaler_app", "tier": "image",
     "note": "Z-Image 2K 放大",
     "params": {"image": GIRL_BAMBOO, "prompt": P_PORTRAIT,
                "neg_prompt": NEG, "seed": SEED}},

    # ================= 视频 20 条 =================
    {"workflow": "h3_t2v_121f", "tier": "video",
     "note": "H3 文生视频 121 帧", "params": {"prompt": P_VID, "seed": SEED}},
    {"workflow": "h3_i2v_121f", "tier": "video",
     "note": "H3 图生视频 121 帧",
     "params": {"image": GIRL_BAMBOO, "prompt": P_VID, "seed": SEED}},
    {"workflow": "ltxv_2b_t2v", "tier": "video",
     "note": "LTXV 2B 文生视频", "params": {"prompt": P_VID, "seed": SEED}},
    {"workflow": "wan21_i2v_480p", "tier": "video",
     "note": "Wan2.1 图生视频 480p",
     "params": {"image": GIRL_BAMBOO, "prompt": P_VID, "seed": SEED}},
    {"workflow": "pack_video_fastvideo_fasth3_i2v", "tier": "video",
     "note": "FastH3 图生视频（快）",
     "params": {"image": GIRL_BAMBOO, "prompt": P_VID, "seed": SEED}},
    {"workflow": "pack_video_fasth3_ominiref_r2v", "tier": "video",
     "note": "FastH3 多参考图生视频",
     "params": {"image": GIRL_BAMBOO, "image_2": GIRL_CREEK, "prompt":
                "subject_definitions:\n<Subject 1> is the woman in <Picture 1>: "
                "a young woman in a cream kimono standing in a bamboo forest.\n"
                "<Subject 2> is the setting in <Picture 2>: a woman sitting by a "
                "quiet creek at golden hour.\n\nsummary: The camera slowly "
                "pushes in as she turns her head toward the light.",
                "seed": SEED}},
    {"workflow": "pack_video_minimax_h3_r2v", "tier": "video",
     "note": "MiniMax H3 两图参考生视频",
     "params": {"image": GIRL_BAMBOO, "image_2": GIRL_CREEK,
                "prompt": "A young woman walks from the bamboo grove toward the creek.",
                "seed": SEED}},
    {"workflow": "pack_video_minimax_h3_multiframe_reference", "tier": "video",
     "note": "MiniMax H3 四帧参考",
     "params": {"image": GIRL_BAMBOO, "image_2": GIRL_CREEK,
                "image_3": SQ_A, "image_4": SQ_B,
                "prompt": "subject_definitions:\n<Subject 1> is the woman in "
                          "<Picture 1>, a young woman in a cream kimono.\n"
                          "<Subject 2> is the setting in <Picture 2>, a quiet creek "
                          "at golden hour.\n\nsummary: The camera slowly pushes in "
                          "as she turns her head toward the light.",
                "seed": SEED}},
    {"workflow": "pack_video_ltx2_3_i2v", "tier": "video",
     "note": "LTX 2.3 图生视频",
     "params": {"image": GIRL_BAMBOO, "prompt": P_VID,
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_video_ltx2_3_flf2v", "tier": "video",
     "note": "LTX 2.3 首尾帧",
     "params": {"image": GIRL_BAMBOO, "image_2": GIRL_CREEK,
                "prompt": "The camera moves from the bamboo forest to a quiet "
                          "stream at golden hour.", "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_video_ltx2_5_flf2v", "tier": "video",
     "note": "LTX 2.5 首尾帧（无 prompt）",
     "params": {"image": GIRL_BAMBOO, "image_2": GIRL_CREEK, "neg_prompt": NEG}},
    {"workflow": "pack_video_ltx2_3_id_lora", "tier": "video",
     "note": "LTX 2.3 身份 LoRA",
     "params": {"image": GIRL_BAMBOO, "prompt": P_VID,
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_video_ltx2_3_ic_lora", "tier": "video",
     "note": "LTX 2.3 双图 IC LoRA",
     "params": {"image": GIRL_BAMBOO, "image_2": GIRL_CREEK, "prompt": P_VID,
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_template_ltx2_3_ic_lora_ingredients", "tier": "video",
     "note": "LTX 2.3 配料模板",
     "params": {"image": GIRL_BAMBOO, "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_video_wan2_2_14b_i2v", "tier": "video",
     "note": "Wan2.2 14B 图生视频",
     "params": {"image": GIRL_BAMBOO, "prompt": P_VID,
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_video_wan2_2_14b_flf2v", "tier": "video",
     "note": "Wan2.2 14B 首尾帧",
     "params": {"image": GIRL_BAMBOO, "image_2": GIRL_CREEK,
                "prompt": "The camera moves from the bamboo forest to a quiet "
                          "stream at golden hour.", "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_video_wan2_2_14b_s2v", "tier": "video",
     "note": "Wan2.2 14B 语音驱动（image + audio）",
     "params": {"image": GIRL_CREEK, "audio": WAV,
                "prompt": "A young woman speaks gently toward the camera, "
                          "subtle head movement", "seed": SEED}},
    {"workflow": "pack_video_wan_animate2", "tier": "video",
     "note": "Wan Animate2 动作迁移（image + video）",
     "params": {"image": GIRL_CREEK, "video": VID_720,
                "prompt": "Character Description: a young woman in a white dress\n"
                          "Background description: a quiet creek at golden hour",
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_video_wan_animate2_distilled", "tier": "video",
     "note": "Wan Animate2 蒸馏版",
     "params": {"image": GIRL_CREEK, "image_2": GIRL_BAMBOO,
                "prompt": "Character Description: a young woman in a white dress\n"
                          "Background description: a quiet creek at golden hour",
                "neg_prompt": NEG, "seed": SEED}},
    {"workflow": "pack_video_wan21_scail2_character_replacement", "tier": "video",
     "note": "SCAIL2 角色替换（video + image）",
     # 负向词曾经抄了正向词的 subject（"A young woman with dark hair"），
     # 等于让模型别画主角 —— 62 分里有一部分是这条脚本自己造成的。
     "params": {"image": VID_720, "image_2": GIRL_CREEK,
                "prompt": "A young woman with dark hair, white dress, walking outdoors",
                "neg_prompt": "blurry, low quality, watermark, deformed hands"}},
    {"workflow": "pack_video_wan21_scail2_character_replacement_int8", "tier": "video",
     "note": "SCAIL2 角色替换 int8",
     # 这个工作流的 schema 只有 prompt / prompt_2，没有 prompt_3
     # （run_experimental.py 里那张旧表写了 prompt_3，会被 resolve 拒掉）。
     "params": {"image": VID_720, "image_2": GIRL_CREEK,
                "prompt": "girl, white dress",
                "prompt_2": "A young East Asian woman with long dark hair, wearing a "
                            "white dress, walking in natural light",
                "neg_prompt": NEG}},
    {"workflow": "seedvr2_3b_upscale_video", "tier": "video",
     "note": "SeedVR2 3B 视频放大",
     "params": {"video": VID_720, "seed": SEED}},

    # ============ 缺权重（预检 no_weight，跑必失败但可验通道）============
    {"workflow": "upscale_4x", "tier": "noweight",
     "note": "4x 放大（缺 4x-UltraSharp.pth）",
     "params": {"image": GIRL_BAMBOO}},
    {"workflow": "pack_utility_seedvr2_7b_int8_upscale_image", "tier": "noweight",
     "note": "SeedVR2 7B int8（缺权重）",
     "params": {"image": GIRL_BAMBOO}},
    {"workflow": "pack_utility_image_upscale_supir", "tier": "noweight",
     "note": "SUPIR 放大（缺权重）",
     "params": {"image": BIG}},
    {"workflow": "pack_template_qwen_image_illustration_lora", "tier": "noweight",
     "note": "插画 LoRA（缺权重）",
     "params": {"prompt": P_IMG, "seed": SEED}},
    {"workflow": "pack_templates_hellorob_facegen_skindetail_upscale", "tier": "noweight",
     "note": "脸/肤细节放大（缺权重）",
     "params": {"image": GIRL_BAMBOO, "prompt": P_PORTRAIT, "seed": SEED}},
    # 视频输入也叫 image（type=video），不是 video —— schema 就是这么定义的
    {"workflow": "pack_utility_void_video_inpainting", "tier": "noweight",
     "note": "Void 视频修补（缺权重）",
     "params": {"image": VID_720, "prompt": P_VID, "neg_prompt": NEG}},
    {"workflow": "pack_video_wan_vace_flf2v", "tier": "noweight",
     "note": "Wan VACE 首尾帧（缺权重）",
     "params": {"image": GIRL_BAMBOO, "image_2": GIRL_CREEK, "prompt": P_VID}},
    {"workflow": "pack_video_wan_vace_14b_v2v", "tier": "noweight",
     "note": "Wan VACE 视频转视频（缺权重）",
     "params": {"image": GIRL_CREEK, "image_2": VID_720,
                "prompt": P_VID, "neg_prompt": NEG}},
    {"workflow": "pack_video_wan_vace_14b_ref2v", "tier": "noweight",
     "note": "Wan VACE 参考生视频（缺权重）",
     "params": {"image": GIRL_BAMBOO, "prompt": P_VID}},
    {"workflow": "pack_video_wan_vace_inpainting", "tier": "noweight",
     "note": "Wan VACE 视频修补（缺权重）",
     "params": {"image": GIRL_CREEK, "image_2": VID_720,
                "prompt": P_VID, "neg_prompt": NEG}},
]

TIERS = {
    "ready": {"image", "video"},
    "image": {"image"},
    "video": {"video"},
    "noweight": {"noweight"},
    "all": {"image", "video", "noweight"},
}


# --------------------------------------------------------------- 预检
def preflight_params(job: dict) -> list[str]:
    """¥0 校验：参数名在 schema 里、必填齐全、素材真实存在。"""
    from tools.p20 import registry

    errs: list[str] = []
    wf = registry.get_workflow(job["workflow"])
    schema = wf.get("params") or {}
    params = job["params"]

    for name in params:
        if name not in schema:
            errs.append(f"参数 {name!r} 不在 schema（可用 {sorted(schema)}）")
    for name, spec in schema.items():
        if isinstance(spec, dict) and spec.get("required") and name not in params:
            errs.append(f"缺必填参数 {name!r}")
    for name, val in params.items():
        spec = schema.get(name) or {}
        if spec.get("type") not in ("image", "video", "audio"):
            continue
        p = INPUTS / str(val)
        if not p.is_file():
            errs.append(f"{name}={val} 文件不存在")
        elif p.stat().st_size < 200:
            errs.append(f"{name}={val} 只有 {p.stat().st_size} 字节，疑似占位")
    return errs


def make_spec(job: dict) -> Path:
    """解析参数 → 写 spec。失败抛异常，调用方负责记账。"""
    from tools.p2 import lifecycle
    from tools.p20 import p20ctl as p20cli
    from tools.p20 import registry

    res = registry.resolve(job["workflow"], params=job["params"],
                           allow_experimental=True)
    spec = p20cli._make_spec(res, job_key=job["workflow"],
                             task_ref="P2.2-全量实测")
    tc, src = lifecycle.normalize_task_class("short")
    spec.update(task_class=tc, task_class_source=src)

    SPEC_DIR.mkdir(parents=True, exist_ok=True)
    path = SPEC_DIR / f"sweep-{job['workflow']}.json"
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全量工作流实测")
    ap.add_argument("--plan", action="store_true", help="只预演，不建作业不花钱")
    ap.add_argument("--tier", default="ready", choices=sorted(TIERS),
                    help="跑哪一层（默认 ready）")
    ap.add_argument("--only", help="只跑这一个 workflow_key")
    ap.add_argument("--skip", action="append", default=[], help="跳过某个 key")
    ap.add_argument("--submit-only", action="store_true",
                    help="只建 spec 和入队，不启动执行")
    ap.add_argument("--yes", action="store_true", help="跳过花费确认")
    ap.add_argument("--concurrency", type=int, default=2,
                    help="同时在跑的作业数（默认 2，与 concurrency.MAX_PARALLEL 一致）")
    ap.add_argument("--from-pool", action="store_true", default=True,
                    help="优先复用预热池（默认开）")
    ap.add_argument("--no-pool", dest="from_pool", action="store_false",
                    help="不复用预热池")
    ap.add_argument("--poll", type=float, default=10.0, help="轮询间隔秒")
    a = ap.parse_args(argv)

    jobs = [j for j in JOBS if j["tier"] in TIERS[a.tier]]
    if a.only:
        jobs = [j for j in JOBS if j["workflow"] == a.only]
    if a.skip:
        jobs = [j for j in jobs if j["workflow"] not in a.skip]
    if not jobs:
        print("✗ 没有匹配的作业", file=sys.stderr)
        return 1

    # ---- 预检（¥0）----
    print(f"预检 {len(jobs)} 条（tier={a.tier}）…\n")
    bad = 0
    for j in jobs:
        errs = preflight_params(j)
        print(f"  {'✓' if not errs else '✗'} {j['workflow']:52s} {j['note']}")
        for e in errs:
            print(f"      ! {e}")
            bad += len(errs)
    if bad:
        print(f"\n✗ {bad} 个问题，先修再跑（本轮 ¥0）", file=sys.stderr)
        return 1
    print(f"\n✓ 参数与素材全就绪")

    n_img = sum(1 for j in jobs if j["tier"] == "image")
    n_vid = sum(1 for j in jobs if j["tier"] == "video")
    n_nw = sum(1 for j in jobs if j["tier"] == "noweight")
    est = n_img * 0.101 + n_vid * 0.270 + n_nw * 0.03
    print(f"\n分层：图像 {n_img} · 视频 {n_vid} · 缺权重 {n_nw}")
    print(f"预计花费 ¥{est:.2f}（图像 ¥0.101 / 视频 ¥0.270 / 缺权重约 ¥0.03 建机费）")

    if a.plan:
        print("\n--plan：以上作业会被创建，但不执行（¥0）")
        return 0

    if not a.yes:
        try:
            ans = input("\n继续？[y/N] ").strip().lower()
        except EOFError:
            print("非交互环境，加 --yes", file=sys.stderr)
            return 1
        if ans not in ("y", "yes"):
            print("已取消，¥0")
            return 0

    # ---- ① 建作业（CREATED，不执行）----
    print(f"\n建作业 {len(jobs)} 条…\n")
    built: list[tuple[dict, str]] = []
    fail = 0
    for i, j in enumerate(jobs, 1):
        try:
            spec = make_spec(j)
        except Exception as e:                    # noqa: BLE001
            print(f"  ✗ {j['workflow']}: resolve 失败 {type(e).__name__}: {e}")
            fail += 1
            continue
        p = subprocess.run([sys.executable, str(JOBCTL), "new",
                            "--spec", str(spec), "--task", "P2.2-全量实测"],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=600)
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode != 0:
            print(f"  ✗ {j['workflow']}: "
                  f"{out.strip().splitlines()[-1] if out.strip() else 'rc!=0'}")
            fail += 1
            continue
        import re
        ids = re.findall(r"p1-\d{8}T\d{4}-[0-9a-f]{6}", out)
        if not ids:
            print(f"  ✗ {j['workflow']}: 建作业成功但取不到 job_id")
            fail += 1
            continue
        jid = ids[-1]
        built.append((j, jid))
        print(f"  ✓ {j['workflow']:52s} → {jid}")

    print(f"\n建作业 {len(built)}/{len(jobs)}，失败 {fail}")
    if not built:
        return 1
    if a.submit_only:
        print("\n--submit-only：只建作业，未执行。")
        print("逐个启动：" + " ".join(f"run.py {jid}" for _, jid in built[:3]) + " …")
        return 0 if fail == 0 else 1

    # ---- ② 并发执行（默认 2，与 concurrency.MAX_PARALLEL 一致）----
    # 队列（jobqueue）已按设计停用（没有生产者），真实执行路径就是
    # spawn `run.py --batch`。这里自己做个小调度器：维持 N 个在跑，
    # 一条结束立刻补位，避免 44 条串行等 3 小时。
    from tools.p1 import state as jobstate

    conc = max(1, a.concurrency)
    running: dict[str, tuple[dict, subprocess.Popen, Path]] = {}
    queue_list = list(built)
    done_ok, done_fail = 0, 0
    print(f"\n并发 {conc} 开跑（预热池 {a.from_pool and '开' or '关'}）\n")

    def _launch(job: dict, jid: str) -> subprocess.Popen:
        log = ROOT / "reports" / "p1" / "jobs" / jid / "run.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(ROOT / "tools" / "p1" / "run.py"), jid,
               "--batch", "--no-keep", "--wait-lock", "14400"]
        if a.from_pool:
            cmd.append("--from-pool")
        fh = log.open("ab")
        fh.write(f"\n--- sweep {dt_stamp()} ---\n".encode())
        fh.flush()
        return subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                start_new_session=True, cwd=str(ROOT))

    while queue_list or running:
        while queue_list and len(running) < conc:
            job, jid = queue_list.pop(0)
            running[jid] = (job, _launch(job, jid), job)
            print(f"▶ {jid}  {job['workflow']}  （在跑 {len(running)}/{conc}）")
        time.sleep(a.poll)
        for jid, (job, proc, _) in list(running.items()):
            rc = proc.poll()
            if rc is None:
                continue
            running.pop(jid, None)
            try:
                st = jobstate.load_job(jid).get("state")
            except Exception:                     # noqa: BLE001
                st = "?"
            if st == "DONE":
                done_ok += 1
                print(f"✓ {jid}  {job['workflow']}  DONE"
                      f"  （成功 {done_ok} / 失败 {done_fail} / 余 {len(queue_list)}）")
            else:
                done_fail += 1
                print(f"✗ {jid}  {job['workflow']}  {st}"
                      f"  （成功 {done_ok} / 失败 {done_fail} / 余 {len(queue_list)}）")

    print(f"\n{'='*60}")
    print(f"完成 {done_ok} 成功 · {done_fail} 失败 · 共 {len(built)} 条")
    return 0 if done_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
