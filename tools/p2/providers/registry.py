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

# zhixingyun 于 2026-11-13 暂停接入（成本不划算，见 reports/L5-智星云*）。
# 适配器与测试全部保留，但**不放进默认可用列表** —— available() 会把它
# 当成可查对象，cost_snapshot 之类的巡检就会去打真实云。
# 恢复方式：把下面这行改回 ("compshare", "zhixingyun", "local", "fake")
# 并设 ZXY_ENABLE=1（适配器层还有一道硬拦截）。
_BUILTIN = ("compshare", "local", "fake")
_SUSPENDED = ("zhixingyun",)


# ------------------------------------------------------------------ 工厂
def get_provider(name: str | None = None, *,
                 credentials: dict[str, str] | None = None,
                 **kwargs: Any) -> Provider:
    """按名字取一个 provider 实例。

    参数：
      name          compshare / zhixingyun / local / fake；None → 读 env DSH_PROVIDER
                    → 有智云凭证则 compshare → 否则 local
      credentials   BYOK：客户自己的云凭证，只对这一家有意义：
                      compshare   {CS_PUBLIC_KEY, CS_PRIVATE_KEY}
                      zhixingyun  {ZXY_API_KEY, ZXY_API_SECRET}（也接受 apikey/apisecret）
      kwargs        透传给具体适配器（如 local 的 host/port/password）

    刻意**不做实例缓存**：provider 持有连接与价格缓存，跨作业复用会让
    价格表过期（智云调价后仍按旧价估价 = 算错钱）。要复用就由调用方持有。
    """
    name = (name or os.environ.get("DSH_PROVIDER") or _default_name()).strip().lower()

    # 搁置的 provider 必须在任何分支**之前**拦下，否则下面的具体分支先命中，
    # 这个检查永远走不到（刚才就这么错过一次）。
    if name in _SUSPENDED:
        from .zhixingyun import DISABLED_MSG
        raise ProviderError(DISABLED_MSG, ProviderError.UNSUPPORTED)

    if name == "compshare":
        from .compshare import CompShareProvider
        return CompShareProvider(credentials) if credentials else CompShareProvider(**kwargs)
    if name == "zhixingyun":
        from .zhixingyun import ZhiXingYunProvider
        # 传了 credentials 就走 BYOK；没传则适配器自己读 env/.zxy/credentials.env。
        # 注意空 dict 不等价于 BYOK：那会让它以为「用户故意给了空凭证」。
        return ZhiXingYunProvider(credentials or None, **kwargs)
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
    """当前环境**真的能用**的 provider 列表。

    「能用」= 构造不抛异常。cost_snapshot 之类的巡检工具用它决定该查谁。
    """
    out: list[str] = []
    for n in _BUILTIN:
        try:
            get_provider(n)
        except Exception:
            continue
        out.append(n)
    return out


# ------------------------------------------------------------------ 能力矩阵
def capability_matrix() -> list[dict]:
    """所有可用 provider 的能力对照，用于跨云选型与文档。

    返回的是**声明值**，不是实测值。声明错了要改 `capabilities()`，
    不要在这里打补丁。
    """
    rows = []
    for n in available():
        try:
            cap = get_provider(n).capabilities()
        except Exception as e:
            rows.append({"provider": n, "error": f"{type(e).__name__}: {e}"})
            continue
        rows.append(cap.as_dict())
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
