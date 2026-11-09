#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L5 多云层：Provider Adapter + 资产缓存 + 跨云一致性。

分层：

    providers/   Provider 契约与各云适配器（compshare / local / fake）
    assets.py    权重资产的「在哪、有没有、校验过没有」，跨云命中与淘汰
    conformance.py 环境描述的跨云一致性校验（manifest ↔ 实际机器）

上层（p1/p2/webui）只 import 这一层，不直接碰任何云的 SDK。
"""
from __future__ import annotations

from .providers import (  # noqa: F401
    Capabilities, InstanceView, Provider, ProviderError, SSHEndpoint,
    available, capability_matrix, classify, describe, get_provider,
)
