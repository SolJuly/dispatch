#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智云 CompShare 适配器：把 `compshare.cs.CS` 包成 Provider 契约。

设计原则：**只做翻译，不改行为**。

没有直接重写 `cs.py` 的原因是它已经被 34 处调用、564 行、含大量实测细节
（8903 过渡态退避、226604 抢货、Spot 不支持定时关机、价格缓存键）。
重写等于把这些踩过的坑重新踩一遍。所以这里是一个薄的适配层：

    CS 的 dict 返回  →  InstanceView（云无关字段）
    CompShareError   →  ProviderError（带 kind，可分流重试）

所有真实逻辑仍在 cs.py，出了问题还能按老路径排查。
"""
from __future__ import annotations

import os

from .base import (
    Capabilities, InstanceView, Provider, ProviderError, SSHEndpoint, classify,
)


def _wrap(fn, *args, **kwargs):
    """调用 cs.py 的方法，把 CompShareError 翻成 ProviderError。"""
    from compshare.cs import CompShareError
    try:
        return fn(*args, **kwargs)
    except CompShareError as e:
        kind, retryable = classify(str(e))
        raise ProviderError(str(e), kind, retryable=retryable) from e


_STATE_MAP = {
    "running": "running", "rebooting": "running",
    "stopped": "stopped", "starting": "starting",
    "stopping": "stopping", "install": "starting",
    "terminating": "terminating", "interrupted": "interrupted",
}


class CompShareProvider(Provider):
    name = "compshare"

    def __init__(self, cfg: dict[str, str] | None = None):
        from compshare.cs import CS
        # cfg 支持 BYOK：直接传客户自己的 {CS_PUBLIC_KEY, CS_PRIVATE_KEY}，
        # 不碰 credentials.env，也不污染进程环境变量。
        self.cs = CS(cfg) if cfg else CS()

    # ---- 元信息
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            can_stop_instance=True,
            # 2026-11-09 实测：系统盘 ≤100GB 免费；镜像存储 ¥0.236/GB/月，
            # 每天 01:10 结算。停机实例的磁盘是否计费仍在 24h 实验中。
            bills_disk_when_stopped=False,
            # 实测：实例内 DNS 失败（Name or service not known），apt/pip/git 全废
            instance_has_internet=False,
            can_pull_oci=False,
            has_shared_weight_mount=True,
            supports_scheduled_stop=True,
            supports_spot=True,
            supports_custom_image=True,
            free_system_disk_gb=100,
            typical_boot_s=95,
            notes={
                "can_pull_oci": "create() 只接受 CompShareImageId，无 registry 字段",
                "instance_has_internet": "DNS 解析失败，provision.sh 在此不可用",
                "bills_disk_when_stopped": "24h 停机实验进行中（tools/p2/idle_billing_probe.py），未出结论前按 False 处理",
                "has_shared_weight_mount": "/model UPFS 40T 只读，跨账号共享",
            },
        )

    # ---- 实例
    def _view(self, d: dict) -> InstanceView:
        state = _STATE_MAP.get(str(d.get("State") or "").lower(), "unknown")
        return InstanceView(
            uid=str(d.get("UHostId") or ""),
            state=state,
            name=str(d.get("Name") or ""),
            gpu_type=str(d.get("GpuType") or ""),
            gpu_count=int(d.get("GPU") or 0),
            cpu=int(d.get("CPU") or 0),
            memory_gb=int(d.get("Memory") or 0) // 1024,
            price_per_hour=float(d.get("_hourly") or 0) - float(d.get("_disk_hourly") or 0),
            disk_per_hour=float(d.get("_disk_hourly") or 0),
            region=str(d.get("Region") or self.cs.region),
            zone=str(d.get("Zone") or self.cs.zone),
            provider=self.name,
            raw=d,
        )

    def create(self, *, image: str, gpu_type: str, gpu: int = 1, cpu: int = 16,
               memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
               name: str = "job", charge_type: str = "Postpay",
               zone: str | None = None, password: str | None = None,
               remark: str = "") -> str:
        ids = _wrap(self.cs.create, image_id=image, gpu_type=gpu_type, gpu=gpu,
                    cpu=cpu, memory_gb=memory_gb, name=name, charge_type=charge_type,
                    disk_gb=disk_gb, data_disk_gb=data_disk_gb, zone=zone,
                    password=password, remark=remark)
        if not ids:
            raise ProviderError("创建未返回实例 ID", ProviderError.TRANSIENT)
        return str(ids[0])

    def get(self, uid: str) -> InstanceView:
        return self._view(_wrap(self.cs.get, uid))

    def list_instances(self, prefix: str = "") -> list[InstanceView]:
        out = [_wrap(self._view, d) for d in _wrap(self.cs.instances)]
        if prefix:
            out = [i for i in out if i.name.startswith(prefix)]
        return out

    def start(self, uid: str) -> None:
        _wrap(self.cs.start, uid)

    def stop(self, uid: str) -> None:
        _wrap(self.cs.stop, uid)

    def terminate(self, uid: str) -> None:
        _wrap(self.cs.terminate, uid)

    def ssh_endpoint(self, uid: str) -> SSHEndpoint:
        inst = _wrap(self.cs.get, uid)
        return SSHEndpoint(
            host=self.cs.public_ip(inst) or "",
            port="23",                      # 智云统一 23 端口
            password=self.cs.password_of(inst) or None,
        )

    def ssh_ready(self, uid: str, *, timeout: int = 180) -> SSHEndpoint:
        """等 sshd 能收命令（Running ≠ sshd 就绪，实测差 20-40s）。"""
        import time
        ep = self.ssh_endpoint(uid)
        if not ep.host:
            raise ProviderError(f"{uid} 没有公网 IP", ProviderError.TRANSIENT)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if _ssh_probe(ep):
                return ep
            time.sleep(5)
        raise ProviderError(f"{uid} sshd {timeout}s 内未就绪", ProviderError.TRANSIENT)

    def scheduled_stop(self, uid: str, at_epoch: int) -> None:
        _wrap(self.cs.stop_at, uid, at_epoch)

    # ---- 计费
    def price(self, *, gpu_type: str, gpu: int = 1, cpu: int = 16,
              memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
              charge_type: str = "Postpay", image: str = "",
              zone: str | None = None) -> dict:
        p = _wrap(self.cs.price, gpu_type, gpu, cpu, memory_gb, charge_type,
                  disk_gb, data_disk_gb, image_id=image, zone=zone)
        inst = float(p.get("instance") or 0)
        disk = float(p.get("system_disk") or 0) + float(p.get("data_disk") or 0)
        return {
            "hourly_instance": inst,
            "hourly_disk": disk,
            "hourly_total": round(inst + disk, 4),
            "currency": "CNY",
            "charge_type": charge_type,
            "raw": p,
        }

    def balance(self) -> float:
        return float(_wrap(self.cs.balance).get("AmountAvailable") or 0)

    # ---- 镜像与库存
    def resolve_image(self, spec: str) -> dict:
        d = _wrap(self.cs.resolve_image, spec)
        return {"image_id": str(d.get("CompShareImageId") or spec),
                "name": str(d.get("Name") or spec), "raw": d}

    def images(self, keyword: str = "") -> list[dict]:
        out = []
        for d in _wrap(self.cs.images, keyword):
            out.append({"image_id": str(d.get("CompShareImageId") or ""),
                        "name": str(d.get("Name") or ""),
                        "type": str(d.get("ImageType") or ""),
                        "raw": d})
        return out

    def stock(self, gpu_type: str, *, spot: bool = False, zone: str | None = None) -> int:
        return int(_wrap(self.cs.stock, gpu_type, spot, zone))


def _ssh_probe(ep: SSHEndpoint) -> bool:
    """一次 ssh 探活。用 sshpass（与 run.py 一致），没有就当就绪避免卡死。"""
    import shutil
    import subprocess
    if not shutil.which("sshpass") or not ep.password:
        return True                 # 没有 sshpass 就没法探，交给上层真连一次
    cmd = ["sshpass", "-p", ep.password, "ssh",
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=8", "-p", ep.port, f"root@{ep.host}",
           "echo ssh-ready"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        return r.returncode == 0 and "ssh-ready" in (r.stdout or "")
    except Exception:
        return False


def from_env() -> CompShareProvider:
    """按环境变量/credentials.env 建一个默认 provider。"""
    return CompShareProvider()
