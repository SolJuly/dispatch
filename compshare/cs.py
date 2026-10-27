# -*- coding: utf-8 -*-
"""优云智算 (CompShare / api.compshare.cn) 轻量封装。

设计目标：
  1. 少等待：状态轮询 / 库存轮询 / 就绪等待都封装成一行调用；
  2. 少花钱：价格、库存、计时、空闲关机一应数据都能直接取到；
  3. 无第三方依赖：只用官方 ucloud-sdk-python3 + requests。

凭证读取顺序：环境变量 CS_PUBLIC_KEY / CS_PRIVATE_KEY > 同目录 credentials.env
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

from ucloud.client import Client
from ucloud.core import exc as ucloud_exc

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "credentials.env"
STATE_DIR = HERE.parent / ".compshare"

# 状态文案（控制台中文 -> API 英文）
STATE_CN = {
    "Running": "运行中",
    "Stopped": "已关机",
    "Starting": "启动中",
    "Stopping": "关机中",
    "Install": "安装/部署中",
    "Rebooting": "重启中",
    "Interrupted": "已中断",
    "Terminating": "删除中",
    "Unknown": "未知",
}

# 运行中 = 已开始计费
BILLING_STATES = {"Running", "Rebooting"}

SPOT_UNSUPPORTED = {"4090_48G", "H20", "P40", "2080"}


def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("CS_PUBLIC_KEY", "CS_PRIVATE_KEY", "CS_REGION", "CS_ZONE",
              "CS_BASE_URL", "CS_PROJECT_ID"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    missing = [k for k in ("CS_PUBLIC_KEY", "CS_PRIVATE_KEY") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"缺少凭证: {', '.join(missing)}，请写入 {ENV_FILE}")
    return cfg


class CompShareError(RuntimeError):
    pass


class CS:
    """薄封装：所有方法都返回 Python 原生 dict/list，方便脚本继续算。"""

    def __init__(self, cfg: dict[str, str] | None = None, debug: bool = False):
        self.cfg = cfg or load_config()
        self.region = self.cfg.get("CS_REGION", "cn-wlcb")
        self.zone = self.cfg.get("CS_ZONE", "cn-wlcb-01")
        self.project_id = self.cfg.get("CS_PROJECT_ID", "")
        if not debug:
            logging.getLogger("ucloud").setLevel(logging.WARNING)
        self._client = Client({
            "region": self.region,
            "public_key": self.cfg["CS_PUBLIC_KEY"],
            "private_key": self.cfg["CS_PRIVATE_KEY"],
            "base_url": self.cfg.get("CS_BASE_URL", "https://api.compshare.cn"),
            "timeout": 60,
        })
        self.cs = self._client.ucompshare()
        self._project_id: str | None = self.project_id or None

    # ------------------------------------------------------------------ 基础
    def _retry(self, fn, *args, **kwargs):
        last = None
        for i in range(4):
            try:
                return fn(*args, **kwargs)
            except ucloud_exc.RetCodeException as e:  # 业务错误不重试
                raise CompShareError(str(e)) from e
            except Exception as e:  # 网络/超时
                last = e
                time.sleep(1.5 * (i + 1))
        raise CompShareError(f"接口重试失败: {last}")

    def call(self, action: str, params: dict[str, Any] | None = None) -> dict:
        """调用任意 Action（自动补 Region/Zone）。"""
        p = {"Region": self.region, **(params or {})}
        return self._retry(self.cs.invoke, action, p)

    def json_call(self, action: str, params: dict[str, Any] | None = None) -> dict:
        """JSON body 调用：少数接口（如监控）只接受真正的数组类型，表单编码会被拒。"""
        from ucloud.core.auth._cfg import verify_ac

        body: dict[str, Any] = {"Action": action, "PublicKey": self.cfg["CS_PUBLIC_KEY"],
                                "Region": self.region, **(params or {})}
        flat: dict[str, str] = {}
        for k, v in body.items():
            if isinstance(v, bool):
                flat[k] = "true" if v else "false"
            elif isinstance(v, (list, tuple)):
                flat[k] = ",".join(str(x) for x in v)
            elif isinstance(v, dict):
                continue
            else:
                flat[k] = str(v)
        body["Signature"] = verify_ac(self.cfg["CS_PRIVATE_KEY"], flat)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.cfg.get("CS_BASE_URL", "https://api.compshare.cn"),
                                     data=data, headers={"Content-Type": "application/json"})
        try:
            raw = urllib.request.urlopen(req, timeout=60).read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        r = json.loads(raw.decode("utf-8"))
        if int(r.get("RetCode", -1)) != 0:
            raise CompShareError(f"{action} - {r.get('RetCode')}: {r.get('Message')}")
        return r

    def project(self) -> str:
        """定时关机等接口强制要求 ProjectId。"""
        if not self._project_id:
            r = self.call("GetProjectList", {})
            ps = r.get("ProjectSet") or []
            self._project_id = (ps[0]["ProjectId"] if ps else "org-default")
        return self._project_id

    def rz(self, inst: dict) -> dict:
        """实例所在地域/可用区（Describe 接口不一定返回，回落到配置）。"""
        return {"Region": inst.get("Region") or self.region,
                "Zone": inst.get("Zone") or self.zone,
                "UHostId": inst["UHostId"]}

    # ------------------------------------------------------------------ 实例
    def instances(self, ids: Iterable[str] | None = None, refresh: bool = True) -> list[dict]:
        params: dict[str, Any] = {"Region": self.region, "Limit": 100}
        if ids:
            params["UHostIds"] = list(ids)
        r = self._retry(self.cs.describe_comp_share_instance, params)
        out = r.get("UHostSet") or []
        for i in out:
            i["_state_cn"] = STATE_CN.get(i.get("State", "Unknown"), i.get("State", "?"))
            if i.get("InstancePrice") is not None:
                i["_hourly"] = float(i["InstancePrice"]) + float(i.get("DiskPrice") or 0)
                i["_disk_hourly"] = float(i.get("DiskPrice") or 0)
            else:  # 该接口常不返回价格，回落到价格接口（带缓存）
                i["_hourly"], i["_disk_hourly"] = self.hourly(i)
        return out

    def hourly(self, inst: dict) -> tuple[float, float]:
        """(算力元/时, 磁盘元/时)；按配置缓存，避免刷屏打接口。"""
        cache: dict = self.__dict__.setdefault("_price_cache", {})
        key = (inst.get("GpuType"), int(inst.get("GPU") or 0), int(inst.get("CPU") or 0),
               int(inst.get("Memory") or 0), inst.get("ChargeType") or "Postpay",
               int(inst.get("TotalDiskSpace") or 100))
        if key not in cache:
            try:
                extra = max(0, key[5] - 100)
                p = self.price(key[0], key[1], key[2], key[3] // 1024, key[4], 100, extra)
                cache[key] = (float(p["instance"]), float(p["system_disk"]) + float(p["data_disk"]))
            except CompShareError:
                cache[key] = (0.0, 0.0)
        return cache[key]

    def get(self, ident: str) -> dict:
        """按实例 ID 或名称取一台（名称支持前缀/包含匹配，唯一命中才算）。"""
        ins = self.instances()
        if not ident:
            raise CompShareError("需要实例 ID 或名称")
        for i in ins:
            if i.get("UHostId") == ident:
                return i
        hit = [i for i in ins if i.get("Name") == ident]
        if not hit:
            hit = [i for i in ins if ident in (i.get("Name") or "")]
        if len(hit) == 1:
            return hit[0]
        if not hit:
            raise CompShareError(f"找不到实例: {ident}")
        raise CompShareError(
            f"名称 '{ident}' 匹配到多台，请用 ID: "
            + ", ".join(f"{i['UHostId']}({i['Name']})" for i in hit))

    def wait_state(self, ident: str, states: Iterable[str], timeout: int = 900,
                   interval: int = 5, quiet: bool = False) -> dict:
        """轮询等待实例进入目标状态，省掉人工刷新控制台。"""
        states = {s.lower() for s in states}
        t0 = time.time()
        last = None
        while True:
            inst = self.get(ident)
            st = (inst.get("State") or "").lower()
            if st != last and not quiet:
                print(f"  [{int(time.time()-t0):>4}s] {inst['UHostId']} {inst.get('Name','')} -> "
                      f"{inst.get('_state_cn')} ({inst.get('State')})", flush=True)
                last = st
            if st in states:
                return inst
            if time.time() - t0 > timeout:
                raise CompShareError(
                    f"等待 {ident} 进入 {states} 超时({timeout}s)，当前 {inst.get('State')}")
            time.sleep(interval)

    def settle(self, ident: str, timeout: int = 900) -> dict:
        """等实例从瞬态（Stopping/Starting/Rebooting/Install）落定，避免 "UHost is in the task"。"""
        transient = {"stopping", "starting", "rebooting", "install"}
        t0 = time.time()
        while True:
            inst = self.get(ident)
            if (inst.get("State") or "").lower() not in transient:
                return inst
            if time.time() - t0 > timeout:
                return inst
            time.sleep(4)

    def create(self, image_id: str, gpu_type: str = "4090", gpu: int = 1,
               cpu: int = 16, memory_gb: int = 64, name: str = "gpu",
               charge_type: str = "Postpay", disk_gb: int = 100,
               data_disk_gb: int = 0, zone: str | None = None,
               password: str | None = None, remark: str = "") -> list[str]:
        disks = [{"IsBoot": True, "Type": "CLOUD_SSD", "Size": int(disk_gb)}]
        if data_disk_gb:
            disks.append({"IsBoot": False, "Type": "CLOUD_SSD", "Size": int(data_disk_gb)})
        p: dict[str, Any] = {
            "Region": self.region,
            "Zone": zone or self.zone,
            "MachineType": "G",
            "CompShareImageId": image_id,
            "GPU": int(gpu),
            "GpuType": gpu_type,
            "CPU": int(cpu),
            "Memory": int(memory_gb) * 1024,
            "ChargeType": charge_type,
            "Name": name[:63],
            "Disks": disks,
        }
        if self.project_id:
            p["ProjectId"] = self.project_id
        if password:
            p.update({"Password": password, "LoginMode": "Password"})
        if remark:
            p["Remark"] = remark[:120]
        r = self._retry(self.cs.create_comp_share_instance, p)
        ids = r.get("UHostIds") or []
        if not ids:
            raise CompShareError(f"创建未返回实例 ID: {r}")
        return ids

    def stop(self, ident: str) -> dict:
        i = self.get(ident)
        return self._retry(self.cs.stop_comp_share_instance, self.rz(i))

    def start(self, ident: str) -> dict:
        i = self.get(ident)
        return self._retry(self.cs.start_comp_share_instance, self.rz(i))

    def reboot(self, ident: str) -> dict:
        i = self.get(ident)
        return self._retry(self.cs.reboot_comp_share_instance, self.rz(i))

    def terminate(self, ident: str) -> dict:
        i = self.get(ident)
        return self._retry(self.cs.terminate_comp_share_instance, self.rz(i))

    def rename(self, ident: str, name: str) -> dict:
        i = self.get(ident)
        return self.call("ModifyCompShareInstanceName", {**self.rz(i), "Name": name})

    # -------------------------------------------------------------- 定时关机
    def stop_at(self, ident: str, ts: int) -> dict:
        """定时关机。平台要求 SchedulerStopTime >= 现在+300 秒；Spot 实例不支持。"""
        i = self.get(ident)
        if i.get("IsSpot"):
            raise CompShareError("抢占式实例不支持定时关机（只能删除或等回收）")
        ts = max(int(ts), int(time.time()) + 300)
        r = self.json_call("UpdateCompShareStopScheduler", {
            **self.rz(i), "ProjectId": self.project(), "SchedulerStopTime": ts})
        record_stop_at(i["UHostId"], ts)  # Describe 不返回该字段，本地留档用于展示
        return r

    def cancel_stop_at(self, ident: str) -> dict:
        i = self.get(ident)
        r = self.json_call("DeleteCompShareStopScheduler", {
            **self.rz(i), "ProjectId": self.project()})
        clear_stop_at(i["UHostId"])
        return r

    # ------------------------------------------------------------------ 监控
    def monitor(self, ident: str) -> dict[str, float]:
        """返回最新一次采样: {'gpu': %, 'gpu_mem': %, 'cpu': %, 'mem': %}"""
        i = self.get(ident)
        keys = {
            "cloudwatch_gpu_util": "gpu",
            "cloudwatch_gpu_memory_usage": "gpu_mem",
            "uhost_cpu_used": "cpu",
            "cloudwatch_memory_usage": "mem",
        }
        out: dict[str, float] = {}
        try:
            r = self.json_call("GetCompShareInstanceMonitor", {
                **self.rz(i), "UHostIds": [i["UHostId"]]})
            for item in (r.get("Data") or {}).get("List") or []:
                for m in item.get("Metrics") or []:
                    k = keys.get(m.get("MetricKey"))
                    if not k:
                        continue
                    vals = []
                    for res in m.get("Results") or []:
                        vals += [v.get("Value") for v in res.get("Values") or []
                                 if v.get("Value") is not None]
                    if vals:
                        out[k] = float(vals[-1])
        except CompShareError:
            pass
        if not out:  # 回落：Describe 里的实时快照
            mm = i.get("MonitorMessages") or {}
            for src, dst in (("GPUUtilization", "gpu"), ("GPU", "gpu"),
                             ("CPUUtilization", "cpu"), ("CPU", "cpu")):
                v = mm.get(src)
                if isinstance(v, (int, float)):
                    out.setdefault(dst, float(v))
                elif isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, (int, float)):
                            out.setdefault(dst, float(vv))
                            break
        return out

    # ------------------------------------------------------------------ 库存
    def inventory_raw(self) -> dict:
        return self.call("DescribeCompShareGpuInventory", {"Region": self.region,
                                                           "Zone": self.zone})

    def inventory(self) -> dict:
        return self.inventory_raw().get("GpuInventory") or {}

    def stock(self, gpu_type: str, spot: bool = False, zone: str | None = None) -> int:
        inv = self.inventory()
        scope = "Spot" if spot else "Exclusive"
        zid = self._zone_id(zone or self.zone)
        return int((inv.get(scope) or {}).get(zid, {}).get(gpu_type, 0) or 0)

    def _zone_id(self, zone: str) -> str:
        if not hasattr(self, "_zones"):
            self._zones = {}
            try:
                r = self.call("DescribeCompShareSupportZone", {})
                for z in r.get("ZoneInfo") or []:
                    self._zones[z.get("Zone")] = str(z.get("ZoneId"))
            except CompShareError:
                pass
        return self._zones.get(zone, "10027")

    # ------------------------------------------------------------------ 价格
    def price(self, gpu_type: str = "4090", gpu: int = 1, cpu: int = 16,
              memory_gb: int = 64, charge_type: str = "Postpay",
              disk_gb: int = 100, data_disk_gb: int = 0,
              image_id: str = "", zone: str | None = None) -> dict:
        disks = [{"IsBoot": True, "Type": "CLOUD_SSD", "Size": int(disk_gb)}]
        if data_disk_gb:
            disks.append({"IsBoot": False, "Type": "CLOUD_SSD", "Size": int(data_disk_gb)})
        p = {"Region": self.region, "Zone": zone or self.zone, "GpuType": gpu_type,
             "Gpu": int(gpu), "Cpu": int(cpu), "Memory": int(memory_gb) * 1024,
             "ChargeType": charge_type, "Disks": disks}
        if image_id:
            p["CompShareImageId"] = image_id
        r = self.call("GetCompShareInstanceUserPrice", p)
        d = (r.get("PriceDetails") or [{}])[0] if r.get("PriceDetails") else {}
        inst = float(d.get("Instance") or 0)
        sysdisk = float(d.get("SystemDisks") or 0)
        disk = float(d.get("Disks") or 0)
        return {"charge_type": charge_type, "instance": inst, "system_disk": sysdisk,
                "data_disk": disk, "total": round(inst + sysdisk + disk, 3),
                "discount": d.get("InstanceDiscountType")}

    def price_matrix(self, gpu_type: str = "4090", gpu: int = 1, cpu: int = 16,
                     memory_gb: int = 64, disk_gb: int = 100) -> list[dict]:
        out = []
        for ct in ("Postpay", "Spot", "Day", "Month"):
            try:
                out.append(self.price(gpu_type, gpu, cpu, memory_gb, ct, disk_gb))
            except CompShareError as e:
                out.append({"charge_type": ct, "total": None, "error": str(e)[:80]})
        return out

    # ------------------------------------------------------------------ 镜像
    def images(self, keyword: str = "", kinds: tuple[str, ...] = ("System", "App", "Other", "Custom"),
               zone: str | None = None) -> list[dict]:
        z = zone or self.zone
        out: list[dict] = []

        def paged(action: str) -> list[dict]:
            got: list[dict] = []
            offset = 0
            while True:
                r = self.call(action, {"Region": self.region, "Zone": z,
                                       "Limit": 100, "Offset": offset})
                batch = r.get("ImageSet") or []
                got += batch
                if len(batch) < 20 or offset > 2000:
                    return got
                offset += len(batch)

        for action in ("DescribeCompShareImages", "DescribeCompShareCustomImages"):
            try:
                for i in paged(action):
                    i.setdefault("ImageType", "Custom")
                    out.append(i)
            except CompShareError:
                pass
        seen, uniq = set(), []
        for i in out:
            iid = i.get("CompShareImageId")
            if iid in seen:
                continue
            seen.add(iid)
            uniq.append(i)
        out = [i for i in uniq if i.get("Status", "Available") == "Available"]
        if keyword:
            kw = keyword.lower()
            out = [i for i in out if kw in (i.get("Name") or "").lower()
                   or kw in (i.get("CompShareImageId") or "").lower()]
        if kinds:
            out = [i for i in out if i.get("ImageType") in kinds]
        return out

    def resolve_image(self, spec: str) -> dict:
        """镜像: 直接给 ID，或按名称包含匹配。"""
        if spec.startswith("compshareImage-"):
            for i in self.images():
                if i.get("CompShareImageId") == spec:
                    return i
            return {"CompShareImageId": spec, "Name": spec}
        hits = self.images(spec)
        if not hits:
            raise CompShareError(f"没有匹配镜像: {spec}")
        hits.sort(key=lambda i: (len(i.get("Name") or ""), -(i.get("CreateTime") or 0)))
        return hits[0]

    def save_image(self, ident: str, name: str, description: str = "") -> dict:
        i = self.get(ident)
        return self.call("CreateCompShareCustomImage", {
            **self.rz(i), "Name": name, "Description": description,
            **({"ProjectId": self.project()} if self.project_id else {})})

    # ------------------------------------------------------------------ 账单
    def balance(self) -> dict:
        return self.call("GetBalance", {}).get("AccountInfo") or {}

    @staticmethod
    def ssh_of(inst: dict) -> str:
        cmd = inst.get("SshLoginCommand") or ""
        return cmd.strip()

    @staticmethod
    def password_of(inst: dict) -> str:
        pw = inst.get("Password") or ""
        try:
            return base64.b64decode(pw).decode("utf-8", "ignore")
        except Exception:
            return pw

    @staticmethod
    def public_ip(inst: dict) -> str:
        for ip in inst.get("IPSet") or []:
            if ip.get("Type") == "International" or ip.get("Type") == "BGP":
                return ip.get("IP") or ""
        ips = inst.get("IPSet") or []
        return (ips[0].get("IP") or "") if ips else ""


def _stop_at_file() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / "stop_at.json"


def record_stop_at(uid: str, ts: int) -> None:
    f = _stop_at_file()
    d = {}
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            d = {}
    d[uid] = int(ts)
    f.write_text(json.dumps(d, indent=2), encoding="utf-8")


def learned_stop_at(uid: str) -> int:
    f = _stop_at_file()
    if not f.exists():
        return 0
    try:
        return int(json.loads(f.read_text(encoding="utf-8")).get(uid, 0) or 0)
    except Exception:
        return 0


def clear_stop_at(uid: str) -> None:
    f = _stop_at_file()
    if not f.exists():
        return
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        d.pop(uid, None)
        f.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def state_path() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / "state.json"


def save_state(data: dict) -> None:
    p = state_path()
    old = {}
    if p.exists():
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            old = {}
    old.update(data)

    def _safe(o):
        if callable(o):
            return repr(o)
        return str(o)

    p.write_text(json.dumps(old, ensure_ascii=False, indent=2, default=_safe), encoding="utf-8")


def load_state() -> dict:
    p = state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
