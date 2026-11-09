#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Provider Adapter 基类：把「一家云的算力租借能力」收敛成一份契约。

为什么需要这一层（2026-11-09 L5 补完）：

  改造前 `compshare/cs.py` 是唯一实现，34 处 `CS()` 直接调用散落在
  run / scheduler / pool / cost_snapshot / riskmon / checkpoint 里。
  这套代码在智云上能跑，但换任何一家云都要改 34 个文件 —— 跨云等于重写。

  这一层只做三件事：
    1. **定义契约**：建机 / 查询 / 起停 / 销毁 / 计费 / 镜像 / 库存，
       全部用与云无关的字段名（capacity / price_per_hour / ssh_endpoint …）。
    2. **声明能力**：每家云的支持面不同（智云不能拉 OCI、没有外网、
       停机实例免费；RunPod 停机磁盘要钱），用 `capabilities()` 显式列出，
       让上层可以在**下决定之前**就知道某条路走不通，而不是跑到一半报错。
    3. **统一异常**：`ProviderError` 带 `kind`，上层按语义分流重试
       （out-of-stock 重试 / quota 停止 / transient 退避）。

刻意的取舍：

  - **不做自动降级**。选哪家云是显式决策（CLI `--provider` 或 manifest），
    不是「这家挂了偷偷换一家」—— 那会让成本账算不清楚。
  - **不隐藏价格口径**。`price()` 返回的是（算力, 系统盘, 数据盘）三元组，
    不做任何折算，因为各家的「免费额度」完全不同（智云系统盘 ≤100GB 免费，
    RunPod 停机照收 $0.20/GB/月），折算会把这种差异抹平，从而算错钱。
  - **不假装所有云都一样**。做不到的能力就 `NotSupported`，绝不返回空结果
    冒充「查询成功但没数据」—— 那是前一轮审计里最贵的错误类型。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ------------------------------------------------------------------ 异常
class ProviderError(RuntimeError):
    """Provider 统一异常。上层按 `kind` 分流，不按字符串匹配。"""

    # kind 取值（故意做成闭集合，避免各适配器自造词）
    OUT_OF_STOCK = "out_of_stock"     # 没货：可重试/换卡
    QUOTA = "quota"                   # 配额满：停止，不要重试
    TRANSIENT = "transient"           # 网络/超时：退避重试
    NOT_FOUND = "not_found"           # 实例/镜像不存在：终态
    AUTH = "auth"                     # 凭证无效：终态，要人介入
    UNSUPPORTED = "unsupported"       # 该云不支持此能力
    BILLING = "billing"               # 余额不足
    BUSY = "busy"                     # 实例在过渡态（如销毁中）：短退避
    UNKNOWN = "unknown"

    def __init__(self, message: str, kind: str = UNKNOWN, *, retryable: bool | None = None):
        super().__init__(message)
        self.kind = kind
        # 没显式给就按 kind 推导，避免调用方各写一套。
        # 注意不能写裸名 OUT_OF_STOCK —— 类体内它们是局部名、不可见，
        # 只有 `类名.属性` 或模块级才能取到（Python 类作用域不会自动查外层）。
        if retryable is None:
            retryable = kind in (ProviderError.OUT_OF_STOCK, ProviderError.TRANSIENT)
        self.retryable = bool(retryable)

    def __str__(self) -> str:  # pragma: no cover - 只影响日志可读性
        return f"[{self.kind}] {super().__str__()}"


def classify(message: str) -> tuple[str, bool]:
    """把各家的中文/英文错误文案归到统一的 kind。

    单独抽出来是为了可测：错误分类写错的代价是「该重试的不重试」或
    「不该重试的疯狂重试烧钱」，这是线上最贵的两类 bug。
    """
    m = (message or "").lower()
    # 智云 226604 = 库存不足；各家自己也有类似文案
    if any(k in m for k in ("226604", "no stock", "out of stock", "sold out",
                            "insufficient capacity", "无现货", "库存不足")):
        return ProviderError.OUT_OF_STOCK, True
    # 智云 8903 = 实例在任务中（销毁/启动过渡态）
    if any(k in m for k in ("8903", "in the task", "transitional state",
                            "operation in progress", "正在处理")):
        return ProviderError.BUSY, True
    if any(k in m for k in ("quota", "limit exceeded", "too many instances",
                            "配额", "超出限制")):
        return ProviderError.QUOTA, False
    if any(k in m for k in ("not found", "no such", "不存在", "找不到")):
        return ProviderError.NOT_FOUND, False
    if any(k in m for k in ("signature", "publickey", "auth", "forbidden",
                            "unauthorized", "401", "403", "鉴权", "签名")):
        return ProviderError.AUTH, False
    if any(k in m for k in ("balance", "insufficient funds", "余额", "欠费")):
        return ProviderError.BILLING, False
    if any(k in m for k in ("timeout", "timed out", "connection", "temporarily",
                            "try again", "超时", "网络")):
        return ProviderError.TRANSIENT, True
    return ProviderError.UNKNOWN, False


# ------------------------------------------------------------------ 能力声明
@dataclass(frozen=True)
class Capabilities:
    """一家云「能做什么」的显式声明。

    上层在决定用哪条路径之前先查这里，不要靠 try/except 试探。
    """

    name: str
    # 实例是否可停机保状态（停机后不再收算力费）
    can_stop_instance: bool = True
    # 停机后是否仍收磁盘/镜像费（None = 未知，必须实测）
    bills_disk_when_stopped: bool | None = None
    # 实例内是否有外网出口（无外网 → provision.sh / pip / docker pull 全部不可用）
    instance_has_internet: bool | None = None
    # 能否用 OCI 镜像直接起容器（False → 只能用平台预置镜像 + 快照）
    can_pull_oci: bool = False
    # 能否挂载外部只读权重库（智云 /model UPFS）
    has_shared_weight_mount: bool = False
    # 是否支持定时关机（防孤儿实例的最后一道闸）
    supports_scheduled_stop: bool = True
    # 是否支持竞价/抢占实例
    supports_spot: bool = False
    # 是否支持保存自定义镜像（快照）
    supports_custom_image: bool = False
    # 系统盘免费额度（GB，0 = 无免费额度）
    free_system_disk_gb: int = 0
    # 开机到 sshd 可用的典型秒数（估价与超时用；None = 未知）
    typical_boot_s: int | None = None
    notes: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "can_stop_instance": self.can_stop_instance,
            "bills_disk_when_stopped": self.bills_disk_when_stopped,
            "instance_has_internet": self.instance_has_internet,
            "can_pull_oci": self.can_pull_oci,
            "has_shared_weight_mount": self.has_shared_weight_mount,
            "supports_scheduled_stop": self.supports_scheduled_stop,
            "supports_spot": self.supports_spot,
            "supports_custom_image": self.supports_custom_image,
            "free_system_disk_gb": self.free_system_disk_gb,
            "typical_boot_s": self.typical_boot_s,
            "notes": dict(self.notes),
        }


# ------------------------------------------------------------------ 与云无关的视图
@dataclass
class InstanceView:
    """一台实例的归一化视图。字段名与云无关，上层只认这个。"""

    uid: str
    state: str                       # running / stopped / starting / stopping / terminated / unknown
    name: str = ""
    gpu_type: str = ""
    gpu_count: int = 0
    cpu: int = 0
    memory_gb: int = 0
    price_per_hour: float = 0.0      # 算力部分（不含磁盘）
    disk_per_hour: float = 0.0       # 磁盘部分
    region: str = ""
    zone: str = ""
    provider: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_per_hour(self) -> float:
        return round(self.price_per_hour + self.disk_per_hour, 4)

    @property
    def billing(self) -> bool:
        """是否正在计费（只有 running/rebooting 计费）。"""
        return self.state in ("running", "rebooting")

    def as_dict(self) -> dict:
        return {
            "uid": self.uid, "state": self.state, "name": self.name,
            "gpu_type": self.gpu_type, "gpu_count": self.gpu_count,
            "cpu": self.cpu, "memory_gb": self.memory_gb,
            "price_per_hour": self.price_per_hour,
            "disk_per_hour": self.disk_per_hour,
            "total_per_hour": self.total_per_hour,
            "billing": self.billing,
            "region": self.region, "zone": self.zone, "provider": self.provider,
        }


@dataclass
class SSHEndpoint:
    host: str
    port: str = "22"
    password: str | None = None
    user: str = "root"


# ------------------------------------------------------------------ 契约
class Provider:
    """所有云的基类。子类必须实现抽象方法；做不到的抛 ProviderError(UNSUPPORTED)。

    上层只调用这里列出的方法。新增能力时先加到这里，再让各家实现 ——
    不要在上层写 `if provider == "compshare"` 这种分支，那就是把这层白做了。
    """

    name = "base"

    # ---- 元信息
    def capabilities(self) -> Capabilities:
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Provider {self.name}>"

    # ---- 实例生命周期
    def create(self, *, image: str, gpu_type: str, gpu: int = 1, cpu: int = 16,
               memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
               name: str = "job", charge_type: str = "Postpay",
               zone: str | None = None, password: str | None = None,
               remark: str = "") -> str:
        """建机，返回云侧实例 ID。失败抛 ProviderError（带 kind）。"""
        raise NotImplementedError

    def get(self, uid: str) -> InstanceView:
        raise NotImplementedError

    def list_instances(self, prefix: str = "") -> list[InstanceView]:
        raise NotImplementedError

    def start(self, uid: str) -> None:
        raise NotImplementedError

    def stop(self, uid: str) -> None:
        raise NotImplementedError

    def terminate(self, uid: str) -> None:
        raise NotImplementedError

    def wait_state(self, uid: str, states: set[str], timeout: int = 900,
                   interval: float = 5.0) -> InstanceView:
        """等实例进入 states 之一。超时抛 ProviderError(TRANSIENT)。"""
        import time
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            last = self.get(uid)
            if last.state in states:
                return last
            time.sleep(interval)
        raise ProviderError(
            f"{uid} 等 {sorted(states)} 超时 {timeout}s（当前 {last.state if last else '?'}）",
            ProviderError.TRANSIENT)

    def ssh_endpoint(self, uid: str) -> SSHEndpoint:
        raise NotImplementedError

    def ssh_ready(self, uid: str, *, timeout: int = 180) -> SSHEndpoint:
        """等 sshd 真的能收命令，返回可用端点。

        默认实现：拿端点 + 探活。`Running` 不等于 sshd 就绪（智云实测还要
        20-40s），不探活直接 push 必然失败 —— 这是历史上最反复的坑。
        """
        raise NotImplementedError

    def scheduled_stop(self, uid: str, at_epoch: int) -> None:
        """设定时关机。不支持就抛 ProviderError(UNSUPPORTED)，上层据此降级。"""
        raise NotImplementedError

    # ---- 计费
    def price(self, *, gpu_type: str, gpu: int = 1, cpu: int = 16,
              memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
              charge_type: str = "Postpay", image: str = "",
              zone: str | None = None) -> dict:
        """返回 {hourly_instance, hourly_disk, hourly_total, currency, ...}。

        各家免费额度不同，所以**分开返回**，不要在这里折算。
        """
        raise NotImplementedError

    def balance(self) -> float:
        raise NotImplementedError

    # ---- 镜像与库存
    def resolve_image(self, spec: str) -> dict:
        """镜像 spec → {image_id, name, ...}。找不到抛 NOT_FOUND。"""
        raise NotImplementedError

    def images(self, keyword: str = "") -> list[dict]:
        raise NotImplementedError

    def stock(self, gpu_type: str, *, spot: bool = False, zone: str | None = None) -> int:
        raise NotImplementedError

    # ---- 能力门禁（上层调用，不要自己写 if 判断）
    def require(self, capability: str) -> None:
        """断言某项能力可用，否则抛 UNSUPPORTED 并说明原因。"""
        caps = self.capabilities()
        val = getattr(caps, capability, None)
        if val is None:
            raise ProviderError(
                f"{self.name} 未声明能力 {capability}，需实测后写入 capabilities()",
                ProviderError.UNSUPPORTED)
        if val is False:
            why = (caps.notes or {}).get(capability) or "该云不支持"
            raise ProviderError(f"{self.name} 不支持 {capability}：{why}",
                                ProviderError.UNSUPPORTED)
