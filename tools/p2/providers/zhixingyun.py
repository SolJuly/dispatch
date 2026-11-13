#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智星云（gpu.ai-galaxy.com / app.ai-galaxy.cn）Provider 适配器。

与 compshare.py 的差别：智云那边已有 `compshare/cs.py` 沉淀了 564 行实测细节，
这里没有那层底子，所以**协议层自己实现**（签名 / 重试 / 错误分类），只依赖
标准库 urllib，不引第三方 SDK。

已实测的接口事实（2026-11-13，只读接口 + 官方文档 82 个 endpoint）：

  协议     POST 表单（x-www-form-urlencoded），基址 https://app.ai-galaxy.cn/openapi/v2
  签名     md5(按参数名升序拼 "k=v"（跳过空值/sign/secret） + "&secret=SK")
          公共参数 apikey / timestamp（秒）/ nonce
  响应     {"success":bool, "code":"2000"|"4000", "message":str, "data":any}
           **code 是字符串**，业务错误也走 HTTP 200，必须按 code 判。

  实例标识 `Container_name`（例 18621629564_lyg0103_...._u1_c0），
  所有实例级接口都用它当 `instance_name`。

  状态     Status: -2 异常已退费 / -1 启动异常 / 0 已释放 / 1 运行中 / 4 启动中
                    / 5 重启中 / 7 重启失败 / 8 保留磁盘
           关机态不是独立 Status，而是 Status==1 且 IsAbnormal==2
           （restart_instance_from_shutdown 的前置条件写死了这个判断）。

  **智星云是租期制（pack_time + unit），不是按小时 pay-as-you-go。**
  所以没有「停机保留、停着不计算力费」这回事：唯一的资源回收动作是
  「提前退租 stop_instance_with_refund（可选择性释放磁盘）」。stop() 因此
  抛 UNSUPPORTED，terminate() 才是真正的省钱动作 —— 这一点与智云相反，
  上层不能把两家的 terminate/stop 当同义词。

计费口径：`price()` 返回**会员折扣后**的实际出账价（query_price 的 FinalCost），
原价与折扣率一并放进 raw，不做任何折算。系统盘固定（Linux 200GB / Windows 350GB）
且已含在规格价里，不单独计费，故 `free_system_disk_gb=200`。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .base import (
    Capabilities, InstanceView, Provider, ProviderError, SSHEndpoint, classify,
)

# ------------------------------------------------------------------ 常量
BASE_URL = "https://app.ai-galaxy.cn/openapi/v2"

# ---------------------------------------------------------------- 暂停开关
# 2026-11-13 19:05 YG 决定：**智星云接入暂做搁置**（原因是成本，不是技术）。
# 搁置期间禁止任何真实云调用 —— 光靠"记得别用"是不够的，必须硬拦：
# 一个跑着的 cron / 误敲的 CLI 就可能建机扣钱。
#
# 要恢复接入，把环境变量 ZXY_ENABLE 置 1（或把下面默认值改 True）。
# 置位后所有方法恢复正常；未置位时构造 provider 仍能成功（能力矩阵照常可见），
# 但任何**发往云侧的请求**都会抛 UNSUPPORTED，不会产生一分钱花费。
ENABLED = os.environ.get("ZXY_ENABLE", "").strip().lower() in ("1", "true", "yes", "on")

DISABLED_MSG = (
    "智星云接入已暂停（2026-11-13 YG 决定，原因是成本不划算："
    "镜像 ¥0.41/GB·月比智云贵 1.74 倍、装机 52 分钟、按租期烧钱）。"
    "所有云侧调用已硬拦截，不会产生费用。"
    "要恢复请设 ZXY_ENABLE=1。详见 reports/L5-智星云*（4 份实测报告）"
)

# 凭证文件（不入库）：env 优先，其次 ~/.zxy/credentials.env，最后仓库 .zxy/credentials.env
ENV_KEYS = ("ZXY_API_KEY", "ZXY_API_SECRET")
CRED_FILES = (
    Path.home() / ".zxy" / "credentials.env",
    Path(__file__).resolve().parents[3] / ".zxy" / "credentials.env",
)

# 云侧 Status → 契约状态。8（保留磁盘）算 terminated：算力已停、实例不可再操作，
# 但磁盘继续计费，需要 release_kept_disk() 才算真收尾。
STATE_MAP: dict[int, str] = {
    -2: "terminated",   # 异常已退费
    -1: "error",        # 启动异常（可走救援 salvage_instance）
    0: "terminated",    # 已结束、已释放
    1: "running",       # 运行中（关机态也报 1，靠 IsAbnormal==2 区分）
    4: "starting",      # 启动中
    5: "rebooting",     # 重启中
    7: "error",         # 重启失败，需运维介入
    8: "terminated",    # 到期保留磁盘（磁盘仍在计费）
}

# IsAbnormal 语义：0 正常 / 1 运行异常 / 2 已关机（可“从关机启动”）
ABNORMAL_SHUTDOWN = 2

# 智星云的错误文案是中文自由文本，code 恒定 "4000"，只能按 message 分类。
# 顺序敏感：先命中先返回。
_ERROR_RULES: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (("sign验证失败", "签名", "sign"), ProviderError.AUTH, False),
    (("apikey", "accesskey", "鉴权", "未授权"), ProviderError.AUTH, False),
    (("没有可用的gpu资源", "没有可用", "无可用资源", "库存不足", "已售罄",
      "no available"), ProviderError.OUT_OF_STOCK, True),
    (("余额不足", "欠费", "请充值", "insufficient"), ProviderError.BILLING, False),
    (("no row found", "查询实例失败", "不存在", "not found"), ProviderError.NOT_FOUND, False),
    (("正在处理", "操作频繁", "请稍后", "in the task", "in progress"),
     ProviderError.BUSY, True),
    (("配额", "quota", "超出限制"), ProviderError.QUOTA, False),
    # 状态不满足（如“该状态不能启动实例!”）属终态：重试多少次都不会变。
    (("该状态不能", "状态异常", "不能启动"), ProviderError.UNKNOWN, False),
    # 已经结束/退过费的实例再退一次：幂等场景，不该当失败
    (("已结束", "无法退费", "已经退费"), ProviderError.UNKNOWN, False),
)


def classify_zxy(message: str, code: Any = None) -> tuple[str, bool]:
    """智星云错误 → (kind, retryable)。先查本地规则，再退回 base.classify()。"""
    m = (message or "").lower()
    for needles, kind, retry in _ERROR_RULES:
        if any(n.lower() in m for n in needles):
            return kind, retry
    # 退租冷却：实测"操作频繁#10001"要等约 3 分钟才放行
    return classify(message)


# ------------------------------------------------------------------ 签名
def sign_params(params: dict[str, Any], secret: str) -> str:
    """按官方签名算法算 sign。

    规则（doc-5644959）：参数名升序 → 跳过 sign/secret/空值 → "k=v" 用 & 连接
    → 末尾直接拼 "&secret=SK" → md5 十六进制小写。

    单独抽成模块函数是为了可测：签名错了整条链路 401，且各家算法不同，
    最容易在“空值是否参与”“secret 是否参与排序”这两点上写错。
    """
    parts = []
    for k in sorted(params):
        if k in ("sign", "secret"):
            continue
        v = params[k]
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    parts.append(f"secret={secret}")
    return hashlib.md5("&".join(parts).encode("utf-8")).hexdigest()


def _load_credentials() -> dict[str, str]:
    """读取凭证：env > ~/.zxy/credentials.env > <repo>/.zxy/credentials.env。"""
    cfg: dict[str, str] = {}
    for f in CRED_FILES:
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    for k in ENV_KEYS:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


# ------------------------------------------------------------------ 适配器
class ZhiXingYunProvider(Provider):
    name = "zhixingyun"

    def __init__(self, cfg: dict[str, str] | None = None, *,
                 base_url: str | None = None, timeout: int = 30,
                 retries: int = 3, default_bandwidth: int = 32,
                 default_pack_hours: int = 1,
                 transport: Callable[[str, dict[str, Any], int], str] | None = None):
        """构造**不校验凭证**。

        这一点与 CompShareProvider 不同，是有意的：`registry.available()` 靠
        「构造不抛异常」来判定某家云能不能查，若缺凭证就抛，能力矩阵里
        永远看不到智星云（验收标准 3 要求看得到）。凭证延到真正发请求时校验，
        缺了抛 ProviderError(AUTH)，语义也更准。

        cfg 支持 BYOK：{"ZXY_API_KEY": ..., "ZXY_API_SECRET": ...}，也接受
        apikey/apisecret/api_key/api_secret 别名。传了就用传入的，不读环境。
        """
        self.cfg = {**({} if cfg is None else _normalize_cfg(cfg))}
        self._byok = bool(self.cfg)
        if not self._byok:
            self.cfg = _load_credentials()
        self.base_url = (base_url or self.cfg.get("ZXY_BASE_URL") or BASE_URL).rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.default_bandwidth = int(default_bandwidth)
        self.default_pack_hours = int(default_pack_hours)
        self._transport = transport
        self._specs_cache: dict[str, tuple[float, list[dict]]] = {}
        self._images_cache: tuple[float, list[dict]] | None = None
        self._gmap_cache: tuple[float, dict[str, Any]] | None = None
        self._gmap_refreshed: str | None = None
        # 仅供离线测试解锁：注入 transport（假网络）时允许调用，因为根本
        # 发不出真实请求。生产路径永远走不到这里 —— 不注入 transport 就只能
        # 靠 ZXY_ENABLE=1 才能解锁。
        self._test_unlocked = self._transport is not None
        self.last_account: dict[str, Any] = {}
        self.last_raw: Any = None

    # -------------------------------------------------------------- 协议层
    def _ensure_creds(self) -> tuple[str, str]:
        ak = self.cfg.get("ZXY_API_KEY", "")
        sk = self.cfg.get("ZXY_API_SECRET", "")
        if not ak or not sk:
            raise ProviderError(
                "智星云凭证缺失：设置 ZXY_API_KEY / ZXY_API_SECRET（或写入 "
                ".zxy/credentials.env），BYOK 用户传 credentials={...}",
                ProviderError.AUTH)
        return ak, sk

    def _post(self, path: str, params: dict[str, Any], timeout: int) -> str:
        """单次 HTTP POST。测试用 transport 注入，零联网。"""
        body = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}).encode("utf-8")
        url = f"{self.base_url}/{path.lstrip('/')}"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        if self._transport is not None:
            return self._transport(path, params, timeout)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:      # 4000 一般走 200，这里兜 404/500
            return e.read().decode("utf-8", "replace")

    def call(self, path: str, params: dict[str, Any] | None = None, *,
             timeout: int | None = None) -> Any:
        """签名 → POST → 校验 code → 返回 data。失败抛 ProviderError(带 kind)。

        **所有云侧调用都经过这里**，所以在这一处拦截就够覆盖全部能力
        （建机/销毁/询价/镜像/余额…）。搁置状态下抛 UNSUPPORTED，绝不发请求。
        """
        if not (ENABLED or self._test_unlocked):
            raise ProviderError(DISABLED_MSG, ProviderError.UNSUPPORTED)
        ak, sk = self._ensure_creds()
        payload: dict[str, Any] = {
            k: v for k, v in (params or {}).items() if v is not None and v != ""}
        payload["apikey"] = ak
        payload["timestamp"] = str(int(time.time()))
        payload["nonce"] = secrets.token_hex(8)
        payload["sign"] = sign_params(payload, sk)

        to = timeout or self.timeout
        last: Exception | None = None
        for i in range(self.retries):
            try:
                raw = self._post(path, payload, to)
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if i + 1 < self.retries:
                    time.sleep(min(2 ** i, 5))
        else:
            raise ProviderError(f"{path} 网络失败：{last}", ProviderError.TRANSIENT)

        try:
            resp = json.loads(raw)
        except Exception:
            raise ProviderError(
                f"{path} 响应不是 JSON：{raw[:200]!r}", ProviderError.TRANSIENT)
        if not isinstance(resp, dict) or "code" not in resp:
            raise ProviderError(
                f"{path} 响应结构异常：{raw[:200]!r}", ProviderError.TRANSIENT)

        self.last_raw = resp.get("data")
        code = str(resp.get("code"))
        if code != "2000":
            msg = str(resp.get("message") or "未知错误")
            kind, retryable = classify_zxy(msg, code)
            raise ProviderError(f"{path}: {msg}（code={code}）", kind,
                                retryable=retryable)
        return resp.get("data")

    # -------------------------------------------------------------- 元信息
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            # ⚠️ 实测纠错（2026-11-13 12:03）：智星云**确实有**关机态 ——
            # 在机器内执行 `poweroff -f` 后云端立刻变成 Status=1 & IsAbnormal=2，
            # 且 `restart_instance_from_shutdown` 能把它唤醒（16s 到 running）。
            # 但两个前提让它**不能当智云的 stop 用**：
            #   ① 平台**没有**任何关机 API（instance/stop_*、shutdown_* 九个候选全 404），
            #      只能靠客户机内 poweroff —— 等于作业自己关自己的饭碗。
            #   ② 关机期间**租期照常消耗**（实测关机 6 分钟后 Due_time 未变、
            #      可退金额仍在下降），算力费一分不省，只省不了什么。
            # 所以接口上 can_stop_instance 仍为 False：不是"没有关机态"，
            # 而是"没有可用来自动止损的关机手段"。
            can_stop_instance=False,
            # 实测 2026-11-13（完整账单闭环）：保留磁盘期间按**整点小时**扣费，
            # 不是实时累积 —— 8 分钟观测 KeeppedDiskCost 恒 0，跨 10:00 那一秒
            # 立刻出账 "保留实例定时扣费(磁盘)" ¥0.0431/h（账单 Type=71），
            # 释放时再结算尾款 ¥0.0052（Type=72）。
            # 0.0431 比"200GB×0.0006=0.12"低，说明只算实际占用（约 29GB 用掉的部分）,
            # 但结论不变：留着过夜就是白烧钱，用完必须 release_kept_disk()。
            bills_disk_when_stopped=True,
            # 实测 2026-11-13：DNS 正常、pypi/github 200、docker pull hello-world 成功，
            # provision.sh --dry-run 全流程跑通。与智云（DNS 全废）有本质区别。
            instance_has_internet=True,
            can_pull_oci=False,
            # 6.0 共享盘（AI 模型库）是**付费挂载**商品，不是智云那种免费只读 /model。
            has_shared_weight_mount=False,
            supports_scheduled_stop=False,
            supports_spot=False,
            # 4.0 自定义镜像：从实例制作 / 续订 / 退订，接口齐全。
            supports_custom_image=True,
            # 系统盘固定 Linux 200GB / Windows 350GB，已含在规格价内（实测 199G）。
            free_system_disk_gb=200,
            # 实测 2026-11-13：CPU 实例 21s 到 running（sshd 再 +2s）；
            # 2080 Ti GPU 实例 36s 到 running。取 GPU 值 + 余量。
            typical_boot_s=45,
            notes={
                "terminate_lock_s": "建机后约 310s 内无法退租（实测 3 台）；这是平台固定冷却窗，不是抖动",
                "can_stop_instance": "有关机态（guest 内 poweroff 可触发）但无平台关机 API，且关机期间租期照常计费；止损只能提前退租（terminate）",
                "bills_disk_when_stopped": "按整点小时扣费（实测 ¥0.0431/h + 释放尾款 ¥0.0052）；Status=8 期间持续计费",
                "instance_has_internet": "实测 DNS/pypi/github/docker 全通，provision.sh 可跑",
                "can_pull_oci": "create_kvm_instance 只收平台 image_name，无镜像仓库字段",
                "has_shared_weight_mount": "共享盘是付费挂载商品（shareddisk/mount_disk），非免费只读库",
                "supports_scheduled_stop": "只有到期时间与自动续费，无定时关机；靠本地 reaper",
                "supports_spot": "文档无竞价/抢占实例类型",
                "supports_custom_image": "4.0 自定义镜像：创建/续订/退订/失败重试齐全",
                "free_system_disk_gb": "Linux 200GB（实测 199G 可用）/ Windows 350GB，含在规格价内",
                "typical_boot_s": "实测 CPU 21s / GPU(2080Ti) 36s 到 running；取 45s 留余量",
                "terminate_lock_s": "建机后约 310s 内退租必然失败（操作频繁#10001）；短作业也要按 6 分钟计生命周期",
            },
        )

    # -------------------------------------------------------------- 视图
    @staticmethod
    def _state(status: Any, is_abnormal: Any = 0) -> str:
        try:
            s = int(status)
        except (TypeError, ValueError):
            return "unknown"
        try:
            ab = int(is_abnormal or 0)
        except (TypeError, ValueError):
            ab = 0
        # 关机态藏在 Status==1 + IsAbnormal==2 里（官方重启接口的前置条件即此）。
        if s == 1 and ab == ABNORMAL_SHUTDOWN:
            return "stopped"
        return STATE_MAP.get(s, "unknown")

    def _view(self, d: dict[str, Any]) -> InstanceView:
        uid = str(d.get("Container_name") or "")
        state = self._state(d.get("Status"), d.get("IsAbnormal"))
        gpu_n = int(d.get("Gpu_num") or 0)
        gpu_type = str(d.get("Gpu_type") or "")
        if gpu_type.upper() == "CPU":
            gpu_type, gpu_n = "", 0
        # 实例详情里没有小时单价字段，只有在建机询价时才知道；成本记账走
        # cost_snapshot（用 billing/get_instance_cost_summary 的真实账单），
        # 这里不给一个猜出来的数字。
        return InstanceView(
            uid=uid,
            state=state,
            name=str(d.get("Note") or uid),
            gpu_type=gpu_type,
            gpu_count=gpu_n,
            cpu=int(d.get("Cpu_num") or 0) + int(d.get("AddCpu") or 0),
            memory_gb=int(d.get("Memory") or 0) + int(d.get("AddMemory") or 0),
            price_per_hour=0.0,
            disk_per_hour=0.0,
            region="",
            zone=str(d.get("District") or ""),
            provider=self.name,
            raw=d,
        )

    # -------------------------------------------------------------- 实例
    def create(self, *, image: str, gpu_type: str, gpu: int = 1, cpu: int = 16,
               memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
               name: str = "job", charge_type: str = "Postpay",
               zone: str | None = None, password: str | None = None,
               remark: str = "", pack_hours: int | None = None,
               bandwidth: int | None = None, ext_uid: str = "",
               idempotency_key: str = "", keep_disk_on_expire: bool = False) -> str:
        """建机，返回 Container_name（后续所有接口的 instance_name）。

        契约字段 → 智星云参数的映射（含做不到的部分，别装作能）：

          image        → image_name（平台镜像 key，例 ubuntu24_nvram_base）
          gpu_type/gpu → gpu_type / gpu_num（0,1,2,4,8）
          cpu/memory   → spec_name：**必须落到某个固定规格**，见 _pick_spec()
          disk_gb      → 忽略（系统盘固定 200GB/350GB，不可调）
          data_disk_gb → add_disk_size
          charge_type  → 只有租期语义：pack_time+unit 固定 hour，
                         Prepay/Postpay 都按 pack_hours 小时预租
          password     → 建机接口不收密码，建完调 change_password 设置
          name         → note（实例备注）
          zone         → district
          keep_disk_on_expire → due_mode（False=-1 到期释放 / True=1 到期保留）

        为什么默认 due_mode=-1（到期释放）：**这是防孤儿实例最硬的一道闸**。
        租期到了机器自动结束，不会因为我们的 reaper 挂了而继续扣钱。
        实测（2026-11-13）：due_mode=-1 的实例提前退租时，即使传
        release_disk=0，磁盘还是被释放（Status→0，DiskReleaseTime 落地）——
        到期处理模式在建机时就把磁盘命运定死了。要真的留盘做实验/换规格，
        建机时就得 keep_disk_on_expire=True。
        """
        if int(gpu) not in (0, 1, 2, 4, 8):
            raise ProviderError(
                f"智星云只提供 0/1/2/4/8 卡规格，收到 {gpu} 卡",
                ProviderError.UNSUPPORTED)
        spec = self._pick_spec(gpu=int(gpu), cpu=int(cpu), memory_gb=int(memory_gb))
        gpu_name = self.canonical_gpu(gpu_type)
        params = {
            "gpu_type": gpu_name,
            "gpu_num": int(gpu),
            "image_name": image,
            "pack_time": int(pack_hours or self.default_pack_hours),
            "unit": "hour",
            # power 优先扣算力券、money 优先扣余额；默认 power（与官方示例一致）
            "pay_type_first": "power",
            "autorenew_on": "false",
            "autorenew_unit": "hour",
            "add_disk_size": int(data_disk_gb or 0),
            "bandwidth": int(bandwidth or self.default_bandwidth),
            # 默认 -1（到期释放）。1 会保留磁盘并按小时计费，见 create() 文档。
            "due_mode": 1 if keep_disk_on_expire else -1,
            "spec_name": spec["SpecName"],
            "note": (name or "job")[:60],
        }
        if zone:
            params["district"] = zone
        if ext_uid:
            params["ext_uid"] = ext_uid
        if idempotency_key:
            # 同一 ext_rid 重复建机Cloud侧会拒绝 —— 这正是我们要的幂等语义。
            params["ext_rid"] = idempotency_key
        data = self.call("store/create_kvm_instance", params, timeout=90)

        uid = _extract_instance_name(data)
        if not uid:
            # 建机已发出但没解析出实例名：这是最危险的分支（可能已扣钱），
            # 先按备注回查一次列表，仍找不到才抛 TRANSIENT 并留下原始响应。
            uid = self._find_by_note(params["note"])
        if not uid:
            raise ProviderError(
                "建机响应里没有 Container_name，且按备注回查不到实例；"
                f"可能已计费，请人工核对 last_raw={self.last_raw!r}",
                ProviderError.TRANSIENT)

        if password:
            try:
                self.change_password(uid, password)
            except ProviderError:
                # 密码没设上不影响机器可用（详情里有初始密码），不吞掉建机结果。
                pass
        return uid

    def get(self, uid: str) -> InstanceView:
        d = self.call("instance/get_instance_detail", {"instance_name": uid})
        if not isinstance(d, dict) or not d:
            raise ProviderError(f"实例 {uid} 详情为空", ProviderError.NOT_FOUND)
        return self._view(d)

    def list_instances(self, prefix: str = "") -> list[InstanceView]:
        out: list[InstanceView] = []
        page = 1
        while True:
            d = self.call("instance/get_instance_list", {
                "page": page, "page_size": 50,
                "status_type": "statusAll", "keyword": prefix or "",
            })
            if not isinstance(d, dict):
                break
            for row in d.get("list") or []:
                if isinstance(row, dict):
                    out.append(self._view(row))
            if not d.get("has_more"):
                break
            page += 1
            if page > 200:                    # 防御：分页异常时不要死循环
                break
        if prefix:
            out = [i for i in out if i.name.startswith(prefix)
                   or i.uid.startswith(prefix)]
        return out

    def start(self, uid: str) -> None:
        """从关机态开机。注意云侧只接受 Status==1 且 IsAbnormal==2（即关机态）。"""
        v = self.get(uid)
        if v.state == "running":
            return
        if v.state in ("terminated", "error"):
            raise ProviderError(
                f"{uid} 当前 {v.state}（Status={v.raw.get('Status')}），"
                "不能启动；保留磁盘请用 create_kvm_instance_from_keepped_disk",
                ProviderError.UNSUPPORTED)
        self.call("instance/restart_instance_from_shutdown", {"instance_name": uid})

    def stop(self, uid: str) -> None:
        """智星云没有「关机保留」接口，只有提前退租。

        刻意不把 stop 实现成 terminate：那会让上层以为「停一下、等下再开」，
        实际机器已经退订、磁盘被释放 —— 这是会直接毁掉作业的语义错误。
        """
        raise ProviderError(
            "智星云（租期制）没有关机接口：要少花钱请用 terminate()（提前退租，"
            "可保留磁盘），要重启请用 restart()",
            ProviderError.UNSUPPORTED)

    def restart(self, uid: str) -> None:
        """重启（云侧实现是关机再开机，约 1-3 分钟，作业会中断）。"""
        self.call("instance/restart_instance", {"instance_name": uid})

    def terminate(self, uid: str, *, release_disk: bool = True,
                  remark: str = "", timeout: int = 420) -> None:
        """提前退租。**退款进算力券账户，不回现金**（实测 2026-11-13）。

        实测账单（建机 ¥0.1668 → 41s 后退租）：
            现金  15.0000 → 14.8332   （扣 0.1668，没退回）
            算力券 0.0000 →  0.1526   （退回 0.1526）
            净损失 0.0142 = 用了 41 秒的钱
        所以「退租后现金没变」是正常的，别以为亏了 —— 券上的钱在下次
        pay_type_first=power 建机时抵扣。这也是 balance() 不把两者相加的原因。

        **硬约束（实测 3 台，2026-11-13）**：建机后约 310 秒内退租**必然失败**，
        云侧一律回 "操作频繁#10001"（BUSY）。三次实测的成功时刻：

            建成后 290s 成功
            建成后 274s 成功
            建成后 312s 成功（60/80/101/…/291s 连续 9 次全失败）

        这不是"偶发抖动"，是建机后的固定冷却窗。所以：
          · 想在 5 分钟内跑完就退租 —— 做不到，必须等
          · 这段时间机器照常计费（CPU 实例约 ¥0.014，GPU 4090 约 ¥0.13）
          · 退租是**唯一能停止烧钱的动作**，重试窗口默认 420s（三次实测都在内）
        """
        params = {
            "instance_name": uid,
            "remark": remark or "provider.terminate",
            "release_disk": "1" if release_disk else "0",
            "ignore_server_offline_error": "1",
        }
        t0 = time.time()
        delay, last = 10, None
        while True:
            try:
                self.call("instance/stop_instance_with_refund", params, timeout=90)
                return
            except ProviderError as e:
                last = e
                # 已经结束的不算失败：幂等调用（reaper 重扫）要能安全重复。
                if "已结束" in str(e) or "无法退费" in str(e):
                    return
                if e.kind not in (ProviderError.BUSY, ProviderError.TRANSIENT):
                    raise
                if time.time() - t0 + delay > timeout:
                    break
                time.sleep(delay)
                delay = min(delay * 1.6, 30)
        raise ProviderError(
            f"{uid} 退租在 {timeout}s 内未放行（最后一次：{last}）。"
            "云侧 BUSY 冷却实测可达 3 分钟；机器可能仍在计费，请复查余额",
            getattr(last, "kind", ProviderError.TRANSIENT))

    def release_kept_disk(self, uid: str) -> None:
        """释放保留磁盘（Status=8）。不释放就一直按小时扣磁盘费。

        实测（2026-11-13）：按**整点小时**扣 ¥0.0431 + 释放时结算尾款 ¥0.0052
        （账单 Type=71 定时扣费 / Type=72 释放结算）。
        """
        self.call("instance/release_keepped_disk", {"instance_name": uid})

    # ---- 自定义镜像（4.0）
    def image_price(self, *, pack_time: int = 1, unit: str = "day",
                    image_gb: int = 200) -> dict:
        """自定义镜像存储询价。**镜像不是一次买断，是按租期付存储费。**

        实测（2026-11-13，真机询价接口）：
            0.0006 元/GB·h × 200GB × 时长 × 会员折扣
            1 小时 ¥0.114 / 1 天 ¥2.736 / 7 天 ¥19.152 / 30 天 ¥82.08

        对比优云智算 ¥0.236/GB·月：智星云折后 ¥0.4104/GB·月，**贵 1.74 倍**。
        所以「做镜像」这件事本身有月固定成本，做之前先算它摊到多少次作业上。
        """
        d = self.call("images/create_image_query_price",
                      {"pack_time": int(pack_time), "unit": unit}) or {}
        sheet = (d or {}).get("PriceInfoSheet") or {}
        item = (d or {}).get("ItemInfoSheet") or {}
        hour_price = float(sheet.get("FinalSingleHourPriceEqual")
                           or sheet.get("SingleHourPrice") or 0)
        return {
            "total": float(sheet.get("FinalCost") or 0),
            "currency": "CNY",
            "unit_price_per_gb_hour": hour_price,
            "per_gb_month": round(hour_price * 24 * 30, 4),
            "image_gb": int(sheet.get("Amount") or image_gb),
            "pack_time": int(pack_time),
            "unit": unit,
            "expire_at": item.get("Due_time_Format"),
            "raw": d,
        }

    def images_custom(self, *, page: int = 1, page_size: int = 10) -> list[dict]:
        """我的自定义镜像列表。"""
        out: list[dict] = []
        while True:
            d = self.call("images/get_user_image_list",
                          {"page": page, "page_size": page_size}) or {}
            rows = d.get("data") if isinstance(d, dict) else None
            for r in rows or []:
                if isinstance(r, dict):
                    out.append(r)
            if not (isinstance(d, dict) and d.get("has_more")):
                break
            page += 1
            if page > 100:
                break
        return out

    def create_image_available(self) -> list[dict]:
        """哪些实例现在可以做镜像。

        实测：运行中的实例**不能**直接做，要先 `suspend_image_instance()`
        （官方文档原话：实例需处于可制作状态（挂起中 / 保留磁盘 / 部分结束状态）；
        运行中需先调用挂起接口）。
        """
        # page_size 上限 10（实测：传 50 云侧回 "page_size参数超限!"）
        d = self.call("images/get_create_available_instance_list",
                      {"page": 1, "page_size": 10}) or {}
        rows = d.get("data") if isinstance(d, dict) else None
        return [r for r in (rows or []) if isinstance(r, dict)]

    def suspend_and_bake(self, uid: str, *, name: str, pack_time: int = 30,
                         unit: str = "day", brief: str = "",
                         ssh: SSHEndpoint | None = None) -> dict:
        """**正确做法**：先 sync 落盘 → 挂起 → 做镜像。

        少了 sync 这一步，镜像里所有「最近写入的文件」都会变成 0 字节
        （实测：文件名/目录/时间戳在，内容空）。ComfyUI 装的源码、.so、
        权重全属于这一类，做出来就是个空壳镜像。

        需要 ssh 端点来进 guest 执行 sync；传 None 时只做挂起+建镜像，
        并在返回值里标注 `synced=False` 提醒调用方。
        """
        synced = False
        if ssh is not None:
            import os as _os
            args = ["sshpass", "-p", ssh.password or "", "ssh",
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "UserKnownHostsFile=/dev/null",
                    "-o", "ConnectTimeout=15", "-p", str(ssh.port),
                    f"{ssh.user}@{ssh.host}",
                    "sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null; sync; echo flushed"]
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=300,
                                   env={**_os.environ, "SSHPASS": ssh.password or ""})
                synced = r.returncode == 0 and "flushed" in (r.stdout or "")
            except Exception:
                synced = False
            if not synced:
                raise ProviderError(
                    f"{uid} 落盘失败，拒绝做镜像 —— 否则镜像里的文件会全是 0 字节",
                    ProviderError.TRANSIENT)
        self.suspend_image_instance(uid)
        time.sleep(5)
        d = self.create_image(uid, name=name, pack_time=pack_time, unit=unit, brief=brief)
        d["synced"] = synced
        return d

    def suspend_image_instance(self, uid: str) -> None:
        """把实例挂起，使其可被制作镜像（运行中 → 挂起）。"""
        self.call("images/instance/suspend", {"instance_name": uid}, timeout=120)

    def resume_image_instance(self, uid: str) -> None:
        """恢复被挂起的实例。"""
        self.call("images/instance/resume", {"instance_name": uid}, timeout=120)

    def create_image(self, uid: str, *, name: str, pack_time: int = 30,
                     unit: str = "day", brief: str = "") -> dict:
        """从实例创建自定义镜像（**会扣费**：按 pack_time 预扣存储费）。

        实测（2026-11-13）两个坑：
          ① 参数名是 **`short_display_name`**（镜像展示名称），不是 image_name。
             猜 image_name / name / ImageName 全部回 "镜像名称不能为空!"，
             而文档里这个字段只写在 requestBody 的第 2 项，很容易漏。
          ② 运行中的实例**不能**直接做镜像，必须先 `suspend_image_instance()`
             （挂起后 IsAbnormal 变 3），否则实例不出现在可制作列表里。

        价目：200GB × 30 天 = ¥82.08（会员折扣后），按租期预扣，不是买断。

        **③ 调用前必须 sync 落盘，否则镜像里的文件全是 0 字节。**
        实测（2026-11-13）第一次做镜像时只 suspend 没 sync，结果新实例上：
            文件名在、目录结构在、时间戳在，但内容 0 字节
        第二次在 guest 里跑 `sync; echo 3 > /proc/sys/vm/drop_caches; sync`
        后再做，8MB blob 的 md5 与源机完全一致 —— 证明是 page cache 没落盘，
        不是镜像机制坏。provider 侧无法替调用方 sync（要进 guest 执行），
        所以这里只负责把话说明白，调用方用 suspend_and_bake() 一次做对。
        """
        params: dict[str, Any] = {
            "instance_name": uid,
            "short_display_name": name,
            "pack_time": int(pack_time),
            "unit": unit,
        }
        if brief:
            params["brief"] = brief
        d = self.call("images/create_image_from_instance", params, timeout=180)
        return d if isinstance(d, dict) else {"raw": d}

    def renew_image(self, image_name: str, *, pack_time: int = 30,
                    unit: str = "day") -> dict:
        """续费自定义镜像。到期不续费会被释放。"""
        d = self.call("images/renew_image", {
            "image_name": image_name, "pack_time": int(pack_time),
            "unit": unit, "pay_type_first": "power"}, timeout=120)
        return d if isinstance(d, dict) else {"raw": d}

    def delete_image(self, image_name: str) -> dict:
        """提前退租并删除镜像（按剩余时长退费）。"""
        d = self.call("images/image_refund_and_delete",
                      {"image_name": image_name}, timeout=120)
        return d if isinstance(d, dict) else {"raw": d}

    def image_cost(self, image_name: str) -> dict:
        """镜像已产生的扣费明细。"""
        d = self.call("images/get_image_cost_list", {
            "image_name": image_name, "page": 1, "page_size": 20}) or {}
        return d if isinstance(d, dict) else {"raw": d}

    # ---- 留盘停放（智星云版的"停机保状态"）
    def suspend_to_disk(self, uid: str, *, remark: str = "",
                        timeout: int = 500) -> str:
        """把实例退租但**保留磁盘** —— 智星云上唯一「花小钱留着环境」的办法。

        为什么需要它（真机实测 2026-11-13）：

            运行中       ¥0.1668/h
            关机态       ¥0.1668/h   ← 租期照烧，等于白停
            留盘停放     ¥0.0431/h   ← 只付磁盘钱，约运行的 1/4

        恢复用 `resume_from_disk()`，实测磁盘上的文件原样还在
        （写了 MARKER 时间戳，恢复后读回同一串）。

        返回：恢复时要用的 instance_name（**不是**传进来的 uid）。
        实测云侧不复活旧实例，而是**新建一台** `_c0` → `_c1` 的新实例，
        旧的变成 Status=0。所以调用方必须存下返回值，否则找不到机器。
        """
        self.terminate(uid, release_disk=False,
                       remark=remark or "provider.suspend_to_disk",
                       timeout=timeout)
        return self.find_kept_disk(uid) or uid

    def find_kept_disk(self, uid_or_prefix: str) -> str:
        """在实例列表里找 Status=8（保留磁盘）的实例，用于 suspend 后定位。

        云侧恢复后 Container_name 会递增（_c0 → _c1），旧名失效，
        所以按 `ContainerStartId`（去掉 _cN 后缀的部分）匹配最稳。
        """
        stem = uid_or_prefix.rsplit("_c", 1)[0]
        for v in self.list_instances():
            if v.state != "terminated":
                continue
            try:
                if int(v.raw.get("Status")) != 8:
                    continue
            except (TypeError, ValueError):
                continue
            if str(v.uid).startswith(stem):
                return v.uid
        return ""

    def resume_from_disk(self, uid: str, *, pack_hours: int = 1,
                         spec_name: str | None = None,
                         image: str | None = None,
                         gpu_type: str | None = None,
                         gpu: int | None = None,
                         zone: str | None = None,
                         timeout: int = 300) -> str:
        """从保留磁盘恢复实例，返回**新**的 instance_name。

        实测要点：
          ① 云侧新建一台实例（`_cN` 递增），旧名不再存在 —— 必须用返回的新名。
          ② 磁盘内容原样保留（已验证标记文件可读回）。
          ③ 价格与新建相同（¥0.1668/h），恢复本身不额外收费。
          ④ 恢复后 Status 已经是 1(running)，基本无需等待。
        """
        kept = self.get(uid)
        raw = kept.raw
        if int(raw.get("Status") or 0) != 8:
            raise ProviderError(
                f"{uid} 不是保留磁盘状态（Status={raw.get('Status')}），"
                "无法从磁盘恢复；只有 Status=8 的实例可以",
                ProviderError.UNSUPPORTED)
        spec = spec_name or str(raw.get("SpecName") or "")
        if not spec:
            spec = self._pick_spec(
                gpu=int(gpu if gpu is not None else (raw.get("Gpu_num") or 0)),
                cpu=int(raw.get("Cpu_num") or 2),
                memory_gb=int(raw.get("Memory") or 2))["SpecName"]
        gname = gpu_type or str(raw.get("Gpu_type") or "CPU")
        params = {
            "instance_name": uid,
            "gpu_type": self.canonical_gpu(gname),
            "gpu_num": int(gpu if gpu is not None else (raw.get("Gpu_num") or 0)),
            "image_name": image or str(raw.get("Image") or ""),
            "spec_name": spec,
            "pack_time": int(pack_hours),
            "unit": "hour",
            "due_mode": 1,               # 恢复后仍是留盘模式，方便再次停放
            "pay_type_first": "power",
            "autorenew_on": "false",
            "autorenew_unit": "hour",
            "add_disk_size": 0,
            "bandwidth": int(self.default_bandwidth),
        }
        if zone:
            params["district"] = zone
        data = self.call("instance/create_kvm_instance_from_keepped_disk",
                         params, timeout=120)
        new_uid = ""
        if isinstance(data, dict):
            new_uid = str(data.get("Container_name") or "")
        if not new_uid:
            new_uid = _extract_instance_name(data)
        if not new_uid:
            raise ProviderError(
                "从保留磁盘恢复未返回 Container_name；可能已扣费，请人工核对",
                ProviderError.TRANSIENT)
        # 实测：恢复接口返回的 Status 通常已经是 1(running)。只有它明确还在
        # 启动中（4）时才值得轮询 —— 否则白等 3 分钟，测试也会被拖慢。
        got_status = data.get("Status") if isinstance(data, dict) else None
        if got_status is not None and str(got_status) != "1":
            try:
                self.wait_state(new_uid, {"running"}, timeout=min(timeout, 180))
            except ProviderError:
                pass
        return new_uid


    def change_password(self, uid: str, password: str) -> None:
        """改访问密码。参数名是 `new_password`，不是 `password`。

        实测（2026-11-13）：传 `password` 云侧回 "新密码不能为空!" —— 文档
        api-244924930 里写的是 `new_password`，字段名对不上时错误文案完全
        指向不了根因。
        """
        self.call("instance/change_password",
                  {"instance_name": uid, "new_password": password})

    # -------------------------------------------------------------- 连接
    def ssh_endpoint(self, uid: str) -> SSHEndpoint:
        d = self.call("instance/get_instance_detail", {"instance_name": uid})
        if not isinstance(d, dict) or not d:
            raise ProviderError(f"实例 {uid} 不存在", ProviderError.NOT_FOUND)
        host = str(d.get("Url") or "")
        port = str(d.get("SshPort") or "22")
        pwd = d.get("Init_passwd") or d.get("LastInitPasswd") or None
        img = str(d.get("Image") or "")
        user = "vipuser" if "win" in img.lower() else str(
            d.get("LoginUserName") or "root")
        if not host:
            raise ProviderError(f"{uid} 还没有公网地址（Status={d.get('Status')}）",
                                ProviderError.TRANSIENT)
        return SSHEndpoint(host=host, port=port,
                           password=str(pwd) if pwd else None, user=user)

    def ssh_ready(self, uid: str, *, timeout: int = 180) -> SSHEndpoint:
        """等 sshd 真能收命令。Running ≠ sshd 就绪，必须探活（照智云的教训）。"""
        t0 = time.time()
        ep = self.ssh_endpoint(uid)
        while time.time() - t0 < timeout:
            if _ssh_probe(ep):
                return ep
            time.sleep(5)
            try:
                ep = self.ssh_endpoint(uid)   # 地址/端口可能在启动过程中才就绪
            except ProviderError:
                pass
        raise ProviderError(f"{uid} sshd {timeout}s 内未就绪（{ep.host}:{ep.port}）",
                            ProviderError.TRANSIENT)

    def scheduled_stop(self, uid: str, at_epoch: int) -> None:
        raise ProviderError(
            "智星云没有定时关机接口（只有到期时间与自动续费）；"
            "防孤儿实例请用本地 reaper + terminate()",
            ProviderError.UNSUPPORTED)

    # -------------------------------------------------------------- 计费
    def price(self, *, gpu_type: str, gpu: int = 1, cpu: int = 16,
              memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
              charge_type: str = "Postpay", image: str = "",
              zone: str | None = None) -> dict:
        """实时询价（和建机同参）。返回**折后**小时价，原价与折扣放 raw。

        两处实测坑，这里都堵掉了：
          1. gpu_type 必须是云侧规范名（"4090" → "GeForce RTX 4090"），
             否则云侧只回 "价格没有获取到（4000）"，看不出是名字的问题。
          2. image_name 必须是该卡 ValidImages 里的一个，且优先 Linux 基础镜像。
        """
        spec = self._pick_spec(gpu=int(gpu), cpu=int(cpu), memory_gb=int(memory_gb))
        gpu_name = self.canonical_gpu(gpu_type)
        params = {
            "gpu_type": gpu_name,
            "gpu_num": int(gpu),
            "image_name": image or self._image_for(gpu_name, gpu=int(gpu)),
            "pack_time": 1, "unit": "hour",
            "due_mode": -1, "pay_type_first": "power",
            "autorenew_on": "false", "autorenew_unit": "hour",
            "add_disk_size": int(data_disk_gb or 0),
            "bandwidth": int(self.default_bandwidth),
            "spec_name": spec["SpecName"],
        }
        if zone:
            params["district"] = zone
        data = self.call("store/create_kvm_instance_query_price", params) or {}
        sheet = (data or {}).get("PackPriceInfoSheet") or {}
        inst = disk = 0.0
        for it in sheet.get("InstanceSubPriceSheet") or []:
            kind = str(it.get("ResourceType") or "")
            cost = float(it.get("FinalCost") or 0)          # 已含会员/时长折扣
            if kind in ("gpu", "spec", "cpu", "memory"):
                inst += cost
            elif kind in ("disk", "add_disk"):
                disk += cost
        inst, disk = round(inst, 4), round(disk, 4)
        return {
            "hourly_instance": inst,
            "hourly_disk": disk,
            "hourly_total": round(inst + disk, 4),
            "currency": "CNY",
            "charge_type": charge_type,
            "spec_name": spec["SpecName"],
            "discount": float(sheet.get("FinalDiscount") or 1.0),
            "note": "折后价（会员折扣已计入）；租期制实际按 pack_time 小时预扣",
            "raw": data,
        }

    def balance(self) -> float:
        """现金余额（Money）。算力券余额（PowerMoney）另见 balance_detail()。

        刻意不把两者相加：扣费顺序由 pay_type_first 决定，相加会掩盖
        「现金见底、券还有」这种真实状态，导致上层误判为有钱。
        """
        d = self.call("account/get_main_account_info") or {}
        self.last_account = dict(d) if isinstance(d, dict) else {}
        return float(self.last_account.get("Money") or 0.0)

    def balance_detail(self) -> dict:
        d = self.call("account/get_main_account_info") or {}
        self.last_account = dict(d) if isinstance(d, dict) else {}
        money = float(self.last_account.get("Money") or 0.0)
        power = float(self.last_account.get("PowerMoney") or 0.0)
        return {"money": round(money, 4), "power": round(power, 4),
                "total": round(money + power, 4),
                "vip_level": self.last_account.get("VipLevel"),
                "custom_discount": self.last_account.get("CustomDiscount"),
                "raw": self.last_account}

    # -------------------------------------------------------------- 镜像
    def resolve_image(self, spec: str) -> dict:
        """镜像 spec → {image_id, name, ...}。找不到抛 NOT_FOUND。

        智星云镜像 key 就是 image_name（例 ubuntu24_nvram_base），先精确匹配，
        再按显示名/包含匹配兜底。只认 kvm 类型：docker 镜像不能建云主机。
        """
        key = (spec or "").strip()
        if not key:
            raise ProviderError("镜像 spec 不能为空", ProviderError.NOT_FOUND)
        all_imgs = self._images_raw()
        hit = all_imgs.get(key)
        if hit is None:
            # 模糊匹配时**跳过 docker 镜像**：它们的 key 里也常带 ubuntu/cuda，
            # 先命中 docker 会把一个合法的 kvm 请求误判成"镜像类型不对"。
            # 精确给出 docker 镜像名时仍然如实报 UNSUPPORTED（下面那段）。
            #
            # 命中多个时按 _image_score 排序取最好的一档：写 "ubuntu" 应落到
            # ubuntu24_nvram_base，而不是字母序靠前的某个琥珀/GROMACS 专用镜像。
            low = key.lower()
            cands: list[tuple[float, str, dict]] = []
            for k, v in all_imgs.items():
                if str(v.get("Type") or "").lower() != "kvm":
                    continue
                if v.get("ShowInRent") is False:
                    continue
                names = [k.lower(), str(v.get("ShortDisplayName") or "").lower(),
                         str(v.get("LongDisplayName") or "").lower()]
                if any(low == n or low in n for n in names):
                    cands.append((_image_score(k, low), k, v))
            if cands:
                cands.sort(key=lambda t: (-t[0], len(t[1])))
                _, key, hit = cands[0]
        if hit is None:
            raise ProviderError(f"智星云没有镜像 {spec!r}", ProviderError.NOT_FOUND)
        if str(hit.get("Type") or "").lower() == "docker":
            raise ProviderError(
                f"镜像 {key!r} 是 docker 容器镜像，不能用于 create_kvm_instance"
                "（云主机只认 kvm 镜像）", ProviderError.UNSUPPORTED)
        return {"image_id": str(hit.get("ImageValue") or key),
                "name": str(hit.get("ShortDisplayName") or key),
                "system": str(hit.get("System") or ""),
                "size_gb": int(hit.get("ImageSize") or 0),
                "raw": hit}

    def images(self, keyword: str = "") -> list[dict]:
        out = []
        low = (keyword or "").lower()
        for key, v in sorted(self._images_raw().items()):
            if str(v.get("Type") or "").lower() != "kvm":
                continue
            if v.get("ShowInRent") is False:
                continue
            name = str(v.get("ShortDisplayName") or key)
            if low and low not in key.lower() and low not in name.lower():
                continue
            out.append({"image_id": key, "name": name,
                        "type": "kvm",
                        "system": str(v.get("System") or ""),
                        "size_gb": int(v.get("ImageSize") or 0),
                        "raw": v})
        return out

    def _images_raw(self) -> dict[str, dict]:
        if self._images_cache and time.time() - self._images_cache[0] < 600:
            return dict(self._images_cache[1])
        d = self.call("store/get_configs") or {}
        m = (d or {}).get("ImageDisplayInfoMap") or {}
        self._images_cache = (time.time(), dict(m))
        return dict(m)

    def _default_image_name(self) -> str:
        """兜底镜像：没有任何可用信息时取第一个可租的 kvm 镜像。"""
        for img in self.images():
            return img["image_id"]
        raise ProviderError("智星云没有可用的 kvm 镜像", ProviderError.OUT_OF_STOCK)

    def _image_for(self, gpu_type: str, *, gpu: int = 1) -> str:
        """给某张卡挑一个它真的能租的 Linux 镜像。

        实测坑（2026-11-13）：`create_kvm_instance_query_price` 传一个 kvm 镜像
        但该卡不支持它时，云侧只回 "价格没有获取到（4000）"，看不出是镜像的锅。
        所以这里先查 selector 的 ValidImages，优先挑 ubuntu/rocky/centos 这类
        Linux 基础镜像（Windows 镜像要额外的 RDP 授权，不适合批量作业）。
        """
        try:
            d = self.stock_detail(gpu_type)
        except ProviderError:
            d = {}
        valid = [str(x) for x in (d.get("images") or [])]
        for pref in ("ubuntu24_nvram_base", "ubuntu22_nvram_base",
                     "ubuntu20_nvram_base", "ubuntu24_cuda13.0"):
            if pref in valid:
                return pref
        for x in valid:
            low = x.lower()
            if low.startswith(("ubuntu", "rocky", "centos", "deepin")) and "win" not in low:
                return x
        if valid:
            return valid[0]
        return self._default_image_name()

    # -------------------------------------------------------------- 库存
    def canonical_gpu(self, gpu_type: str) -> str:
        """上层写的卡名 → 云侧规范名（"4090" → "GeForce RTX 4090"）。

        实测（2026-11-13）：`create_kvm_instance_query_price` 传 "4090" 会直接回
        "价格没有获取到（4000）"，而传 "GeForce RTX 4090" 正常；`create_kvm_instance`
        同理。所以任何发往云侧的 gpu_type 都必须先过这里。

        对不上就**原样返回**（不猜）：让云侧报错，比擅自换一张卡便宜。
        任何查询失败也原样返回 —— 询价/建机不能因为「改名辅助失败」而中断。
        """
        raw = (gpu_type or "").strip()
        if not raw or raw.upper() == "CPU":
            return "CPU"
        try:
            gmap = self._gpu_map()
        except Exception:
            return raw
        return self._match_gpu(raw, gmap) or raw

    def _gpu_map(self) -> dict[str, Any]:
        """可租显卡表，5 分钟缓存：建机/询价都要用，不该每次都回源。"""
        if self._gmap_cache and time.time() - self._gmap_cache[0] < 300:
            return dict(self._gmap_cache[1])
        d = self.call("store/get_kvm_available_selector") or {}
        gmap = (d or {}).get("gpu_resource_map") or {}
        self._gmap_refreshed = (d or {}).get("last_refresh_time")
        self._gmap_cache = (time.time(), dict(gmap))
        return dict(gmap)

    def stock(self, gpu_type: str, *, spot: bool = False, zone: str | None = None) -> int:
        """可租卡档数量（0/1/2/4/8 里有多少档可租）。0 = 当前无货。

        契约只给了 gpu_type，而智星云真正的库存判定接口
        （query_kvm_dispatch_status）还要 image_name+spec_name，故用
        get_kvm_available_selector 的 ValidCardsRentable 汇总。
        """
        detail = self.stock_detail(gpu_type, zone=zone)
        return int(detail["rentable_tiers"])

    def stock_detail(self, gpu_type: str, *, zone: str | None = None) -> dict:
        gmap = self._gpu_map()
        key = self._match_gpu(gpu_type, gmap)
        if key is None:
            return {"gpu_type": gpu_type, "rentable_tiers": 0,
                    "cards": {}, "image": None, "districts": [],
                    "known_types": sorted(gmap)}
        g = gmap[key] or {}
        rentable = {str(k): bool(v)
                    for k, v in (g.get("ValidCardsRentable") or {}).items()}
        tiers = sum(1 for v in rentable.values() if v)
        return {"gpu_type": key,
                "rentable_tiers": tiers,
                "cards": rentable,
                "districts": list(g.get("ValidDistricts") or []),
                "images": list(g.get("ValidImages") or []),
                "refreshed_at": self._gmap_refreshed}

    def _match_gpu(self, gpu_type: str, gmap: dict[str, Any]) -> str | None:
        """把 4090 / RTX 4090 / GeForce RTX 4090 这类写法都匹配上。

        匹配优先级（顺序很重要，否则 4090 会被 4090PLUS 抢走）：
          1. 原样精确
          2. 归一化后精确（去品牌词/空格）
          3. 归一化后前缀匹配，取最短的那档（4090 优先于 4090PLUS）
          4. 唯一包含匹配
        仍然有歧义就返回 None —— 宁可不猜，也不把 4090 的单调到 4090PLUS（更贵）。
        """
        if not gpu_type:
            return None
        low = gpu_type.strip().lower()
        for k in gmap:
            if k.lower() == low:
                return k

        def norm(s: str) -> str:
            s = s.lower()
            for w in ("geforce", "nvidia", "tesla", "rtx ", "gtx ", " "):
                s = s.replace(w, "")
            return s.strip()

        n = norm(low)
        if not n:
            return None
        exact = [k for k in gmap if norm(k) == n]
        if exact:
            return exact[0]
        prefix = sorted((k for k in gmap if norm(k).startswith(n)), key=lambda k: len(k))
        if prefix:
            return prefix[0]
        contains = [k for k in gmap if n in norm(k)]
        return contains[0] if len(contains) == 1 else None

    # -------------------------------------------------------------- 规格
    def specifications(self, gpu: int | None = None) -> list[dict]:
        """实例规格表（cpu/memory/价格），带 5 分钟缓存。

        缓存键必须带 GpuNum：云侧按 GpuNum 过滤，传 0 只回 CPU 规格、传 1 只回
        单卡规格。若只用时间做键，先查 1 卡会把 CPU 规格查空（实测踩过）。
        """
        key = "all" if gpu is None else str(int(gpu))
        hit = self._specs_cache.get(key)
        if not hit or time.time() - hit[0] > 300:
            params = {} if gpu is None else {"GpuNum": str(int(gpu))}
            d = self.call("store/get_instance_specifications", params) or {}
            specs = list((d or {}).get("Specifications") or [])
            hit = (time.time(), specs)
            self._specs_cache[key] = hit
        specs = hit[1]
        if gpu is None:
            return list(specs)
        return [s for s in specs if int(s.get("GpuNum") or 0) == int(gpu)]

    def _pick_spec(self, *, gpu: int, cpu: int, memory_gb: int) -> dict:
        """把契约的 (gpu, cpu, memory) 落到一个真实 SpecName。

        规则：同卡数、未禁用、CPU≥要求、内存≥要求，取**最便宜**的一档；
        都不满足就抛 UNSUPPORTED 并列出最近的三档，别让上层去猜。
        """
        rows = [s for s in self.specifications(gpu)
                if not s.get("Disabled") and s.get("SpecName")]
        ok = [s for s in rows
              if int(s.get("CpuNum") or 0) >= int(cpu)
              and int(s.get("Memory") or 0) >= int(memory_gb)]
        if ok:
            return min(ok, key=lambda s: (float(s.get("HourPrice") or 0),
                                          int(s.get("CpuNum") or 0)))
        near = sorted(rows, key=lambda s: (int(s.get("CpuNum") or 0),
                                           int(s.get("Memory") or 0)))[:3]
        raise ProviderError(
            f"智星云没有满足 {gpu}卡/{cpu}核/{memory_gb}GB 的实例规格；"
            f"可选近邻：{[(s.get('SpecName'), s.get('CpuNum'), s.get('Memory')) for s in near]}",
            ProviderError.UNSUPPORTED)

    # -------------------------------------------------------------- 内部
    def _find_by_note(self, note: str) -> str:
        """建机响应缺 Container_name 时，按备注回查最新一台（防孤儿实例）。"""
        try:
            rows = self.call("instance/get_instance_list", {
                "page": 1, "page_size": 20,
                "status_type": "statusAll", "keyword": note})
        except ProviderError:
            return ""
        items = [r for r in ((rows or {}).get("list") or []) if isinstance(r, dict)]
        items = [r for r in items if str(r.get("Note") or "") == note]
        if not items:
            return ""
        items.sort(key=lambda r: int(r.get("Ctime") or 0), reverse=True)
        return str(items[0].get("Container_name") or "")


# ------------------------------------------------------------------ 工具
def _normalize_cfg(cfg: dict[str, str]) -> dict[str, str]:
    """把 BYOK 的多种写法归一成 ZXY_API_KEY / ZXY_API_SECRET。"""
    alias = {"apikey": "ZXY_API_KEY", "api_key": "ZXY_API_KEY",
             "api-key": "ZXY_API_KEY", "accesskey": "ZXY_API_KEY",
             "apisecret": "ZXY_API_SECRET", "api_secret": "ZXY_API_SECRET",
             "api-secret": "ZXY_API_SECRET", "secret": "ZXY_API_SECRET",
             "secretkey": "ZXY_API_SECRET", "sk": "ZXY_API_SECRET"}
    out: dict[str, str] = {}
    for k, v in (cfg or {}).items():
        key = alias.get(str(k).strip().lower(), str(k).strip())
        if v is not None:
            out[key] = str(v)
    return out


def _image_score(key: str, query: str) -> float:
    """模糊匹配镜像时的排序分数：越"基础/通用/新"越高。

    实测查询 "ubuntu" 会同时命中 ubuntu24_nvram_base、ubuntu18_amber18gromacs…
    这类专用镜像。给「nvram_base（裸系统）」和较新版本加权，因为批量作业要的是
    干净系统盘，而不是预装了 Amber/GROMACS 的大镜像。
    """
    k = key.lower()
    score = 0.0
    if "nvram_base" in k:
        score += 6            # 裸系统镜像，首选
    if k.endswith("_base"):
        score += 4
    if "cuda" in k:
        score += 1
    for bad in ("amber", "gromacs", "matlab", "vasp", "win", "toonflow",
                "digitalhuman", "multifracs", "minmax", "lingbot", "openclaw"):
        if bad in k:
            score -= 8
    # 版本越新越好：ubuntu24 优先于 ubuntu22、centos_stream9 优先于 centos7
    m = re.search(r"(?:ubuntu|rocky|centos|deepin)[^0-9]{0,4}(\d+)", k)
    if m:
        score += min(int(m.group(1)), 40) / 10.0
    return score


def _extract_instance_name(data: Any) -> str:
    """从建机响应里取实例名。响应形状官方未给全，按已知三种兜住。"""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for k in ("Container_name", "container_name", "instance_name",
                  "InstanceName", "Instance_name"):
            if data.get(k):
                return str(data[k])
        for v in data.values():            # 有的版本套了一层
            got = _extract_instance_name(v)
            if got:
                return got
        return ""
    if isinstance(data, (list, tuple)):
        for item in data:
            got = _extract_instance_name(item)
            if got:
                return got
    return ""


def _ssh_probe(ep: SSHEndpoint) -> bool:
    """一次 ssh 探活；没有 sshpass 时退化为 TCP 连通性检查。"""
    try:
        port = int(ep.port)
    except (TypeError, ValueError):
        port = 22
    if shutil.which("sshpass") and ep.password:
        cmd = ["sshpass", "-p", ep.password, "ssh",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=8", "-p", str(port), f"{ep.user}@{ep.host}",
               "echo ssh-ready"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
            return r.returncode == 0 and "ssh-ready" in (r.stdout or "")
        except Exception:
            return False
    try:
        with socket.create_connection((ep.host, port), timeout=5):
            return True
    except OSError:
        return False


def from_env() -> ZhiXingYunProvider:
    """按环境变量 / .zxy/credentials.env 建一个默认 provider。"""
    return ZhiXingYunProvider()
