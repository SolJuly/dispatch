#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尺寸推导：把「短边档位 + 宽高比」算成具体的 width/height。

## 为什么需要（2026-11-16）

H3 两张卡的 `width` / `height` 是两个各自独立的字面量，默认 768×512。
两个问题：

### ① 描述和现实不符

`height` 的说明写着「与宽度配套调整，保持原始素材的宽高比」，
但它并不联动 —— 改 width，height 纹丝不动，出来的画面直接被拉变形。

### ② 默认值只有官方 38% 的像素

H3 是 768p 模型，官方 `MiniMaxH3ImageToVideo` 节点默认 `1344×768`
（**短边 768**）。我们默认 768×512，短边只有 512：

        1344 × 768 = 1032192     官方
         768 × 512 =  393216     我们 = 官方的 38%

短边是决定清晰度的那个量。「画质糊」有一部分就是这里欠的账。

## 解法：引入两个虚拟参数

    resolution   短边像素（512 草稿 / 640 / 768 标准 / 1024 高清）
    aspect       宽高比（source 跟随素材 / 9:16 / 16:9 / 1:1）

`width`/`height` 由这两个量算出来，同时**保留手动覆盖**：
用户显式给了 width 或 height，就听用户的，不推导。

## 优先级（明确可预测）

    用户显式给 width/height  >  resolution+aspect 推导  >  卡片默认值

判据是 `registry.resolve()` 返回的 `defaults_applied`：出现在里面说明
那个值是**填默认**来的，不是用户填的，可以安全覆盖。
"""
from __future__ import annotations

import struct
from pathlib import Path

# 常见宽高比字面量。source 是特殊值：从素材实际尺寸推。
ASPECTS: dict[str, float | None] = {
    "source": None,
    "9:16": 9 / 16,
    "16:9": 16 / 9,
    "1:1": 1.0,
    "3:4": 3 / 4,
    "4:3": 4 / 3,
}


def probe_size(path: Path) -> tuple[int, int] | None:
    """读图片宽高。纯标准库，不调 ffprobe（渲染路径上要纯函数、要快）。

    支持 PNG / JPEG / WebP —— 素材库里的全部格式。
    读不出来返回 None（调用方回落到声明的 aspect）。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if len(head) < 12:
                return None
            # PNG: 8 字节签名 + IHDR(长度4 类型4) + width(4) height(4)
            if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            # WebP
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                return _probe_webp(f)
            # JPEG: 扫 SOFn 段
            if head[:2] == b"\xff\xd8":
                return _probe_jpeg(f)
    except (OSError, struct.error, ValueError):
        return None
    return None


def _probe_webp(f) -> tuple[int, int] | None:
    f.seek(12)
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            return None
        fourcc = hdr[:4]
        size = struct.unpack("<I", hdr[4:])[0]
        if fourcc == b"VP8X":
            data = f.read(10)
            if len(data) < 10:
                return None
            w = 1 + (data[4] | (data[5] << 8) | (data[6] << 16))
            h = 1 + (data[7] | (data[8] << 8) | (data[9] << 16))
            return int(w), int(h)
        if fourcc == b"VP8 ":
            data = f.read(10)
            if len(data) < 10:
                return None
            w, h = struct.unpack("<HH", data[6:10])
            return int(w) & 0x3FFF, int(h) & 0x3FFF
        if fourcc == b"VP8L":
            data = f.read(5)
            if len(data) < 5:
                return None
            b = data[1:5]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return int(w), int(h)
        f.seek(size + (size & 1), 1)


def _probe_jpeg(f) -> tuple[int, int] | None:
    f.seek(2)
    while True:
        b = f.read(1)
        while b and b != b"\xff":
            b = f.read(1)
        if not b:
            return None
        marker = f.read(1)
        while marker == b"\xff":
            marker = f.read(1)
        if not marker:
            return None
        m = marker[0]
        if m in (0xD8, 0xD9) or 0xD0 <= m <= 0xD7:
            continue
        ln_raw = f.read(2)
        if len(ln_raw) < 2:
            return None
        seglen = struct.unpack(">H", ln_raw)[0]
        # SOF0-SOF15，排除 DHT(0xC4)/JPG(0xC8)/DAC(0xCC)
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            data = f.read(5)
            if len(data) < 5:
                return None
            h, w = struct.unpack(">HH", data[1:5])
            return int(w), int(h)
        f.seek(seglen - 2, 1)


def fit(short_side: int, aspect: float, multiple: int = 16,
        lo: int = 256, hi: int = 1280) -> tuple[int, int]:
    """按「短边 = short_side、宽高比 = aspect」算出对齐后的 (w, h)。

    短边是决定清晰度的那个量（H3 官方就是按短边 768 设计的），
    所以让它当锚点，长边由比例推。两个边都向下取整到 multiple 的倍数
    （模型要求 16 / 32 对齐），因此实际短边可能略小于 short_side ——
    这个方向是安全的（宁可少几个像素，也不要超出显存预算）。
    """
    if aspect >= 1:            # 横向或方形
        h = short_side
        w = round(short_side * aspect)
    else:                      # 纵向
        w = short_side
        h = round(short_side / aspect)
    w = max(multiple, (w // multiple) * multiple)
    h = max(multiple, (h // multiple) * multiple)
    w = max(lo, min(hi, w))
    h = max(lo, min(hi, h))
    return int(w), int(h)


def apply(wf: dict, params: dict, *, provided: dict | None = None,
          defaults_applied: list[str] | None = None,
          inputs_dir: Path | None = None) -> dict:
    """按卡里的 resolution / aspect 推出 width / height，写进 params。

    什么时候不动手：
      · 卡里没有 resolution 参数 → 这个工作流不参与推导
      · 用户**显式**给了 width 和 height → 完全听用户的

    只给了一个（比如只填 width）时：另一个由比例推 —— 这正是
    「height 跟随 width」要的效果。
    """
    schema = wf.get("params") or {}
    if "resolution" not in schema:
        return dict(params)

    out = dict(params)
    provided = provided or {}
    applied = set(defaults_applied or [])

    # 用户两个都显式给了：不推导，原样返回
    if "width" in provided and "height" in provided:
        return out

    multiple = int((schema.get("width") or {}).get("multiple") or 16)
    lo = int((schema.get("width") or {}).get("min") or 256)
    hi = int((schema.get("width") or {}).get("max") or 1280)

    short = out.get("resolution")
    try:
        short = int(short)
    except (TypeError, ValueError):
        short = int((schema.get("resolution") or {}).get("default") or 768)

    # ---- 决定宽高比 ----
    ratio: float
    aspect = str(out.get("aspect") or (schema.get("aspect") or {}).get("default") or "source")
    src = _source_size(schema, out, provided, inputs_dir)
    if aspect == "source" and src:
        w0, h0 = src
        if w0 and h0:
            # 用户只给了一个边时，以那个边为准反推另一个
            if "width" in provided and "height" not in provided:
                out["height"] = max(multiple, (round(int(provided["width"]) * h0 / w0) // multiple) * multiple)
                out.setdefault("width", int(provided["width"]))
                return out
            if "height" in provided and "width" not in provided:
                out["width"] = max(multiple, (round(int(provided["height"]) * w0 / h0) // multiple) * multiple)
                out.setdefault("height", int(provided["height"]))
                return out
            ratio = w0 / h0
        else:
            ratio = 16 / 9
    else:
        ratio = ASPECTS.get(aspect) or (16 / 9)

    w, h = fit(short, ratio, multiple=multiple, lo=lo, hi=hi)
    # 覆盖条件：用户没显式给，且（值缺失 或 值来自卡片默认）。
    #
    # 「值缺失」这个分支是 2026-11-16 补的：H3 两张卡已经不声明
    # width/height 的 default 了（否则表单会显示一个静态的 768×512，
    # 而实际渲染是别的尺寸 —— 用户看到的和出来的对不上）。
    # 没默认值 = 必须推，否则这两个键根本不会出现在 params 里。
    if "width" not in provided and ("width" in applied or "width" not in out):
        out["width"] = w
    if "height" not in provided and ("height" in applied or "height" not in out):
        out["height"] = h
    return out


def _source_size(schema: dict, params: dict, provided: dict,
                 inputs_dir: Path | None) -> tuple[int, int] | None:
    """找到这张卡引用的素材图，读它的真实尺寸。

    参数名不固定：H3 i2v 叫 image，其它卡叫 image_2 / mask / video…
    所以按「声明为 type=image」找，而不是猜名字。
    """
    if inputs_dir is None:
        return None
    for name, spec in schema.items():
        if not isinstance(spec, dict) or spec.get("type") != "image":
            continue
        val = params.get(name)
        if not isinstance(val, str) or not val:
            continue
        p = Path(val)
        if not p.is_absolute():
            p = Path(inputs_dir) / val
        if p.is_file():
            got = probe_size(p)
            if got:
                return got
    return None
