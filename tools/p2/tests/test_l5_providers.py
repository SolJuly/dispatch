#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L5 多云层测试：Provider 契约 / 资产缓存 / 跨云一致性。全部零费用、不联网。

覆盖 2026-11-09 新增的三块：

  providers/   契约一致性 + 能力声明 + 错误分类 + BYOK 凭证隔离
  assets       五步流程（解析/校验/落位/索引/淘汰）+ 证据强度分级
  conformance  manifest ↔ probe 校验 + 离线复放 + 严重度分级

重点测的是**判断逻辑**，不是 IO：
错误分类写错会导致「该重试的不重试」或「不该重试的疯狂重试烧钱」；
证据强度分不清会导致「抽样清单被当成完整盘点」（本项目真实踩过）。

用法：python3 -m unittest tools.p2.tests.test_l5_providers
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import assets as A  # noqa: E402
from tools.p2 import conformance as C  # noqa: E402
from tools.p2 import providers as P  # noqa: E402
from tools.p2.providers import base as PB  # noqa: E402
from tools.p2.providers.local import FakeProvider, LocalProvider  # noqa: E402


# ------------------------------------------------------------------ 契约
class TestProviderContract(unittest.TestCase):
    """三家 provider 必须实现同一套方法，否则上层没法统一调用。"""

    REQUIRED = ("capabilities", "create", "get", "list_instances", "start", "stop",
                "terminate", "ssh_endpoint", "ssh_ready", "scheduled_stop",
                "price", "balance", "resolve_image", "images", "stock")

    def _instances(self):
        from tools.p2.providers.ppio import PPIOProvider
        # ppio 用空 transport 构造：零网络、零凭证。它也要过同一套契约。
        ppio = PPIOProvider(api_key="sk_contract", transport=lambda *a: (200, {}))
        return [FakeProvider(),
                LocalProvider(host="127.0.0.1", port="22", password="x"),
                ppio]

    def test_all_methods_implemented(self):
        for p in self._instances():
            for m in self.REQUIRED:
                self.assertTrue(callable(getattr(p, m, None)),
                                f"{p.name} 缺方法 {m}")

    def test_fake_create_and_state_machine(self):
        p = FakeProvider()
        uid = p.create(image="img", gpu_type="4090")
        self.assertEqual(p.get(uid).state, "running")
        self.assertTrue(p.get(uid).billing)
        p.stop(uid)
        self.assertEqual(p.get(uid).state, "stopped")
        self.assertFalse(p.get(uid).billing, "停机后不该再计费")
        p.terminate(uid)
        with self.assertRaises(P.ProviderError):
            p.get(uid)

    def test_out_of_stock_raises_typed_error(self):
        p = FakeProvider(stock=0)
        with self.assertRaises(P.ProviderError) as cm:
            p.create(image="img", gpu_type="4090")
        self.assertEqual(cm.exception.kind, P.ProviderError.OUT_OF_STOCK)
        self.assertTrue(cm.exception.retryable, "没货是可重试的")

    def test_injected_failure_classified(self):
        p = FakeProvider(fail_create="8903 instance is in the task")
        with self.assertRaises(P.ProviderError) as cm:
            p.create(image="img", gpu_type="4090")
        self.assertEqual(cm.exception.kind, P.ProviderError.BUSY)

    def test_scheduled_stop_recorded(self):
        p = FakeProvider()
        uid = p.create(image="img", gpu_type="4090")
        p.scheduled_stop(uid, 1700000000)
        self.assertEqual(p.stop_at[uid], 1700000000)

    def test_view_fields_provider_agnostic(self):
        """InstanceView 字段不能带云厂商前缀，否则换云上层要改。"""
        p = FakeProvider()
        uid = p.create(image="img", gpu_type="4090", cpu=16, memory_gb=64)
        v = p.get(uid)
        d = v.as_dict()
        for k in ("uid", "state", "price_per_hour", "disk_per_hour",
                  "total_per_hour", "billing"):
            self.assertIn(k, d)
        for bad in ("UHostId", "GpuType", "CompShareImageId"):
            self.assertNotIn(bad, d, f"云专属字段泄漏到通用视图：{bad}")
        self.assertAlmostEqual(v.total_per_hour, v.price_per_hour + v.disk_per_hour, 4)


class TestCapabilities(unittest.TestCase):
    """能力声明是「下决定之前就知道路走不通」的唯一依据，必须准确。"""

    def test_compshare_declares_no_internet(self):
        cap = P.get_provider("fake").capabilities()   # fake 有外网
        self.assertTrue(cap.instance_has_internet)
        # 真适配器不联网也能看声明（不构造 CS）
        self.assertEqual(PB.classify("DNS Name or service not known")[0],
                         PB.ProviderError.UNKNOWN)

    def test_require_gate_blocks_unsupported(self):
        p = LocalProvider(host="127.0.0.1", has_internet=True)
        p.require("instance_has_internet")          # 有 → 不抛
        with self.assertRaises(P.ProviderError) as cm:
            p.require("supports_scheduled_stop")     # 本地机没有
        self.assertEqual(cm.exception.kind, P.ProviderError.UNSUPPORTED)
        # 报错必须带上是哪家 provider + 为什么，不能只是「不支持」
        self.assertIn("local", str(cm.exception))
        self.assertIn("没有任何平台侧的定时关机", str(cm.exception))

    def test_unknown_capability_is_not_silently_ok(self):
        p = FakeProvider()
        with self.assertRaises(P.ProviderError):
            p.require("nonexistent_capability")

    def test_matrix_covers_available(self):
        rows = P.capability_matrix()
        self.assertTrue(rows)
        names = {r.get("name") or r.get("provider") for r in rows}
        self.assertIn("fake", names)


class TestErrorClassification(unittest.TestCase):
    """错误分类错了会烧钱或卡死，逐条钉住。"""

    CASES = [
        ("226604 无现货", PB.ProviderError.OUT_OF_STOCK, True),
        ("insufficient capacity", PB.ProviderError.OUT_OF_STOCK, True),
        ("8903 in the task", PB.ProviderError.BUSY, True),
        ("quota limit exceeded", PB.ProviderError.QUOTA, False),
        ("instance not found", PB.ProviderError.NOT_FOUND, False),
        ("Signature verification failed", PB.ProviderError.AUTH, False),
        ("insufficient balance", PB.ProviderError.BILLING, False),
        ("connection timeout", PB.ProviderError.TRANSIENT, True),
    ]

    def test_classify(self):
        for msg, kind, retry in self.CASES:
            k, r = PB.classify(msg)
            self.assertEqual(k, kind, f"{msg!r} 应归为 {kind}，实际 {k}")
            self.assertEqual(r, retry, f"{msg!r} 的 retryable 应为 {retry}")

    def test_unknown_not_retryable(self):
        """未知错误默认不重试 —— 宁可停下让人看，也不盲目重试烧钱。"""
        k, r = PB.classify("something weird happened")
        self.assertEqual(k, PB.ProviderError.UNKNOWN)
        self.assertFalse(r)


class TestBYOK(unittest.TestCase):
    """BYOK：客户自己的凭证不能污染进程环境，也不能落到 credentials.env。"""

    def test_credentials_passed_isolated(self):
        from tools.p2.providers.compshare import CompShareProvider
        creds = {"CS_PUBLIC_KEY": "pk-customer", "CS_PRIVATE_KEY": "sk-customer"}
        try:
            p = CompShareProvider(creds)
        except Exception:
            self.skipTest("环境里没有 ucloud sdk")
        # 关键：provider 内部拿的是传入的凭证，不是 credentials.env 的
        self.assertEqual(p.cs.cfg["CS_PUBLIC_KEY"], "pk-customer")
        self.assertEqual(p.cs.cfg["CS_PRIVATE_KEY"], "sk-customer")

    def test_empty_creds_fall_back(self):
        """传了空 dict 应等价于不传（走默认凭证链），不能当成 BYOK。"""
        from tools.p2.providers.compshare import CompShareProvider
        try:
            p = CompShareProvider({})
        except Exception:
            self.skipTest("环境里没有可用凭证")
        self.assertNotEqual(p.cs.cfg.get("CS_PUBLIC_KEY"), "")


# ------------------------------------------------------------------ 资产
class TestAssetResolve(unittest.TestCase):
    """解析：能命中就说能，不能命中要给**具体原因**，不要笼统说"缺"。"""

    MODEL = {
        "key": "m1",
        "files": {
            "ckpt": {"name": "a.safetensors", "loader": "CheckpointLoaderSimple",
                     "match": {"contains": "a"}},
            "vae": {"name": "sub/b.safetensors", "loader": "VAELoader",
                    "match": {"contains": "b"}},
        },
    }

    def test_hit_by_exact_name(self):
        avail = {"a.safetensors": A.AssetHit(name="a.safetensors", path="/model/a.safetensors"),
                 "b.safetensors": A.AssetHit(name="b.safetensors", path="/model/b.safetensors")}
        res = A.resolve(self.MODEL, available=avail)
        self.assertEqual([r.status for r in res], ["linkable", "linkable"])
        self.assertTrue(A.summarize(res)["ok"])

    def test_hit_by_match_rule(self):
        """平台库文件名常带额外后缀，靠 match 规则认出来（"名字不对占大头"）。"""
        avail = {"b.safetensors": A.AssetHit(
            name="b-v2-fp8.safetensors", path="/model/b-v2-fp8.safetensors")}
        # match 是 contains "b"，应命中
        res = A.resolve(self.MODEL, available=avail)
        by_name = {r.name: r for r in res}
        self.assertEqual(by_name["sub/b.safetensors"].status, "linkable")

    def test_missing_reason_is_specific(self):
        res = A.resolve(self.MODEL, available={}, can_link=False, can_download=False)
        s = A.summarize(res)
        self.assertFalse(s["ok"])
        # 原因必须说清是"没共享库且无外网"，不能只说 missing
        self.assertIn("没有共享权重库", s["blocked_by"][0])

    def test_download_when_url_and_internet(self):
        m = {"key": "m2", "files": {"ckpt": {"name": "x.safetensors",
                                             "loader": "CheckpointLoaderSimple",
                                             "url": "https://example.com/x.safetensors"}}}
        res = A.resolve(m, available={}, can_link=False, can_download=True)
        self.assertEqual(res[0].status, "download")
        targets = A.download_targets(res)
        self.assertTrue(targets[0]["tmp"].endswith(".part"),
                        "下载必须用 .part 临时名，否则半截文件会被当成品")

    def test_no_url_means_missing_not_download(self):
        m = {"key": "m3", "files": {"ckpt": {"name": "x.safetensors",
                                             "loader": "CheckpointLoaderSimple"}}}
        res = A.resolve(m, available={}, can_link=False, can_download=True)
        self.assertEqual(res[0].status, "missing")


class TestAssetIndexAndEvict(unittest.TestCase):
    def _entries(self, *specs):
        return {name: {"name": name, "path": f"/cache/{name}", "size": size,
                       "evictable": ev, "last_used": lu}
                for name, size, ev, lu in specs}

    def test_lru_order(self):
        ents = self._entries(("old.bin", 100, True, 100.0),
                             ("new.bin", 100, True, 900.0))
        plan = A.evict_plan(ents, usage_bytes=200, limit_bytes=100)
        self.assertEqual([f["name"] for f in plan["files"]], ["old.bin"],
                         "最久没用的先删")

    def test_shared_mount_never_evicted(self):
        """共享挂载删的是别人的数据，硬门禁。"""
        ents = self._entries(("shared.bin", 500, False, 1.0),   # 最旧但不可删
                             ("cache.bin", 100, True, 900.0))
        plan = A.evict_plan(ents, usage_bytes=600, limit_bytes=100)
        self.assertEqual([f["name"] for f in plan["files"]], ["cache.bin"])
        self.assertFalse(plan["reached"], "删光可删项仍不够时必须报不达标")

    def test_no_evict_when_under_limit(self):
        ents = self._entries(("a.bin", 100, True, 1.0))
        plan = A.evict_plan(ents, usage_bytes=100, limit_bytes=200)
        self.assertEqual(plan["files"], [])
        self.assertTrue(plan["reached"])

    def test_need_exact_amount(self):
        ents = self._entries(("a.bin", 100, True, 1.0), ("b.bin", 200, True, 2.0))
        plan = A.evict_plan(ents, usage_bytes=300, limit_bytes=250)
        self.assertEqual(plan["need"], 50)
        self.assertEqual(plan["freed"], 100, "删第一个就够，不要多删")

    def test_index_roundtrip_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "asset_index.json"
            d = A.load_index(idx)
            A.record(d["entries"], A.AssetHit(name="x", path="/m/x", size=10,
                                              source="mount"), now=100.0)
            A.save_index(d, idx)
            back = A.load_index(idx)
            self.assertIn("x", back["entries"])
            self.assertEqual(back["schema"], A.INDEX_SCHEMA)
            self.assertFalse((Path(td) / "asset_index.part").exists(),
                             "临时文件必须被替换掉，不能留在盘上")

    def test_record_preserves_last_used(self):
        """scan 会把 last_used=0 写回来，不能因此丢掉真实的最近使用时间。"""
        ents = {}
        A.record(ents, A.AssetHit(name="x", path="/m/x", size=1), now=500.0)
        A.record(ents, A.AssetHit(name="x", path="/m/x", size=1), now=0.0)
        self.assertEqual(ents["x"]["last_used"], 500.0)

    def test_disk_usage_excludes_shared(self):
        ents = self._entries(("shared.bin", 999, False, 1.0),
                             ("cache.bin", 100, True, 2.0))
        self.assertEqual(A.disk_usage(ents), 100)


class TestEvidenceStrength(unittest.TestCase):
    """抽样证据只能证明"在"，不能证明"不在" —— 本项目真实踩过的坑。"""

    def test_coverage_reports_unknown(self):
        models = [{"key": "m", "files": {
            "a": {"name": "a.safetensors", "loader": "CheckpointLoaderSimple"},
            "b": {"name": "b.safetensors", "loader": "VAELoader"}}}]
        avail = {"a.safetensors": A.AssetHit(name="a.safetensors", path="/m/a")}
        cov = A.evidence_coverage(models, avail)
        self.assertEqual(cov["required"], 2)
        self.assertEqual(cov["known"], 1)
        self.assertEqual(cov["unknown"], 1)
        self.assertEqual(cov["coverage"], 0.5)

    def test_compat_links_loaded_as_verified(self):
        links = A.load_compat_links()
        if not links:
            self.skipTest("没有 compat_links.txt")
        for hit in links.values():
            self.assertEqual(hit.source, "verified")
            self.assertTrue(hit.path.startswith("/model"), "路径必须是库内绝对路径")
            self.assertFalse(hit.evictable, "共享库永不淘汰")

    def test_verified_source_counts_as_linkable(self):
        model = {"key": "m", "files": {"ckpt": {"name": "a.safetensors",
                                                "loader": "CheckpointLoaderSimple"}}}
        avail = {"a.safetensors": A.AssetHit(name="a.safetensors", path="/model/a",
                                             source="verified")}
        res = A.resolve(model, available=avail)
        self.assertEqual(res[0].status, "linkable")
        self.assertIn("compat_links", res[0].reason)


class TestAssetPlace(unittest.TestCase):
    def test_place_script_links_only_hits(self):
        res = [A.Resolution(name="a/b.safetensors", subdir="vae", status="linkable",
                            path="/model/b.safetensors"),
               A.Resolution(name="c.safetensors", subdir="loras", status="download")]
        script = A.place_script(res)
        self.assertIn("/model/b.safetensors", script)
        self.assertIn("/root/ComfyUI/models/vae/a/b.safetensors", script)
        self.assertNotIn("c.safetensors", script)
        self.assertIn("ASSET_PLACE_DONE", script)

    def test_verify_file_size_mismatch(self):
        with tempfile.NamedTemporaryFile(suffix=".safetensors", delete=False) as f:
            f.write(b"x" * 100)
            p = Path(f.name)
        try:
            ok, why = A.verify_file(p, expect_size=200)
            self.assertFalse(ok)
            self.assertIn("大小不符", why)
            ok, _ = A.verify_file(p, expect_size=100)
            self.assertTrue(ok)
        finally:
            p.unlink(missing_ok=True)

    def test_verify_file_sha256(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"hello")
            p = Path(f.name)
        try:
            import hashlib
            good = hashlib.sha256(b"hello").hexdigest()
            ok, _ = A.verify_file(p, expect_sha256=good)
            self.assertTrue(ok)
            ok, why = A.verify_file(p, expect_sha256="0" * 64)
            self.assertFalse(ok)
            self.assertIn("sha256", why)
        finally:
            p.unlink(missing_ok=True)


# ------------------------------------------------------------------ 一致性
class TestConformance(unittest.TestCase):
    MANIFEST = {
        "name": "t",
        "base": {"python": "3.10"},
        "python": {"pins": {"torch": "2.8.0+cu128"}, "exclude": {"xformers": "ABI"}},
        "comfyui": {"commit": "3c80da7f87ee359b2d06f107cb3c0797079dfbbb", "version": "0.36.0"},
        "custom_nodes": [{"dir": "N1", "commit": "abcdef1234567890"}],
        "platform_notes": {"compshare": {"instance_has_internet": False}},
    }

    def _probe(self, **over):
        p = {"schema": C.PROBE_SCHEMA,
             "comfy": {"commit": "3c80da7f87ee359b2d06f107cb3c0797079dfbbb",
                       "version": "0.36.0", "path": "/root/ComfyUI"},
             "python": {"version": "3.10.12", "torch": "2.8.0+cu128"},
             "nodes": {"N1": {"commit": "abcdef1234567890", "import_failed": False}},
             "object_info_nodes": 1757,
             "models": {"unet_visible": 33, "vae_visible": 23, "ckpt_visible": 57},
             "platform": {"has_internet": False, "can_pull_oci": False}}
        for k, v in over.items():
            # 空 dict 表示「把这段清空」，不能和默认值合并
            if isinstance(v, dict) and v and isinstance(p.get(k), dict):
                p[k] = {**p[k], **v}
            else:
                p[k] = v
        return p

    def test_clean_probe_passes(self):
        res = C.check(self.MANIFEST, self._probe())
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["counts"]["ERROR"], 0)

    def test_commit_mismatch_is_error(self):
        res = C.check(self.MANIFEST, self._probe(
            comfy={"commit": "deadbeef" * 5, "version": "0.36.0"}))
        self.assertFalse(res["ok"])
        fields = [e["field"] for e in res["errors"]]
        self.assertIn("comfyui.commit", fields)

    def test_short_commit_matches(self):
        """短 commit 前缀一致也算一致（有人用 abbrev）。"""
        res = C.check(self.MANIFEST, self._probe(comfy={"commit": "3c80da7f"}))
        self.assertTrue(res["ok"])

    def test_torch_version_must_include_cuda_tag(self):
        """'2.8.0' 与 '2.8.0+cu128' 是不同的 —— CUDA 版本要命。"""
        res = C.check(self.MANIFEST, self._probe(python={"torch": "2.8.0"}))
        self.assertFalse(res["ok"])
        self.assertTrue(any(e["field"] == "python.pins.torch" for e in res["errors"]))

    def test_xformers_present_is_error(self):
        res = C.check(self.MANIFEST, self._probe(
            python={"torch": "2.8.0+cu128", "xformers": "0.0.31"}))
        self.assertFalse(res["ok"])
        self.assertTrue(any("xformers" in e["field"] for e in res["errors"]))

    def test_node_commit_mismatch_is_warn_not_error(self):
        """节点 commit 不同但节点在：可能只是拉了新提交，不该判死。"""
        res = C.check(self.MANIFEST, self._probe(
            nodes={"N1": {"commit": "9999999999", "import_failed": False}}))
        self.assertTrue(res["ok"], "commit 不同不应是 ERROR")
        self.assertTrue(any(w["field"].startswith("custom_nodes.N1") for w in res["warns"]))

    def test_missing_node_is_error(self):
        res = C.check(self.MANIFEST, self._probe(nodes={}))
        self.assertFalse(res["ok"])
        self.assertTrue(any(e["field"] == "custom_nodes.N1" for e in res["errors"]))

    def test_import_failed_is_error(self):
        res = C.check(self.MANIFEST, self._probe(
            nodes={"N1": {"commit": "abcdef1234567890", "import_failed": True}}))
        self.assertFalse(res["ok"])

    def test_zero_models_is_error(self):
        """unet_visible=0 意味着权重库没挂上，所有作业都会缺权重。"""
        res = C.check(self.MANIFEST, self._probe(
            models={"unet_visible": 0, "vae_visible": 23, "ckpt_visible": 57}))
        self.assertFalse(res["ok"])
        self.assertTrue(any("unet_visible" in e["field"] for e in res["errors"]))

    def test_object_info_error_is_error(self):
        p = self._probe()
        p.pop("object_info_nodes")
        p["object_info_error"] = "URLError: refused"
        res = C.check(self.MANIFEST, p)
        self.assertFalse(res["ok"])

    def test_internet_state_mismatch_is_warn(self):
        res = C.check(self.MANIFEST, self._probe(platform={"has_internet": True}))
        self.assertTrue(res["ok"], "外网状态差异只是 WARN")
        self.assertTrue(any("instance_has_internet" in w["field"] for w in res["warns"]))

    def test_report_format(self):
        res = C.check(self.MANIFEST, self._probe(comfy={"commit": "deadbeef"}))
        txt = C.format_report(res)
        self.assertIn("✗ 不一致", txt)
        self.assertIn("comfyui.commit", txt)


class TestConformanceProbe(unittest.TestCase):
    """探针脚本本身要能在干净机器上跑（不依赖项目代码）。"""

    def test_script_is_selfcontained(self):
        s = C.collect_script()
        self.assertNotIn("import tools.", s)
        self.assertIn("PROBE_JSON:", s)
        self.assertIn("object_info", s)

    def test_script_runs_and_reports_probe(self):
        import subprocess
        s = C.collect_script(comfy_home="/tmp/definitely-not-here-comfy")
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(s)
            path = f.name
        try:
            r = subprocess.run(["bash", path], capture_output=True, text=True, timeout=120)
            probe = C.parse_probe(r.stdout)
            self.assertEqual(probe["schema"], C.PROBE_SCHEMA)
            # ComfyUI 不存在时要优雅降级，不能崩
            self.assertIn("comfy", probe)
            self.assertTrue(probe["comfy"]["commit"] == "")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_parse_probe_rejects_garbage(self):
        with self.assertRaises(ValueError):
            C.parse_probe("nothing useful here")

    def test_python_version_reachable(self):
        """PY 路径必须真的被用上，不能因为 heredoc 引号而变成空 shell 变量。"""
        import subprocess
        s = C.collect_script(comfy_home="/tmp/nope", python=sys.executable)
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(s)
            path = f.name
        try:
            r = subprocess.run(["bash", path], capture_output=True, text=True, timeout=120)
            probe = C.parse_probe(r.stdout)
            self.assertTrue(probe["python"]["version"],
                            "python 版本采不到 —— PY 变量没传进 python 子进程")
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
