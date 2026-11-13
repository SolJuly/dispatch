#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Provider 注册表 + 工厂：按名字取适配器，按能力查可用性。

这是 L5「多云」的入口。上层（run / scheduler / pool / cost_snapshot）只调
`get_provider()`，不再 import `compshare.cs`。

**BYOK 设计**（2026-11-09 复核后的商业模型）：

    客户 ──自己的智云账号──> 智云（算力合同在客户和智云之间）
      └──用我们的软件──> 我们（只收调度服务费）

代码上只需要让 provider 接受一份外部凭证，而不是全局读 credentials.env。
`get_provider("compshare", credentials={...})` 就是这个口子 ——
`CS(cfg)` 本来就支持传 cfg，不需要改 cs.py 的 34 个调用点。
"""
from __future__ import annotations

import os
from typing import Any

from .base import (  # noqa: F401  (re-export 给上层统一 import)
    Capabilities, InstanceView, Provider, ProviderError, SSHEndpoint, classify,
)

_BUILTIN = ("compshare", "ppio", "local", "fake")


# ------------------------------------------------------------------ 工厂
def get_provider(name: str | None = None, *,
                 credentials: dict[str, str] | None = None,
                 **kwargs: Any) -> Provider:
    """按名字取一个 provider 实例。

    参数：
      name          compshare / ppio / local / fake；None → 读 env DSH_PROVIDER → 默认
      credentials   BYOK：客户自己的凭证
                    compshare → {CS_PUBLIC_KEY, CS_PRIVATE_KEY}
                    ppio      → {api_key}（也认 PPIO_API_KEY）
                    其它 provider 忽略
      kwargs        透传给具体适配器（如 local 的 host/port/password）

    刻意**不做实例缓存**：provider 持有连接与价格缓存，跨作业复用会让
    价格表过期（智云调价后仍按旧价估价 = 算错钱）。要复用就由调用方持有。
    """
    name = (name or os.environ.get("DSH_PROVIDER") or _default_name()).strip().lower()

    if name == "compshare":
        from .compshare import CompShareProvider
        return CompShareProvider(credentials) if credentials else CompShareProvider(**kwargs)
    if name == "ppio":
        from .ppio import PPIOProvider
        return PPIOProvider(credentials, **kwargs)
    if name == "local":
        from .local import LocalProvider
        return LocalProvider(**{**({"password": credentials.get("password")}
                                   if credentials else {}), **kwargs})
    if name == "fake":
        from .local import FakeProvider
        return FakeProvider(**kwargs)
    raise ProviderError(
        f"未知 provider {name!r}；可用：{', '.join(available())}",
        ProviderError.UNSUPPORTED)


def _default_name() -> str:
    """没有显式指定时的默认 provider。

    有智云凭证 → compshare；否则 → local（避免在没有凭证的机器上，
    连"查一下余额"这种只读操作都抛异常）。
    """
    try:
        from compshare.cs import load_config
        load_config()
        return "compshare"
    except Exception:
        return "local"


def available() -> list[str]:
    """当前环境**真的能用**的 provider 列表（构造成功 **且** `ready()` 不抛）。

    「能用」的判据从「构造不抛」升级成「ready() 不抛」，因为两者是不同的问题：
    能力声明不该被凭证挡住（矩阵要展示 PPIO 能做什么），但巡检工具不该去查一个
    必然撞 401 的云。没凭证的 ppio 现在会出现在 `capability_matrix()` 里，
    但不会出现在 `available()` 里 —— 这是对的。

    `cost_snapshot` 之类的巡检工具用它决定该查谁。
    """
    out: list[str] = []
    for n in _BUILTIN:
        try:
            get_provider(n).ready()
        except Exception:
            continue
        out.append(n)
    return out


# ------------------------------------------------------------------ 能力矩阵
def capability_matrix() -> list[dict]:
    """所有**内置** provider 的能力对照，用于跨云选型与文档。

    刻意遍历 `_BUILTIN` 而不是 `available()`：能力声明是静态事实，跟本机有没有
    凭证无关。用 available() 会让「这台机器没配 PPIO」变成「文档里没有 PPIO」，
    而选型最需要的恰恰是这一行。

    返回的是**声明值**，不是实测值。声明错了要改 `capabilities()`，
    不要在这里打补丁。
    """
    rows = []
    for n in _BUILTIN:
        try:
            p = get_provider(n)          # 只构造一次：构造可能读文件/建客户端
            row = p.capabilities().as_dict()
        except Exception as e:
            rows.append({"provider": n, "error": f"{type(e).__name__}: {e}"})
            continue
        # 声明之外再标一个「本机能不能调」——否则矩阵里分不出
        # 「这家支持」和「这家在这台机器上配好了」。
        try:
            p.ready()
            row["ready"] = True
        except Exception:
            row["ready"] = False
        rows.append(row)
    return rows


def describe(name: str | None = None) -> str:
    """给人看的单行能力摘要（CLI 用）。"""
    p = get_provider(name)
    c = p.capabilities()
    flags = [
        ("停机", "✓" if c.can_stop_instance else "—"),
        ("外网", {True: "✓", False: "✗", None: "?"}[c.instance_has_internet]),
        ("OCI", "✓" if c.can_pull_oci else "✗"),
        ("共享权重", "✓" if c.has_shared_weight_mount else "✗"),
        ("定时关机", "✓" if c.supports_scheduled_stop else "✗"),
        ("竞价", "✓" if c.supports_spot else "✗"),
        ("自定义镜像", "✓" if c.supports_custom_image else "✗"),
    ]
    return f"{c.name:12s} " + "  ".join(f"{k}={v}" for k, v in flags)
