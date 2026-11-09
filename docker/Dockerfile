# syntax=docker/dockerfile:1.6
# P2.1 运行时镜像（从实机清单重建，不含模型权重）
#
# 目标：把 p2-comfy-media-v3 的“运行时”部分（ComfyUI + 10 个自定义节点 + pip 依赖）
# 变成可重建、可推到任意 OCI registry 的镜像；权重仍由运行时挂载/下载。
#
# 与实机 v3 的对应关系：
#   - ComfyUI 3c80da7f（2026-09-18）
#   - 10 个 active custom_nodes（commit 见 install-nodes.sh）
#   - torch 2.8.0+cu128 / Python 3.10（与 v4 实机一致；v3 的 2.10+cu130 已废弃）
#   - 不含 xformers（与 torch 2.8 ABI 不匹配，已移除）
#   - 不含任何权重（实机用 /model 只读挂载 + 软链）

ARG BASE=python:3.10-slim-bookworm
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 运行时依赖：ffmpeg（视频节点）、libGL（opencv）、编译工具（insightface 等）
RUN apt-get update && apt-get install -y --no-install-recommends \
        git wget aria2 ffmpeg libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
        libgomp1 build-essential cmake \
    && rm -rf /var/lib/apt/lists/*

COPY constraints.txt /tmp/constraints.txt

# torch 走 cu128 wheel；其余包由 constraints 锁版本
RUN pip install --upgrade pip \
 && pip install --extra-index-url https://download.pytorch.org/whl/cu128 \
        -c /tmp/constraints.txt \
        torch==2.8.0+cu128 torchaudio==2.8.0+cu128

ARG COMFY_COMMIT=3c80da7f87ee359b2d06f107cb3c0797079dfbbb
RUN git clone https://github.com/comfyanonymous/ComfyUI /root/ComfyUI \
 && cd /root/ComfyUI \
 && git checkout ${COMFY_COMMIT} \
 && pip install --extra-index-url https://download.pytorch.org/whl/cu128 \
        -r requirements.txt -c /tmp/constraints.txt

COPY install-nodes.sh /tmp/install-nodes.sh
RUN bash /tmp/install-nodes.sh

# xformers 与 torch 2.8 的 ABI 不匹配，会让 SeedVR2/SUPIR import 失败
RUN pip uninstall -y xformers || true

COPY start-comfyui.sh /usr/local/bin/start-comfyui.sh
RUN chmod +x /usr/local/bin/start-comfyui.sh

EXPOSE 8188
CMD ["/usr/local/bin/start-comfyui.sh"]
