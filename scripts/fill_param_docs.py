#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 registry 里缺 description 的参数补说明。

现状：54 个工作流都有顶层说明，但 328 个参数里只有 55 个（17%）有说明。
用户在界面上看到 seed / cfg / frames 这些字段时不知道是什么。

分两层：
  1. COMMON：按参数名套用公共说明（同名参数在 49/46/33 个工作流里重复出现，
     说明通用）。只在该参数确实没有说明时填充，绝不覆盖已有内容。
  2. 个别工作流的特殊参数（如 upscale_model、color_correction）单独补。

用法：
  python3 scripts/fill_param_docs.py            # 预览：会补哪些
  python3 scripts/fill_param_docs.py --apply     # 写回 json
  python3 scripts/fill_param_docs.py --apply --import   # 写回后顺便导库

注意：改完必须 p20ctl import，否则 registry.db 里还是旧说明。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
WFDIR = PROJ / "registry" / "workflows"

# 按参数名的公共说明。措辞目标：说清「填什么」+「影响什么」，不写废话。
COMMON = {
    "seed": "随机种子。同一个种子 + 同一组参数能得到完全相同的结果；想复现某次出图就记下它。"
            "点「随机」换一个。",
    "prompt": "正向提示词：描述你想要的画面内容（主体、外貌、衣着、背景、风格）。"
              "写得具体比写得多更重要。",
    "neg_prompt": "负向提示词：不想出现的东西。默认值是一套通用的避坑词（防手部畸形、"
                  "画面静止、糊、水印等），没有特殊需求就保持原样。",
    "image": "主输入图。人脸/人物类工作流用它当身份或首帧来源。",
    "image_2": "第二张输入图（多图输入的第二个位置，常见于参考图 + 素材的组合）。",
    "image_3": "第三张输入图（多图输入序列的第三个位置）。",
    "image_4": "第四张输入图（多图输入序列的第四个位置）。",
    "width": "输出宽度（像素）。越大越清晰但越慢越贵；改完后高度要按同样的比例缩放，"
             "否则画面会被拉变形。",
    "height": "输出高度（像素）。与宽度配套调整，保持原始素材的宽高比。",
    "cfg": "提示词遵从度（CFG scale）。低（1-2）= 更自由、更贴合输入素材；"
           "高（7+）= 更严格按提示词来，但容易僵硬或过饱和。改图/动作迁移类建议留在低位。",
    "steps": "采样步数。步数越多细节越好但线性变慢；蒸馏版模型（名字带 distilled / fast / "
             "8 步）刻意用很少的步数换速度，不要随意调高。",
    "sampler": "采样器。不同采样器的风格倾向不同；没有特别理由就沿用默认值，"
               "换采样器可能让原本稳定的结果变得不可预期。",
    "scheduler": "噪声调度器。与采样器配合决定去噪节奏；默认值通常已针对该模型调过。",
    "frames": "生成帧数。视频类一般是 4n+1（如 81 / 121），因为模型按 4 帧一组处理。"
              "帧数直接决定时长和花费：帧数翻倍，耗时和钱基本也翻倍。",
    "batch_size": "一次生成几张/几段。批量能省下重复的建机与加载时间，"
                  "但显存占用会成倍上升，OOM 时先降这个。",
    "duration": "目标时长（秒）。与帧率一起决定总帧数。",
    "trim_seconds": "驱动视频截取秒数（0 = 用整条）。动作迁移会按驱动视频的帧数分轮跑，"
                    "每 81 帧约 3 分钟；截 4 秒（16fps→64 帧）约 ¥0.2，不截可能 20 分钟 ¥0.9。",
    "denoise": "重绘强度。0 = 完全不动，1 = 完全重画。改图类工作流靠它控制「改多少」："
               "低（0.2-0.4）保留原图结构，只修细节；高（0.6+）大改，容易跑偏。",
    "upscale_model": "放大模型文件名（需在共享库里存在）。不同模型的风格不同："
                     "4x-UltraSharp 偏锐利，适合照片；其他模型可能偏平滑或带锐化痕迹。",
    "color_correction": "色彩校正方式。none = 不做校正；"
                        "mtb / wavelet 等会尝试把生成结果的色调拉回参考素材，减少偏色。",
    "fps": "输出帧率。与帧数一起决定时长；改帧率而不改帧数会让视频变快或变慢。",
    "strength": "变换强度/权重，具体含义取决于工作流用途；先用默认值出一版再微调。",
    "lora_weight": "LoRA 权重。0 = 不生效，1 = 标准强度，超过 1 容易过拟合出怪样子。",
    "reference_image": "参考图：提供风格、构图或人物特征，不直接作为首帧。",
    "mask": "遮罩图：白色区域会被重绘，黑色区域保持原样。",
    "video": "输入视频。作为驱动源（动作/节奏/镜头运动）使用。",
    "prompt_2": "第二组提示词。多阶段工作流里对应后一阶段；"
                "单阶段工作流里通常与 prompt 相同，无需单独修改。",
    "prompt_3": "第三组提示词（常见于两段式采样的负向词）。一般与 neg_prompt 相同。",
    "prompt_4": "第四组提示词（两段式采样的第二阶段正向）。一般与 prompt 相同。",
    "prompt_5": "第五组提示词（两段式采样的第二阶段姿态描述）。一般与 prompt_2 相同。",
}

# 仅特定工作流需要的说明（参数名不够通用）
SPECIAL = {
    "pack_video_wan_animate2": {
        "image_2": "驱动视频（MP4/MOV/WebM），提供动作和姿态来源；"
                   "和人物图 image 一起提交，两者缺一不可。",
    },
    "pack_video_wan_animate2_distilled": {
        "image_2": "驱动视频（MP4/MOV/WebM），提供动作和姿态来源；"
                   "和人物图 image 一起提交，两者缺一不可。",
    },
    "zimage_turbo_controlnet_canny": {
        "canny_low": "Canny 边缘检测的弱阈值。像素梯度低于它就一定不是边缘。"
                     "调低 = 保留更多细节线条，可能把噪点也当成边；调高 = 只留最明显的轮廓。",
        "canny_high": "Canny 边缘检测的强阈值。梯度高于它一定算边缘，介于两者之间的"
                      "按连通性决定。与 canny_low 配合，一般高:低 ≈ 2:1 或 3:1。",
        "start": "ControlNet 生效的起始步数比例（0-1）。0 = 从第一步就约束构图，"
                 "越接近 1 = 越晚介入，留给模型自由发挥的空间越大。",
        "end": "ControlNet 停止生效的步数比例（0-1）。1 = 全程约束；"
               "提前结束（如 0.8）能减少线条感残留，让成品更自然。",
    },
    "sdxl_t2i_lora": {
        "lora_strength": "LoRA 权重强度。0 = 不生效（等于基础模型），1 = 标准强度。"
                         "超过 1 容易过拟合出怪样子；建议 0.6-0.9 之间试。",
    },
    "qwen_image_edit_2509": {
        "negative_prompt": "负向提示词：不想出现的内容。与 neg_prompt 同义，"
                           "不同工作流的字段命名习惯不同而已。",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="写回后执行 p20ctl import")
    args = ap.parse_args()

    files = sorted(WFDIR.glob("*.json"))
    plan, touched, skipped = [], 0, 0
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        key = d.get("key") or f.stem
        params = d.get("params") or {}
        changed = False
        for name, spec in params.items():
            spec = spec or {}
            if spec.get("description"):
                skipped += 1
                continue
            text = (SPECIAL.get(key) or {}).get(name) or COMMON.get(name)
            if not text:
                continue
            spec["description"] = text
            params[name] = spec
            plan.append((key, name, text))
            changed = True
        if changed:
            touched += 1
            if args.apply:
                f.write_text(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")

    print(f"扫描 {len(files)} 个工作流")
    print(f"已有说明（跳过）：{skipped}")
    print(f"本次补充：{len(plan)} 个参数，涉及 {touched} 个工作流")
    if not args.apply:
        print("\n预览（前 15 条）：")
        for k, n, t in plan[:15]:
            print(f"  {k}.{n}: {t[:70]}…")
        print("\n加 --apply 写入")
        return 0

    print("✓ 已写回")
    if args.do_import:
        print("正在导库…")
        r = subprocess.run([sys.executable, "-m", "tools.p20.p20ctl", "import"],
                           cwd=str(PROJ), capture_output=True, text=True)
        print((r.stdout or "") + (r.stderr or ""))
    else:
        print("⚠ 记得跑：python3 -m tools.p20.p20ctl import")
    return 0


if __name__ == "__main__":
    sys.exit(main())
