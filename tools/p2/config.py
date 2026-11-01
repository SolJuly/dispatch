#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 集中配置：镜像分层、卡型、路径约定。

M1 两层镜像（D2）：
  L0 平台基础镜像（ComfyUI 0.36 纯净版）
  L1 p2-comfy-base-v1  环境固化 + rsync，不含 ffmpeg
  L2 p2-comfy-media-v1 在 base 上叠加 ffmpeg

选层：video job → media；image job → base。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

BASE_IMAGE_KEYWORD = "ComfyUI基础镜像纯净版0.36.0"
LAYER_NAMES = {
    "base": "p2-comfy-base-v1",
    "media": "p2-comfy-media-v1",
}
DEFAULT_GPU = "4090"
DEFAULT_ZONE = "cn-wlcb-01"

IMAGE_JSON = ROOT / "reports" / "p2" / "image.json"
BENCH_JSON = ROOT / "reports" / "p2" / "bench-boot.json"

# 体积预算（GB）
SIZE_BUDGET_GB = {"base": 32.0, "media": 33.0}


def layer_for_kind(kind: str) -> str:
    return "media" if kind == "video" else "base"
