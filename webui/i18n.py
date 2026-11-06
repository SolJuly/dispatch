# -*- coding: utf-8 -*-
"""控制台文案：参数名中文化 + 参数分组 + 状态标签。"""

PARAM_LABELS = {
    "prompt": "提示词",
    "neg_prompt": "负向提示词",
    "negative_prompt": "负向提示词",
    "seed": "随机种子",
    "steps": "采样步数",
    "cfg": "引导强度（CFG）",
    "sampler": "采样器",
    "scheduler": "调度器",
    "width": "宽度",
    "height": "高度",
    "frames": "帧数",
    "length": "帧数",
    "fps": "帧率",
    "duration": "时长（秒）",
    "batch_size": "批量张数",
    "count": "数量",
    "num_images": "张数",
    "image": "输入图片",
    "image_2": "参考图 2",
    "image_3": "参考图 3",
    "image_4": "参考图 4",
    "first_frame": "首帧",
    "last_frame": "尾帧",
    "ref_image": "参考图",
    "mask": "蒙版",
    "audio": "音频",
    "video": "驱动视频",
    "denoise": "重绘强度（denoise）",
    "strength": "强度",
    "scale": "放大倍数",
    "resolution": "放大后短边",
    "upscale_model": "放大模型",
    "color_correction": "色彩校正",
    "lora_strength": "LoRA 强度",
    "guidance": "引导系数",
    "negative": "负向条件",
    "positive": "正向条件",
    "prompt_2": "第二段提示词",
    "negative_prompt_2": "第二段负向提示词",
}

TYPE_LABELS = {
    "int": "整数",
    "float": "小数",
    "string": "文本",
    "bool": "开关",
    "enum": "选项",
    "image": "图片",
    "audio": "音频",
    "video": "视频",
}

# 参数分组：决定表单里块的出现顺序
GROUP_ORDER = ["输入", "提示词", "采样", "尺寸/时长", "其他"]
GROUP_OF = {
    "image": "输入", "image_2": "输入", "image_3": "输入", "image_4": "输入",
    "mask": "输入", "audio": "输入", "video": "输入",
    "first_frame": "输入", "last_frame": "输入", "ref_image": "输入",
    "prompt": "提示词", "neg_prompt": "提示词", "negative_prompt": "提示词",
    "prompt_2": "提示词", "negative_prompt_2": "提示词",
    "seed": "采样", "steps": "采样", "cfg": "采样", "sampler": "采样",
    "scheduler": "采样", "denoise": "采样", "strength": "采样",
    "guidance": "采样", "lora_strength": "采样",
    "width": "尺寸/时长", "height": "尺寸/时长", "frames": "尺寸/时长",
    "length": "尺寸/时长", "fps": "尺寸/时长", "duration": "尺寸/时长",
    "resolution": "尺寸/时长", "scale": "尺寸/时长",
}

STATUS_TEXT = {
    "verified": ("可使用", "ok"),
    "equivalent": ("可使用", "ok"),
    "experimental": ("尚未开发", "warn"),
    "incompatible": ("不可用", "bad"),
}


def param_label(name: str) -> str:
    return PARAM_LABELS.get(name, name)


def type_label(t: str | None) -> str:
    return TYPE_LABELS.get(t or "", t or "")


def group_of(name: str) -> str:
    return GROUP_OF.get(name, "其他")


def status_text(status: str | None) -> tuple[str, str]:
    return STATUS_TEXT.get(status or "", ("未知", "idle"))


def ordered_params(params: dict) -> list[tuple[str, dict]]:
    """按分组排序：输入 → 提示词 → 采样 → 尺寸 → 其他；组内保持原顺序。"""
    buckets: dict[str, list[tuple[str, dict]]] = {g: [] for g in GROUP_ORDER}
    for name, spec in (params or {}).items():
        buckets.setdefault(group_of(name), []).append((name, spec))
    out: list[tuple[str, dict]] = []
    for g in GROUP_ORDER:
        out.extend(buckets.get(g, []))
    return out
