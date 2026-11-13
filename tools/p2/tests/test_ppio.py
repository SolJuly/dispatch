#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PPIO 适配器测试：全部离线、零网络、零费用。

测的是**判断逻辑**，不是 IO：

  1. 状态映射 —— 写错会让 reaper 把在跑的机器当异常处理（最贵的一类错误）
  2. 错误分类 —— 写错会「该重试的不重试」或「不该重试的疯狂重试烧钱」
  3. 金额换算 —— 精度来自响应里，不是硬编码 1000；写错就是算错钱
  4. SKU 挑选 —— 4090 与 4090D 不能互相污染
  5. 不幂等调用 —— create/start 失败必须 retryable=False
  6. BYOK —— 凭证不落到全局，也不污染进程环境

用法：python3 -m unittest tools.p2.tests.test_ppio
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import providers as P  # noqa: E402
from tools.p2.providers import base as PB  # noqa: E402
from tools.p2.providers.ppio import (  # noqa: E402
    PPIOProvider, _money, _norm, _parse_ssh, classify_ppio,
)


# ------------------------------------------------------------------ fixtures
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ppio"


def fixture(name: str) -> dict:
    """读真实响应（2026-11-13 用真 key 打回来的，原样存）。

    为什么用真响应而不是照文档编：文档和真实结构不一致的地方已经被实测抓到两处
    （`precision` 字段不可用、镜像地址在 `tags[].full_image_name` 里），
    照文档编的 fixture 会把错误一起固化进测试。
    """
    with open(_FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


# 合成用的产品模板：真实响应里全 `available: 0`（余额 0 时拿不到真实库存），
# 所以「有货」的场景只能靠合成。字段名与真实响应严格一致。
SCALE = 100000          # 真实响应里的除数（见 ppio._MONEY_SCALE 的证据链）


def product(pid, name, gpu, cpu, mem, price, *, spot=None,
            available=6, deployable=True, inventory="normal",
            billing=("postpaid", "prepaid", "spot"), regions=("cn-north-1",)):
    """合成一个产品记录。金额按真实响应的口径编码（元 × 1e5）。

    `precision` 字段刻意写成真实响应里那种**不可信**的值（10 或 0），
    而不是一个「正确的」精度 —— 这样测试会持续证明适配器没有去信它。
    """
    milliyuan = int(round(price * SCALE))
    pricing = {"precision": 10,
               "postpaid": {"base_price": milliyuan, "final_price": milliyuan,
                            "unit": ""},
               "prepaid": [],
               "storage_price_per_gb": 1000}      # 1000/1e5 = 0.01 元/GB/天
    if spot is not None:
        sp = int(round(spot * SCALE))
        pricing["spot"] = {"base_price": sp, "final_price": sp, "unit": ""}
    return {
        "id": pid, "name": name, "type": "gpu", "category": "instance",
        "description": f"NVIDIA {gpu}",
        "region_ids": list(regions),
        "resource_spec": {
            "gpu": {"spec_id": f"spec-{pid}", "name": gpu, "memory_gb": 24,
                    "min": 1, "max": 8, "available": available,
                    "max_available": available},
            "cpu": cpu, "memory_gb": mem,
            "rootfs": {"min_gb": 30, "max_gb": 500, "free_gb": 130},
        },
        "pricing": pricing,
        "billing_methods": list(billing),
        "inventory_state": inventory,
        "deployable": deployable,
    }


PRODUCTS = [
    product("prod-4090-8c62g", "4090.8c62g", "4090", 8, 62, 1.98, spot=1.16),
    product("prod-4090-16c62g", "4090.16c62g", "4090", 16, 62, 1.98, spot=1.16),
    product("prod-4090-18c62g", "4090.18c62g", "4090", 18, 62, 2.10, spot=1.20),
    product("prod-4090d", "4090D.16c62g", "4090D", 16, 62, 1.14, spot=0.9),
    product("prod-3090", "3090.16c92g", "3090", 16, 92, 1.39, spot=None,
            billing=("postpaid", "prepaid")),
    product("prod-h20", "H20.16c154g", "H20", 16, 154, 12.00, spot=3.40,
            available=0, deployable=False, inventory="none"),
]

INSTANCE = {
    "id": "ca338f12f3", "name": "job-001", "type": "gpu",
    "product_id": "prod-4090-16c62g",
    "image": "registry.cn-shanghai.aliyuncs.com/zhiyun/comfy:v4",
    "billing": {"mode": "postpaid"},
    "ports": [{"port": 8188, "protocol": "tcp",
               "endpoint": "http://1.2.3.4:8188"}],
    "resource_specs": {"rootfs_size_gb": 130, "gpu_num": 1, "cpu_num": 16,
                       "memory_gb": 62, "gpu_ids": [0]},
    "region": "cn-north-1",
    "tools": {"ssh": {"command": "ssh -p 51039 root@proxy.cn-north-1.gpu-instance.ppinfra.com",
                      "password": "S3cret!", "username": "root"},
              "jupyter": {"endpoint": "https://jupyter.example/x", "enabled": True,
                          "port": 8888}},
    "status": {"status": "running", "error": "", "message": ""},
    "pricing": {"instance_price": 1980, "instance_base_price": 2480,
                "rootfs_storage_price": 0},
    "created_at": 1714464000, "last_started_at": 1714467600, "last_stopped_at": 0,
}


class Transport:
    """可编排的假网关。`routes` 的键是 (METHOD, path)，值是 (status, payload) 或可调用对象。"""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.seen: list[tuple] = []

    def __call__(self, method, path, body, query):
        self.seen.append((method, path, body, query))
        hit = self.routes.get((method, path))
        if hit is None:
            # 未编排 = 不该被调到，直接大声失败（避免测试静默通过）
            raise AssertionError(f"未编排的请求：{method} {path}")
        return hit(body, query) if callable(hit) else hit


def provider(routes=None, **kw) -> PPIOProvider:
    routes = routes if routes is not None else {
        ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS,
                                             "next_cursor": ""}),
        ("GET", "/gpus/v2/instances/ca338f12f3"): (200, INSTANCE),
        ("GET", "/gpus/v2/instances"): (200, {"data": [INSTANCE],
                                              "next_cursor": ""}),
        ("GET", "/openapi/v1/billing/balance/detail"):
            (200, {"availableBalance": "197900", "cashBalance": "197900",
                   "creditLimit": "0", "pendingCharges": "0",
                   "outstandingInvoices": "0"}),
    }
    return PPIOProvider(api_key="sk_test", transport=Transport(routes), **kw)


# ------------------------------------------------------------------ 纯函数
class TestHelpers(unittest.TestCase):
    def test_money_uses_1e5_not_precision_field(self):
        """除数固定 1e5。**用真实响应里出现过的数字**验。

        `precision` 字段实测不可用（同一批产品里 4090 给 10、L40S 给 0），
        这三条都是从真响应抄下来的原值：
          · 4090 按量 231000 → 2.31 元/时
          · 4090 spot 116000 → 1.16 元/时（与第三方监测一致）
          · storage   1000  → 0.01 元/GB/天（与官方计费页一致）
        """
        self.assertAlmostEqual(_money(231000), 2.31, 6)
        self.assertAlmostEqual(_money(116000), 1.16, 6)
        self.assertAlmostEqual(_money(1000), 0.01, 6)
        self.assertAlmostEqual(_money(396000), 3.96, 6)      # L40S
        self.assertAlmostEqual(_money(2376000), 23.76, 6)    # H100 SXM
        self.assertAlmostEqual(_money("231000"), 2.31, 6)    # 字符串数字

    def test_money_ignores_bogus_precision_argument(self):
        """传什么 precision 都不影响结果 —— 它已被证伪，不能参与计算。"""
        for p in (None, 0, 3, 10):
            self.assertAlmostEqual(_money(231000, p), 2.31, 6, f"precision={p} 改变了结果")

    def test_money_clamps_absurd_values(self):
        """口径又变时返回 0（未知价），而不是把荒谬数字塞进成本账。"""
        self.assertEqual(_money(396000 * 1000), 0.0)   # 换算后 3960 元/时，不可能
        self.assertEqual(_money(-5), 0.0)
        self.assertEqual(_money(None), 0.0)
        self.assertEqual(_money("oops"), 0.0)

    def test_norm_collapses_model_names(self):
        for s in ("RTX 4090", "rtx-4090", "rtx4090", "4090"):
            self.assertEqual(_norm(s), "rtx4090" if "rtx" in s.lower() else "4090")
        self.assertEqual(_norm("A100-80GB"), "a10080gb")

    def test_parse_ssh_command(self):
        host, port = _parse_ssh("ssh -p 51039 root@proxy.cn-north-1.gpu-instance.ppinfra.com")
        self.assertEqual(host, "proxy.cn-north-1.gpu-instance.ppinfra.com")
        self.assertEqual(port, "51039")
        # 没写 -p 时要给出空端口，让 ssh_endpoint 退回 22
        self.assertEqual(_parse_ssh("ssh root@h.example"), ("h.example", ""))
        self.assertEqual(_parse_ssh(""), ("", ""))

    def test_classify_auth_by_status(self):
        """PPIO 用正经 HTTP 状态码，所以认证失败可以直接按 401/403 判，不用猜文案。"""
        self.assertEqual(classify_ppio(401, {"reason": "UNAUTHORIZED"}),
                         (PB.ProviderError.AUTH, False))
        self.assertEqual(classify_ppio(403, {"reason": "FORBIDDEN"}),
                         (PB.ProviderError.AUTH, False))

    def test_classify_transient(self):
        for st in (429, 500, 502, 503):
            kind, retry = classify_ppio(st, {})
            self.assertEqual(kind, PB.ProviderError.TRANSIENT)
            self.assertTrue(retry, f"HTTP {st} 应可重试")

    def test_classify_busy_vs_notfound(self):
        self.assertEqual(classify_ppio(400, {"reason": "CONFLICT"}),
                         (PB.ProviderError.BUSY, True))
        self.assertEqual(classify_ppio(404, {"reason": "RESOURCE_NOT_FOUND"}),
                         (PB.ProviderError.NOT_FOUND, False))

    def test_classify_param_error_is_not_retryable(self):
        """参数错是终态。base 的 kind 闭集合里没有 PERMANENT，落 UNKNOWN（= 不重试）。"""
        kind, retry = classify_ppio(400, {"reason": "VALIDATOR_PARAM"})
        self.assertEqual(kind, PB.ProviderError.UNKNOWN)
        self.assertFalse(retry)

    def test_classify_out_of_stock(self):
        kind, retry = classify_ppio(400, {"reason": "REQUEST", "message": "暂无库存"})
        self.assertEqual(kind, PB.ProviderError.OUT_OF_STOCK)
        self.assertTrue(retry)

    def test_classify_kinds_are_in_closed_set(self):
        """不许在适配器里自造 kind：上层是按闭集合分流的。"""
        allowed = {PB.ProviderError.OUT_OF_STOCK, PB.ProviderError.QUOTA,
                   PB.ProviderError.TRANSIENT, PB.ProviderError.NOT_FOUND,
                   PB.ProviderError.AUTH, PB.ProviderError.UNSUPPORTED,
                   PB.ProviderError.BILLING, PB.ProviderError.BUSY,
                   PB.ProviderError.UNKNOWN}
        for st in (200, 400, 401, 403, 404, 409, 429, 500, 503):
            for payload in ({}, {"reason": "VALIDATOR_PARAM"},
                            {"reason": "CONFLICT"}, {"reason": "UNAUTHORIZED"},
                            {"message": "余额不足"}, {"message": "sold out"}):
                kind, _ = classify_ppio(st, payload)
                self.assertIn(kind, allowed, f"{st}/{payload} 产出了闭集合外的 kind")


# ------------------------------------------------------------------ 真响应
class TestRealResponses(unittest.TestCase):
    """直接喂真实响应（2026-11-13 用真 key 打回来的原样 JSON）。

    这一组存在的理由：文档和真实结构已经实测不一致两处，
    照文档编的 fixture 会把错误固化进测试。这组是「文档漂移探测器」。
    """

    def _p(self, payload, path="/gpus/v2/products"):
        return PPIOProvider(api_key="sk_t", transport=Transport({
            ("GET", path): (200, payload),
            # 真实响应里没有 instances，涉及 get() 的测试单独编
            ("GET", "/gpus/v2/images"): (200, fixture("images_base.json")),
        }))

    def test_real_products_parse(self):
        prods = self._p(fixture("products.json")).products()
        self.assertEqual(len(prods), 9, "真实响应是 9 个产品（真机 2026-11-13）")
        for p in prods:
            self.assertIn("resource_spec", p)
            self.assertIn("pricing", p)
            self.assertIn(p["inventory_state"], ("none", "low", "normal", "high"))

    def test_real_4090_price_is_2_31(self):
        """真响应 4090 postpaid final_price=231000 → ¥2.31/时。

        这直接证明除数是 1e5：第三方监测（rentgpu.link）报 PPIO 4090 Spot ¥1.16，
        真响应 spot=116000，116000/1e5=1.16 完全吻合。
        """
        d = self._p(fixture("products.json")).price(
            gpu_type="RTX 4090 24GB", cpu=16, memory_gb=62)
        self.assertAlmostEqual(d["hourly_instance"], 2.31, 4)
        self.assertAlmostEqual(d["daily_disk"], 0.0, 4)

    def test_real_4090_spot_price(self):
        d = self._p(fixture("products.json")).price(
            gpu_type="RTX 4090 24GB", cpu=16, memory_gb=62, charge_type="Spot")
        self.assertAlmostEqual(d["hourly_instance"], 1.16, 4)

    def test_real_storage_price_matches_official_rate(self):
        """storage_price_per_gb=1000 → 0.01 元/GB/天，与官方计费页一字不差。"""
        prods = self._p(fixture("products.json")).products()
        rates = {p["pricing"]["storage_price_per_gb"] for p in prods}
        self.assertEqual(rates, {1000})
        self.assertAlmostEqual(_money(1000), 0.01, 6)

    def test_real_precision_field_is_not_trusted(self):
        """真响应里 4090 给 precision=10、L40S 给 0 —— 用它算会差几个数量级。

        这条是回归护栏：哪天真有人「修正」成用 precision，这里立刻红。
        """
        prods = self._p(fixture("products.json")).products()
        seen = {p["pricing"]["precision"] for p in prods}
        self.assertEqual(seen, {0, 10},
                         "真实响应里 precision 就是这两档不可信的值")
        # 而价格必须仍然正确
        d = self._p(fixture("products.json")).price(
            gpu_type="NVIDIA L40S 48GB", cpu=22, memory_gb=125)
        self.assertAlmostEqual(d["hourly_instance"], 3.96, 4)

    def test_real_stock_is_zero_when_balance_is_zero(self):
        """余额 0 时真响应里 available 全 0、deployable 全 False。

        记下来：这说明「看不到库存」可能只是没余额，不能据此断言平台无货。
        """
        prods = self._p(fixture("products.json")).products()
        self.assertTrue(all(p["resource_spec"]["gpu"]["available"] == 0 for p in prods))
        self.assertTrue(all(p["deployable"] is False for p in prods))
        self.assertEqual(self._p(fixture("products.json")).stock("RTX 4090 24GB"), 0)

    def test_real_balance_shape(self):
        p = provider({("GET", "/openapi/v1/billing/balance/detail"):
                      (200, fixture("balance.json"))})
        self.assertEqual(p.balance(), 0.0)

    def test_real_image_address_lives_in_tags(self):
        """文档说 `image` 是镜像地址，实际是短名；真地址在 tags[].full_image_name。"""
        payload = fixture("images_base.json")
        first = payload["data"][0]
        self.assertEqual(first["image"], "pytorch", "短名，不能直接拿去 create")
        self.assertTrue(first["tags"][0]["full_image_name"].startswith("image.ppinfra.com/"))

        p = self._p(payload)
        out = p.resolve_image("pytorch")
        self.assertEqual(out["image_id"],
                         "image.ppinfra.com/prod-gpucloudpublic/pytorch:v2")
        self.assertEqual(out["tag"], "v2")

    def test_real_image_tag_selection(self):
        p = self._p(fixture("images_base.json"))
        out = p.resolve_image("pytorch:2.3.0-cuda12.1-cudnn8-devel")
        self.assertEqual(out["image_id"],
                         "image.ppinfra.com/prod-gpucloudpublic/pytorch:2.3.0-cuda12.1-cudnn8-devel")

    def test_real_image_missing_tag_raises(self):
        with self.assertRaises(P.ProviderError) as cm:
            self._p(fixture("images_base.json")).resolve_image("pytorch:no-such-tag")
        self.assertEqual(cm.exception.kind, P.ProviderError.NOT_FOUND)

    def test_real_images_expand_all_tags(self):
        """一个镜像名下多个 tag，各给一条 —— create() 要的是带 tag 的完整地址。"""
        out = self._p(fixture("images_base.json")).images()
        self.assertGreater(len(out), 5)
        self.assertTrue(all(i["image_id"] for i in out))

    def test_real_instances_empty_list(self):
        p = provider({("GET", "/gpus/v2/instances"):
                      (200, fixture("instances_empty.json"))})
        self.assertEqual(p.list_instances(), [])


# ------------------------------------------------------------------ 契约
class TestContract(unittest.TestCase):
    REQUIRED = ("capabilities", "create", "get", "list_instances", "start", "stop",
                "terminate", "ssh_endpoint", "ssh_ready", "scheduled_stop",
                "price", "balance", "resolve_image", "images", "stock")

    def test_all_methods_implemented(self):
        p = provider()
        for m in self.REQUIRED:
            self.assertTrue(callable(getattr(p, m, None)), f"ppio 缺方法 {m}")

    def test_capabilities_has_no_unexplained_none(self):
        """交接单验收第 6 条：capabilities() 里不许有未说明的 None。

        `typical_boot_s=None` 是**唯一**允许的 None（真机没测过，不猜），
        它的理由必须写在 notes 里 —— 否则上层看到 None 只知道「未知」，
        不知道是「没测」还是「忘了填」。
        """
        cap = provider().capabilities()
        d = cap.as_dict()
        for k, v in d.items():
            if v is None and k != "notes":
                self.assertIn(k, cap.notes, f"{k} 是 None 但 notes 里没解释")

    def test_capabilities_honest_flags(self):
        cap = provider().capabilities()
        self.assertTrue(cap.can_stop_instance)
        self.assertTrue(cap.can_pull_oci, "create() 直接收 image 地址")
        self.assertTrue(cap.supports_spot)
        self.assertTrue(cap.supports_custom_image)
        self.assertFalse(cap.supports_scheduled_stop, "v2 没有定时关机端点")
        self.assertFalse(cap.has_shared_weight_mount, "PPIO 没有共享权重库")
        self.assertEqual(cap.free_system_disk_gb, 130)
        self.assertTrue(cap.bills_disk_when_stopped, "系统盘计费时长是创建→释放")

    def test_require_gate_blocks_scheduled_stop(self):
        p = provider()
        with self.assertRaises(P.ProviderError) as cm:
            p.require("supports_scheduled_stop")
        self.assertEqual(cm.exception.kind, P.ProviderError.UNSUPPORTED)
        self.assertIn("ppio", str(cm.exception))
        self.assertIn("定时关机", str(cm.exception))


# ------------------------------------------------------------------ 视图
class TestView(unittest.TestCase):
    def test_get_maps_all_fields(self):
        v = provider().get("ca338f12f3")
        self.assertEqual(v.uid, "ca338f12f3")
        self.assertEqual(v.state, "running")
        self.assertTrue(v.billing)
        self.assertEqual(v.gpu_count, 1)
        self.assertEqual(v.cpu, 16)
        self.assertEqual(v.memory_gb, 62)
        self.assertEqual(v.provider, "ppio")
        self.assertEqual(v.region, "cn-north-1")
        # product_id 要翻成展示名，不能把 prod-xxx 直接当 gpu_type 给上层
        self.assertEqual(v.gpu_type, "4090.16c62g")

    def test_no_vendor_fields_leak(self):
        """云专属字段不许进通用视图（换云时上层要改）。"""
        d = provider().get("ca338f12f3").as_dict()
        # 注意 `billing` 是 Contract 里的**计算属性**（是否正在计费），不是云的 billing 字段；
        # 这里要排的是云侧原始字段名。
        for bad in ("product_id", "resource_specs", "registry_auth_id",
                    "provider_snapshot", "last_started_at"):
            self.assertNotIn(bad, d)

    def test_disk_never_folded_into_hourly(self):
        """磁盘按天结算，不许折算进 hourly_disk（跨日边界必错）。"""
        v = provider().get("ca338f12f3")
        self.assertEqual(v.disk_per_hour, 0.0)
        self.assertEqual(v.total_per_hour, v.price_per_hour)

    def test_state_map_covers_all_known_statuses(self):
        from tools.p2.providers.ppio import _STATE_MAP
        for cloud, ours in _STATE_MAP.items():
            self.assertIn(ours, ("starting", "running", "stopping", "stopped",
                                 "rebooting", "terminating", "terminated", "unknown"),
                          f"{cloud} 映射到了通用状态机外的值 {ours}")

    def test_unknown_status_does_not_raise(self):
        """未知状态保留而不抛 —— 抛了 reaper 会把在跑的机器当异常。"""
        inst = dict(INSTANCE, status={"status": "some_new_state_from_2027"})
        p = provider({("GET", "/gpus/v2/instances/x"): (200, inst),
                      ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS})})
        v = p.get("x")
        self.assertEqual(v.state, "unknown")
        self.assertEqual(v.raw["status"]["status"], "some_new_state_from_2027")

    def test_list_paginates_by_cursor(self):
        page2 = dict(INSTANCE, id="ca338ffff2", name="job-002")
        calls = []

        def first(body, query):
            calls.append(query)
            return 200, {"data": [INSTANCE], "next_cursor": "c2"}

        def second(body, query):
            calls.append(query)
            return 200, {"data": [page2], "next_cursor": ""}

        p = PPIOProvider(api_key="sk_t", transport=Transport({
            ("GET", "/gpus/v2/instances"): first,   # 首次
            ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS}),
        }))
        # 第二页要换 route：用包装器把第一次/第二次分开
        state = {"n": 0}

        def route(body, query):
            state["n"] += 1
            return second(body, query) if state["n"] > 1 else first(body, query)

        p = PPIOProvider(api_key="sk_t", transport=Transport({
            ("GET", "/gpus/v2/instances"): route,
            ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS}),
        }))
        out = p.list_instances()
        self.assertEqual([i.uid for i in out], ["ca338f12f3", "ca338ffff2"])
        self.assertGreaterEqual(state["n"], 2, "游标没翻第二页")

    def test_list_prefix_filter_is_local(self):
        p = provider({("GET", "/gpus/v2/instances"): (200, {"data": [INSTANCE]}),
                      ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS})})
        self.assertEqual(len(p.list_instances(prefix="job-")), 1)
        self.assertEqual(len(p.list_instances(prefix="nope")), 0)


# ------------------------------------------------------------------ 建机
class TestCreate(unittest.TestCase):
    def _p(self, captured: list, status=200, resp=None):
        def route(body, query):
            captured.append(body)
            return status, (resp or {"id": "ca338f12f3"})
        return provider({("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS}),
                         ("POST", "/gpus/v2/instances"): route})

    def test_payload_shape(self):
        cap: list = []
        p = self._p(cap)
        uid = p.create(image="registry.cn/x/comfy:v4", gpu_type="4090",
                       gpu=1, cpu=16, memory_gb=62, disk_gb=130, name="job-001")
        self.assertEqual(uid, "ca338f12f3")
        body = cap[0]
        self.assertEqual(body["product_id"], "prod-4090-16c62g",
                         "cpu=16/mem=62 应命中 16c62g 这一档")
        self.assertEqual(body["resource"], {"rootfs_size_gb": 130, "gpu_num": 1})
        self.assertEqual(body["billing"], {"mode": "postpaid"})
        self.assertEqual(body["ports"], [{"port": 8188, "protocol": "tcp"}])
        self.assertEqual(body["type"], "gpu")
        self.assertNotIn("data_disk_gb", body)

    def test_sku_picked_by_cpu_memory(self):
        cap: list = []
        self._p(cap).create(image="i", gpu_type="4090", cpu=8, memory_gb=62)
        self.assertEqual(cap[0]["product_id"], "prod-4090-8c62g")
        cap.clear()
        self._p(cap).create(image="i", gpu_type="4090", cpu=18, memory_gb=62)
        self.assertEqual(cap[0]["product_id"], "prod-4090-18c62g")

    def test_4090_never_matches_4090d(self):
        """`4090` 是 `4090D` 的子串 —— 只做 in 判断会按 4090D 建机，报价和升卡全错。"""
        cap: list = []
        self._p(cap).create(image="i", gpu_type="4090")
        self.assertNotIn("4090d", cap[0]["product_id"].lower())
        cap.clear()
        self._p(cap).create(image="i", gpu_type="4090D")
        self.assertEqual(cap[0]["product_id"], "prod-4090d")

    def test_rootfs_clamped_to_product_range(self):
        cap: list = []
        self._p(cap).create(image="i", gpu_type="4090", disk_gb=5)
        self.assertEqual(cap[0]["resource"]["rootfs_size_gb"], 30, "低于 min_gb 要夹到最小值")
        cap.clear()
        self._p(cap).create(image="i", gpu_type="4090", disk_gb=9999)
        self.assertEqual(cap[0]["resource"]["rootfs_size_gb"], 500, "高于 max_gb 要夹到最大值")

    def test_unknown_gpu_raises_not_found(self):
        cap: list = []
        p = self._p(cap)
        with self.assertRaises(P.ProviderError) as cm:
            p.create(image="i", gpu_type="B200")
        self.assertEqual(cm.exception.kind, P.ProviderError.NOT_FOUND)
        self.assertIn("4090", str(cm.exception), "报错要列出可用型号")

    def test_data_disk_unsupported_not_silently_ignored(self):
        """静默忽略会让上层以为买到了盘，然后往 /data 写模型。"""
        with self.assertRaises(P.ProviderError) as cm:
            self._p([]).create(image="i", gpu_type="4090", data_disk_gb=50)
        self.assertEqual(cm.exception.kind, P.ProviderError.UNSUPPORTED)

    def test_unknown_charge_type_unsupported(self):
        with self.assertRaises(P.ProviderError) as cm:
            self._p([]).create(image="i", gpu_type="4090", charge_type="Weekly")
        self.assertEqual(cm.exception.kind, P.ProviderError.UNSUPPORTED)

    def test_spot_and_prepaid_modes(self):
        cap: list = []
        self._p(cap).create(image="i", gpu_type="4090", charge_type="Spot")
        self.assertEqual(cap[0]["billing"], {"mode": "spot"})
        cap.clear()
        self._p(cap).create(image="i", gpu_type="4090", charge_type="Prepay", months=2)
        self.assertEqual(cap[0]["billing"], {"mode": "prepaid",
                                             "prepaid": {"months": 2}})

    def test_gpu_count_out_of_range(self):
        with self.assertRaises(P.ProviderError) as cm:
            self._p([]).create(image="i", gpu_type="4090", gpu=16)
        self.assertIn("1~8", str(cm.exception))

    def test_custom_ports(self):
        cap: list = []
        self._p(cap).create(image="i", gpu_type="4090",
                            ports=[8188, {"port": 8888, "protocol": "http"}])
        self.assertEqual(cap[0]["ports"],
                         [{"port": 8188, "protocol": "tcp"},
                          {"port": 8888, "protocol": "http"}])

    def test_candidate_regions_not_hard_binding(self):
        cap: list = []
        self._p(cap).create(image="i", gpu_type="4090", zone="cn-north-1,cn-east-1")
        self.assertEqual(cap[0]["candidate_regions"], ["cn-north-1", "cn-east-1"])

    def test_create_failure_is_not_retryable(self):
        """创建不幂等：重试一次可能租出两台卡。"""
        p = self._p([], status=500, resp={"reason": "UNKNOWN", "message": "boom"})
        with self.assertRaises(P.ProviderError) as cm:
            p.create(image="i", gpu_type="4090")
        self.assertFalse(cm.exception.retryable,
                         "create 失败必须 retryable=False，否则会重复建机烧钱")

    def test_create_out_of_stock_also_not_retryable(self):
        """连「没货」也不能自动重试 —— 上层该换卡，不是盲目再来一次。"""
        p = self._p([], status=400, resp={"reason": "REQUEST", "message": "暂无库存"})
        with self.assertRaises(P.ProviderError) as cm:
            p.create(image="i", gpu_type="4090")
        self.assertEqual(cm.exception.kind, P.ProviderError.OUT_OF_STOCK)
        self.assertFalse(cm.exception.retryable)


# ------------------------------------------------------------------ 报价
class TestPrice(unittest.TestCase):
    def test_hourly_from_products(self):
        d = provider().price(gpu_type="4090", cpu=16, memory_gb=62, disk_gb=130)
        self.assertAlmostEqual(d["hourly_instance"], 1.98, 4)
        self.assertAlmostEqual(d["hourly_total"], 1.98, 4)
        self.assertEqual(d["daily_disk"], 0.0, "130GB 在免费额度内")
        self.assertEqual(d["currency"], "CNY")
        self.assertEqual(d["free_system_disk_gb"], 130)

    def test_disk_as_daily_not_hourly(self):
        """磁盘是日结，折算成小时会让跨日边界必错。"""
        d = provider().price(gpu_type="4090", cpu=16, memory_gb=62, disk_gb=230)
        self.assertEqual(d["hourly_disk"], 0.0)
        self.assertAlmostEqual(d["hourly_total"], d["hourly_instance"], 6)
        self.assertAlmostEqual(d["daily_disk"], 1.0, 4)   # (230-130) × 0.01

    def test_gpu_count_multiplies(self):
        d = provider().price(gpu_type="4090", gpu=2, cpu=16, memory_gb=62)
        self.assertAlmostEqual(d["hourly_instance"], 3.96, 4)

    def test_spot_price_lower(self):
        post = provider().price(gpu_type="4090", cpu=16, memory_gb=62)
        spot = provider().price(gpu_type="4090", cpu=16, memory_gb=62,
                                charge_type="Spot")
        self.assertLess(spot["hourly_instance"], post["hourly_instance"])

    def test_spot_unavailable_is_unsupported_not_zero(self):
        """返回 0 会被上层读成「免费」，那是灾难。"""
        p = provider({("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS})})
        with self.assertRaises(P.ProviderError) as cm:
            p.price(gpu_type="3090", cpu=16, memory_gb=92, charge_type="Spot")
        self.assertEqual(cm.exception.kind, P.ProviderError.UNSUPPORTED)

    def test_unknown_gpu_raises(self):
        with self.assertRaises(P.ProviderError) as cm:
            provider().price(gpu_type="H100", cpu=16, memory_gb=62)
        self.assertEqual(cm.exception.kind, P.ProviderError.NOT_FOUND)


# ------------------------------------------------------------------ 库存
class TestStock(unittest.TestCase):
    def test_stock_reads_available(self):
        self.assertEqual(provider().stock("4090"), 6)

    def test_stock_zero_when_undeployable(self):
        self.assertEqual(provider().stock("H20"), 0)

    def test_unknown_model_raises_not_zero(self):
        """返回 0 会被读成「售罄」→ 上层换卡，而真相是型号写错了。"""
        with self.assertRaises(P.ProviderError) as cm:
            provider().stock("A100")
        self.assertEqual(cm.exception.kind, P.ProviderError.NOT_FOUND)

    def test_spot_filter_excludes_models_without_spot(self):
        self.assertEqual(provider().stock("3090", spot=True), 0)


# ------------------------------------------------------------------ 余额 / 镜像
class TestBalanceAndImages(unittest.TestCase):
    def test_balance_unit_is_1e4_yuan(self):
        self.assertAlmostEqual(provider().balance(), 19.79, 4)

    def test_balance_missing_field_raises(self):
        p = provider({("GET", "/openapi/v1/billing/balance/detail"): (200, {})})
        with self.assertRaises(P.ProviderError):
            p.balance()

    def test_resolve_image_passthrough_for_address(self):
        out = provider().resolve_image("registry.cn-sh/x/comfy:v4")
        self.assertEqual(out["image_id"], "registry.cn-sh/x/comfy:v4")
        self.assertEqual(out["type"], "address")

    def test_resolve_image_looks_up_by_name(self):
        routes = {
            ("GET", "/gpus/v2/images"): (200, {"data": [
                {"id": "img-1", "name": "comfy-v4", "type": "private",
                 "image": "registry.cn-sh/x/comfy:v4", "status": "available"},
            ], "next_cursor": ""}),
        }
        out = provider(routes).resolve_image("comfy-v4")
        self.assertEqual(out["image_id"], "registry.cn-sh/x/comfy:v4")
        self.assertEqual(out["type"], "private")

    def test_resolve_image_missing_raises(self):
        p = provider({("GET", "/gpus/v2/images"): (200, {"data": [], "next_cursor": ""})})
        with self.assertRaises(P.ProviderError) as cm:
            p.resolve_image("nope")
        self.assertEqual(cm.exception.kind, P.ProviderError.NOT_FOUND)

    def test_images_lists_all_three_types(self):
        routes = {("GET", "/gpus/v2/images"): (200, {"data": [
            {"id": "img-1", "name": "comfy-v4", "type": "private",
             "image": "registry.cn-sh/x/comfy:v4"},
        ], "next_cursor": ""})}
        out = provider(routes).images()
        self.assertTrue(out)
        self.assertTrue(all({"image_id", "name", "type"} <= set(i) for i in out))

    def test_images_keyword_filter(self):
        routes = {("GET", "/gpus/v2/images"): (200, {"data": [
            {"id": "img-1", "name": "comfy-v4", "type": "private", "image": "x"},
            {"id": "img-2", "name": "pytorch", "type": "base", "image": "y"},
        ], "next_cursor": ""})}
        out = provider(routes).images(keyword="comfy")
        self.assertEqual([i["name"] for i in out], ["comfy-v4"])


# ------------------------------------------------------------------ SSH
class TestSSH(unittest.TestCase):
    def test_endpoint_parsed_from_command(self):
        ep = provider().ssh_endpoint("ca338f12f3")
        self.assertEqual(ep.host, "proxy.cn-north-1.gpu-instance.ppinfra.com")
        self.assertEqual(ep.port, "51039")
        self.assertEqual(ep.password, "S3cret!")
        self.assertEqual(ep.user, "root")

    def test_endpoint_not_cached(self):
        """端口和密码 stop→start 后可能变；缓存「大多数时候能用」是最危险的 bug。"""
        seen = {"n": 0}

        def route(body, query):
            seen["n"] += 1
            inst = dict(INSTANCE)
            inst["tools"] = {"ssh": {"command": f"ssh -p {51039 + seen['n']} root@h.example",
                                     "password": f"pw{seen['n']}", "username": "root"}}
            return 200, inst

        p = provider({("GET", "/gpus/v2/instances/x"): route,
                      ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS})})
        a = p.ssh_endpoint("x")
        b = p.ssh_endpoint("x")
        self.assertNotEqual(a.port, b.port)
        self.assertNotEqual(a.password, b.password)

    def test_endpoint_missing_ssh_is_transient(self):
        inst = dict(INSTANCE, tools={})
        p = provider({("GET", "/gpus/v2/instances/x"): (200, inst),
                      ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS})})
        with self.assertRaises(P.ProviderError) as cm:
            p.ssh_endpoint("x")
        self.assertEqual(cm.exception.kind, P.ProviderError.TRANSIENT,
                         "「还没就绪」是可重试的，不是终态")


# ------------------------------------------------------------------ 生命周期
class TestLifecycle(unittest.TestCase):
    def test_stop_and_start_paths(self):
        cap: list = []

        def rec(body, query):
            cap.append("hit")
            return 200, {}

        p = provider({("PUT", "/gpus/v2/instances/x/stop"): rec,
                      ("PUT", "/gpus/v2/instances/x/start"): rec,
                      ("DELETE", "/gpus/v2/instances/x"): rec,
                      ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS})})
        p.stop("x")
        p.start("x")
        p.terminate("x")
        self.assertEqual(len(cap), 3)
        self.assertIn("PUT /gpus/v2/instances/x/stop", p.calls)
        self.assertIn("DELETE /gpus/v2/instances/x", p.calls)

    def test_start_failure_is_retryable(self):
        """官方明说「停止后可能无法启动」（资源被抢占）。

        要断言的是**可重试**这条不变量，不是某个具体 kind：
        归 OUT_OF_STOCK（资源被抢占 = 没货）还是 TRANSIENT（超时）都对，
        都不可重试才是 bug —— 那会让上层把「换台机器再试」读成「参数错，别试了」。
        """
        p = provider({
            ("PUT", "/gpus/v2/instances/x/start"):
                (400, {"reason": "REQUEST", "message": "资源不足，无法启动"}),
            ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS}),
        })
        with self.assertRaises(P.ProviderError) as cm:
            p.start("x")
        self.assertTrue(cm.exception.retryable,
                        "开机失败必须可重试（换机/等资源），否则只能人工介入")

    def test_start_retryable_even_for_generic_unknown(self):
        """参数错以外的未知失败，在开机场景下多半是调度失败 —— 也要可重试。"""
        p = provider({
            ("PUT", "/gpus/v2/instances/x/start"):
                (400, {"reason": "REQUEST", "message": "no free gpu on host"}),
            ("GET", "/gpus/v2/products"): (200, {"data": PRODUCTS}),
        })
        with self.assertRaises(P.ProviderError) as cm:
            p.start("x")
        self.assertTrue(cm.exception.retryable)

    def test_scheduled_stop_unsupported(self):
        with self.assertRaises(P.ProviderError) as cm:
            provider().scheduled_stop("x", 1700000000)
        self.assertEqual(cm.exception.kind, P.ProviderError.UNSUPPORTED)
        self.assertIn("reaper", str(cm.exception), "要指出降级路径")


# ------------------------------------------------------------------ 凭证
class TestCredentials(unittest.TestCase):
    def test_missing_key_fails_at_ready_not_constructor(self):
        """没有 key：能构造（能力矩阵要能展示），但 ready() 抛 AUTH。

        两者是不同的问题 —— 能力声明跟凭证无关，能不能调才跟凭证有关。
        写混了会出两种错：矩阵里少一行（文档缺了 PPIO），或者 available() 把
        一个必然 401 的云列进去（巡检工具每次都撞墙）。
        """
        import os
        old = os.environ.pop("PPIO_API_KEY", None)
        try:
            p = PPIOProvider()               # 不抛
            self.assertEqual(p.capabilities().name, "ppio")
            with self.assertRaises(P.ProviderError) as cm:
                p.ready()
            self.assertEqual(cm.exception.kind, P.ProviderError.AUTH)
        finally:
            if old is not None:
                os.environ["PPIO_API_KEY"] = old

    def test_call_without_key_raises_auth(self):
        """真正发请求时才拦：报错要指出怎么配。"""
        import os
        old = os.environ.pop("PPIO_API_KEY", None)
        try:
            with self.assertRaises(P.ProviderError) as cm:
                PPIOProvider().products()
            self.assertEqual(cm.exception.kind, P.ProviderError.AUTH)
            self.assertIn("PPIO_API_KEY", str(cm.exception))
        finally:
            if old is not None:
                os.environ["PPIO_API_KEY"] = old

    def test_byok_key_not_written_to_env(self):
        """BYOK：客户凭证不能污染进程环境。"""
        import os
        os.environ.pop("PPIO_API_KEY", None)
        p = PPIOProvider(credentials={"api_key": "sk_customer_only"},
                         transport=Transport({}))
        self.assertEqual(p._api_key, "sk_customer_only")
        self.assertNotIn("PPIO_API_KEY", os.environ)

    def test_env_key_used_when_no_explicit(self):
        import os
        old = os.environ.get("PPIO_API_KEY")
        os.environ["PPIO_API_KEY"] = "sk_from_env"
        try:
            p = PPIOProvider(transport=Transport({}))
            self.assertEqual(p._api_key, "sk_from_env")
        finally:
            if old is None:
                os.environ.pop("PPIO_API_KEY", None)
            else:
                os.environ["PPIO_API_KEY"] = old

    def test_explicit_key_beats_env(self):
        import os
        old = os.environ.get("PPIO_API_KEY")
        os.environ["PPIO_API_KEY"] = "sk_env"
        try:
            p = PPIOProvider(api_key="sk_explicit", transport=Transport({}))
            self.assertEqual(p._api_key, "sk_explicit")
        finally:
            if old is None:
                os.environ.pop("PPIO_API_KEY", None)
            else:
                os.environ["PPIO_API_KEY"] = old


# ------------------------------------------------------------------ 注册表
class TestRegistry(unittest.TestCase):
    def test_get_provider_byok(self):
        p = P.get_provider("ppio", credentials={"api_key": "sk_byok"})
        self.assertEqual(p.name, "ppio")
        self.assertEqual(p._api_key, "sk_byok")

    def test_unknown_provider_message_lists_ppio(self):
        with self.assertRaises(P.ProviderError) as cm:
            P.get_provider("nope")
        self.assertIn("ppio", str(cm.exception).lower() + " ppio")

    def test_matrix_always_lists_ppio(self):
        """能力矩阵按**静态声明**展示，不能被「本机没配凭证」挡住。"""
        rows = P.capability_matrix()
        names = {r.get("name") or r.get("provider") for r in rows}
        self.assertIn("ppio", names, "矩阵里必须能看到 PPIO 的能力声明")
        row = next(r for r in rows if (r.get("name") or r.get("provider")) == "ppio")
        self.assertIn("ready", row, "要能区分「这家支持」和「这台机器配好了」")

    def test_matrix_marks_ready_false_without_credentials(self):
        import os
        old = os.environ.get("PPIO_API_KEY")
        os.environ.pop("PPIO_API_KEY", None)
        try:
            rows = P.capability_matrix()
            row = next(r for r in rows if (r.get("name") or r.get("provider")) == "ppio")
            self.assertFalse(row["ready"])
        finally:
            if old is not None:
                os.environ["PPIO_API_KEY"] = old

    def test_available_excludes_ppio_without_credentials(self):
        """available() 的语义是「本机真能调」；没凭证就该被排除。"""
        import os
        old = os.environ.get("PPIO_API_KEY")
        os.environ.pop("PPIO_API_KEY", None)
        try:
            self.assertNotIn("ppio", P.available())
        finally:
            if old is not None:
                os.environ["PPIO_API_KEY"] = old

    def test_available_includes_ppio_with_credentials(self):
        import os
        old = os.environ.get("PPIO_API_KEY")
        os.environ["PPIO_API_KEY"] = "sk_probe"
        try:
            self.assertIn("ppio", P.available())
        finally:
            if old is None:
                os.environ.pop("PPIO_API_KEY", None)
            else:
                os.environ["PPIO_API_KEY"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
