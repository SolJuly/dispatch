#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Provider Adapter 包：多云算力能力的统一入口。

    base.py        契约（Provider / InstanceView / Capabilities / ProviderError）
    compshare.py   智云适配器（薄封装 compshare.cs）
    local.py       本地机 / 自建机 + FakeProvider（测试）
    registry.py    工厂 get_provider() / 能力矩阵
"""
from __future__ import annotations

from .base import (  # noqa: F401
    Capabilities, InstanceView, Provider, ProviderError, SSHEndpoint, classify,
)
from .registry import (  # noqa: F401
    available, capability_matrix, describe, get_provider,
)
