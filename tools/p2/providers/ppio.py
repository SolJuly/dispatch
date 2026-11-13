#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PPIO 派欧云适配器：GPU 容器 API v2。

## 为什么是这家

AutoDL 那条线（`feat/l5-autodl`）调研完发现 5 处硬缺口：CPU/内存不可选、无数据盘、
无定时关机、**无 Pro 库存、无价格目录**、只能平台镜像（不能 `docker pull`）。
其中「无价格、无库存」是致命的 —— `run.py` 的预算闸门和 `_pick_gpu` 的选卡都靠这两项，
接一家查不到价也算不出货的云，等于把已经写好的钱闸关掉。

PPIO 的 v2 API 是唯一一个把 **price + stock + OCI + 自定义端口** 四项都给全的：
`GET /gpus/v2/products` 一个响应里同时带 `pricing.postpaid.final_price`、
`resource_spec.gpu.available`、`inventory_state`、`deployable`。

详见 `reports/l5/国内GPU平台选型-20261113.md`。

## 与智云/AutoDL 的三个关键差异

1. **它用正经 HTTP 状态码**（401/403/400/429/500 + `{code, reason, message}`），
   不像 AutoDL「永远 200 + code 字符串」。所以分类可以按状态码走，比文案匹配可靠。
2. **金额固定 ÷1e5**：`final_price: 231000` = ¥2.31/时。
   ⚠️ 官方 spec 说除数是响应里的 `pricing.precision`，**实测该字段不可用**
   （同一批产品里 4090 给 10、L40S 给 0，而 storage 单价都是 1000）。
   三条独立证据锁定 1e5，详见 `_MONEY_SCALE` 的注释。
3. **创建不是单一大参数**：CPU/内存由「产品规格」决定（4090 有 8c62g / 16c62g /
   18c62g / 16c125g 等多个 SKU），所以要按 `gpu + cpu + memory` 三元组挑 SKU，
   而不是像智云那样直接把 cpu/memory 塞进建机参数。

## 真机实测结论（2026-11-13 21:53~22:02，实例 9c109a3b31cf9b03，花费 ¥0.04）

- **状态枚举**：`running` / `exited`（停机后是 `exited`，不是 `stopped`）/ `starting`。
  见 `_STATE_MAP` 里的注释 —— 原先漏了 `exited`，会让 state 掉进 unknown。
- **耗时**：create → running 4s；stop → start 3s。
- **停机后能重新开机**（官方说「可能无法正常启动」，本次实测成功）。
- **stop→start 后 SSH 端口与密码不变**（`36221` / 同密码）。
- **计费**：不是实时扣，是整点 + 关机时扣。运行 90s 余额不动，整点掉 ¥0.04。
- **SSH 连不上**：`kex_exchange_identification: Connection reset`。
  官方 2026-09-24 公告：TCP 端口映射（含 SSH）必须提交**访问源 IP 白名单**。
  这是平台策略，不是适配器问题 —— 但意味着 `ssh_ready()` 在配白名单前必然失败。
- **建机必须显式给 `candidate_regions`**：不传会得到
  `400 VALIDATOR_PARAM: the region is not active`。不是「平台自动调度」。

**路径存在性在无凭证下无法探测**：PPIO 网关在路由之前就做鉴权，随便编一个
`/gpus/v2/zzz-nonexistent` 也返回 401 `UNAUTHORIZED`（实测 2026-11-13）。
所以本文件的每个 uri 都标了出处文档，改动前先对文档，别靠探测。
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .base import (
    Capabilities, InstanceView, Provider, ProviderError, SSHEndpoint,
)

BASE_URL = "https://api.ppio.com"

# ------------------------------------------------------------------ 云侧状态映射
# ⚠️ PENDING：官方没给枚举，只有 `running` 一个例子。这里按「控制台文案 → 通用状态」
# 写映射，未知值一律落 unknown 并原样留在 raw 里 —— 抛异常会让 reaper 把在跑的机器
# 当异常处理，那是最贵的一类错误。
_STATE_MAP = {
    # ⚠️ 实测（2026-11-13 21:55，真机）：停机后的状态是 **`exited`**，
    # 不是文档里暗示的 `stopped`。没这一条时 state 会掉进 unknown，
    # 上层 reaper 会把一台正常的停机实例当成异常 —— 这正是「未知状态保留而不抛」
    # 设计要兜住的情况，但兜住不等于正确。
    "exited": "stopped",
    "creating": "starting",
    "pending": "starting",
    "starting": "starting",
    "scheduling": "starting",
    "pulling": "starting",
    "running": "running",
    "stopping": "stopping",
    "stopped": "stopped",
    "restarting": "rebooting",
    "rebooting": "rebooting",
    "upgrading": "rebooting",
    "migrating": "rebooting",
    "deleting": "terminating",
    "deleted": "terminated",
    "releasing": "terminating",
    "released": "terminated",
    "failed": "unknown",
    "error": "unknown",
}

# 计费方式：我们的 charge_type ↔ PPIO billing.mode
_BILLING_MAP = {
    "postpay": "postpaid", "postpaid": "postpaid", "on_demand": "postpaid",
    "spot": "spot", "preemptible": "spot", "竞价": "spot",
    "prepay": "prepaid", "prepaid": "prepaid", "包月": "prepaid",
}

# 系统盘超出免费额度的日单价（官方计费页：超出 130GB 后 0.01 元/GB/天）
_ROOTFS_FREE_GB = 130
_ROOTFS_YUAN_PER_GB_DAY = 0.01

_UA = "zhiyun-p2-ppio/1.0"


# 型号名里的「噪音词」：厂商前缀 + 容量后缀。
#
# 实测 PPIO **同一个型号有两套写法**，出现在不同接口里：
#   products → `RTX 4090 24GB`、`NVIDIA L40S 48GB`、`H100 SXM 80GB`
#   regions  → `NVIDIA GeForce RTX 4090 24GB`、`RTX 4090 24GB`、`A100 SXM 80GB`
# 不剥掉这些词，`stock("NVIDIA GeForce RTX 4090 24GB")` 会 NOT_FOUND，
# 而这个字符串是**官方接口原样返回的** —— 上层照抄就会踩。
_VENDOR_WORDS = ("nvidia", "geforce", "amd", "企业级")


def _norm(s: str) -> str:
    """归一化型号名：小写 + 去非字母数字（保留字母数字，其它全删）。

    `RTX 4090` / `rtx-4090` / `4090.16c62g` 都能对上同一个产品。
    """
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def _norm_model(s: str) -> str:
    """型号名归一化：在 `_norm` 之上再剥掉厂商前缀与「SXM/Ada/Generation」这类代号。

    `NVIDIA GeForce RTX 4090 24GB` 与 `RTX 4090 24GB` → 同一个 key。
    显存容量**保留**（`24GB` vs `48GB` 是不同产品，不能混）。
    """
    t = str(s or "").lower()
    for w in _VENDOR_WORDS:
        t = t.replace(w, " ")
    for w in ("sxm", "generation", "ada", "pcie", "nvlink"):
        t = t.replace(w, " ")
    return re.sub(r"[^a-z0-9]", "", t)


# 金额除数：⚠️ **实测推翻了文档**，固定 1e5。
#
# 官方 OpenAPI spec 说价格是「整数 × `pricing.precision`」，我第一版就是照它写的
# （`_money(raw, precision)`）。2026-11-13 拿真 key 打 `/gpus/v2/products`，
# `precision` 字段一测就废：同一批产品里 4090 给 `precision: 10`、L40S 给 `precision: 0`，
# 而两者的 `storage_price_per_gb` 都是 1000。
#
# 按 precision 算会得到两个荒谬结果：
#   4090  231000 / 10**10 = 0.0000231 元/时
#   L40S  396000 / 10**0  = 396000    元/时
#
# 除数是 1e5，三条互相独立的证据：
#   ① storage_price_per_gb=1000 → 1000/1e5 = 0.01，正是官方计费页的「0.01 元/GB/天」
#   ② 4090 spot=116000 → 1.16 元/时，与第三方监测（rentgpu.link）的「4090 Spot ¥1.16/h」一致
#   ③ 4090 按量=231000 → 2.31 元/时；包月 12 月 137182000/1e5/720h = 1.905 元/时，
#      包月比按量便宜 17%，符合「长租有折扣」的常识
#
# 所以不用 precision 字段，固定 1e5。**这是实测值，不是可配置项** ——
# 写进 response 里的 precision 已经证明不可信，再让它影响算账只会错得更隐蔽。
_MONEY_SCALE = 100000.0


def _money(raw: Any, precision: Any = None) -> float:
    """金额整数 → 元。除数固定 1e5（见 `_MONEY_SCALE` 的证据链）。

    `precision` 参数保留只为兼容旧调用点，**不再参与计算** ——
    它是错的，用它算等于把 ¥2.31 报成两个数量级之外的数。

    做一次合理性夹取：PPIO 的实例价不可能超过 ¥200/时（最贵的 H100 SXM 是 ¥23.76），
    算出来超出这个范围说明口径又变了，返回 0 让上层按「未知价格」处理，
    而不是把一个明显错的数字塞进成本账。
    """
    try:
        v = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    yuan = v / _MONEY_SCALE
    if yuan < 0 or yuan > 200:
        return 0.0
    return round(yuan, 6)


def _items(payload: Any) -> list[dict]:
    """列表接口的两种形状：`{"data": [...]}`（products/images/list）和裸数组。"""
    if isinstance(payload, dict):
        for k in ("data", "items", "list"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        return []
    return [x for x in payload if isinstance(x, dict)] if isinstance(payload, list) else []


def _next_cursor(payload: Any) -> str:
    if isinstance(payload, dict):
        for k in ("next_cursor", "cursor"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v
    return ""


# ------------------------------------------------------------------ 错误分类
def classify_ppio(status: int, payload: Any, message: str = "") -> tuple[str, bool]:
    """PPIO 的错误比 AutoDL 好判：先看 HTTP 状态码，再看 reason。

    返回 `(kind, retryable)`。kind 必须落在 base.ProviderError 的闭集合里
    —— 那边**没有 PERMANENT**，「参数错」这种终态非重试落 `UNKNOWN`（= 不重试），
    不要在这里自造新词，上层是按闭集合分流的。
    """
    reason = ""
    if isinstance(payload, dict):
        reason = str(payload.get("reason") or payload.get("code") or "")
    text = f"{reason} {message} {payload if isinstance(payload, dict) else ''}".lower()

    if status in (401, 403) or any(k in reason.upper() for k in ("UNAUTHORIZED", "FORBIDDEN")):
        return ProviderError.AUTH, False
    if status == 429 or "RATE_LIMIT" in reason.upper() or "too many" in text:
        # 实测（2026-11-13）：连续删 4 台实例时第 2 台起就 429。
        # 归 TRANSIENT 是对的（退避后能成功），但**退避必须够长** ——
        # 立刻重试只会继续 429。提示里点明，别让上层以为是网络抖动。
        return ProviderError.TRANSIENT, True
    if status >= 500:
        return ProviderError.TRANSIENT, True
    if status == 404 or "RESOURCE_NOT_FOUND" in reason.upper() or "not found" in text:
        return ProviderError.NOT_FOUND, False
    if "CONFLICT" in reason.upper():
        # 实例在过渡态（正在关机/正在迁移）：短退避可重试
        return ProviderError.BUSY, True
    if any(k in text for k in ("balance", "insufficient fund", "余额", "欠费", "arrears")):
        return ProviderError.BILLING, False
    if any(k in text for k in ("stock", "sold out", "no available", "inventory",
                               "库存", "无货", "暂无", "资源不足")):
        return ProviderError.OUT_OF_STOCK, True
    if any(k in text for k in ("timeout", "timed out", "connection reset", "超时")):
        return ProviderError.TRANSIENT, True
    # 400 VALIDATOR_PARAM / REQUEST：参数错，终态不重试。闭集合里最接近的是 UNKNOWN。
    return ProviderError.UNKNOWN, False


class PPIOProvider(Provider):
    """PPIO 派欧云 GPU 容器实例。"""

    name = "ppio"

    def __init__(self, credentials: dict[str, str] | None = None, *,
                 api_key: str | None = None, base_url: str | None = None,
                 timeout: float = 30.0,
                 transport: Callable[[str, str, dict | None, dict | None],
                                     tuple[int, Any]] | None = None):
        """凭证优先级：显式 api_key > credentials["api_key"] > env PPIO_API_KEY。

        `transport` 只给测试用（离线 fixture）：签名 `(method, path, body, query) ->
        (http_status, payload)`。生产留空即走 urllib，不引入第三方依赖。

        **构造期允许没有 key**：能力声明与凭证无关，`capability_matrix()` 要能
        展示「PPIO 能做什么」，不能被「本机没配凭证」挡住。凭证缺失由 `ready()` 报，
        `registry.available()` 调它 —— 这样「矩阵里有它」和「本机真能调它」是两件事。
        """
        creds = dict(credentials or {})
        key = (api_key or creds.get("api_key") or creds.get("PPIO_API_KEY")
               or os.environ.get("PPIO_API_KEY") or "").strip()
        self._api_key = key
        self.base_url = (base_url or os.environ.get("PPIO_BASE_URL") or BASE_URL).rstrip("/")
        self.timeout = float(timeout)
        self._transport = transport
        self.calls: list[str] = []          # 测试断言用：记录实际发出的请求
        self._catalog_cache: list[dict] | None = None

    def ready(self) -> None:
        """有 key（或注入了 transport）才算本机可用。见 base.Provider.ready()。"""
        if not self._api_key and self._transport is None:
            raise ProviderError(
                "未配置 PPIO API 密钥：传 credentials={'api_key': ...} 或设 PPIO_API_KEY"
                "（控制台 → API 密钥管理，密钥以 sk_ 开头）",
                ProviderError.AUTH)

    # ------------------------------------------------------------ 元信息
    def capabilities(self) -> Capabilities:
        return Capabilities(
            name=self.name,
            can_stop_instance=True,
            # 系统盘 130GB 内免费；超出部分「计费时长 = 创建到释放」，停机照收。
            # 所以声明 True（而不是 None）—— 免费额度内为 0 这件事写在 notes 里。
            bills_disk_when_stopped=True,
            instance_has_internet=True,
            can_pull_oci=True,
            has_shared_weight_mount=False,
            # API 里没有「定时关机」：find 遍 v2 全部 40 个 spec 也没这个端点。
            supports_scheduled_stop=False,
            supports_spot=True,
            supports_custom_image=True,
            free_system_disk_gb=_ROOTFS_FREE_GB,
            # 实测 2026-11-13：create → running 在 4s 内，stop→start 约 3s。
            typical_boot_s=5,
            notes={
                "bills_disk_when_stopped": "系统盘 130GB 内 0 元；超出 0.01 元/GB/天，"
                                           "计费时长是「创建→释放」，停机也收",
                "supports_scheduled_stop": "v2 无定时关机端点；靠 p2-reaper.timer + "
                                           "本地 stop_at 台账兜底",
                "can_pull_oci": "create() 直接收 image 地址 + registry_auth_id",
                "instance_has_internet": "有；且对 github/huggingface/pypi/docker 有加速",
                "cpu_memory": "CPU/内存由产品规格（SKU）决定；create() 按 (gpu, cpu, memory) "
                              "在候选 SKU 里挑最接近的一个，实际值见返回的 InstanceView",
                "data_disk": "create() 不收本地数据盘大小；持久化靠云存储挂载（volumes）",
                "spot": "按量价 5 折，1 小时保护期，回收前 1 小时通知，可读 spot_reclaim_time",
                "typical_boot_s": "实测 create→running 4s、stop→start 3s；"
                                  "但 running ≠ sshd 就绪（TCP 探测另有白名单限制）",
            },
        )

    # ------------------------------------------------------------ HTTP 层
    def _require_key(self) -> str:
        if not self._api_key:
            raise ProviderError(
                "未配置 PPIO API 密钥：传 credentials={'api_key': ...} 或设 PPIO_API_KEY",
                ProviderError.AUTH)
        return self._api_key

    def _call(self, method: str, path: str, *,
              body: dict | None = None, query: dict | None = None,
              idempotent: bool = True) -> Any:
        """发一个请求，成功返回 payload（已剥掉 `data` 信封，如果有）。

        `idempotent=False` 用于 create / start：**创建不幂等**，重试一次可能租出两台卡，
        所以这类失败一律 `retryable=False`，让上层停下而不是自动重试。
        """
        key = self._require_key()
        q = {k: v for k, v in (query or {}).items() if v is not None and v != ""}
        self.calls.append(f"{method} {path}")

        if self._transport is not None:                 # 离线测试路径
            status, payload = self._transport(method, path, body, q or None)
        else:
            status, payload = self._http(method, path, body, q or None, key)

        if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0", "Success"):
            kind, retry = classify_ppio(status, payload)
            msg = payload.get("message") or payload.get("msg") or payload.get("reason") or ""
            raise ProviderError(f"PPIO {status} {payload.get('reason') or ''}: {msg}".strip(),
                                kind, retryable=retry and idempotent)
        if status >= 400:
            kind, retry = classify_ppio(status, payload, str(payload)[:200])
            raise ProviderError(f"PPIO HTTP {status} {method} {path}: {str(payload)[:200]}",
                                kind, retryable=retry and idempotent)
        if isinstance(payload, dict) and "data" in payload and len(payload) <= 3:
            # 某些端点套 `{"data": ...}`，某些直接返回对象。两种都认。
            # 但 products/images 的 data 是列表信封，调用方需要 next_cursor，
            # 所以这里只在「data 不是列表」时剥壳。
            if not isinstance(payload["data"], list):
                return payload["data"]
        return payload

    def _http(self, method: str, path: str, body: dict | None,
              query: dict | None, key: str) -> tuple[int, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = None
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": _UA,
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, _loads(raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            return e.code, _loads(raw)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError(f"PPIO 请求失败 {method} {path}: {e}",
                                ProviderError.TRANSIENT) from e

    # ------------------------------------------------------------ 产品 / 规格
    def products(self) -> list[dict]:
        """GPU 产品目录（含价格 + 实时库存）。这是本适配器的地基。

        刻意**不做缓存**：价格和库存都是实时值，缓存会让估价失真
        （registry 的注释也写了同一条理由）。
        """
        payload = self._call("GET", "/gpus/v2/products",
                             query={"type": "gpu", "category": "instance", "limit": 200})
        return _items(payload)

    def _match(self, products: list[dict], gpu_type: str) -> list[dict]:
        """按 gpu_type 过滤产品，三档优先级。

        `4090` 是 `4090D` 的子串 —— 只做 `in` 判断会把 4090D 当成 4090，
        按显存取错卡（24G 都是 24G，但 4090D 的算力约等于 4090 的 8 折，
        报价和升卡排序都会错）。所以分三档：

          1. **型号名精确**：产品的 `resource_spec.gpu.name`（官方叫「型号名」，
             如 `A100`、`4090`）归一化后**等于** want
          2. **产品 id/name 精确**：`4090.16c62g` 这种 SKU id、或展示名
          3. **子串**：取最短的候选（`4090` 命中 `rtx4090` 与 `4090d` 时选短的）

        档 1 命中就不再往下走，这样 `4090` 与 `4090D` 永远不会互相污染。
        """
        if not _norm_model(gpu_type):
            return products

        def gpu_keys(p: dict) -> list[str]:
            """型号名 + spec_id，都过 `_norm_model`（剥厂商前缀）。"""
            spec = (p.get("resource_spec") or {}).get("gpu") or {}
            return [k for k in (_norm_model(spec.get("name")),
                                _norm_model(spec.get("spec_id"))) if k]

        def all_keys(p: dict) -> list[str]:
            return [k for k in gpu_keys(p)
                    + [_norm_model(p.get("id")), _norm_model(p.get("name"))] if k]

        want = _norm_model(gpu_type)
        for tier in (gpu_keys, all_keys):
            exact = [p for p in products if want in tier(p)]
            if exact:
                return exact

        fuzzy = [p for p in products if any(want in k for k in gpu_keys(p) or all_keys(p))]
        return sorted(fuzzy, key=lambda p: len(_norm_model(
            (p.get("resource_spec") or {}).get("gpu", {}).get("name") or p.get("name"))))

    @staticmethod
    def _pick_sku(cands: list[dict], cpu: int, memory_gb: int) -> dict:
        """在同一 GPU 的多个 SKU 里挑最接近 (cpu, memory) 的那个。

        PPIO 的 CPU/内存是 SKU 固定的（4090.8c62g / 16c62g / 18c62g / 16c125g…），
        不是建机参数。所以「满足 cpu/mem」只能靠选 SKU —— 这也是它比 AutoDL 好的地方：
        AutoDL 的 CPU/内存完全由平台按卡数倍数分配，连选都没得选。

        评分：CPU 差额权重 2（差 8 核比差 30G 内存更影响 ComfyUI 的采样/解码吞吐），
        内存差额按 GB 计。优先在「内存 ≥ 需求」的候选里挑，没有再退到全集。
        """
        def score(p: dict) -> tuple:
            spec = p.get("resource_spec") or {}
            c = int(spec.get("cpu") or 0)
            m = int(spec.get("memory_gb") or 0)
            return (abs(c - int(cpu or 0)) * 2 + abs(m - int(memory_gb or 0)), c, m)

        enough = [p for p in cands
                  if int((p.get("resource_spec") or {}).get("memory_gb") or 0)
                  >= int(memory_gb or 0)]
        return sorted(enough or cands, key=score)[0]

    # ------------------------------------------------------------ 实例生命周期
    def create(self, *, image: str, gpu_type: str, gpu: int = 1, cpu: int = 16,
               memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
               name: str = "job", charge_type: str = "Postpay",
               zone: str | None = None, password: str | None = None,
               remark: str = "", **extra: Any) -> str:
        """建机。返回云侧实例 ID（形如 `ca338f12f3`）。

        额外关键字（不进契约，但真机联调要用）：
          ports        要给公网的端口列表，默认 [8188]（ComfyUI）
          command      容器启动命令（对应 docker CMD）
          entrypoint   容器入口（对应 docker ENTRYPOINT）
          envs         [{"key":..,"value":..}]
          months       仅 charge_type 为包月时需要
        """
        if int(data_disk_gb or 0) > 0:
            # 不静默忽略：上层会以为买到了盘，然后往 /data 写模型。
            raise ProviderError(
                "PPIO create() 不接受本地数据盘大小；持久化请用云存储挂载（volumes）",
                ProviderError.UNSUPPORTED)
        mode = _BILLING_MAP.get(str(charge_type or "").strip().lower())
        if mode is None:
            raise ProviderError(
                f"PPIO 不认识的计费方式 {charge_type!r}；支持 Postpay / Spot / Prepay",
                ProviderError.UNSUPPORTED)

        prods = self.products()
        cands = self._match(prods, gpu_type)
        if not cands:
            raise ProviderError(
                f"PPIO 没有型号 {gpu_type!r}；可用：{', '.join(sorted({p.get('name') or '?' for p in prods}))}",
                ProviderError.NOT_FOUND)
        prod = self._pick_sku(cands, cpu, memory_gb)

        gpu_spec = (prod.get("resource_spec") or {}).get("gpu") or {}
        lo = int(gpu_spec.get("min") or 1)
        hi = int(gpu_spec.get("max") or 8)
        if not (lo <= int(gpu) <= hi):
            raise ProviderError(
                f"PPIO {prod.get('name')} 的 GPU 数量必须在 {lo}~{hi}，收到 {gpu}",
                ProviderError.UNKNOWN)

        rootfs = _rootfs_gb(prod, disk_gb)
        ports = extra.get("ports")
        if ports is None and extra.get("port"):
            ports = [extra["port"]]
        if ports is None:
            ports = [8188]          # ComfyUI：lab/*.py 全都打这个端口
        port_list = []
        for p in ports:
            if isinstance(p, dict):
                port_list.append({"port": int(p["port"]),
                                  "protocol": str(p.get("protocol") or "tcp")})
            else:
                port_list.append({"port": int(p), "protocol": "tcp"})

        body: dict[str, Any] = {
            "name": str(name or "job")[:255],
            "product_id": str(prod.get("id") or ""),
            "image": str(image),
            "billing": {"mode": mode},
            "resource": {"rootfs_size_gb": rootfs, "gpu_num": int(gpu)},
            "type": "gpu",
            "ports": port_list,
        }
        if mode == "prepaid":
            months = int(extra.get("months") or 1)
            body["billing"]["prepaid"] = {"months": months}
        if extra.get("registry_auth_id"):
            body["registry_auth_id"] = str(extra["registry_auth_id"])
        if extra.get("command"):
            body["command"] = str(extra["command"])
        if extra.get("entrypoint"):
            body["entrypoint"] = str(extra["entrypoint"])
        if extra.get("envs"):
            body["envs"] = list(extra["envs"])
        if zone:
            # PPIO 是「候选区域列表，最终只会落到其中一个」，不是硬绑定
            body["candidate_regions"] = [z.strip() for z in str(zone).split(",") if z.strip()]

        out = self._call("POST", "/gpus/v2/instances", body=body, idempotent=False)
        uid = _extract_id(out)
        if not uid:
            raise ProviderError(f"PPIO 建机未返回实例 ID：{str(out)[:200]}",
                                ProviderError.TRANSIENT)
        return uid

    def get(self, uid: str) -> InstanceView:
        d = self._call("GET", f"/gpus/v2/instances/{uid}")
        return self._view(d if isinstance(d, dict) else {})

    def list_instances(self, prefix: str = "") -> list[InstanceView]:
        """游标翻页走完。`limit` 取 100，与文档上限一致。"""
        out: list[InstanceView] = []
        cursor: str | None = None
        for _ in range(50):                     # 防御：最多 50 页，避免死循环
            payload = self._call("GET", "/gpus/v2/instances",
                                 query={"limit": 100, "cursor": cursor})
            out.extend(self._view(x) for x in _items(payload))
            cursor = _next_cursor(payload)
            if not cursor:
                break
        return [i for i in out if i.name.startswith(prefix)] if prefix else out

    # 开机失败里「值得再试一次」的 kind。与 create() 不同，**start 是可以安全重试的**：
    # 对已在运行的实例再开一次只会报错，不会像 create 那样多租出一台卡。
    _START_RETRYABLE = frozenset({
        ProviderError.OUT_OF_STOCK,     # 资源被抢占，等下/换机可能就有
        ProviderError.TRANSIENT,        # 网络/超时
        ProviderError.BUSY,             # 过渡态
        ProviderError.UNKNOWN,          # 参数错以外的未知，开机场景下多为调度失败
    })

    def start(self, uid: str) -> None:
        try:
            self._call("PUT", f"/gpus/v2/instances/{uid}/start", body={})
        except ProviderError as e:
            if e.kind in self._START_RETRYABLE and not e.retryable:
                # 官方原文：「停止后，如有其他用户抢占资源，您的实例可能无法正常启动」。
                # 这类失败要能被上层重试或换机，不能当成「参数错，别试了」。
                raise ProviderError(f"{uid} 开机失败（资源可能已被抢占）：{e}",
                                    e.kind, retryable=True) from e
            raise

    def stop(self, uid: str) -> None:
        self._call("PUT", f"/gpus/v2/instances/{uid}/stop", body={})

    def terminate(self, uid: str) -> None:
        """删除实例。与 AutoDL 不同，PPIO 的 DELETE 不要求先关机。

        **带退避重试**：实测连续删除多次会触发 429 `RATE_LIMIT_EXCEEDED`
        （2026-11-13：删第 2 台起）。删除失败意味着实例还在计费，
        所以这里不能像别的调用那样「失败就抛」—— 必须退避后重试几次。
        与其他方法不同，`terminate` 重试是**安全且必要**的：
        删一个已不存在的实例只会得到 NOT_FOUND，不会像 create 那样多花钱。
        """
        delay = 3.0
        last: ProviderError | None = None
        for attempt in range(4):
            try:
                self._call("DELETE", f"/gpus/v2/instances/{uid}")
                return
            except ProviderError as e:
                last = e
                # 只在「限流 / 网络类」失败时重试；NOT_FOUND 说明已经删掉了，直接当成功
                if e.kind == ProviderError.NOT_FOUND:
                    return
                if e.kind not in (ProviderError.TRANSIENT, ProviderError.BUSY):
                    raise
                if attempt < 3:
                    time.sleep(delay)
                    delay *= 2
        raise last if last else ProviderError(f"{uid} 删除失败", ProviderError.TRANSIENT)

    def ssh_endpoint(self, uid: str) -> SSHEndpoint:
        """从 `tools.ssh` 取连接信息。

        v2 给的是 `command`（`ssh -p 51039 root@proxy.xxx`）+ `username` + `password`，
        不像 v1 有 `proxy_host`/`ssh_port` 独立字段，所以要解析。

        ⚠️ 不缓存：端口和密码在 stop→start 后可能变（可能被调度到另一台机器）。
        缓存「大多数时候还能用」恰恰是最危险的 bug 形态。
        """
        d = self._call("GET", f"/gpus/v2/instances/{uid}")
        tools = (d or {}).get("tools") or {}
        ssh = tools.get("ssh") or {}
        host, port = _parse_ssh(ssh.get("command") or "")
        if not host:
            raise ProviderError(f"{uid} 还没有 SSH 信息（可能尚未就绪）",
                                ProviderError.TRANSIENT)
        return SSHEndpoint(host=host, port=port or "22",
                           password=ssh.get("password") or None,
                           user=str(ssh.get("username") or "root"))

    def ssh_ready(self, uid: str, *, timeout: int = 180) -> SSHEndpoint:
        """等 sshd 真的能收命令。

        ⚠️ **在 PPIO 上大概率失败**，原因已定位（2026-11-13 实测）：

          - sshd 不是镜像提供的，是平台 sidecar（`pod-<id>-ssh`），日志显示启动成功
          - TCP 三次握手能完成 → 说明有监听
          - 但**发出任何字节都会立刻 RST**（发 SSH 版本串 / HTTP 请求 / 纯字节都一样）
          - 只有「完全不发数据」时连接保持

        这是**代理层的来源放行**问题，不是 SSH 协议被封、也不是镜像没起 sshd。
        官方公告（2026-09-24）：TCP 端口映射（含 SSH）必须提交访问源 IP 白名单。

        所以这里重试到底也不会通。探测失败时直接把原因说出来，
        让上层知道该去配白名单，而不是以为「再等等就好」。
        """
        ep = self.ssh_endpoint(uid)
        t0 = time.time()
        last = ""
        rst_seen = False
        while time.time() - t0 < timeout:
            try:
                with socket.create_connection((ep.host, int(ep.port)), timeout=5) as sock:
                    # 主动发一个字节：PPIO 的代理在收到数据后会立刻 RST，
                    # 只完成握手的「假连通」骗不过这一关，也骗不过我们自己。
                    try:
                        sock.sendall(b"SSH-2.0-probe\r\n")
                        data = sock.recv(64)
                        if data:
                            last = "收到 banner，sshd 就绪"
                            if _ssh_probe(ep):
                                return ep
                    except ConnectionResetError:
                        rst_seen = True
                        last = "TCP 通但代理层立刻 RST"
                    except socket.timeout:
                        last = "TCP 通但无响应"
            except OSError as e:
                last = str(e)
            time.sleep(5)
            try:                                    # 端口可能刚变，重取一次
                ep = self.ssh_endpoint(uid)
            except ProviderError:
                pass

        hint = ("；该端口发任何数据都被 RST，按官方公告需提交 TCP 访问源 IP 白名单"
                "（工单或客户经理）" if rst_seen else "")
        raise ProviderError(f"{uid} sshd {timeout}s 内未就绪（{last}）{hint}",
                            ProviderError.TRANSIENT)

    def scheduled_stop(self, uid: str, at_epoch: int) -> None:
        raise ProviderError(
            "PPIO 没有定时关机接口；上层用 p2-reaper.timer + 本地 stop_at 台账兜底",
            ProviderError.UNSUPPORTED)

    # ------------------------------------------------------------ 计费
    def price(self, *, gpu_type: str, gpu: int = 1, cpu: int = 16,
              memory_gb: int = 64, disk_gb: int = 100, data_disk_gb: int = 0,
              charge_type: str = "Postpay", image: str = "",
              zone: str | None = None) -> dict:
        """报价。**磁盘不折算成小时** —— 这是刻意的。

        PPIO 的系统盘是「超出 130GB 后 ¥0.01/GB/天」，按天结算。
        把它除 24 折成 hourly_disk 会让跨日边界必错（`base.py` 顶部
        「不隐藏价格口径」说的就是这件事）。所以：

            hourly_instance  算力，元/小时（真值，来自 products）
            hourly_disk      恒为 0.0
            hourly_total     等于 hourly_instance（不含磁盘）
            daily_disk       磁盘，元/天（超出免费额度的部分）
        """
        mode = _BILLING_MAP.get(str(charge_type or "").strip().lower(), "postpaid")
        prods = self.products()
        cands = self._match(prods, gpu_type)
        if not cands:
            raise ProviderError(f"PPIO 没有型号 {gpu_type!r}", ProviderError.NOT_FOUND)
        prod = self._pick_sku(cands, cpu, memory_gb)

        pricing = prod.get("pricing") or {}
        slot = pricing.get(mode) or {}
        unit_price = _money(slot.get("final_price"))
        base_price = _money(slot.get("base_price"))
        if not unit_price:                      # 该计费方式不可用
            raise ProviderError(
                f"PPIO {prod.get('name')} 不支持 {charge_type} 计费"
                f"（支持：{', '.join(prod.get('billing_methods') or []) or '未知'}）",
                ProviderError.UNSUPPORTED)

        inst = round(unit_price * max(1, int(gpu)), 4)
        over = max(0, int(disk_gb or 0) - _ROOTFS_FREE_GB)
        daily_disk = round(over * _ROOTFS_YUAN_PER_GB_DAY, 4)
        return {
            "hourly_instance": inst,
            "hourly_disk": 0.0,
            "hourly_total": inst,
            "daily_disk": daily_disk,
            "currency": "CNY",
            "charge_type": charge_type,
            "billing_mode": mode,
            "free_system_disk_gb": _ROOTFS_FREE_GB,
            "product": prod.get("name"),
            "product_id": prod.get("id"),
            "base_price": base_price,
            "raw": {"pricing": pricing, "resource_spec": prod.get("resource_spec")},
        }

    def balance(self) -> float:
        """可用余额（元）。

        注意单位：`availableBalance` 是 **1/10000 元**（10000 = ¥1），不是分也不是毫。
        这个端点挂在 `/openapi/v1/...` 而不是 `/gpus/v2/...`，两套前缀并存是官方现状。
        """
        d = self._call("GET", "/openapi/v1/billing/balance/detail")
        d = d if isinstance(d, dict) else {}
        for k in ("availableBalance", "cashBalance"):
            if d.get(k) is not None:
                try:
                    return round(float(d[k]) / 10000.0, 4)
                except (TypeError, ValueError):
                    continue
        raise ProviderError(f"PPIO 余额响应缺字段：{str(d)[:200]}", ProviderError.TRANSIENT)

    # ------------------------------------------------------------ 镜像与库存
    def resolve_image(self, spec: str) -> dict:
        """镜像 spec → 可传给 create() 的 image 地址。

        ⚠️ 真实响应结构与文档不一致（2026-11-13 实测）：
          文档说镜像记录有 `image`（镜像标识，字符串）。
          实际 `image` 是**短名**（`pytorch`），真正的仓库地址在
          `tags[].full_image_name`（`image.ppinfra.com/prod-gpucloudpublic/pytorch:v2`）。
          直接把 `image` 塞进 create() 会拿到一个拉不动的名字。

        所以解析优先级：
          ① 含 `/` 的地址 → 原样返回（自己 push 的镜像走这条）
          ② `名字:tag` → 在 tags 里精确找 tag
          ③ 纯名字 → 找该镜像的默认 tag（列表第一个，官方把 `v2`/`v1` 排在首位）
        """
        s = str(spec or "").strip()
        if not s:
            raise ProviderError("镜像 spec 为空", ProviderError.NOT_FOUND)
        if "/" in s:
            return {"image_id": s, "name": s, "type": "address", "raw": {}}

        want_name, _, want_tag = s.partition(":")
        for typ in ("private", "community", "base"):
            payload = self._call("GET", "/gpus/v2/images",
                                 query={"type": typ, "name": want_name, "limit": 100})
            for it in _items(payload):
                if _norm(it.get("name")) != _norm(want_name) and _norm(it.get("id")) != _norm(want_name):
                    continue
                tags = it.get("tags") or []
                if want_tag:
                    hit = next((t for t in tags if str(t.get("tag") or "") == want_tag), None)
                    if not hit:
                        continue
                else:
                    hit = tags[0] if tags else None
                full = str((hit or {}).get("full_image_name") or "")
                if not full:
                    # 没有 tags 的历史记录：退回 `image` 字段（可能仍是短名，
                    # 但至少有值，让 create() 报出真实错误而不是在这里凭空失败）
                    full = str(it.get("image") or "")
                return {"image_id": full,
                        "name": want_name,
                        "tag": str((hit or {}).get("tag") or ""),
                        "type": str(it.get("type") or typ),
                        "raw": it}
        raise ProviderError(f"PPIO 找不到镜像 {s!r}", ProviderError.NOT_FOUND)

    def images(self, keyword: str = "") -> list[dict]:
        """列出镜像。每条按 tag 展开 —— 一个镜像名下有多个 tag，各有各的完整地址。

        展开而不是压平，是因为 `create()` 要的是**带 tag 的完整地址**，
        只给名字等于让调用方自己去拼（而拼法只有官方知道）。
        """
        out: list[dict] = []
        for typ in ("base", "community", "private"):
            payload = self._call("GET", "/gpus/v2/images",
                                 query={"type": typ, "name": keyword, "limit": 100})
            for it in _items(payload):
                name = str(it.get("name") or "")
                tags = it.get("tags") or []
                if not tags:
                    out.append({"image_id": str(it.get("image") or ""),
                                "name": name, "tag": "",
                                "type": str(it.get("type") or typ), "raw": it})
                    continue
                for t in tags:
                    out.append({"image_id": str(t.get("full_image_name") or ""),
                                "name": name,
                                "tag": str(t.get("tag") or ""),
                                "type": str(it.get("type") or typ),
                                "raw": it})
        if keyword:
            kw = _norm(keyword)
            out = [i for i in out if kw in _norm(i["name"]) or kw in _norm(i["image_id"])]
        # 去重：同一条镜像可能同时出现在 base/community 两次查询里（服务端按 type 过滤，
        # 但 `name` 是模糊匹配，命中不互斥）。按 image_id 取首次出现。
        seen: set[str] = set()
        uniq: list[dict] = []
        for i in out:
            key = i["image_id"] or i["name"]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(i)
        return uniq

    def stock(self, gpu_type: str, *, spot: bool = False,
              zone: str | None = None) -> int:
        """实时可租卡数。

        用 `resource_spec.gpu.available`（当前最优节点上的可用 GPU 数）。
        **找不到型号就抛 NOT_FOUND，不返回 0** —— 返回 0 会被上层读成「售罄」，
        那就从「型号写错」变成了「没货，换卡」的错误决策（AutoDL 那份笔记里
        记过同类的坑：库存接口语义对不上比没有库存更糟）。
        """
        prods = self.products()
        cands = self._match(prods, gpu_type)
        if not cands:
            raise ProviderError(f"PPIO 没有型号 {gpu_type!r}", ProviderError.NOT_FOUND)
        if zone:
            z = str(zone)
            cands = [p for p in cands if z in (p.get("region_ids") or [])] or cands
        best = 0
        for p in cands:
            spec = (p.get("resource_spec") or {}).get("gpu") or {}
            # 注意 `pricing` 里 spot 可能是**缺省键**而不是 null（实测：L40S 只有 postpaid）。
            # 只看 `billing_methods` 是权威来源，不要去读 pricing.spot 的存在性。
            if spot and "spot" not in (p.get("billing_methods") or []):
                continue
            if p.get("deployable") is False:
                continue
            best = max(best, int(spec.get("available") or 0))
        return best

    # ------------------------------------------------------------ 视图归一
    def _catalog(self) -> list[dict]:
        """展示用目录，**在一个 provider 对象内**记忆一次。

        与 `products()` 的区别，以及为什么允许这里缓存：

          `products()` 是价格/库存的**决策输入**，必须每次现取（调价后按旧价估价 = 算错钱）。
          `_catalog()` 只用来把 `product_id` 翻成展示名 —— 这是元数据，
          一次对象生命周期内不会变，而 `get()` 在 wait_state 里每 5 秒调一次，
          每次都拉一遍目录纯属浪费。

        跨对象不复用（registry 刻意不做 provider 实例缓存），所以不会「旧价跨作业」。
        """
        if self._catalog_cache is None:
            try:
                self._catalog_cache = self.products()
            except ProviderError:
                self._catalog_cache = []
        return self._catalog_cache

    def _product_label(self, product_id: str) -> str:
        """`product_id` → 展示名（如 `RTX 4090`）。查不到就原样返回 id。"""
        pid = str(product_id or "")
        if not pid:
            return ""
        for p in self._catalog():
            if str(p.get("id") or "") == pid:
                return str(p.get("name") or pid)
        return pid

    def _view(self, d: dict) -> InstanceView:
        st = d.get("status")
        st = st if isinstance(st, dict) else {"status": st}
        raw_state = str(st.get("status") or "").strip().lower()
        # 未知状态原样留在 raw 里、state 记 unknown —— 不抛异常。
        # 抛异常会让 reaper 把在跑的机器当异常处理，那是最贵的一类错误。
        state = _STATE_MAP.get(raw_state, "unknown")

        spec = d.get("resource_specs") or {}
        pricing = d.get("pricing") or {}
        # 详情里的价格同样固定 ÷1e5（见 _MONEY_SCALE 的证据链）。
        price = _money(pricing.get("instance_price"))
        # 磁盘按天计，不折算进 hourly（见 price() 的说明）；这里恒为 0。
        return InstanceView(
            uid=str(d.get("id") or ""),
            state=state,
            name=str(d.get("name") or ""),
            gpu_type=self._product_label(str(d.get("product_id") or "")),
            gpu_count=int(spec.get("gpu_num") or 0),
            cpu=int(spec.get("cpu_num") or 0),
            memory_gb=int(spec.get("memory_gb") or 0),
            price_per_hour=price,
            disk_per_hour=0.0,
            region=str(d.get("region") or ""),
            zone=str(d.get("region") or ""),
            provider=self.name,
            raw=d,
        )


# ------------------------------------------------------------------ 小工具
def _loads(raw: str) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"_raw": raw[:500]}


_SSH_RE = re.compile(r"ssh\s+(?:-p\s*(\d+)\s+)?(?:([\w.-]+)@)?([\w.-]+)")


def _parse_ssh(command: str) -> tuple[str, str]:
    """从 `ssh -p 51039 root@proxy.x.com` 里取 (host, port)。"""
    if not command:
        return "", ""
    m = _SSH_RE.search(str(command))
    if not m:
        return "", ""
    return m.group(3) or "", m.group(1) or ""


def _extract_id(payload: Any) -> str:
    """建机响应可能是 `"ca338f12f3"`、`{"id": ...}`、`{"data": {"id": ...}}`。"""
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if isinstance(payload, dict):
        for k in ("id", "instance_id", "instanceId"):
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v
        inner = payload.get("data")
        if isinstance(inner, dict):
            return _extract_id(inner)
        if isinstance(inner, str) and inner:
            return inner
    return ""


def _rootfs_gb(prod: dict, disk_gb: int) -> int:
    """系统盘大小，夹在产品允许的 [min_gb, max_gb] 里。"""
    rf = ((prod.get("resource_spec") or {}).get("rootfs") or {})
    try:
        lo = int(rf.get("min_gb") or 2)
    except (TypeError, ValueError):
        lo = 2
    try:
        hi = int(rf.get("max_gb") or 500)
    except (TypeError, ValueError):
        hi = 500
    want = int(disk_gb or lo)
    return max(lo, min(hi, want))


def _ssh_probe(ep: SSHEndpoint) -> bool:
    """一次 ssh 探活。有 sshpass 才探，没有就当就绪（与 compshare.py 一致）。"""
    import shutil
    import subprocess
    if not shutil.which("sshpass") or not ep.password:
        return True
    cmd = ["sshpass", "-p", ep.password, "ssh",
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=8", "-p", ep.port, f"{ep.user}@{ep.host}",
           "echo ssh-ready"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        return r.returncode == 0 and "ssh-ready" in (r.stdout or "")
    except Exception:
        return False


def from_env() -> PPIOProvider:
    return PPIOProvider()
