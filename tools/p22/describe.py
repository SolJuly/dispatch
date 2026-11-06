#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 60 条工作流补「人话」名称与描述（name / description），便于控制台选择。

- name：能力 + 模型/特点，尽量一眼看懂
- description：做什么、要什么输入、出什么、当前状态
- notes：保留原有技术备注
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

VERIFIED_NOTE = "✅ 可使用"
EXP_NOTE = "🚧 尚未开发"

SUPERSEDED: set[str] = set()  # 旧的 6 条已删除

# key: (name, description)
CATALOG = {
    # ---------------- 图片：文生图 ----------------
    "zimage_turbo_t2i": ("文生图 · Z-Image-Turbo 1024（8 步基线）",
        "主力文生图，8 步约 6–8 秒出 1024×1024。输入：提示词；输出：PNG。"),
    "zimage_turbo_t2i_hq": ("文生图 2K · Z-Image-Turbo（8 步 + 2× 细化）",
        "先出 1024 再 2 倍细化到 2048×2048。输入：提示词；输出：2K PNG。"),
    "zimage_turbo_batch4": ("批量出图 · Z-Image-Turbo（一次 4 张）",
        "一次生成 4 张 1024×1024，方便挑图。输入：提示词；输出：4 张 PNG。"),
    "zimage_turbo_img2img": ("图生图 · Z-Image-Turbo（denoise 可控）",
        "以原图为底重绘，denoise 越低越接近原图。输入：图片 + 提示词；输出：1024 PNG。"),
    "zimage_turbo_controlnet_canny": ("ControlNet 构图 · Z-Image-Turbo（Canny 边缘）",
        "用边缘图控制构图。输入：参考图 + 提示词；输出：1024 PNG。"),
    "qwen_image_t2i": ("文生图 · Qwen-Image 1024（全参数）",
        "Qwen-Image 20B，步数/CFG/采样器/尺寸都可调。输入：提示词；输出：1024×1024 PNG。"),
    "qwen_image_t2i_landscape": ("文生图横版 · Qwen-Image 1024×768",
        "固定横版尺寸，适合风景/场景。输入：提示词；输出：1024×768 PNG。"),
    "qwen_image_t2i_portrait": ("文生图竖版 · Qwen-Image 768×1024",
        "固定竖版尺寸，适合人像/海报。输入：提示词；输出：768×1024 PNG。"),
    "flux2_klein_4b_fast": ("文生图 · FLUX.2 Klein 4B（4 步，快）",
        "4 步出 1024 图，速度优先，适合快速出方案。输入：提示词；输出：PNG。"),
    "sdxl_t2i": ("文生图 · SDXL 1024（最快基线）",
        "约 7 秒一张的基础出图。输入：提示词；输出：1024×1024 PNG。"),
    "sdxl_img2img": ("图生图 · SDXL（denoise 控制）",
        "以原图为底重绘，denoise 越低越接近原图。输入：图片 + 提示词；输出：1024 PNG。"),
    "sdxl_inpaint": ("局部重绘 · SDXL（蒙版）",
        "只改蒙版区域，其余保持。输入：底图 + 蒙版 + 提示词；输出：1024 PNG。"),
    "sdxl_t2i_lora": ("文生图 + 风格 LoRA · SDXL（厚涂）",
        "带厚涂风格 LoRA 的 SDXL 出图。输入：提示词；输出：1024 PNG。"),
    "pack_image_z_image_turbo": ("文生图 · Z-Image（旧版）",
        "早期 Z-Image 基线，已被 zimage_turbo_t2i 系列替代，不建议使用。"),
    "pack_image_z_image_turbo_fun_union_controlnet": ("ControlNet · Z-Image（旧版）",
        "已被 zimage_turbo_controlnet_canny 替代，不建议使用。"),
    "pack_image_flux2_klein_text_to_image": ("文生图 · FLUX.2 Klein（旧版）",
        "已被 flux2_klein_4b_fast 替代，不建议使用。"),
    "pack_template_qwen_image_illustration_lora": ("插画风格 · Qwen-Image + LoRA（尚未开发）",
        "加载插画 LoRA 出风格化图。输入：提示词；输出：1024 PNG。缺 illustration LoRA 权重。"),
    "pack_utility_z_image_turbo_2k_upscaler_app": ("2K 精绘 · Z-Image-Turbo（8 步 + 细化）",
        "先出 1024 再细化到 2048。输入：提示词；输出：2048×2048 PNG。"),

    # ---------------- 图片：改图 / 编辑 ----------------
    "qwen_image_edit_2509": ("指令改图 · Qwen-Image-Edit 2509（4 步）",
        "按中英文指令改图（换背景/改风格/加删元素），4 步 Lightning。输入：底图 + 指令；输出：1024 PNG。"),
    "pack_image_qwen_image_edit_2511_int8": ("指令改图 · Qwen-Image-Edit 2511（int8，尚未开发）",
        "2511 新版指令改图，int8 省显存。输入：底图 + 指令；输出：编辑图。当前缺节点输入。"),
    "pack_image_qwen_image_edit_2509": ("指令改图 · Qwen-Image-Edit 2509（旧版）",
        "已被 qwen_image_edit_2509 替代，不建议使用。"),
    "pack_image_qwen_image_layered": ("图层分离 · Qwen-Image Layered（尚未开发）",
        "把一张图拆成前景/背景等图层，便于分别修改。输入：1 张图；输出：多层结果。当前缺 CLIP 软链。"),
    "pack_image_qwen_image_2_1_image_edit": ("语义编辑 · Qwen-Image 2.1（换脸/换装，尚未开发）",
        "按指令改图，支持多参考保持一致性。输入：底图 + 参考图 + 指令；输出：编辑图。当前缺核心节点。"),
    "pack_image_qwen_image_2_1_background_removal": ("抠背景 · Qwen-Image 2.1（尚未开发）",
        "自动分离主体与背景。输入：1 张图；输出：去背景 PNG。当前缺核心节点。"),
    "pack_image_flux2_klein_image_edit_4b_distilled": ("换装/多图编辑 · FLUX.2 Klein 4B（蒸馏）",
        "两张图做换装或多图合成。输入：底图 + 参考图；输出：1024 编辑图。Apache-2.0，可商用。"),

    # ---------------- 图片：修复 / 放大 ----------------
    "detail_polish": ("细节增强 · 低 denoise 重绘 + 4× 放大",
        "把偏糊/缺皮肤纹理的图低强度重绘再放大 4 倍。输入：1 张图；输出：4× PNG（768×1024 → 3072×4096）。"),
    "upscale_4x": ("放大 4× · UltraSharp",
        "传统超分，4 倍放大，快但不补细节。输入：1 张图；输出：4× PNG。"),
    "seedvr2_3b_upscale": ("还原放大 · SeedVR2 3B（短边到 2K）",
        "还原式放大，短边到 2048，适合修复低清素材。输入：1 张图；输出：2K PNG。"),
    "seedvr2_3b_upscale_video": ("视频放大 · SeedVR2 3B（短边到 1080）",
        "逐帧还原放大视频。输入：MP4；输出：1080p MP4。"),
    "pack_utility_seedvr2_3b_int8_upscale_image": ("图片超分 · SeedVR2 3B int8（旧版）",
        "已被 seedvr2_3b_upscale 替代，不建议使用。"),
    "pack_utility_seedvr2_3b_int8_upscale_video": ("视频超分 · SeedVR2 3B int8（旧版）",
        "已被 seedvr2_3b_upscale_video 替代，不建议使用。"),
    "pack_utility_seedvr2_7b_int8_upscale_image": ("还原放大 · SeedVR2 7B（尚未开发）",
        "7B 版还原放大，细节更强但更吃显存。输入：1 张图；输出：放大图。缺 7B int8 权重。"),
    "pack_utility_image_upscale_supir": ("放大 · SUPIR（尚未开发）",
        "SUPIR 高质量放大。输入：1 张图；输出：放大图。缺 Juggernaut v9 配置。"),
    "pack_templates_hellorob_facegen_skindetail_upscale": ("人像精修 · 皮肤细节 + 风格 LoRA + 放大（尚未开发）",
        "人像洗图流水线：皮肤细节、风格化、超分。输入：1 张人像；输出：高清图。缺权重与节点。"),

    # ---------------- 视频 ----------------
    "pack_video_wan2_2_14b_i2v": ("图生视频 · Wan 2.2 14B（双专家 + 4 步）",
        "单图生成约 5 秒视频。输入：首帧图 + 提示词；输出：640×640/81 帧 MP4。"),
    "pack_video_wan2_2_14b_flf2v": ("首尾帧生视频 · Wan 2.2 14B",
        "给首帧和尾帧，生成中间过渡。输入：2 张图 + 提示词；输出：640×640/81 帧 MP4。"),
    "pack_video_wan2_2_14b_s2v": ("音频驱动 · Wan 2.2 S2V（对口型）",
        "用音频驱动人物口型与表情。输入：人物图 + 音频；输出：约 5 秒 MP4。"),
    "wan21_i2v_480p": ("图生视频 · Wan2.1 480P（49 帧）",
        "首帧生成约 3 秒 480P 视频。输入：首帧图 + 提示词；输出：MP4。"),
    "h3_i2v_121f": ("图生视频 · MiniMax H3（121 帧）",
        "单图驱动 121 帧（约 5 秒）视频。输入：首帧图 + 提示词；输出：MP4。"),
    "h3_t2v_121f": ("文生视频 · MiniMax H3（121 帧）",
        "纯文本生成 121 帧视频。输入：提示词；输出：MP4。"),
    "ltxv_2b_t2v": ("文生视频 · LTXV 2B（73 帧）",
        "轻量文生视频，约 2.9 秒。输入：提示词；输出：MP4。快但画质一般。"),
    "pack_video_fastvideo_fasth3_i2v": ("图生视频 · FastH3 8 步（快）",
        "FastH3 加速版，单图出短视频。输入：首帧图 + 提示词；输出：MP4。"),
    "pack_video_minimax_h3_r2v": ("多参考生视频 · MiniMax H3（R2V）",
        "多张参考图保持主体一致。输入：2+ 张参考图 + 提示词；输出：MP4。"),
    "pack_video_minimax_h3_multiframe_reference": ("多帧参考生视频 · MiniMax H3（4 张锚定）",
        "用 4 张关键帧锚定时间线生成连贯视频。输入：4 张图 + 提示词；输出：MP4。"),
    "pack_video_wan_animate2": ("动作迁移 · Wan Animate 2",
        "把驱动视频的动作迁移到目标人物。输入：人物图 + 驱动视频；输出：MP4。"),
    "pack_video_wan_animate2_distilled": ("动作迁移 · Wan Animate 2 蒸馏版（快）",
        "同上，蒸馏加速。输入：人物图 + 驱动视频；输出：MP4。"),
    "pack_video_ltx2_3_i2v": ("图生视频 · LTX 2.3（带原生音频，尚未开发）",
        "单图生成带音频的视频。输入：首帧图 + 提示词；输出：MP4。缺 LoRA 权重。"),
    "pack_video_ltx2_3_flf2v": ("首尾帧生视频 · LTX 2.3（尚未开发）",
        "首帧+尾帧生成过渡。输入：2 张图 + 提示词；输出：MP4。当前缺文本编码器输入。"),
    "pack_video_ltx2_3_ic_lora": ("视频风格/结构控制 · LTX 2.3 IC-LoRA（尚未开发）",
        "用 IC-LoRA 控制风格或结构。输入：参考 + 提示词；输出：MP4。缺权重/节点。"),
    "pack_video_ltx2_3_id_lora": ("人物一致性 · LTX 2.3 ID-LoRA（尚未开发）",
        "ID-LoRA 保持人物一致。输入：参考图 + 提示词；输出：MP4。缺 LoRA。"),
    "pack_video_ltx2_5_flf2v": ("首尾帧生视频 · LTX 2.5（更快、带音频，尚未开发）",
        "LTX 2.5 首尾帧过渡，自带音频。输入：2 张图；输出：MP4。当前绑定错位。"),
    "pack_template_ltx2_3_ic_lora_ingredients": ("素材驱动视频 · LTX 2.3 IC-LoRA（尚未开发）",
        "用参考素材 + IC-LoRA 控制视频风格。输入：参考图 + 提示词；输出：MP4。缺模型文件。"),
    "pack_video_fasth3_ominiref_r2v": ("多参考生视频 · FastH3 OmniRef（尚未开发）",
        "多张参考图驱动视频生成。输入：参考图 + 提示词；输出：MP4。当前张量尺寸不匹配。"),
    "pack_video_wan21_scail2_character_replacement": ("角色替换 · SCAIL-2（fp16，尚未开发）",
        "把视频里的人换成目标角色。输入：视频 + 角色图；输出：MP4。当前节点调用不匹配。"),
    "pack_video_wan21_scail2_character_replacement_int8": ("角色替换 · SCAIL-2（int8，尚未开发）",
        "同上，int8 省显存。当前。"),
    "pack_video_wan_vace_14b_ref2v": ("参考生视频 · Wan VACE 14B（尚未开发）",
        "参考图 + 文本生成视频。输入：参考图 + 提示词；输出：MP4。缺 VACE 官方权重。"),
    "pack_video_wan_vace_14b_v2v": ("视频重绘 · Wan VACE 14B（尚未开发）",
        "按提示词重绘整段视频。输入：视频 + 提示词；输出：MP4。缺权重。"),
    "pack_video_wan_vace_flf2v": ("首尾帧 · Wan VACE 14B（尚未开发）",
        "VACE 版首尾帧过渡。输入：2 张图；输出：MP4。缺权重。"),
    "pack_video_wan_vace_inpainting": ("视频换背景 · SAM3 分割 + VACE 重绘（尚未开发）",
        "先分割主体再重绘背景。输入：视频 + 蒙版；输出：MP4。缺权重。"),
    "pack_utility_void_video_inpainting": ("视频擦除/修复 · VOID（尚未开发）",
        "对视频做区域擦除与重绘。输入：视频 + 蒙版；输出：修复 MP4。缺权重且类型不匹配。"),
}


def status_note(key: str, compat: dict) -> str:
    if key in SUPERSEDED:
        return "⛔ 已替代，勿用"
    st = (compat.get(key) or {}).get("status")
    if st == "verified":
        return VERIFIED_NOTE
    if st == "equivalent":
        return "✅ 可使用（等价）"
    return EXP_NOTE


def main() -> int:
    compat = {e["workflow"]: e for e in json.load(open(ROOT / "registry/compat.json"))["entries"]}
    files = sorted((ROOT / "registry/workflows").glob("*.json"))
    missing = []
    changed = 0
    for f in files:
        d = json.load(open(f))
        key = d["key"]
        if key not in CATALOG:
            missing.append(key)
            continue
        name, desc = CATALOG[key]
        full = f"{desc}（{status_note(key, compat)}）"
        if d.get("name") == name and d.get("description") == full:
            continue
        new = {}
        for k, v in d.items():
            if k == "name":
                new["name"] = name
                new["description"] = full
            elif k == "description":
                continue
            else:
                new[k] = v
        if "description" not in new:
            new["description"] = full
        json.dump(new, open(f, "w"), ensure_ascii=False, indent=1)
        changed += 1
    print("updated", changed, "missing", missing)
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
