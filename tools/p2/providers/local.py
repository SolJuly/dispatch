#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地机器 / 裸机适配器：把「已经存在的一台 GPU 机器」当成 provider。

用途有两个：

  1. **自建机 / 自有服务器**：不走云的建机 API，直接给 host/port/密码。
     这时 `create()` 不建机，只登记一个"实例"，状态恒为 running。
  2. **跨云一致性验证**：`provision.sh` 在有外网的机器上装完环境后，
     用同一个 Provider 契约跑作业，从而证明「换云只是换了 create()」。

计费语义：本地机没有按小时计费，`price()` 返回 0，`balance()` 返回 inf。
这不是偷懒 —— 把本地机的"成本"伪造成一个数字会污染成本账，
真实做法是由上层用 `--budget-hours` 之类的外部口径另记（见 cost_snapshot）。
"""
from __future__ import annotations

import os
import socket
import time

from .base import (
    Capabilities, InstanceView, Provider, ProviderError, SSHEndpoint, classify,
)


class LocalProvider(Provider):
    name = "local"

    def __init__(self, *, host: str | None = None, port: str | None = None,
                 password: str | None = None, uid: str = "local-0",
                 name: str = "", gpu_type: str = "local", gpu: int = 1,
                 cpu: int | None = None, memory_gb: int | None = None,
                 comfy_port: int = 8188, has_internet: bool = True,
                 shared_weight_mount: str | None = None):
        self.host = host or os.environ.get("LOCAL_HOST") or "127.0.0.1"
        self.port = str(port or os.environ.get("LOCAL_SSH_PORT") or "22")
        self.password = password or os.environ.get("LOCAL_SSH_PASSWORD") or None
        self.uid = uid
        self._name = name or f"local-{self.host}"
        self.gpu_type = gpu_type
        self.gpu = gpu
        self.cpu = cpu if cpu is not None else (os.cpu_count() or 0)
        self.memory_gb = memory_gb if memory_gb is not None else 0
        self.comfy_port = comfy_port
        self._has_internet = has_internet
        self._mount = shared_weight_mount
        self._terminated = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            can_stop_instance=False,          # 本地机没有"停机不计费"概念
            bills_disk_when_stopped=False,
            instance_has_internet=self._has_internet,
            can_pull_oci=True,                # 有 docker 就能拉
            has_shared_weight_mount=bool(self._mount),
            supports_scheduled_stop=False,
            supports_spot=False,
            supports_custom_image=False,
            free_system_disk_gb=0,
            typical_boot_s=0,
            notes={
                "can_stop_instance": "本地机常开，没有停机保状态语义",
                "supports_scheduled_stop": "没有任何平台侧的定时关机",
                "has_shared_weight_mount": self._mount or "未配置共享权重挂载",
            },
        )

    # ---- 实例（单台，恒 running）
    def create(self, **_kw) -> str:
        if self._terminated:
            raise ProviderError(f"{self.uid} 已终止，不能复用", ProviderError.NOT_FOUND)
        return self.uid

    def get(self, uid: str) -> InstanceView:
        if uid != self.uid or self._terminated:
            raise ProviderError(f"本地 provider 只有 {self.uid}", ProviderError.NOT_FOUND)
        return InstanceView(
            uid=self.uid, state="running", name=self._name,
            gpu_type=self.gpu_type, gpu_count=self.gpu, cpu=self.cpu,
            memory_gb=self.memory_gb, price_per_hour=0.0, disk_per_hour=0.0,
            provider=self.name,
        )

    def list_instances(self, prefix: str = "") -> list[InstanceView]:
        if self._terminated:
            return []
        v = self.get(self.uid)
        return [v] if (not prefix or self._name.startswith(prefix)) else []

    def start(self, uid: str) -> None:
        self.get(uid)                       # 只校验存在

    def stop(self, uid: str) -> None:
        raise ProviderError("本地 provider 不支持 stop()：没有停机语义，"
                            "要停就停进程", ProviderError.UNSUPPORTED)

    def terminate(self, uid: str) -> None:
        self.get(uid)
        self._terminated = True             # 只是从视图里摘掉，不真关机器

    def ssh_endpoint(self, uid: str) -> SSHEndpoint:
        self.get(uid)
        return SSHEndpoint(host=self.host, port=self.port, password=self.password)

    def ssh_ready(self, uid: str, *, timeout: int = 60) -> SSHEndpoint:
        """探 TCP 连通即可：本地/自建机的 sshd 通常早就起好了。"""
        ep = self.ssh_endpoint(uid)
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with socket.create_connection((ep.host, int(ep.port)), timeout=3):
                    return ep
            except OSError:
                time.sleep(2)
        raise ProviderError(f"{ep.host}:{ep.port} {timeout}s 内不可连",
                            ProviderError.TRANSIENT)

    def scheduled_stop(self, uid: str, at_epoch: int) -> None:
        raise ProviderError("本地 provider 没有平台侧定时关机；"
                            "要在机器上自己装 cron/at", ProviderError.UNSUPPORTED)

    # ---- 计费
    def price(self, **_kw) -> dict:
        return {"hourly_instance": 0.0, "hourly_disk": 0.0, "hourly_total": 0.0,
                "currency": "CNY", "charge_type": "owned", "raw": {}}

    def balance(self) -> float:
        return float("inf")                 # 自有机器，无余额概念

    # ---- 镜像与库存
    def resolve_image(self, spec: str) -> dict:
        return {"image_id": spec, "name": spec, "raw": {}}

    def images(self, keyword: str = "") -> list[dict]:
        return []

    def stock(self, gpu_type: str, *, spot: bool = False, zone: str | None = None) -> int:
        return 1                            # 就一台


class FakeProvider(Provider):
    """测试用内存 provider：完整实现契约，零网络零花费。

    状态机与智云对齐（starting→running→stopping→stopped→terminated），
    这样上层逻辑在 Fake 上写对，到真云上不会因为状态差异翻车。
    """

    name = "fake"

    def __init__(self, *, stock: int = 99, balance: float = 100.0,
                 hourly: float = 2.15, disk_hourly: float = 0.0,
                 fail_create: str | None = None):
        self._n = 0
        self.insts: dict[str, InstanceView] = {}
        self.calls: list[str] = []
        self._stock = stock
        self._balance = balance
        self._hourly = hourly
        self._disk_hourly = disk_hourly
        self._fail_create = fail_create        # 注入建机失败文案
        self.stop_at: dict[str, int] = {}
        self.cfg: dict = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name, can_stop_instance=True, bills_disk_when_stopped=False,
            instance_has_internet=True, can_pull_oci=True,
            has_shared_weight_mount=True, supports_scheduled_stop=True,
            supports_spot=True, supports_custom_image=True,
            free_system_disk_gb=100, typical_boot_s=1,
        )

    def create(self, *, image: str, gpu_type: str, gpu: int = 1, cpu: int = 16,
               memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
               name: str = "job", charge_type: str = "Postpay",
               zone: str | None = None, password: str | None = None,
               remark: str = "") -> str:
        if self._fail_create:
            kind, retryable = classify(self._fail_create)
            raise ProviderError(self._fail_create, kind, retryable=retryable)
        if self._stock <= 0:
            raise ProviderError("226604 库存不足", ProviderError.OUT_OF_STOCK)
        self._stock -= 1
        self._n += 1
        uid = f"uhost-fake{self._n:04d}"
        self.insts[uid] = InstanceView(
            uid=uid, state="running", name=name, gpu_type=gpu_type, gpu_count=gpu,
            cpu=cpu, memory_gb=memory_gb, price_per_hour=self._hourly,
            disk_per_hour=self._disk_hourly, provider=self.name, raw={"image": image},
        )
        self.calls.append(f"create:{uid}")
        return uid

    def get(self, uid: str) -> InstanceView:
        if uid not in self.insts:
            raise ProviderError(f"实例 {uid} 不存在", ProviderError.NOT_FOUND)
        return self.insts[uid]

    def list_instances(self, prefix: str = "") -> list[InstanceView]:
        out = list(self.insts.values())
        return [i for i in out if i.name.startswith(prefix)] if prefix else out

    def _set(self, uid: str, state: str, call: str) -> None:
        self.get(uid).state = state
        self.calls.append(f"{call}:{uid}")

    def start(self, uid: str) -> None:
        self._set(uid, "running", "start")

    def stop(self, uid: str) -> None:
        self._set(uid, "stopped", "stop")

    def terminate(self, uid: str) -> None:
        self.get(uid)
        self.insts.pop(uid)
        self.calls.append(f"terminate:{uid}")

    def ssh_endpoint(self, uid: str) -> SSHEndpoint:
        v = self.get(uid)
        return SSHEndpoint(host=f"10.0.0.{v.uid[-1]}", port="23", password="fake")

    def ssh_ready(self, uid: str, *, timeout: int = 180) -> SSHEndpoint:
        self.get(uid)
        return self.ssh_endpoint(uid)       # Fake 立刻就绪，不 sleep

    def scheduled_stop(self, uid: str, at_epoch: int) -> None:
        self.get(uid)
        self.stop_at[uid] = int(at_epoch)
        self.calls.append(f"stop_at:{uid}")

    def price(self, *, gpu_type: str, gpu: int = 1, cpu: int = 16,
              memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
              charge_type: str = "Postpay", image: str = "",
              zone: str | None = None) -> dict:
        return {"hourly_instance": self._hourly, "hourly_disk": self._disk_hourly,
                "hourly_total": round(self._hourly + self._disk_hourly, 4),
                "currency": "CNY", "charge_type": charge_type, "raw": {}}

    def balance(self) -> float:
        return self._balance

    def resolve_image(self, spec: str) -> dict:
        return {"image_id": spec, "name": spec, "raw": {}}

    def images(self, keyword: str = "") -> list[dict]:
        return [{"image_id": "compshareImage-fake0001", "name": "p2-comfy-media-v4",
                 "type": "Custom", "raw": {}}]

    def stock(self, gpu_type: str, *, spot: bool = False, zone: str | None = None) -> int:
        return self._stock
