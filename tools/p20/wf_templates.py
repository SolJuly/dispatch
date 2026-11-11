#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作流模板库：新增工作流时**先选骨架，再填内容**。

## 为什么要模板

54 条工作流里，同一类问题的修法完全一样，但每次都要重新想一遍：

    文生图      要哪些参数？steps/cfg/sampler/scheduler 要不要暴露？
    图生视频    帧数怎么绑？时长判据怎么给？
    动作迁移    两个媒体输入怎么命名？哪个是驱动、哪个是被驱动？
    超分/放大   放大倍数怎么表达？尺寸期望要不要翻倍？

想漏一个，后果不是报错，是**这条工作流能跑但用不对**：出 1 秒的废片判 PASS、
用户改了帧数结果成片长度不变、第二个上传框不知道传什么。

模板把这些固化成**可执行的默认值**，`wfctl new` 直接拿来用。

## 模板给的是「骨架」，不是「成品」

每个模板里带 `REVIEW` 标记的字段必须在落盘前由人确认：

    REVIEW_MODEL   模型 key（模板里是占位符）
    REVIEW_SLOTS   模型槽位（从图推断，必须对一遍）
    REVIEW_PROMPT  正负提示词方向（反了出片必糊，不报错）

`wfctl new` 会把没填 REVIEW 的模板直接拒掉。

## 用法

    python3 -m tools.p20.wf_templates            # 列出全部模板
    python3 -m tools.p20.wf_templates t2i        # 看某个模板的完整内容
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

REVIEW_MODEL = "REVIEW_MODEL"
REVIEW_SLOTS = "REVIEW_SLOTS"
REVIEW_PROMPT = "REVIEW_PROMPT"

# 通用参数说明。措辞面向**终端用户**，不是开发者 ——
# 用户看不懂 "cfg" 这个词，但看得懂「提示词遵循强度」。
DESC = {
    "prompt": "正向提示词：描述你想要的画面内容（主体、外貌、衣着、背景、风格）。"
              "写得具体比写得多更重要。",
    "neg_prompt": "负向提示词：不想出现的东西（糊、水印、畸形手指、字幕等）。"
                  "留默认即可，不必改。",
    "seed": "随机种子。同一个种子 + 同一组参数能得到完全相同的结果；"
            "想复现某次出图就记下它。点「随机」换一个。",
    "steps": "采样步数。越多越细也越慢。轻量蒸馏模型 4–8 步就够，"
             "常规模型 20–30 步。",
    "cfg": "提示词遵循强度。越高越贴提示词也越容易过饱和；"
           "加速/蒸馏模型通常用 1。",
    "sampler": "采样算法。一般保持默认（euler / dpmpp_2m）。",
    "scheduler": "噪声调度。一般保持默认（simple / karras）。",
    "denoise": "重绘幅度。1.0 = 完全重画；0.2–0.4 = 保留原图结构做细节增强。",
    "width": "输出宽度（像素）。多数模型要求是 16 的倍数。",
    "height": "输出高度（像素）。多数模型要求是 16 的倍数。",
    "frames": "总帧数。时长 = 帧数 ÷ 帧率。改它成片长度会跟着变。",
    "image": "主输入图。这条工作流用它当身份/首帧/参考。",
    "image_2": "第二张输入图。首尾帧工作流里是**尾帧**；"
               "参考类工作流里是参考对象。",
    "video": "驱动视频。动作、节奏、镜头运动从这条素材继承。",
    "audio": "输入音频。音色/口型/节奏从这条素材继承。",
}

# 各类工作流的期望判据基线。
# 视频一律要 duration_s + min_bytes —— 2026-11-09 的教训：
# 只声明 min_bytes 时，出 1 秒的废片也会判 PASS。
EXPECT_VIDEO = {"duration_s": [1.0, 120.0], "min_bytes": 50000, "ext": "mp4"}
EXPECT_IMAGE = {"min_bytes": 20000, "ext": "png"}


@dataclass
class Template:
    key: str
    name: str
    kind: str
    when: str                      # 什么时候用这个模板
    params: dict                   # 参数骨架
    output: dict
    expect: dict
    slots_hint: str = ""           # 槽位从哪来
    tips: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"key": self.key, "name": self.name, "kind": self.kind,
                "when": self.when, "params": self.params,
                "output": self.output, "expect": self.expect,
                "slots_hint": self.slots_hint, "tips": self.tips}


def _p(**kw) -> dict:
    """快捷构造参数：_p(prompt=(type, required)), 值即 spec。"""
    return dict(kw)


# ---------------------------------------------------------------- 模板定义
TEMPLATES: dict[str, Template] = {}


def _reg(t: Template) -> None:
    TEMPLATES[t.key] = t


_reg(Template(
    key="t2i",
    name="文生图（text → image）",
    kind="image",
    when="只要提示词就能出图。最稳的一类，参数契约也最标准。",
    params={
        "prompt": {"type": "string", "required": True, "description": DESC["prompt"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "neg_prompt": {"type": "string", "required": False, "default": "",
                       "description": DESC["neg_prompt"],
                       "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "seed": {"type": "int", "required": False, "default": 0,
                 "min": 0, "max": 2147483647, "description": DESC["seed"],
                 "bindings": [{"node": REVIEW_SLOTS, "input": "seed"}]},
        "steps": {"type": "int", "required": False, "default": 20,
                  "min": 1, "max": 150, "description": DESC["steps"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "steps"}]},
        "cfg": {"type": "float", "required": False, "default": 4.0,
                "min": 0.0, "max": 30.0, "description": DESC["cfg"],
                "bindings": [{"node": REVIEW_SLOTS, "input": "cfg"}]},
        "sampler": {"type": "enum", "required": False, "description": DESC["sampler"],
                    "values": ["euler", "dpmpp_2m", "dpmpp_2m_sde", "uni_pc"],
                    "bindings": [{"node": REVIEW_SLOTS, "input": "sampler_name"}]},
        "scheduler": {"type": "enum", "required": False, "description": DESC["scheduler"],
                      "values": ["simple", "karras", "normal", "beta"],
                      "bindings": [{"node": REVIEW_SLOTS, "input": "scheduler"}]},
        "width": {"type": "int", "required": False, "default": 1024,
                  "min": 64, "max": 4096, "multiple": 16,
                  "description": DESC["width"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "width"}]},
        "height": {"type": "int", "required": False, "default": 1024,
                   "min": 64, "max": 4096, "multiple": 16,
                   "description": DESC["height"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "height"}]},
    },
    output={"node": REVIEW_SLOTS, "input": "filename_prefix", "kind": "image",
            "ext": "png", "pick": ["images"]},
    expect=dict(EXPECT_IMAGE),
    slots_hint="unet / clip / vae 三类角色，从 UNETLoader / CLIPLoader / VAELoader 推",
    tips=["neg_prompt 的默认值**不要**写成和 prompt 一样 —— 2026-11-11 两张卡这么错，"
          "出片必糊但作业 PASS",
          "seed 的 default 用 0，别抄图里的 {{SEED}} 占位符"],
))

_reg(Template(
    key="i2i",
    name="图生图 / 局部重绘（image + prompt → image）",
    kind="image",
    when="拿一张图当底，改风格/加细节/局部重绘。denoise < 1 才保留原结构。",
    params={
        "image": {"type": "image", "required": True, "description": DESC["image"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "image"}]},
        "prompt": {"type": "string", "required": True, "description": DESC["prompt"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "neg_prompt": {"type": "string", "required": False,
                       "default": "blurry, lowres, deformed, watermark",
                       "description": DESC["neg_prompt"],
                       "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "denoise": {"type": "float", "required": False, "default": 0.55,
                    "min": 0.0, "max": 1.0, "description": DESC["denoise"],
                    "bindings": [{"node": REVIEW_SLOTS, "input": "denoise"}]},
        "steps": {"type": "int", "required": False, "default": 25,
                  "min": 1, "max": 150, "description": DESC["steps"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "steps"}]},
        "cfg": {"type": "float", "required": False, "default": 6.0,
                "min": 0.0, "max": 30.0, "description": DESC["cfg"],
                "bindings": [{"node": REVIEW_SLOTS, "input": "cfg"}]},
        "seed": {"type": "int", "required": False, "default": 0,
                 "min": 0, "max": 2147483647, "description": DESC["seed"],
                 "bindings": [{"node": REVIEW_SLOTS, "input": "seed"}]},
    },
    output={"node": REVIEW_SLOTS, "input": "filename_prefix", "kind": "image",
            "ext": "png", "pick": ["images"]},
    expect=dict(EXPECT_IMAGE),
    slots_hint="ckpt（CheckpointLoaderSimple）或 unet+clip+vae 三件套",
    tips=["denoise 是这类工作流唯一真正重要的旋钮，务必暴露成参数",
          "输出尺寸继承输入图时，不要暴露 width/height —— 换了也和图对不上"],
))

_reg(Template(
    key="t2v",
    name="文生视频（text → video）",
    kind="video",
    when="只给提示词出片子。",
    params={
        "prompt": {"type": "string", "required": True, "description": DESC["prompt"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "neg_prompt": {"type": "string", "required": False, "default": "",
                       "description": DESC["neg_prompt"],
                       "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "seed": {"type": "int", "required": False, "default": 0,
                 "min": 0, "max": 2147483647, "description": DESC["seed"],
                 "bindings": [{"node": REVIEW_SLOTS, "input": "seed"}]},
        "frames": {"type": "int", "required": False, "default": 81,
                   "min": 1, "max": 1024, "description": DESC["frames"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "length"}]},
        "steps": {"type": "int", "required": False, "default": 20,
                  "min": 1, "max": 150, "description": DESC["steps"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "steps"}]},
        "cfg": {"type": "float", "required": False, "default": 1.0,
                "min": 0.0, "max": 30.0, "description": DESC["cfg"],
                "bindings": [{"node": REVIEW_SLOTS, "input": "cfg"}]},
        "width": {"type": "int", "required": False, "default": 832,
                  "min": 64, "max": 2048, "multiple": 16,
                  "description": DESC["width"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "width"}]},
        "height": {"type": "int", "required": False, "default": 480,
                   "min": 64, "max": 2048, "multiple": 16,
                   "description": DESC["height"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "height"}]},
    },
    output={"node": REVIEW_SLOTS, "input": "filename_prefix", "kind": "video",
            "ext": "mp4", "pick": ["videos", "gifs"]},
    expect=dict(EXPECT_VIDEO),
    slots_hint="unet/ckpt + clip + vae（含音频的模型另有 audio_vae / text_encoder）",
    tips=["frames 必须真的绑到决定 latent 长度的节点（EmptyXxxLatentVideo.length），"
          "绑到 ImageFromBatch.length 是**裁切**不是总帧数 —— 2026-11-11 踩过，"
          "改参数成片长度不变",
          "expect_defaults 必须有 duration_s，否则出 1 秒也判 PASS"],
))

_reg(Template(
    key="i2v",
    name="图生视频（image + text → video）",
    kind="video",
    when="拿一张图当首帧推运动。当前最常用的一类。",
    params={
        "image": {"type": "image", "required": True, "description": DESC["image"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "image"}]},
        "prompt": {"type": "string", "required": True, "description": DESC["prompt"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "neg_prompt": {"type": "string", "required": False, "default": "",
                       "description": DESC["neg_prompt"],
                       "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "seed": {"type": "int", "required": False, "default": 0,
                 "min": 0, "max": 2147483647, "description": DESC["seed"],
                 "bindings": [{"node": REVIEW_SLOTS, "input": "seed"}]},
        "frames": {"type": "int", "required": False, "default": 81,
                   "min": 1, "max": 1024, "description": DESC["frames"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "length"}]},
        "steps": {"type": "int", "required": False, "default": 20,
                  "min": 1, "max": 150, "description": DESC["steps"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "steps"}]},
        "cfg": {"type": "float", "required": False, "default": 1.0,
                "min": 0.0, "max": 30.0, "description": DESC["cfg"],
                "bindings": [{"node": REVIEW_SLOTS, "input": "cfg"}]},
    },
    output={"node": REVIEW_SLOTS, "input": "filename_prefix", "kind": "video",
            "ext": "mp4", "pick": ["videos", "gifs"]},
    expect=dict(EXPECT_VIDEO),
    slots_hint="unet/ckpt + clip + vae（LTX 系另有 audio_vae / text_encoder）",
    tips=["输入图绑到 LoadImage.image；LTX 系要过 LTXVImgToVideoInplace 才算真首帧",
          "带原生音频的模型（LTX 2.3）必须同时有 audio_vae 槽位，否则渲染报缺槽位"],
))

_reg(Template(
    key="flf2v",
    name="首尾帧生视频（image + image_2 + text → video）",
    kind="video",
    when="给首帧和尾帧，中间自己插值。",
    params={
        "image": {"type": "image", "required": True, "description": DESC["image"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "image"}]},
        "image_2": {"type": "image", "required": True, "description": DESC["image_2"],
                    "bindings": [{"node": REVIEW_SLOTS, "input": "image"}]},
        "prompt": {"type": "string", "required": True, "description": DESC["prompt"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "neg_prompt": {"type": "string", "required": False, "default": "",
                       "description": DESC["neg_prompt"],
                       "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "seed": {"type": "int", "required": False, "default": 0,
                 "min": 0, "max": 2147483647, "description": DESC["seed"],
                 "bindings": [{"node": REVIEW_SLOTS, "input": "seed"}]},
        "frames": {"type": "int", "required": False, "default": 81,
                   "min": 2, "max": 1024, "description": DESC["frames"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "length"}]},
    },
    output={"node": REVIEW_SLOTS, "input": "filename_prefix", "kind": "video",
            "ext": "mp4", "pick": ["videos", "gifs"]},
    expect=dict(EXPECT_VIDEO),
    slots_hint="同 i2v",
    tips=["两个图片字段的命名必须是 image / image_2 —— 前端按顺序渲染上传框，"
          "起名 image_start/image_end 会让用户不知道哪个是哪个",
          "**首尾帧的帧数由两张图决定时不要暴露 frames**：改了会让尾帧对不上",
          "ComfyMathExpression 的两个节点常被写成同一个表达式（都 a+b），"
          "2026-11-11 踩过：一个算 fps 一个算 length，改成 76+5 才对"],
))

_reg(Template(
    key="v2v",
    name="视频重绘 / 动作迁移（video + image + text → video）",
    kind="video",
    when="拿一条视频驱动动作，用一张图给身份，或整片重绘。",
    params={
        "video": {"type": "video", "required": True, "description": DESC["video"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "file"}]},
        "image": {"type": "image", "required": True, "description": DESC["image"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "image"}]},
        "prompt": {"type": "string", "required": True, "description": DESC["prompt"],
                   "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "neg_prompt": {"type": "string", "required": False, "default": "",
                       "description": DESC["neg_prompt"],
                       "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "length": {"type": "int", "required": False, "default": 81,
                   "min": 1, "max": 1024,
                   "description": "输出帧数。**要和驱动视频的长度对齐**，"
                                  "否则动作会在中途断掉或重复。",
                   "bindings": [{"node": REVIEW_SLOTS, "input": "length"}]},
    },
    output={"node": REVIEW_SLOTS, "input": "filename_prefix", "kind": "video",
            "ext": "mp4", "pick": ["videos", "gifs"]},
    expect=dict(EXPECT_VIDEO),
    slots_hint="unet + clip + vae + clip_vision（角色替换类还要 sam3 分割 ckpt + LoRA）",
    tips=["LoadVideo 的输入名是 `file`，不是 `video` —— 绑错就是渲不出",
          "输出 motion 继承自素材，静态素材出静态片是**合法结果**："
          "这类工作流的 freeze 判据要改 material_driven，否则好片被判 FAIL",
          "frames/length 在图里常被 ImageFromBatch 的裁切长度混淆，"
          "务必绑到决定输出的那个节点",
          "驱动视频要在 p20ctl new 时先 trim 到目标时长（trim_seconds），"
          "否则 128MB 上限和推理时长都会爆"],
))

_reg(Template(
    key="upscale",
    name="超分 / 放大（image → image，尺寸翻倍）",
    kind="image",
    when="把图放大或做细节还原。",
    params={
        "image": {"type": "image", "required": True, "description": DESC["image"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "image"}]},
        "seed": {"type": "int", "required": False, "default": 0,
                 "min": 0, "max": 2147483647, "description": DESC["seed"],
                 "bindings": [{"node": REVIEW_SLOTS, "input": "seed"}]},
    },
    output={"node": REVIEW_SLOTS, "input": "filename_prefix", "kind": "image",
            "ext": "png", "pick": ["images"]},
    expect={"min_bytes": 20000, "ext": "png"},
    slots_hint="upscale（UpscaleModelLoader.model_name）或 SeedVR2 的 DiT/VAE 两个槽位",
    tips=["放大倍数要写成 expect_scale：{width: 4, height: 4}，"
          "否则 verify 会按原图尺寸判 FAIL（2026-11-06 踩过）",
          "`LatentUpscaleModelLoader` 读的是 `latent_upscale_models/`，"
          "不是 `upscale_models/` —— 目录搞错时候选恒为空，报「缺权重」但 ls 看着是对的",
          "放大模型的权重不该暴露成用户参数，那是模型属性"],
))

_reg(Template(
    key="inpaint",
    name="擦除 / 局部修复（image + mask 或 image + video → image/video）",
    kind="image",
    when="抹掉画面里的东西，或补一块区域。",
    params={
        "image": {"type": "image", "required": True, "description": DESC["image"],
                  "bindings": [{"node": REVIEW_SLOTS, "input": "image"}]},
        "prompt": {"type": "string", "required": False, "default": "",
                   "description": "想让被擦除的区域变成什么。留空 = 纯擦除。",
                   "bindings": [{"node": REVIEW_SLOTS, "input": "text"}]},
        "denoise": {"type": "float", "required": False, "default": 0.9,
                    "min": 0.0, "max": 1.0, "description": DESC["denoise"],
                    "bindings": [{"node": REVIEW_SLOTS, "input": "denoise"}]},
        "seed": {"type": "int", "required": False, "default": 0,
                 "min": 0, "max": 2147483647, "description": DESC["seed"],
                 "bindings": [{"node": REVIEW_SLOTS, "input": "seed"}]},
    },
    output={"node": REVIEW_SLOTS, "input": "filename_prefix", "kind": "image",
            "ext": "png", "pick": ["images"]},
    expect=dict(EXPECT_IMAGE),
    slots_hint="ckpt 或 unet+clip+vae；掩码由 SAM3 / BBoxDetector 等分割节点给",
    tips=["mask 参数是可选的：多数「换背景」工作流自己在图里生成掩码，"
          "暴露 mask 会让用户以为必须传",
          "视频擦除类（VOID、VACE-inpainting）kind 仍是 video，"
          "expect 要用 EXPECT_VIDEO"],
))


# ---------------------------------------------------------------- 校验
def review_placeholders(obj) -> list[str]:
    """找出还没人确认的 REVIEW_* 占位符。

    `wfctl new` 会拒绝落盘 —— 模板给的是骨架，模型 key、槽位、正负方向
    这三样机器判不了，必须人看过。
    """
    todo: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            todo += review_placeholders(v)
    elif isinstance(obj, list):
        for v in obj:
            todo += review_placeholders(v)
    elif isinstance(obj, str) and obj.startswith("REVIEW_"):
        todo.append(obj)
    return todo


def get(key: str) -> Template | None:
    return TEMPLATES.get(key)


def pick_by_kind(kind: str, *, has_video_input: bool = False,
                 has_two_images: bool = False, has_image_input: bool = False) -> list[str]:
    """按图的输入形态推荐模板（给 wf_infer 的结果用来选骨架）。"""
    if kind == "video":
        if has_video_input:
            return ["v2v"]
        if has_two_images:
            return ["flf2v", "i2v"]
        if has_image_input:
            return ["i2v", "upscale"]
        return ["t2v"]
    if has_image_input:
        return ["i2i", "upscale", "inpaint"]
    return ["t2i"]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="wf_templates", description="工作流模板库")
    ap.add_argument("key", nargs="?", help="看某个模板的完整内容")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    if a.key:
        t = get(a.key)
        if not t:
            print(f"✗ 没有模板 {a.key!r}；可选：{', '.join(TEMPLATES)}", file=sys.stderr)
            return 2
        print(json.dumps(t.as_dict(), ensure_ascii=False, indent=1))
        return 0

    if a.json:
        print(json.dumps({k: v.as_dict() for k, v in TEMPLATES.items()},
                         ensure_ascii=False, indent=1))
        return 0

    print(f"{len(TEMPLATES)} 个工作流模板\n")
    for t in TEMPLATES.values():
        print(f"  {t.key:9s} {t.kind:6s} {t.name}")
        print(f"  {'':9s} 什么时候用：{t.when}")
        print(f"  {'':9s} 参数 {len(t.params)} 个 · 槽位：{t.slots_hint}")
        for tip in t.tips[:2]:
            print(f"  {'':9s} · {tip}")
        print()
    print("看单个模板：python3 -m tools.p20.wf_templates i2v")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
