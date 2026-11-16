#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 预热池单测（¥0）：FakeCS + 临时 pool.json，不联网。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import pool  # noqa: E402


class FakeCS:
    def __init__(self):
        self.insts: dict[str, dict] = {}
        self.n = 0
        self.calls: list[str] = []

    def create(self, **kw):
        self.n += 1
        uid = f"uhost-fake{self.n}"
        self.insts[uid] = {"UHostId": uid, "State": "Running", "Name": kw.get("name"),
                           "CompShareImageId": kw.get("image_id"),
                           "CompShareImageName": kw.get("image_id")}
        self.calls.append(f"create:{uid}")
        return [uid]

    def instances(self):
        return list(self.insts.values())

    def get(self, uid):
        if uid not in self.insts:
            raise RuntimeError(f"找不到实例: {uid}")
        return self.insts[uid]

    def start(self, uid):
        self.insts[uid]["State"] = "Running"
        self.calls.append(f"start:{uid}")

    def stop(self, uid):
        self.insts[uid]["State"] = "Stopped"
        self.calls.append(f"stop:{uid}")

    def terminate(self, uid):
        self.insts.pop(uid, None)
        self.calls.append(f"terminate:{uid}")

    def wait_state(self, uid, states, timeout=0):
        return self.insts.get(uid, {})

    def public_ip(self, inst):
        return "1.2.3.4"

    def password_of(self, inst):
        return "pw"

    def resolve_image(self, spec):
        return {"CompShareImageId": spec, "Name": spec}


class PoolTest(unittest.TestCase):
    def setUp(self):
        self._orig_db = pool.POOL_JSON
        self._td = tempfile.TemporaryDirectory()
        pool.POOL_JSON = Path(self._td.name) / "pool.json"
        self.cs = FakeCS()
        self._orig_cs = pool.CS
        pool.CS = lambda: self.cs
        # 不真连 ssh
        self._orig_sshd, self._orig_ready = pool._wait_sshd, pool._wait_ready
        pool._wait_sshd = lambda *a, **k: True
        pool._wait_ready = lambda *a, **k: True

    def tearDown(self):
        pool.POOL_JSON = self._orig_db
        pool.CS = self._orig_cs
        pool._wait_sshd, pool._wait_ready = self._orig_sshd, self._orig_ready
        self._td.cleanup()

    def test_ensure_creates_and_stops(self):
        st = pool.ensure(2, gpu="4090", max_total=4)
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["by_state"].get("idle"), 2)
        self.assertEqual([c for c in self.cs.calls if c.startswith("stop:")].__len__(), 2)

    def test_ensure_creates_with_v4_not_base_keyword(self):
        """池实例必须用 image.json 的镜像，不能回落到 BASE_IMAGE_KEYWORD。

        回归（2026-11-16 实测）：pool.py 原来在 image_id 为空时
        直接 `cs.resolve_image(p2cfg.BASE_IMAGE_KEYWORD)`，于是池里躺的是
        0.36.0 纯净版，而作业新建机走 image.json → v4。
        同一个工作流落到池机还是新机，运行时不一样 —— 最难查的那类问题。
        """
        from tools.p2 import config as p2cfg
        import json as _json
        want = _json.loads(p2cfg.IMAGE_JSON.read_text(encoding="utf-8"))["layers"]["media"]["image_id"]
        pool.ensure(1, gpu="4090", max_total=4)
        created = [v for v in self.cs.insts.values()]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].get("CompShareImageId"), want,
                         "池实例没用 image.json 的镜像")

    def test_ensure_recycles_wrong_image(self):
        """池里已有镜像不对的实例时，ensure 应删掉它重建。"""
        from tools.p2 import config as p2cfg
        import json as _json
        want = _json.loads(p2cfg.IMAGE_JSON.read_text(encoding="utf-8"))["layers"]["media"]["image_id"]
        # 造一台「旧镜像」实例
        self.cs.insts["uhost-old"] = {
            "UHostId": "uhost-old", "State": "Stopped", "Name": "p2-pool-1",
            "CompShareImageId": "compshareImage-old036",
            "CompShareImageName": "ComfyUI基础镜像纯净版0.36.0"}
        pool.POOL_JSON.write_text(_json.dumps({
            "schema": "p2.pool/v1", "gpu": "4090", "size": 1, "image_id": "",
            "max_total": 4,
            "instances": [{"uid": "uhost-old", "state": "idle", "job_id": None,
                           "created_at": 0, "last_used": 0}]}), encoding="utf-8")

        pool.ensure(1, gpu="4090", max_total=4)
        self.assertIn("terminate:uhost-old", self.cs.calls, "旧镜像实例没被回收")
        d = pool.load()
        for it in d["instances"]:
            self.assertEqual(self.cs.insts[it["uid"]].get("CompShareImageId"), want)

    def test_ensure_keeps_matching_image(self):
        """镜像本来就对的实例不该被无谓重建（否则每次 ensure 都烧钱）。"""
        from tools.p2 import config as p2cfg
        import json as _json
        want = _json.loads(p2cfg.IMAGE_JSON.read_text(encoding="utf-8"))["layers"]["media"]["image_id"]
        pool.ensure(1, gpu="4090", max_total=4)
        uid = pool.load()["instances"][0]["uid"]
        n_term = sum(1 for c in self.cs.calls if c.startswith("terminate:"))
        pool.ensure(1, gpu="4090", max_total=4)
        self.assertEqual(sum(1 for c in self.cs.calls if c.startswith("terminate:")), n_term,
                         "镜像一致的实例被误回收")

    def test_ensure_respects_max_total(self):
        self.cs.create(name="busy-1")          # 已占 1 台
        st = pool.ensure(3, gpu="4090", max_total=2)   # 只剩 1 个槽
        self.assertEqual(st["total"], 1)

    def test_ensure_idempotent(self):
        pool.ensure(2, gpu="4090", max_total=4)
        n_create = sum(1 for c in self.cs.calls if c.startswith("create:"))
        pool.ensure(2, gpu="4090", max_total=4)
        self.assertEqual(sum(1 for c in self.cs.calls if c.startswith("create:")), n_create)

    def test_acquire_and_release(self):
        pool.ensure(1, gpu="4090", max_total=4)
        uid = pool.load()["instances"][0]["uid"]
        got = pool.acquire("job-x")
        self.assertIsNotNone(got)
        self.assertEqual(got["uhost_id"], uid)
        self.assertTrue(got["from_pool"])
        self.assertEqual(pool.load()["instances"][0]["state"], "busy")
        self.assertIn(f"start:{uid}", self.cs.calls)
        pool.release(uid, keep=True)
        self.assertEqual(pool.load()["instances"][0]["state"], "idle")

    def test_acquire_empty_returns_none(self):
        self.assertIsNone(pool.acquire("job-x"))

    def test_reconcile_reaps_gone(self):
        pool.ensure(1, gpu="4090", max_total=4)
        uid = pool.load()["instances"][0]["uid"]
        self.cs.insts.pop(uid)                 # 云端消失
        out = pool.reconcile()
        self.assertEqual(out["total"], 0)
        self.assertTrue(any("gone" in c for c in out["changed"]))

    def test_reconcile_busy_to_idle_when_stopped(self):
        pool.ensure(1, gpu="4090", max_total=4)
        uid = pool.load()["instances"][0]["uid"]
        pool.acquire("job-x")
        self.cs.insts[uid]["State"] = "Stopped"   # 被外部停机
        out = pool.reconcile()
        self.assertEqual(pool.load()["instances"][0]["state"], "idle")
        self.assertTrue(any("busy→idle" in c for c in out["changed"]))

    def test_drain_terminate(self):
        pool.ensure(2, gpu="4090", max_total=4)
        out = pool.drain(terminate=True)
        self.assertEqual(len(out["drained"]), 2)
        self.assertEqual(pool.load()["instances"], [])
        self.assertEqual(len(self.cs.insts), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
