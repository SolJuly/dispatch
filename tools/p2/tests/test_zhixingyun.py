#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""智星云 provider 离线测试：零网络、零花费，全部用真实抓来的 fixture 驱动。

fixture 来源（2026-11-13 用官方文档示例凭证打的只读接口，已脱敏）：
  account_info / price_info / specs_gpu1 / selector / configs / image_info /
  instance_list / instance_detail / query_price / dispatch_status
  以及三类真实业务错误：err_auth（sign 验证失败）、err_not_found、
  err_out_of_stock（没有可用的gpu资源）、err_bad_param

覆盖点（对照交接单「五、测试要求」）：
  · create() 参数构造正确（spec 选择、due_mode 必须 -1、ext_rid 幂等）
  · 状态映射覆盖全部云侧状态（含 Status=1+IsAbnormal=2 的关机态）
  · 错误码分类（中文文案 → kind/retryable）
  · 不支持的能力正确抛 UNSUPPORTED（stop / scheduled_stop / 非法卡数）
  · 签名算法与官方文档样例逐字节一致
  · BYOK 凭证隔离 + 缺凭证报 AUTH
  · balance() 不把算力券混进现金余额

用法：python3 -m unittest tools.p2.tests.test_zhixingyun
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p2.providers import ProviderError as PE  # noqa: E402
from tools.p2.providers import zhixingyun as Z  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "zxy"


def load(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


class FakeTransport:
    """按 path 返回 fixture，并记录每次请求（含签名后的完整参数）。"""

    def __init__(self, routes: dict[str, dict] | None = None):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []
        self.fail_times = 0          # 前 N 次抛网络错误，用于测重试

    def __call__(self, path: str, params: dict, timeout: int) -> str:
        self.calls.append((path, dict(params)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise TimeoutError("simulated timeout")
        route = self.routes.get(path)
        if callable(route):
            return json.dumps(route(params), ensure_ascii=False)
        if route is None:
            raise AssertionError(f"未登记的请求路径：{path}")
        return json.dumps(route, ensure_ascii=False)

    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    def last(self, path: str | None = None) -> dict:
        for p, params in reversed(self.calls):
            if path is None or p == path:
                return params
        raise AssertionError(f"没有请求 {path}")


def detail_with(status: int, abnormal: int = 0, **over) -> dict:
    """拿真实详情 fixture，只改 Status/IsAbnormal，得到指定状态的响应。"""
    payload = json.loads((FIX / "instance_detail.json").read_text(encoding="utf-8"))
    payload["data"].update({"Status": status, "IsAbnormal": abnormal, **over})
    return payload


def provider(routes: dict, **kw) -> tuple[Z.ZhiXingYunProvider, FakeTransport]:
    t = FakeTransport(routes)
    p = Z.ZhiXingYunProvider(
        {"ZXY_API_KEY": "ak-test", "ZXY_API_SECRET": "sk-test"},
        transport=t, retries=3, **kw)
    return p, t


# ------------------------------------------------------------------ 签名
class TestSignature(unittest.TestCase):
    """签名错了整条链路 401，且 md5 没法靠肉眼 debug，必须钉死。"""

    def test_matches_official_example(self):
        """接口签名文档 doc-5644959 给的原文样例，逐字节对齐。"""
        import hashlib
        params = {"apikey": "8b90cf872569460a", "timestamp": 1733814154,
                  "nonce": "CrpsYHp", "param1": "iamcoolman", "param2": 18,
                  "param3": "true", "param4": ""}
        expect = hashlib.md5(
            "apikey=8b90cf872569460a&nonce=CrpsYHp&param1=iamcoolman"
            "&param2=18&param3=true&timestamp=1733814154&secret=SK"
            .encode()).hexdigest()
        self.assertEqual(Z.sign_params(params, "SK"), expect)

    def test_empty_value_excluded(self):
        """空字符串不参与签名（文档明确：param4 值为空所以不参与）。"""
        a = Z.sign_params({"a": "1", "b": "", "c": None}, "s")
        b = Z.sign_params({"a": "1"}, "s")
        self.assertEqual(a, b)

    def test_sorted_by_key_not_by_value(self):
        a = Z.sign_params({"b": "zzz", "a": "yyy"}, "s")
        import hashlib
        self.assertEqual(a, hashlib.md5(b"a=yyy&b=zzz&secret=s").hexdigest())

    def test_request_carries_apikey_timestamp_nonce_sign(self):
        p, t = provider({"account/get_main_account_info": load("account_info")})
        p.balance()
        params = t.last()
        for k in ("apikey", "timestamp", "nonce", "sign"):
            self.assertIn(k, params)
        self.assertEqual(params["apikey"], "ak-test")
        self.assertTrue(len(params["nonce"]) >= 8, "nonce 建议 8 位以上")
        self.assertEqual(params["sign"], Z.sign_params(params, "sk-test"))


# ------------------------------------------------------------------ 状态映射
class TestStateMapping(unittest.TestCase):
    def test_all_cloud_states_mapped(self):
        """云侧每个 Status（以及 Status=1+IsAbnormal=2）都要有确定映射。"""
        cases = load("state_cases")
        expect = {
            "running": "running", "shutdown": "stopped", "starting": "starting",
            "rebooting": "rebooting", "kept_disk": "terminated",
            "error": "error", "refunded": "terminated",
            "released": "terminated", "restart_failed": "error",
            "unknown": "unknown",
        }
        for name, payload in cases["cases"].items():
            d = payload["data"]
            got = Z.ZhiXingYunProvider._state(d["Status"], d["IsAbnormal"])
            self.assertEqual(got, expect[name],
                             f"Status={d['Status']} IsAbnormal={d['IsAbnormal']} "
                             f"应映射为 {expect[name]}，实际 {got}")

    def test_every_status_in_map_is_reachable(self):
        """STATE_MAP 里没有死条目 —— 每个值都能被真实 Status 命中。"""
        hit = {Z.ZhiXingYunProvider._state(s, 0) for s in Z.STATE_MAP}
        self.assertTrue(set(Z.STATE_MAP.values()) <= hit | {"stopped"})

    def test_billing_semantics_on_states(self):
        """只有 running/rebooting 计费；关机态与保留磁盘都不该算计费中。"""
        for status, abnormal, billing in ((1, 0, True), (5, 0, True),
                                          (1, 2, False), (8, 0, False),
                                          (0, 0, False)):
            p, _ = provider({"instance/get_instance_detail":
                             detail_with(status, abnormal)})
            v = p.get("x")
            self.assertEqual(v.billing, billing,
                             f"Status={status} IsAbnormal={abnormal} 计费应为 {billing}")

    def test_gpu_type_cpu_instance_has_no_gpu(self):
        """CPU 机的 Gpu_type 是字符串 'CPU'、Gpu_num=0，视图里必须清空。"""
        payload = detail_with(1, 0, Gpu_type="CPU", Gpu_num=0)
        p, _ = provider({"instance/get_instance_detail": payload})
        v = p.get("x")
        self.assertEqual(v.gpu_type, "")
        self.assertEqual(v.gpu_count, 0)


# ------------------------------------------------------------------ create
class TestCreate(unittest.TestCase):
    def _specs(self):
        return load("specs_gpu1")

    def test_picks_cheapest_spec_meeting_request(self):
        """请求 1卡/16核/32GB → 应该落到 gcs.g1.xlarge，而不是最便宜的 gcs.g1.base。"""
        routes = {
            "store/get_instance_specifications": self._specs(),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": {
                                              "Container_name": "186_x_u1_c0"}},
        }
        p, t = provider(routes)
        uid = p.create(image="ubuntu24_nvram_base", gpu_type="GeForce RTX 4090",
                       gpu=1, cpu=16, memory_gb=32)
        self.assertEqual(uid, "186_x_u1_c0")
        params = t.last("store/create_kvm_instance")
        self.assertEqual(params["spec_name"], "gcs.g1.xlarge")
        self.assertEqual(params["gpu_num"], 1)
        self.assertEqual(params["gpu_type"], "GeForce RTX 4090")
        self.assertEqual(params["image_name"], "ubuntu24_nvram_base")

    def test_due_mode_always_release(self):
        """due_mode 必须 -1：留 1 会「到期保留磁盘」并继续按小时扣钱。"""
        p, t = provider({
            "store/get_instance_specifications": self._specs(),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": "186_y"},
        })
        p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1, cpu=8, memory_gb=16)
        self.assertEqual(t.last("store/create_kvm_instance")["due_mode"], -1)

    def test_keep_disk_on_expire_flips_due_mode(self):
        """要保留磁盘做实验时必须显式 due_mode=1，默认是 -1（到期释放）。"""
        p, t = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": "186_k"},
        })
        p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1, cpu=8,
                 memory_gb=16, keep_disk_on_expire=True)
        self.assertEqual(t.last("store/create_kvm_instance")["due_mode"], 1)

    def test_autorenew_off(self):
        """自动续费必须显式关掉，否则租期到了会继续扣。"""
        p, t = provider({
            "store/get_instance_specifications": self._specs(),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": "186_z"},
        })
        p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1, cpu=8, memory_gb=16)
        params = t.last("store/create_kvm_instance")
        self.assertEqual(str(params["autorenew_on"]), "false")

    def test_idempotency_key_passed_as_ext_rid(self):
        p, t = provider({
            "store/get_instance_specifications": self._specs(),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": "186_i"},
        })
        p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1, cpu=8, memory_gb=16,
                 idempotency_key="job-42")
        self.assertEqual(t.last("store/create_kvm_instance")["ext_rid"], "job-42")

    def test_bad_gpu_count_unsupported(self):
        """智星云只提供 0/1/2/4/8 卡；3 卡要去创建前就拦住，不浪费一次请求。"""
        p, t = provider({})
        with self.assertRaises(PE) as cm:
            p.create(image="img", gpu_type="GeForce RTX 4090", gpu=3)
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertEqual(t.calls, [], "非法卡数不该发请求")

    def test_no_spec_match_raises_unsupported(self):
        p, _ = provider({"store/get_instance_specifications": self._specs()})
        with self.assertRaises(PE) as cm:
            p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1,
                     cpu=1024, memory_gb=4096)
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertIn("gcs.g1", str(cm.exception), "报错要给出可选近邻")

    def test_missing_container_name_falls_back_to_note_lookup(self):
        """建机响应没 Container_name 时，必须按备注回查，不能直接当没建。"""
        note = "job-abc"
        routes = {
            "store/get_instance_specifications": self._specs(),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": {}},
            "instance/get_instance_list": {"success": True, "code": "2000",
                                           "message": "", "data": {
                                               "list": [{"Container_name": "186_found",
                                                         "Note": note, "Ctime": 10}],
                                               "has_more": False}},
        }
        p, _ = provider(routes)
        uid = p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1,
                       cpu=8, memory_gb=16, name=note)
        self.assertEqual(uid, "186_found")

    def test_orphan_risk_message_when_nothing_found(self):
        p, _ = provider({
            "store/get_instance_specifications": self._specs(),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": {}},
            "instance/get_instance_list": {"success": True, "code": "2000",
                                           "message": "", "data": {"list": [],
                                                                   "has_more": False}},
        })
        with self.assertRaises(PE) as cm:
            p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1,
                     cpu=8, memory_gb=16, name="job-orphan")
        self.assertEqual(cm.exception.kind, PE.TRANSIENT)
        self.assertIn("可能已计费", str(cm.exception))


# ------------------------------------------------------------------ 错误分类
class TestErrorClassification(unittest.TestCase):
    CASES = [
        ("sign验证失败!", PE.AUTH, False),
        ("没有可用的gpu资源", PE.OUT_OF_STOCK, True),
        ("查询实例失败!<QuerySeter> no row found", PE.NOT_FOUND, False),
        ("gpu数量参数解析失败!strconv.Atoi", PE.UNKNOWN, False),
        ("余额不足，请充值", PE.BILLING, False),
        ("该状态不能启动实例!", PE.UNKNOWN, False),
        ("操作频繁，请稍后重试", PE.BUSY, True),
        ("超出配额限制", PE.QUOTA, False),
    ]

    def test_rules(self):
        for msg, kind, retry in self.CASES:
            k, r = Z.classify_zxy(msg, "4000")
            self.assertEqual(k, kind, f"{msg!r} 应归为 {kind}，实际 {k}")
            self.assertEqual(r, retry, f"{msg!r} retryable 应为 {retry}")

    def test_falls_back_to_base_classify(self):
        """本地规则没命中时，交给 base.classify（那里有跨云通用文案）。"""
        k, r = Z.classify_zxy("connection timeout", "4000")
        self.assertEqual(k, PE.TRANSIENT)
        self.assertTrue(r)

    def test_real_fixtures_raise_right_kind(self):
        for name, kind in (("err_auth", PE.AUTH),
                           ("err_out_of_stock", PE.OUT_OF_STOCK),
                           ("err_not_found", PE.NOT_FOUND)):
            payload = load(name)
            p, _ = provider({"instance/get_instance_detail": payload})
            with self.assertRaises(PE) as cm:
                p.get("x")
            self.assertEqual(cm.exception.kind, kind, f"{name} 分类错了")

    def test_auth_error_never_retried(self):
        """鉴权错误重试只会把 IP 打进黑名单，且永远好不了。"""
        p, t = provider({"account/get_main_account_info": load("err_auth")})
        with self.assertRaises(PE):
            p.balance()
        self.assertEqual(len(t.calls), 1, "AUTH 不该有第二次请求")

    def test_out_of_stock_is_retryable(self):
        p, _ = provider({"instance/get_instance_detail": load("err_out_of_stock")})
        with self.assertRaises(PE) as cm:
            p.get("x")
        self.assertTrue(cm.exception.retryable, "没货是可重试的（换卡/等释放）")

    def test_network_error_retried_then_succeeds(self):
        t = FakeTransport({"account/get_main_account_info": load("account_info")})
        t.fail_times = 2
        p = Z.ZhiXingYunProvider({"ZXY_API_KEY": "a", "ZXY_API_SECRET": "b"},
                                 transport=t, retries=3)
        self.assertAlmostEqual(p.balance(), 128.5)
        self.assertEqual(len(t.calls), 3, "前两次网络失败，第三次成功")

    def test_network_error_exhausted_is_transient(self):
        t = FakeTransport({"account/get_main_account_info": load("account_info")})
        t.fail_times = 9
        p = Z.ZhiXingYunProvider({"ZXY_API_KEY": "a", "ZXY_API_SECRET": "b"},
                                 transport=t, retries=2)
        with self.assertRaises(PE) as cm:
            p.balance()
        self.assertEqual(cm.exception.kind, PE.TRANSIENT)


# ------------------------------------------------------------------ 不支持的能力
class TestUnsupported(unittest.TestCase):
    def test_stop_is_unsupported_not_terminate(self):
        """stop 绝不能悄悄变成 terminate —— 那会把机器直接退订掉。"""
        p, t = provider({})
        with self.assertRaises(PE) as cm:
            p.stop("x")
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertIn("terminate", str(cm.exception))
        self.assertEqual(t.calls, [], "stop 不该发出任何请求")

    def test_scheduled_stop_unsupported(self):
        p, t = provider({})
        with self.assertRaises(PE) as cm:
            p.scheduled_stop("x", 1700000000)
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertEqual(t.calls, [])

    def test_require_gate_on_declared_capabilities(self):
        p, _ = provider({})
        p.require("instance_has_internet")            # 声明 True → 不抛
        with self.assertRaises(PE) as cm:
            p.require("supports_scheduled_stop")      # 声明 False → 抛
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertIn("zhixingyun", str(cm.exception))

    def test_docker_image_rejected_for_kvm(self):
        """docker 镜像不能建云主机，提前拦，别等云侧报错。"""
        p, _ = provider({"store/get_configs": load("configs")})
        with self.assertRaises(PE) as cm:
            p.resolve_image("aigalaxy:cuda12.4")
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertIn("docker", str(cm.exception))


# ------------------------------------------------------------------ 能力声明
class TestCapabilities(unittest.TestCase):
    def test_declaration_is_honest(self):
        """交接单要求：不许为了「看起来完整」乱填 True。"""
        c = provider({})[0].capabilities()
        self.assertEqual(c.name, "zhixingyun")
        self.assertFalse(c.can_stop_instance, "租期制没有停机保状态")
        self.assertTrue(c.bills_disk_when_stopped, "保留磁盘按小时计费，实测非 0")
        self.assertFalse(c.has_shared_weight_mount, "共享盘是付费商品，不是免费只读库")
        self.assertFalse(c.supports_scheduled_stop)
        self.assertFalse(c.supports_spot)
        self.assertFalse(c.can_pull_oci)
        # 已实测：CPU 21s / GPU(2080Ti) 36s 到 running（2026-11-13），取 45s 留余量
        self.assertEqual(c.typical_boot_s, 45)
        self.assertTrue(c.instance_has_internet,
                        "实测 DNS/pypi/github/docker 全通，provision.sh 可跑")

    def test_every_none_or_false_capability_has_reason(self):
        """每个「不支持 / 未知」都要在 notes 里写清原因，否则上层只能猜。"""
        c = provider({})[0].capabilities()
        for k, v in c.as_dict().items():
            if k in ("name", "notes"):
                continue
            if v is None or v is False:
                self.assertIn(k, c.notes, f"{k}={v} 但 notes 里没有原因")

    def test_registry_blocks_suspended_provider(self):
        """搁置后 registry 必须拦住，不能让人拿到实例去打真实云。

        适配器类本身保留（能力声明仍可直接构造验证），但工厂入口封了。
        """
        from tools.p2.providers import get_provider
        with self.assertRaises(PE) as cm:
            get_provider("zhixingyun")
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertIn("暂停", str(cm.exception))
        # 直接构造仍然可用：适配器与离线测试不因搁置而失效
        p = Z.ZhiXingYunProvider({})
        self.assertEqual(p.name, "zhixingyun")


# ------------------------------------------------------------------ BYOK
class TestBYOK(unittest.TestCase):
    def test_credentials_isolated_from_env(self):
        """BYOK：客户凭证必须只存在实例里，不碰 credentials 文件/进程环境。"""
        import os
        p = Z.ZhiXingYunProvider({"ZXY_API_KEY": "pk-cust", "ZXY_API_SECRET": "sk-cust"})
        self.assertEqual(p._ensure_creds(), ("pk-cust", "sk-cust"))
        self.assertNotIn("ZXY_API_KEY", os.environ, "BYOK 不能污染进程环境")

    def test_alias_keys_accepted(self):
        p = Z.ZhiXingYunProvider({"apikey": "a1", "apisecret": "s1"})
        self.assertEqual(p._ensure_creds(), ("a1", "s1"))

    def test_empty_credentials_not_byok(self):
        """空 dict 不该被当成 BYOK「用户故意给了空凭证」。"""
        p = Z.ZhiXingYunProvider({})
        self.assertFalse(p._byok)

    def test_missing_credentials_raise_auth(self):
        p = Z.ZhiXingYunProvider({"ZXY_API_KEY": "", "ZXY_API_SECRET": ""})
        p.cfg = {}
        # 搁置状态下先撞的是暂停拦截，所以这里显式解锁到凭证校验那一层
        p._test_unlocked = True
        with self.assertRaises(PE) as cm:
            p.balance()
        self.assertEqual(cm.exception.kind, PE.AUTH)
        self.assertIn("ZXY_API_KEY", str(cm.exception))

    def test_construction_without_credentials_does_not_raise(self):
        """available() 靠「构造不抛异常」判定可用性，构造必须不校验凭证。"""
        p = Z.ZhiXingYunProvider({})
        self.assertEqual(p.name, "zhixingyun")


# ------------------------------------------------------------------ 计费
class TestBilling(unittest.TestCase):
    def test_balance_returns_cash_only(self):
        """余额只返回现金：现金见底而券还有钱，是两种不同的现实。"""
        p, _ = provider({"account/get_main_account_info": load("account_info")})
        self.assertAlmostEqual(p.balance(), 128.5)
        d = p.balance_detail()
        self.assertAlmostEqual(d["money"], 128.5)
        self.assertAlmostEqual(d["power"], 5000.0)
        self.assertAlmostEqual(d["total"], 5128.5)

    def test_price_from_query_price_real_fixture(self):
        """price() 用实时询价，返回折后价并把原价/折扣留在 raw。"""
        qp = load("query_price")
        p, t = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/create_kvm_instance_query_price": qp,
        })
        pr = p.price(gpu_type="GeForce RTX 4090", gpu=1, cpu=8, memory_gb=16,
                     image="ubuntu24_nvram_base")
        sheet = qp["data"]["PackPriceInfoSheet"]
        self.assertAlmostEqual(pr["hourly_instance"], sheet["FinalCost"], 4)
        self.assertEqual(pr["hourly_disk"], 0.0, "add_disk_size=0 时不该有磁盘费")
        self.assertAlmostEqual(pr["hourly_total"], pr["hourly_instance"], 4)
        self.assertEqual(pr["currency"], "CNY")
        self.assertAlmostEqual(pr["discount"], 0.95)
        self.assertIn("raw", pr)

    def test_price_splits_data_disk(self):
        """数据盘费必须单独返回，不能混进算力单价（各家免费额度不同）。"""
        qp = load("query_price")
        sheet = qp["data"]["PackPriceInfoSheet"]
        sheet["InstanceSubPriceSheet"] = sheet["InstanceSubPriceSheet"] + [
            {"ResourceType": "disk", "Type": "add_disk", "Title": "添加数据盘:100GB",
             "SingleHourPrice": 0.0006, "Amount": 100, "HourLen": 1,
             "FinalDiscount": 0.95, "FinalCost": 0.057}]
        p, t = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/get_kvm_available_selector": load("selector"),
            "store/get_configs": load("configs"),
            "store/create_kvm_instance_query_price": qp,
        })
        pr = p.price(gpu_type="GeForce RTX 4090", gpu=1, cpu=8, memory_gb=16,
                     data_disk_gb=100)
        self.assertAlmostEqual(pr["hourly_disk"], 0.057, 4)
        self.assertAlmostEqual(pr["hourly_total"],
                               round(pr["hourly_instance"] + pr["hourly_disk"], 4), 4)
        self.assertEqual(t.last("store/create_kvm_instance_query_price")["add_disk_size"], 100)

    def test_price_never_uses_stale_static_table(self):
        """价格必须来自接口，不能读代码里的静态表（调价后仍按旧价估价=算错钱）。"""
        p, t = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/get_kvm_available_selector": load("selector"),
            "store/get_configs": load("configs"),
            "store/create_kvm_instance_query_price": load("query_price"),
        })
        p.price(gpu_type="GeForce RTX 4090", gpu=1, cpu=8, memory_gb=16)
        self.assertIn("store/create_kvm_instance_query_price", t.paths())


# ------------------------------------------------------------------ 库存 / 镜像
class TestStockAndImages(unittest.TestCase):
    def test_stock_counts_rentable_tiers(self):
        """可租档位数 = ValidCardsRentable 里为 true 的个数（不是“有几张卡”）。"""
        p, _ = provider({"store/get_kvm_available_selector": load("selector")})
        gmap = load("selector")["data"]["gpu_resource_map"]
        for name in ("GeForce RTX 3080", "GeForce RTX 4090"):
            sel = gmap[name]
            self.assertEqual(p.stock(name),
                             sum(1 for v in sel["ValidCardsRentable"].values() if v),
                             f"{name} 档位统计不对")
        # 抓 fixture 那天 3080 有货、4090 无货；两者都要如实反映
        self.assertGreater(p.stock("GeForce RTX 3080"), 0)
        self.assertEqual(p.stock("GeForce RTX 4090"), 0)

    def test_stock_zero_for_unknown_gpu(self):
        p, _ = provider({"store/get_kvm_available_selector": load("selector")})
        self.assertEqual(p.stock("不存在的卡"), 0)

    def test_stock_detail_reports_tiers_and_districts(self):
        p, _ = provider({"store/get_kvm_available_selector": load("selector")})
        d = p.stock_detail("4090")
        self.assertEqual(d["gpu_type"], "GeForce RTX 4090")
        self.assertIn("cards", d)
        self.assertTrue(d["districts"])

    def test_canonical_gpu_for_request_params(self):
        """发往云侧的 gpu_type 必须是规范名：实测传 "4090" 询价直接 4000。"""
        p, _ = provider({"store/get_kvm_available_selector": load("selector")})
        self.assertEqual(p.canonical_gpu("4090"), "GeForce RTX 4090")
        self.assertEqual(p.canonical_gpu("A100-SXM4-80GB"), "NVIDIA A100-SXM4-40GB"
                         if False else "NVIDIA A100-SXM4-80GB")
        self.assertEqual(p.canonical_gpu("CPU"), "CPU")
        self.assertEqual(p.canonical_gpu(""), "CPU")

    def test_canonical_gpu_passes_through_when_unknown(self):
        """认不出来就原样传，让云侧报错 —— 擅自换卡会静默多花钱。"""
        p, _ = provider({"store/get_kvm_available_selector": load("selector")})
        self.assertEqual(p.canonical_gpu("H100"), "H100")

    def test_price_uses_canonical_name_and_valid_image(self):
        """两条实测坑一起钉住：规范卡名 + 该卡 ValidImages 里的 Linux 镜像。"""
        p, t = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/get_kvm_available_selector": load("selector"),
            "store/get_configs": load("configs"),
            "store/create_kvm_instance_query_price": load("query_price"),
        })
        p.price(gpu_type="4090", gpu=1, cpu=8, memory_gb=16)
        params = t.last("store/create_kvm_instance_query_price")
        self.assertEqual(params["gpu_type"], "GeForce RTX 4090")
        valid = load("selector")["data"]["gpu_resource_map"]["GeForce RTX 4090"]["ValidImages"]
        self.assertIn(params["image_name"], valid,
                      f"询价镜像 {params['image_name']} 不在该卡可租列表里")
        self.assertNotIn("win", params["image_name"].lower(),
                         "批量作业不该默认选 Windows 镜像")

    def test_create_uses_canonical_gpu_name(self):
        p, t = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/get_kvm_available_selector": load("selector"),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": "186_c"},
        })
        p.create(image="ubuntu24_nvram_base", gpu_type="4090", gpu=1,
                 cpu=8, memory_gb=16)
        self.assertEqual(t.last("store/create_kvm_instance")["gpu_type"],
                         "GeForce RTX 4090")

    def test_gpu_name_fuzzy_match(self):
        """上层历来写 "4090"，云侧叫 "GeForce RTX 4090"，必须自动对上。"""
        p, _ = provider({"store/get_kvm_available_selector": load("selector")})
        for spelling in ("4090", "RTX 4090", "GeForce RTX 4090", "geforce rtx 4090",
                         " Geforce RTX 4090 "):
            got = p.stock_detail(spelling)["gpu_type"]
            self.assertEqual(got, "GeForce RTX 4090", f"{spelling!r} 匹配成了 {got!r}")

    def test_fuzzy_match_prefers_shorter_model_not_plus(self):
        """4090 不能匹配到 4090PLUS：PLUS 更贵，选错了就是多花钱。"""
        p, _ = provider({"store/get_kvm_available_selector": load("selector")})
        self.assertEqual(p.stock_detail("4090")["gpu_type"], "GeForce RTX 4090")
        self.assertEqual(p.stock_detail("4090PLUS")["gpu_type"],
                         "GeForce RTX 4090PLUS")

    def test_vague_name_returns_none_instead_of_guessing(self):
        """只有前缀能对上一堆型号时宁可返回 None，也不能瞎猜一个别的卡。"""
        gmap = {"GeForce RTX 4090": {}, "GeForce RTX 4090PLUS": {}}
        p, _ = provider({})
        self.assertIsNone(p._match_gpu("RTX", gmap))

    def test_images_filters_to_kvm_only(self):
        p, _ = provider({"store/get_configs": load("configs")})
        imgs = p.images()
        self.assertTrue(imgs)
        for i in imgs:
            self.assertEqual(i["type"], "kvm")
            self.assertTrue(i["image_id"])

    def test_resolve_fuzzy_prefers_base_linux_image(self):
        """写 "ubuntu" 应落到裸系统镜像，不能落到 amber/gromacs 专用大镜像。"""
        p, _ = provider({"store/get_configs": load("configs")})
        r = p.resolve_image("ubuntu")
        self.assertEqual(r["image_id"], "ubuntu24_nvram_base")
        self.assertNotIn("amber", r["image_id"])

    def test_resolve_fuzzy_skips_docker_images(self):
        """"ubuntu" 也命中 docker 镜像（aigalaxy:cuda11.7），必须选 kvm 那一档。"""
        p, _ = provider({"store/get_configs": load("configs")})
        imgs = load("configs")["data"]["ImageDisplayInfoMap"]
        docker_hits = [k for k, v in imgs.items()
                       if "ubuntu" in k.lower() and
                       str(v.get("Type") or "").lower() == "docker"]
        self.assertTrue(docker_hits, "fixture 里应有命中 ubuntu 的 docker 镜像")
        picked = p.resolve_image("ubuntu")["image_id"]
        self.assertNotIn(picked, docker_hits)
        self.assertEqual(str(imgs[picked].get("Type") or "").lower(), "kvm")

    def test_images_keyword_filter(self):
        p, _ = provider({"store/get_configs": load("configs")})
        got = p.images("ubuntu")
        self.assertTrue(got)
        self.assertTrue(all("ubuntu" in i["image_id"].lower() or
                            "ubuntu" in i["name"].lower() for i in got))

    def test_resolve_image_exact_and_unknown(self):
        p, _ = provider({"store/get_configs": load("configs")})
        r = p.resolve_image("ubuntu24_nvram_base")
        self.assertEqual(r["image_id"], "ubuntu24_nvram_base")
        with self.assertRaises(PE) as cm:
            p.resolve_image("绝对不存在的镜像")
        self.assertEqual(cm.exception.kind, PE.NOT_FOUND)


# ------------------------------------------------------------------ 实测回归
class TestFieldFindings(unittest.TestCase):
    """把真机实测确认过的行为钉成回归测试（2026-11-13 首台真机）。

    这些不是猜的：下面每条断言背后都有一次真实建机/退租的账单与响应。
    """

    def test_change_password_uses_new_password_field(self):
        """实测：参数名写成 password 时云侧回 "新密码不能为空!"，看不出根因。"""
        p, t = provider({"instance/change_password": {
            "success": True, "code": "2000", "message": "", "data": None}})
        p.change_password("x", "pw123456")
        params = t.last("instance/change_password")
        self.assertEqual(params["new_password"], "pw123456")
        self.assertNotIn("password", params)

    def test_create_response_data_may_be_null(self):
        """实测：create_kvm_instance 的 data 就是 null，必须靠备注回查实例名。"""
        routes = {
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": None},
            "instance/get_instance_list": {"success": True, "code": "2000",
                                           "message": "", "data": {
                                               "list": [{"Container_name": "150_lyg_x",
                                                         "Note": "l5-zxy-probe",
                                                         "Ctime": 1794533081}],
                                               "has_more": False}},
        }
        p, _ = provider(routes)
        uid = p.create(image="img", gpu_type="CPU", gpu=0, cpu=2, memory_gb=2,
                       name="l5-zxy-probe")
        self.assertEqual(uid, "150_lyg_x")

    def test_busy_error_from_terminate_is_retryable(self):
        """实测：刚建完退租回 "操作频繁#10001"，冷却可达 3 分钟。"""
        self.assertEqual(Z.classify_zxy("操作频繁#10001", "4000"),
                         (PE.BUSY, True))

    def test_finished_instance_refund_message(self):
        """实测：已结束的实例再退费回 "当前实例已结束，无法退费!"。"""
        k, r = Z.classify_zxy("当前实例已结束，无法退费!", "4000")
        self.assertFalse(r, "已结束是终态，不该重试")

    def test_price_with_zero_cash_and_no_voucher(self):
        """实测账单：¥0.1756 规格 × 0.95 折扣 = ¥0.1668，询价必须对得上。"""
        pr = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/get_kvm_available_selector": load("selector"),
            "store/get_configs": load("configs"),
            "store/create_kvm_instance_query_price": load("query_price"),
        })[0].price(gpu_type="4090", gpu=1, cpu=8, memory_gb=16)
        self.assertAlmostEqual(pr["discount"], 0.95)
        # 折后价 = 原价 × 折扣
        raw_sheet = load("query_price")["data"]["PackPriceInfoSheet"]
        self.assertAlmostEqual(pr["hourly_total"],
                               round(raw_sheet["TotalHourPrice"] * 0.95, 4), 4)

    def test_stop_semantics_learned_from_field_probe(self):
        """实测（2026-11-13 12:03）：关机态存在，但不能当智云的 stop 用。

        三条实测事实：
          ① guest 内 `poweroff -f` → Status=1 & IsAbnormal=2（state=stopped）
          ② 平台无任何关机 API（9 个候选全 404）
          ③ 关机期间 Due_time 不变、可退金额持续下降 → 租期照常计费
        所以 provider.stop() 必须继续抛 UNSUPPORTED，且 notes 要写明原因，
        否则下一个人看到「有关机态」就会把 can_stop_instance 改成 True。
        """
        p, t = provider({})
        with self.assertRaises(PE) as cm:
            p.stop("x")
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertEqual(t.calls, [])
        note = p.capabilities().notes["can_stop_instance"]
        self.assertIn("无平台关机 API", note)
        self.assertIn("租期照常计费", note)

    def test_stopped_state_detected_and_wakeable(self):
        """关机态 = Status 1 + IsAbnormal 2；start() 走 from_shutdown 唤醒它。"""
        p, t = provider({
            "instance/get_instance_detail": detail_with(1, 2),
            "instance/restart_instance_from_shutdown": {
                "success": True, "code": "2000", "message": "", "data": {}},
        })
        self.assertEqual(p.get("x").state, "stopped")
        p.start("x")
        self.assertIn("instance/restart_instance_from_shutdown", t.paths())

    def test_image_price_is_lease_not_purchase(self):
        """实测：自定义镜像按**租期**收存储费，不是一次买断。

        真机询价（2026-11-13）：0.0006 元/GB·h × 200GB × 时长 × 0.95
            1 小时 ¥0.114 / 1 天 ¥2.736 / 30 天 ¥82.08
        对比优云智算 ¥0.236/GB·月，智星云折后 ¥0.4104/GB·月（贵 1.74 倍）。
        """
        sheet = {"PriceInfoSheet": {
            "ResourceType": "image", "Type": "image_save",
            "Title": "自定义镜像存储:200GB", "SingleHourPrice": 0.0006,
            "Amount": 200, "HourLen": 24, "FinalDiscount": 0.95,
            "FinalSingleHourPriceEqual": 0.00057,
            "TotalCost": 2.736, "FinalCost": 2.736},
            "ItemInfoSheet": {"Due_time_Format": "2026-11-14 16:00:34"}}
        p, t = provider({"images/create_image_query_price": {
            "success": True, "code": "2000", "message": "", "data": sheet}})
        pr = p.image_price(pack_time=1, unit="day")
        self.assertAlmostEqual(pr["total"], 2.736, 4)
        self.assertAlmostEqual(pr["unit_price_per_gb_hour"], 0.00057, 5)
        self.assertAlmostEqual(pr["per_gb_month"], 0.4104, 4)
        self.assertEqual(pr["image_gb"], 200)
        self.assertEqual(pr["expire_at"], "2026-11-14 16:00:34")

    def test_create_image_uses_short_display_name(self):
        """实测坑：镜像名参数是 short_display_name，不是 image_name。

        猜 image_name / name / ImageName 全回"镜像名称不能为空!"，
        这个字段在文档 requestBody 里排第 2，很容易漏。
        """
        p, t = provider({"images/create_image_from_instance": {
            "success": True, "code": "2000", "message": "", "data": {
                "ImageValue": "ubuntu24_uimg.abc.v1", "ShortDisplayName": "test"}}})
        p.create_image("x", name="我的镜像", pack_time=1, unit="day", brief="说明")
        params = t.last("images/create_image_from_instance")
        self.assertEqual(params["short_display_name"], "我的镜像")
        self.assertEqual(params["brief"], "说明")
        self.assertNotIn("image_name", params)
        self.assertNotIn("pay_type_first", params, "文档里没有这个字段，不该传")

    def test_running_instance_must_be_suspended_first(self):
        """实测：运行中的实例不能直接做镜像，要先 suspend（IsAbnormal 变 3）。"""
        p, t = provider({"images/instance/suspend": {
            "success": True, "code": "2000", "message": "", "data": None}})
        p.suspend_image_instance("x")
        self.assertIn("images/instance/suspend", t.paths())

    def test_image_lease_must_cover_instance_lease(self):
        """实测坑：镜像到期早于实例到期时建机被拒。

        "所选镜像到期时间早于实例到期时间，请先续费镜像!"
        → 用镜像建机前必须确认镜像 lease 覆盖实例租期，否则先 renew_image()。
        """
        k, r = Z.classify_zxy("所选镜像到期时间早于实例到期时间，请先续费镜像!", "4000")
        self.assertFalse(r, "这是配置问题，重试无用")

    def test_image_page_size_limit(self):
        """实测：images 接口 page_size 上限 10，传 50 回"page_size参数超限!"。"""
        p, t = provider({"images/get_create_available_instance_list": {
            "success": True, "code": "2000", "message": "", "data": {
                "data": [], "has_more": False}}})
        p.create_image_available()
        self.assertLessEqual(t.last()["page_size"], 10)

    def test_zero_byte_snapshot_is_the_sync_trap(self):
        """实测事故：没 sync 就挂起做镜像 → 文件全变 0 字节。

        现象：文件名/目录/时间戳都在，内容全是 0 字节。
        第二次加 `sync; echo 3 > drop_caches; sync` 后再做，8MB blob 的 md5
        与源机一致 —— 证明这是 page cache 未落盘，不是镜像机制坏。
        这条测试把这个坑写进文档，提醒此后所有 image bake 必须先 sync。
        """
        import inspect
        src = inspect.getsource(Z.ZhiXingYunProvider.suspend_image_instance)
        self.assertIn("suspend", src)
        body = inspect.getsource(Z)
        self.assertIn("sync", body, "provider 里要提醒 sync 落盘")
        # 文档化的坑必须在 create_image 的 docstring 里
        self.assertIn("sync", Z.ZhiXingYunProvider.create_image.__doc__ or "")

    def test_terminate_lock_window_is_declared(self):
        """实测 3 台：建机后约 310s 内退租必然失败（操作频繁#10001）。

        这是平台固定冷却窗而不是抖动 —— 上层算生命周期/成本时必须知道，
        否则会以为"跑完立刻销毁"是瞬间完成的，把 6 分钟的空转算漏。
        """
        notes = provider({})[0].capabilities().notes
        self.assertIn("terminate_lock_s", notes)
        self.assertIn("310", notes["terminate_lock_s"])
        self.assertIn("必然失败", notes["terminate_lock_s"])

    def test_terminate_survives_multi_minute_lock(self):
        """冷却窗内持续 BUSY 时要一直重试，不能在第 5 次就放弃。

        实测成功时刻：290s / 274s / 312s —— 都超过"重试 5 次 × 30s"。
        """
        import tools.p2.providers.zhixingyun as ZM
        calls = {"n": 0}

        def route(params):
            calls["n"] += 1
            # 模拟前 12 次 BUSY（按 10→30s 退避累计约 300s）
            if calls["n"] <= 12:
                return {"success": False, "code": "4000",
                        "message": "操作频繁#10001", "data": None}
            return {"success": True, "code": "2000", "message": "", "data": None}

        p, _ = provider({"instance/stop_instance_with_refund": route})
        orig = ZM.time.sleep
        ZM.time.sleep = lambda s: None
        try:
            p.terminate("x", release_disk=True)
        finally:
            ZM.time.sleep = orig
        self.assertEqual(calls["n"], 13, "必须扛过 12 次 BUSY")

    def test_kept_disk_bills_by_whole_hour(self):
        """实测账单闭环：保留磁盘按**整点小时**扣费，不是实时累积。

        观测序列（账本 reports/l5-zxy-trial/14-keepdisk-hourly.log）：
          09:37 退租保留磁盘 → 8 分钟内 KeeppedDiskCost 恒为 0
          10:00:31 出账 ¥0.0431 "保留实例定时扣费(磁盘)"   Type=71
          10:03:15 释放时结算尾款 ¥0.0052                  Type=72
        所以 capabilities 必须声明 bills_disk_when_stopped=True，且上层
        「停机就省钱」的假设在这里不成立。
        """
        c = provider({})[0].capabilities()
        self.assertTrue(c.bills_disk_when_stopped)
        self.assertIn("整点", c.notes["bills_disk_when_stopped"])

    def test_suspend_to_disk_keeps_disk_and_finds_instance(self):
        """留盘停放 = terminate(release_disk=False)，返回恢复时要用的新名。

        实测：云侧恢复后 _c0 → _c1，旧名失效，所以 suspend 必须把新名找出来。
        """
        uid = "10000000000_lyg_c889_u0_c0"
        routes = {
            "instance/stop_instance_with_refund": {
                "success": True, "code": "2000", "message": "", "data": None},
            "instance/get_instance_list": {"success": True, "code": "2000",
                                           "message": "", "data": {
                                               "list": [{"Container_name": uid,
                                                         "Status": 8,
                                                         "IsAbnormal": 0}],
                                               "has_more": False}},
        }
        p, t = provider(routes)
        got = p.suspend_to_disk(uid)
        self.assertEqual(got, uid)
        self.assertEqual(t.last("instance/stop_instance_with_refund")["release_disk"], "0")

    def test_find_kept_disk_ignores_released_instances(self):
        """Status=0（已释放）不能当成保留磁盘，否则恢复会报错。"""
        routes = {"instance/get_instance_list": {
            "success": True, "code": "2000", "message": "", "data": {
                "list": [{"Container_name": "100_x_u0_c0", "Status": 0},
                         {"Container_name": "100_x_u0_c1", "Status": 8}],
                "has_more": False}}}
        p, _ = provider(routes)
        self.assertEqual(p.find_kept_disk("100_x_u0_c0"), "100_x_u0_c1")

    def test_resume_from_disk_requires_status_8(self):
        """只有 Status=8 能恢复；对运行中的实例调用要明确拒绝。"""
        p, _ = provider({"instance/get_instance_detail": detail_with(1, 0)})
        with self.assertRaises(PE) as cm:
            p.resume_from_disk("x")
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)
        self.assertIn("Status", str(cm.exception))

    def test_resume_from_disk_returns_new_instance_name(self):
        """实测：恢复会新建 _c1，返回的必须是新名，不能沿用旧名。"""
        old = "10000000000_lyg_c889_u0_c0"
        new = "10000000000_lyg_c889_u0_c1"
        payload = detail_with(8)
        payload["data"]["Container_name"] = old
        routes = {
            "store/get_instance_specifications": load("specs_gpu1"),
            "instance/get_instance_detail": payload,
            "instance/create_kvm_instance_from_keepped_disk": {
                "success": True, "code": "2000", "message": "", "data": {
                    "Container_name": new, "Status": 1}},
        }
        p, t = provider(routes)
        got = p.resume_from_disk(old)
        self.assertEqual(got, new)
        params = t.last("instance/create_kvm_instance_from_keepped_disk")
        self.assertEqual(params["instance_name"], old)
        self.assertEqual(params["due_mode"], 1, "恢复后应保持留盘模式便于再次停放")

    def test_release_kept_disk_is_the_only_way_to_stop_disk_billing(self):
        """Status=8 的实例，唯一停止磁盘计费的动作是 release_kept_disk()。"""
        p, t = provider({"instance/release_keepped_disk": {
            "success": True, "code": "2000", "message": "", "data": None}})
        p.release_kept_disk("x")
        self.assertEqual(t.last()["instance_name"], "x")

    def test_specs_cache_keyed_by_gpu_count(self):
        """实测踩过：只用时间做缓存键，先查 1 卡会把 CPU 规格查空。"""
        calls = {"n": 0}

        def route(params):
            calls["n"] += 1
            gn = int(params.get("GpuNum") or 0)
            return {"success": True, "code": "2000", "message": "", "data": {
                "Specifications": [{"SpecName": f"s{gn}", "GpuNum": gn,
                                    "CpuNum": 8, "Memory": 16, "HourPrice": 1.0,
                                    "Disabled": False}]}}

        p, t = provider({"store/get_instance_specifications": route})
        self.assertEqual([s["SpecName"] for s in p.specifications(1)], ["s1"])
        self.assertEqual([s["SpecName"] for s in p.specifications(0)], ["s0"],
                         "CPU 规格被 1 卡的缓存污染了")
        self.assertEqual([s["SpecName"] for s in p.specifications(1)], ["s1"])
        self.assertEqual(calls["n"], 2, "第三问应命中缓存")


# ------------------------------------------------------------------ SSH
class TestSSHEndpoint(unittest.TestCase):
    def test_endpoint_from_detail(self):
        d = load("instance_detail")["data"]
        p, _ = provider({"instance/get_instance_detail": load("instance_detail")})
        ep = p.ssh_endpoint("x")
        self.assertEqual(ep.host, d["Url"])
        self.assertEqual(ep.port, str(d["SshPort"]))
        self.assertEqual(ep.user, "root")
        self.assertTrue(ep.password)

    def test_windows_image_uses_vipuser(self):
        payload = load("instance_detail")
        payload["data"]["Image"] = "win10_ai_cuda12.4"
        p, _ = provider({"instance/get_instance_detail": payload})
        self.assertEqual(p.ssh_endpoint("x").user, "vipuser")

    def test_no_public_url_is_transient(self):
        payload = load("instance_detail")
        payload["data"]["Url"] = ""
        p, _ = provider({"instance/get_instance_detail": payload})
        with self.assertRaises(PE) as cm:
            p.ssh_endpoint("x")
        self.assertEqual(cm.exception.kind, PE.TRANSIENT)


# ------------------------------------------------------------------ 实例视图
class TestInstanceView(unittest.TestCase):
    def test_no_vendor_specific_fields_leak(self):
        p, _ = provider({"instance/get_instance_detail": load("instance_detail")})
        d = p.get("x").as_dict()
        for bad in ("Container_name", "Gpu_type", "Init_passwd", "IsAbnormal"):
            self.assertNotIn(bad, d, f"云专属字段泄漏到通用视图：{bad}")

    def test_extra_cpu_memory_added_in(self):
        """加购的 CPU/内存要算进总配置，否则资源判断会偏低。"""
        payload = load("instance_detail")
        payload["data"].update({"Cpu_num": 8, "AddCpu": 4,
                                "Memory": 16, "AddMemory": 32})
        p, _ = provider({"instance/get_instance_detail": payload})
        v = p.get("x")
        self.assertEqual(v.cpu, 12)
        self.assertEqual(v.memory_gb, 48)

    def test_list_instances_paginates(self):
        page1 = {"success": True, "code": "2000", "message": "", "data": {
            "list": [{"Container_name": "a", "Status": 1, "IsAbnormal": 0}],
            "has_more": True}}
        page2 = {"success": True, "code": "2000", "message": "", "data": {
            "list": [{"Container_name": "b", "Status": 1, "IsAbnormal": 0}],
            "has_more": False}}
        i = {"n": 0}

        def route(params):
            i["n"] += 1
            return page1 if i["n"] == 1 else page2

        p, t = provider({"instance/get_instance_list": route})
        uids = [v.uid for v in p.list_instances()]
        self.assertEqual(uids, ["a", "b"])
        self.assertEqual(len(t.calls), 2)

    def test_list_instances_prefix_filter(self):
        payload = {"success": True, "code": "2000", "message": "", "data": {
            "list": [{"Container_name": "job-1", "Note": "job-1", "Status": 1},
                     {"Container_name": "other", "Note": "other", "Status": 1}],
            "has_more": False}}
        p, _ = provider({"instance/get_instance_list": payload})
        self.assertEqual([v.uid for v in p.list_instances("job-")], ["job-1"])


# ------------------------------------------------------------------ 生命周期
class TestLifecycleCalls(unittest.TestCase):
    def test_terminate_releases_disk_by_default(self):
        """默认连磁盘一起释放 —— 不释放就按小时继续扣。"""
        p, t = provider({"instance/stop_instance_with_refund": {
            "success": True, "code": "2000", "message": "", "data": None}})
        p.terminate("x")
        params = t.last("instance/stop_instance_with_refund")
        self.assertEqual(params["release_disk"], "1")

    def test_terminate_retries_through_busy_cooldown(self):
        """实测：刚建完退租，云侧连续回 "操作频繁#10001" 约 3 分钟。

        退租是唯一能停止烧钱的动作，必须扛过 BUSY 冷却而不是让机器继续计费。
        这里用极短 timeout + 直接打桩去掉 sleep 来验证重试路径本身。
        """
        import tools.p2.providers.zhixingyun as ZM
        calls = {"n": 0}

        def route(params):
            calls["n"] += 1
            if calls["n"] <= 2:
                return {"success": False, "code": "4000",
                        "message": "操作频繁#10001", "data": None}
            return {"success": True, "code": "2000", "message": "", "data": None}

        p, _ = provider({"instance/stop_instance_with_refund": route})
        orig = ZM.time.sleep
        ZM.time.sleep = lambda s: None            # 不打桩就要真等 15s
        try:
            p.terminate("x", release_disk=False)
        finally:
            ZM.time.sleep = orig
        self.assertEqual(calls["n"], 3, "前两次 BUSY，第三次才该成功")

    def test_terminate_gives_up_after_timeout(self):
        """BUSY 一直不放行时要明确报错，并提醒「可能仍在计费」。"""
        p, _ = provider({"instance/stop_instance_with_refund": {
            "success": False, "code": "4000", "message": "操作频繁#10001",
            "data": None}})
        with self.assertRaises(PE) as cm:
            p.terminate("x", timeout=0)
        self.assertEqual(cm.exception.kind, PE.BUSY)
        self.assertIn("仍在计费", str(cm.exception))

    def test_terminate_on_finished_instance_is_idempotent(self):
        """reaper 重扫已退租的机器不该报错 —— 幂等调用要安全。"""
        p, _ = provider({"instance/stop_instance_with_refund": {
            "success": False, "code": "4000",
            "message": "当前实例已结束，无法退费!", "data": None}})
        p.terminate("x")           # 不抛
        p.terminate("x")           # 再来一次也不抛

    def test_terminate_does_not_retry_permanent_errors(self):
        """参数错/无权限重试只会把请求打成风控，必须立刻抛。"""
        p, t = provider({"instance/stop_instance_with_refund": {
            "success": False, "code": "4000", "message": "sign验证失败!",
            "data": None}})
        with self.assertRaises(PE) as cm:
            p.terminate("x")
        self.assertEqual(cm.exception.kind, PE.AUTH)
        self.assertEqual(len(t.calls), 1)

    def test_terminate_can_keep_disk(self):
        p, t = provider({"instance/stop_instance_with_refund": {
            "success": True, "code": "2000", "message": "", "data": None}})
        p.terminate("x", release_disk=False)
        self.assertEqual(t.last()["release_disk"], "0")

    def test_release_kept_disk(self):
        p, t = provider({"instance/release_keepped_disk": {
            "success": True, "code": "2000", "message": "", "data": None}})
        p.release_kept_disk("x")
        self.assertIn("instance/release_keepped_disk", t.paths())

    def test_start_on_running_is_noop(self):
        p, t = provider({"instance/get_instance_detail": detail_with(1, 0)})
        p.start("x")
        self.assertEqual(t.paths(), ["instance/get_instance_detail"])

    def test_start_on_terminated_unsupported(self):
        p, _ = provider({"instance/get_instance_detail": detail_with(8)})
        with self.assertRaises(PE) as cm:
            p.start("x")
        self.assertEqual(cm.exception.kind, PE.UNSUPPORTED)

    def test_start_on_shutdown_calls_restart_from_shutdown(self):
        p, t = provider({
            "instance/get_instance_detail": detail_with(1, 2),
            "instance/restart_instance_from_shutdown": {
                "success": True, "code": "2000", "message": "", "data": {}},
        })
        p.start("x")
        self.assertIn("instance/restart_instance_from_shutdown", t.paths())

    def test_create_sets_password_after_create(self):
        p, t = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": "186_p"},
            "instance/change_password": {"success": True, "code": "2000",
                                         "message": "", "data": None},
        })
        p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1, cpu=8,
                 memory_gb=16, password="S3cret!")
        params = t.last("instance/change_password")
        self.assertEqual(params["instance_name"], "186_p")
        # 参数名是 new_password：实测传 password 云侧回 "新密码不能为空!"
        self.assertEqual(params["new_password"], "S3cret!")
        self.assertNotIn("password", params)

    def test_password_failure_does_not_lose_instance(self):
        """改密码失败不该把已建好的机器丢掉（详情里有初始密码可用）。"""
        p, _ = provider({
            "store/get_instance_specifications": load("specs_gpu1"),
            "store/create_kvm_instance": {"success": True, "code": "2000",
                                          "message": "", "data": "186_q"},
            "instance/change_password": {"success": False, "code": "4000",
                                         "message": "该状态不能改密码", "data": None},
        })
        uid = p.create(image="img", gpu_type="GeForce RTX 4090", gpu=1, cpu=8,
                       memory_gb=16, password="x")
        self.assertEqual(uid, "186_q")


# ------------------------------------------------------------------ 搁置开关
class TestSuspension(unittest.TestCase):
    """2026-11-13 YG 决定暂停接入。搁置必须**硬拦**，不能靠记得别用。

    一个跑着的 cron、一个误敲的 CLI 就可能建机扣钱，所以要在唯一的
    请求出口（call()）上拦，这样覆盖建机/销毁/询价/镜像/余额全部能力。
    """

    def test_all_cloud_calls_blocked_when_suspended(self):
        """非测试注入（真实网络路径）下，任何方法都不该发出请求。"""
        p = Z.ZhiXingYunProvider({"ZXY_API_KEY": "k", "ZXY_API_SECRET": "s"})
        # 构造后 _test_unlocked 为 False（没注入 transport）→ 应被拦
        self.assertFalse(p._test_unlocked)
        for fn in (p.balance, lambda: p.get("x"),
                   lambda: p.create(image="i", gpu_type="CPU", gpu=0),
                   lambda: p.terminate("x"),
                   lambda: p.price(gpu_type="CPU", gpu=0, cpu=2, memory_gb=2),
                   lambda: p.images_custom(),
                   lambda: p.image_price()):
            with self.assertRaises(PE) as cm:
                fn()
            self.assertEqual(cm.exception.kind, PE.UNSUPPORTED,
                             f"{fn} 未被拦")

    def test_block_message_explains_why_and_how_to_resume(self):
        """拦截信息要能自解释：为什么暂停、怎么恢复、去哪看细节。"""
        p = Z.ZhiXingYunProvider({})
        with self.assertRaises(PE) as cm:
            p.balance()
        msg = str(cm.exception)
        self.assertIn("暂停", msg)
        self.assertIn("ZXY_ENABLE", msg)
        self.assertIn("reports/L5-智星云", msg)

    def test_construction_still_works(self):
        """构造不受搁置影响：能力矩阵/离线测试仍可用。"""
        p = Z.ZhiXingYunProvider({})
        self.assertEqual(p.capabilities().name, "zhixingyun")

    def test_transport_injection_unlocks_for_offline_tests(self):
        """注入假 transport 即视为离线测试环境，允许跑到逻辑层（发不出真请求）。"""
        p, _ = provider({"account/get_main_account_info": load("account_info")})
        self.assertTrue(p._test_unlocked)
        self.assertAlmostEqual(p.balance(), 128.5)


# ------------------------------------------------------------------ 契约一致性
class TestContractCompleteness(unittest.TestCase):
    REQUIRED = ("capabilities", "create", "get", "list_instances", "start", "stop",
                "terminate", "ssh_endpoint", "ssh_ready", "scheduled_stop",
                "price", "balance", "resolve_image", "images", "stock")

    def test_all_contract_methods_present(self):
        p, _ = provider({})
        for m in self.REQUIRED:
            self.assertTrue(callable(getattr(p, m, None)), f"缺方法 {m}")

    def test_capabilities_type(self):
        from tools.p2.providers.base import Capabilities
        self.assertIsInstance(provider({})[0].capabilities(), Capabilities)


if __name__ == "__main__":
    unittest.main(verbosity=2)
