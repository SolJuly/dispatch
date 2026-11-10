#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生命周期三态测试（2026-11-10 定版）。

覆盖四块：

  lifecycle     短任务恒 0 / 三档 / 非法值回落 / 停机计时器
  reaper 三态   保活 · 停机 · 销毁，含各条边界
  run.py        短任务销毁 / 长任务只归还槽位（静态断言 + 行为）
  webui/api     task_class / idle_tier 贯通 + 非法值静默回落

全部 ¥0：不建机、不联网、不碰真实运行态文件。

用法：python3 -m unittest tools.p2.tests.test_lifecycle
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import lifecycle as L  # noqa: E402
from tools.p2 import reaper as R  # noqa: E402

RUN_PY = _ROOT / "tools" / "p1" / "run.py"


def _mk_job(root: Path, jid: str, state: str, *, uhost: str = "", model: str = "m",
            workflow: str = "w", task_class: str | None = None,
            idle_tier: int | None = None, idle_after_done_s: int | None = None,
            ended_ago_s: float | None = None) -> Path:
    """造一条作业。`ended_ago_s` 给了就写 events.jsonl，用来控制空闲时长。"""
    d = root / jid
    d.mkdir(parents=True, exist_ok=True)
    job = {
        "schema": "p1.job/v1", "job_id": jid, "state": state,
        "created_at": "2026-01-01T00:00:00+08:00", "kind": "image",
        "model": {"key": model, "registry": {"workflow_key": workflow,
                                             "model_key": model}},
        "params": {"prompt": "x"}, "expect": {}, "steps": {},
        "instance": {"uhost_id": uhost} if uhost else {},
    }
    if task_class is not None:
        job["task_class"] = task_class
    if idle_tier is not None:
        job["idle_tier"] = idle_tier
    if idle_after_done_s is not None:
        job["idle_after_done_s"] = idle_after_done_s
    (d / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    if ended_ago_s is not None:
        (d / "events.jsonl").write_text(
            json.dumps({"seq": 9, "ts": time.time() - ended_ago_s, "to": state}) + "\n",
            encoding="utf-8")
    return d


def _mk_inst(uid: str, *, state: str = "Running", age_min: float = 60) -> dict:
    return {"UHostId": uid, "Name": f"t-{uid[-4:]}", "State": state,
            "StartTime": time.time() - age_min * 60}


class _StoppedIsolated(unittest.TestCase):
    """把停机计时器指到临时文件，别往真实运行态写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_stopped = L.STOPPED_JSON
        L.STOPPED_JSON = self.root / "stopped_at.json"

    def tearDown(self):
        L.STOPPED_JSON = self._orig_stopped
        self.tmp.cleanup()


# ================================================================ 策略
class TestLifecyclePolicy(_StoppedIsolated):
    """短任务恒 0 / 长任务三档 / 非法值回落。"""

    def test_short_is_always_zero(self):
        """短任务是**硬规则**，不是"小一点的窗口"。

        短任务本身只花 ¥0.085–0.139；给 120 秒空转 = ¥0.0717 = 成本的 53%~84%。
        所以短任务必须恒 0，连传入档位都不该生效。
        """
        for chosen in (None, 60, 90, 120, 300, -1, "90"):
            with self.subTest(chosen=chosen):
                self.assertEqual(L.long_idle_s("short", chosen), 0)

    def test_long_three_tiers(self):
        for sec in L.IDLE_TIERS:
            with self.subTest(sec=sec):
                self.assertEqual(L.long_idle_s("long", sec), sec)

    def test_long_default_is_ninety(self):
        self.assertEqual(L.long_idle_s("long", None), 90)
        self.assertEqual(L.DEFAULT_LONG_IDLE_S, 90)

    def test_long_invalid_falls_back_to_default_not_error(self):
        """非法档位回落默认，不抛错。

        这个值可能来自旧版 UI / 手改的 job.json。为了一个展示性参数
        让整条作业失败不值得 —— 但必须回落到已知档位，不能变成任意值。
        """
        for bad in (0, 1, 59, 61, 300, 600, -90, "abc", "", [], {}):
            with self.subTest(bad=bad):
                got = L.long_idle_s("long", bad)
                self.assertEqual(got, 90, f"{bad!r} 应回落默认档 90")
                self.assertIn(got, L.IDLE_TIERS)

    def test_unknown_task_class_is_short(self):
        """未知 task_class 按短任务处理 —— 省钱方向，判错代价小。"""
        for bad in ("", None, "weird", "LONGG", "short "):
            with self.subTest(bad=bad):
                self.assertEqual(L.long_idle_s(bad, 120), 0
                                 if str(bad).strip().lower() != "long" else 120)

    def test_tiers_are_tuple_not_env(self):
        """三档写死成元组，**不开环境变量**。

        一开就会出现 P2_LONG_IDLE_S=600 这种配置，绕过所有经济约束
        （600 秒空转 = ¥0.358，占一条 600 秒长任务成本的 84%）。
        """
        self.assertIsInstance(L.IDLE_TIERS, tuple)
        self.assertEqual(L.IDLE_TIERS, (60, 90, 120))
        src = Path(L.__file__).read_text(encoding="utf-8")
        code = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
        joined = "\n".join(code)
        # 只看"真去读环境变量"的代码行 —— 文档里把 P2_LONG_IDLE_S 当反例讲是正常的
        self.assertNotIn('environ.get("P2_LONG_IDLE_S"', joined,
                         "长任务窗口不能开环境变量（会绕过三档约束）")
        self.assertNotIn("environ['P2_LONG_IDLE_S'", joined)
        self.assertNotIn('environ["P2_LONG_IDLE_S"', joined)

    def test_normalize_task_class_records_source(self):
        """source 字段现在没用，但要趁早补数据 —— 事后补不回来。"""
        self.assertEqual(L.normalize_task_class("long"), ("long", "user"))
        self.assertEqual(L.normalize_task_class("Short"), ("short", "user"))
        self.assertEqual(L.normalize_task_class(""), ("short", "default"))
        self.assertEqual(L.normalize_task_class(None), ("short", "default"))
        self.assertEqual(L.normalize_task_class("weird"), ("short", "default"))

    def test_normalize_tier_rejects_non_tier(self):
        self.assertEqual(L.normalize_tier(90, task_class="long"), 90)
        self.assertIsNone(L.normalize_tier(300, task_class="long"))
        self.assertIsNone(L.normalize_tier(90, task_class="short"),
                          "短任务没有档位可言")
        self.assertEqual(L.normalize_tier("90", task_class="long"), 90,
                         "字符串形式的合法档位要认（HTTP 来的都是字符串）")
        self.assertIsNone(L.normalize_tier(None, task_class="long"))

    def test_idle_for_job_prefers_recorded_value(self):
        """优先用落盘的 idle_after_done_s（审计能直接看到当时实算的值）。"""
        self.assertEqual(L.idle_for_job({"idle_after_done_s": 120}), 120)
        self.assertEqual(L.idle_for_job({"idle_after_done_s": 0}), 0)
        # 落盘的非法值也要收敛到已知档位
        self.assertEqual(L.idle_for_job({"idle_after_done_s": 300}), 90)

    def test_idle_for_job_falls_back_to_task_class(self):
        """没有 idle_after_done_s 的旧作业按 task_class 现算。"""
        self.assertEqual(L.idle_for_job({"task_class": "long", "idle_tier": 60}), 60)
        self.assertEqual(L.idle_for_job({"task_class": "long"}), 90)
        self.assertEqual(L.idle_for_job({"task_class": "short"}), 0)
        self.assertEqual(L.idle_for_job({}), 0, "定版前的作业按短任务")

    def test_each_tier_has_explanation(self):
        """三档每一档都要有说明文案 —— 用户要能理解"我在为什么付费"。"""
        for sec in L.IDLE_TIERS:
            with self.subTest(sec=sec):
                self.assertTrue(L.tier_note(sec), f"{sec}s 缺说明")
                self.assertTrue(L.tier_label(sec))
        self.assertIn("打平", L.tier_note(60), "60 秒的定位是「数学上打平」")
        self.assertIn("默认", L.tier_label(90))
        self.assertIn("8%", L.tier_note(120), "120 秒要说清占长任务成本的多少")

    def test_short_label_and_note(self):
        self.assertIn("短任务", L.tier_label(0))
        self.assertIn("立即回收", L.tier_note(0))


# ================================================================ 停机计时
class TestStoppedTimer(_StoppedIsolated):
    """停机 2 小时 → 销毁。计时从**最后一次停机**算。"""

    def test_default_ttl_is_two_hours(self):
        """你定的 2 小时（先说过 4 小时，同轮改成 2 小时 —— 2 是准的）。"""
        old = L.os.environ.pop("P2_STOPPED_TTL_MIN", None)
        try:
            self.assertEqual(L.stopped_ttl_min(), 120)
        finally:
            if old is not None:
                L.os.environ["P2_STOPPED_TTL_MIN"] = old

    def test_ttl_env_override(self):
        old = L.os.environ.get("P2_STOPPED_TTL_MIN")
        try:
            L.os.environ["P2_STOPPED_TTL_MIN"] = "30"
            self.assertEqual(L.stopped_ttl_min(), 30)
        finally:
            if old is None:
                L.os.environ.pop("P2_STOPPED_TTL_MIN", None)
            else:
                L.os.environ["P2_STOPPED_TTL_MIN"] = old

    def test_ttl_bad_env_falls_back_not_zero(self):
        """0 或负数回落默认 —— 0 会让刚停机的实例立刻被销毁。"""
        old = L.os.environ.get("P2_STOPPED_TTL_MIN")
        try:
            for bad in ("0", "-5", "abc", ""):
                with self.subTest(bad=bad):
                    L.os.environ["P2_STOPPED_TTL_MIN"] = bad
                    self.assertEqual(L.stopped_ttl_min(), 120)
        finally:
            if old is None:
                L.os.environ.pop("P2_STOPPED_TTL_MIN", None)
            else:
                L.os.environ["P2_STOPPED_TTL_MIN"] = old

    def test_record_and_read(self):
        L.record_stopped("u1", 1000.0)
        self.assertEqual(L.stopped_since("u1"), 1000.0)
        self.assertEqual(L.stopped_since("nope"), 0.0,
                         "没记录返回 0（= 不知道，别销毁）")

    def test_record_refreshes_not_keeps_first(self):
        """**关键边界**：计时从最后一次停机算。

        一台实例跑完 A 停机、被唤醒跑 B 又停机 —— 保留期必须从 B 停机那刻
        重新算。保留第一次的话，B 跑完立刻就会因为"停机超过 2 小时"被销毁。
        """
        L.record_stopped("u1", 1000.0)
        L.record_stopped("u1", 5000.0)
        self.assertEqual(L.stopped_since("u1"), 5000.0, "应刷新为最新一次停机")

    def test_clear(self):
        L.record_stopped("u1", 1000.0)
        L.clear_stopped("u1")
        self.assertEqual(L.stopped_since("u1"), 0.0)
        L.clear_stopped("nope")          # 幂等，不抛

    def test_remaining_min(self):
        now = time.time()
        L.record_stopped("u1", now - 60 * 30)      # 半小时前停的
        self.assertAlmostEqual(L.stopped_remaining_min("u1", now=now), 90.0, delta=0.5)
        L.record_stopped("u2", now - 60 * 200)     # 超过 2 小时
        self.assertLess(L.stopped_remaining_min("u2", now=now), 0)
        # None 而不是 -1：-1 会和"已过期恰好剩 -1 分钟"撞在一起，
        # 结果是停机 121 分钟的实例被当成"没有记录"永远不销毁（配额被吃满）。
        self.assertIsNone(L.stopped_remaining_min("nope", now=now))


# ================================================================ reaper 三态
class TestThreeStateDecision(_StoppedIsolated):
    """keep / stop / terminate 三态，逐条边界。"""

    def _assess(self, insts, **kw):
        return R.assess(insts, jobs_dir=self.root, **kw)

    # ---- 保活
    def test_running_job_keeps(self):
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING", uhost="u1")
        rows = self._assess([_mk_inst("u1", age_min=999)])
        self.assertEqual(rows[0].action, "keep")
        self.assertTrue(rows[0].busy)

    def test_long_task_inside_window_keeps(self):
        """长任务跑完 30 秒，窗口 90 秒 → 保活。"""
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u1",
                task_class="long", idle_tier=90, ended_ago_s=30)
        rows = self._assess([_mk_inst("u1", age_min=180)])
        self.assertEqual(rows[0].action, "keep")
        self.assertGreater(rows[0].idle_remaining_s, 0)
        self.assertEqual(rows[0].idle_window_s, 90)
        self.assertIn("长任务空转窗口内", rows[0].action_reason)

    def test_short_task_no_window_at_all(self):
        """短任务窗口 0 秒 —— 跑完 5 秒就已经该停机（不等 10 分钟）。"""
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u1",
                task_class="short", ended_ago_s=5)
        rows = self._assess([_mk_inst("u1", age_min=180)])
        self.assertEqual(rows[0].action, "stop", "短任务不空转")
        self.assertEqual(rows[0].idle_window_s, 0)
        self.assertIn("短任务", rows[0].action_reason)

    def test_failed_job_kept_for_resume(self):
        """FAILED_RUN 还能续跑，实例不能动 —— 否则 `--resume` 无处可去。"""
        for st in ("FAILED_PROVISION", "FAILED_RUN", "FAILED_FETCH", "FAILED_VERIFY"):
            with self.subTest(state=st):
                tmp = tempfile.TemporaryDirectory()
                L.STOPPED_JSON = Path(tmp.name) / "s.json"
                root = Path(tmp.name) / "jobs"
                _mk_job(root, "p1-20260101T0000-aaaaaa", st, uhost="u1")
                rows = R.assess([_mk_inst("u1", age_min=999)], jobs_dir=root)
                self.assertEqual(rows[0].action, "keep", f"{st} 应保留等续跑")
                self.assertIn("续跑", rows[0].action_reason)
                tmp.cleanup()

    def test_queued_same_group_keeps(self):
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u1",
                model="qwen_image", task_class="short", ended_ago_s=999)
        with mock.patch.object(R, "queued_groups", return_value={"qwen_image": 2}):
            rows = self._assess([_mk_inst("u1")])
        self.assertEqual(rows[0].action, "keep")
        self.assertIn("规则 B 让路", rows[0].action_reason)

    # ---- 停机
    def test_long_task_past_window_stops(self):
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u1",
                task_class="long", idle_tier=60, ended_ago_s=120)
        rows = self._assess([_mk_inst("u1", age_min=180)])
        self.assertEqual(rows[0].action, "stop")
        self.assertIn("超过 60s 窗口", rows[0].action_reason)

    def test_no_job_uses_fallback_idle_minutes(self):
        """开着但一次都没用过的机器，只能按"开了多久"判 → 兜底 10 分钟。"""
        rows = self._assess([_mk_inst("u1", age_min=3)])
        self.assertEqual(rows[0].action, "keep", "刚开 3 分钟不该关")
        self.assertEqual(rows[0].idle_window_s, 10 * 60)
        rows = self._assess([_mk_inst("u1", age_min=30)])
        self.assertEqual(rows[0].action, "stop")

    # ---- 销毁
    def test_stopped_past_ttl_terminates(self):
        L.record_stopped("u1", time.time() - 150 * 60)      # 停了 2.5 小时
        rows = self._assess([_mk_inst("u1", state="Stopped", age_min=999)])
        self.assertEqual(rows[0].action, "terminate")
        self.assertIn("超保留期", rows[0].action_reason)

    def test_stopped_within_ttl_keeps(self):
        L.record_stopped("u1", time.time() - 30 * 60)
        rows = self._assess([_mk_inst("u1", state="Stopped", age_min=999)])
        self.assertEqual(rows[0].action, "keep")
        self.assertIn("还剩", rows[0].action_reason)

    def test_stopped_without_record_never_terminates(self):
        """没有停机记录的停机实例**不销毁**。

        停机 ¥0（实测 11.5 小时 14 次采样全 +0.00），销毁不可逆。
        拿不准的时候留着 —— 这个不对称决定了默认方向。
        """
        rows = self._assess([_mk_inst("u1", state="Stopped", age_min=999)])
        self.assertEqual(rows[0].action, "keep")
        self.assertIn("无停机记录", rows[0].action_reason)

    def test_transitional_state_keeps(self):
        for st in ("Stopping", "Starting", "Rebooting"):
            with self.subTest(state=st):
                rows = self._assess([_mk_inst("u1", state=st, age_min=999)])
                self.assertEqual(rows[0].action, "keep")
                self.assertIn("过渡", rows[0].action_reason)

    def test_missing_start_time_keeps(self):
        """云端没返回 StartTime 时不猜 —— 不处理比误关安全。"""
        inst = {"UHostId": "u1", "Name": "t", "State": "Running"}
        rows = self._assess([inst])
        self.assertEqual(rows[0].action, "keep")
        self.assertIn("StartTime", rows[0].action_reason)


# ================================================================ reap 执行
class TestReapExecution(_StoppedIsolated):
    """执行层：默认 auto（该停的停、该销毁的销毁），默认不误销毁。"""

    def test_auto_stops_and_terminates(self):
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u-stop",
                task_class="short", ended_ago_s=60)
        L.record_stopped("u-term", time.time() - 200 * 60)
        insts = [_mk_inst("u-stop", age_min=180),
                 _mk_inst("u-term", state="Stopped", age_min=999)]
        fake = mock.MagicMock()
        with mock.patch("compshare.cs.CS", return_value=fake, create=True):
            R.reap(insts, jobs_dir=self.root, say=lambda *_: None)
        fake.stop.assert_called_once_with("u-stop")
        fake.terminate.assert_called_once_with("u-term")
        # 停机要记计时器；销毁要清计时器
        self.assertGreater(L.stopped_since("u-stop"), 0)
        self.assertEqual(L.stopped_since("u-term"), 0.0)

    def test_stop_only_does_not_terminate(self):
        L.record_stopped("u-term", time.time() - 200 * 60)
        insts = [_mk_inst("u-term", state="Stopped", age_min=999)]
        fake = mock.MagicMock()
        with mock.patch("compshare.cs.CS", return_value=fake, create=True):
            R.reap(insts, jobs_dir=self.root, action="stop", say=lambda *_: None)
        fake.terminate.assert_not_called()

    def test_dry_run_touches_nothing(self):
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u1",
                task_class="short", ended_ago_s=60)
        insts = [_mk_inst("u1", age_min=180)]
        fake = mock.MagicMock()
        with mock.patch("compshare.cs.CS", return_value=fake, create=True):
            got = R.reap(insts, jobs_dir=self.root, dry_run=True, say=lambda *_: None)
        self.assertEqual([s.uid for s in got], ["u1"])
        fake.stop.assert_not_called()
        self.assertEqual(L.stopped_since("u1"), 0.0, "演练不该记停机计时器")

    def test_idle_min_override_beats_tier(self):
        """显式 --idle-min 要能盖过短任务的 0 秒窗口（排障用）。"""
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u1",
                task_class="short", ended_ago_s=120)      # 已空闲 2 分钟
        insts = [_mk_inst("u1", age_min=180)]
        self.assertEqual([s.uid for s in R.reapable(insts, idle_min=10,
                                                    jobs_dir=self.root)], [],
                         "阈值 10 分钟时不该停")
        self.assertEqual([s.uid for s in R.reapable(insts, idle_min=1,
                                                    jobs_dir=self.root)], ["u1"])

    def test_terminable_and_reapable_are_disjoint(self):
        L.record_stopped("u-term", time.time() - 200 * 60)
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "DONE", uhost="u-stop",
                task_class="short", ended_ago_s=60)
        insts = [_mk_inst("u-stop", age_min=180),
                 _mk_inst("u-term", state="Stopped", age_min=999)]
        stops = {s.uid for s in R.reapable(insts, jobs_dir=self.root)}
        terms = {s.uid for s in R.terminable(insts, jobs_dir=self.root)}
        self.assertEqual(stops, {"u-stop"})
        self.assertEqual(terms, {"u-term"})
        self.assertEqual(stops & terms, set(), "两种动作不能落在同一台")


# ================================================================ 存量兜底
class TestLegacyStoppedInstances(_StoppedIsolated):
    """定版之前停掉的实例没有 stopped_at 记录，要能兜底。

    2026-11-10 实测踩到：仓库里那台 uhost-1xhk1c21vlfw 已经停机 21 小时，
    而 stopped_at.json 是这一版才有的文件 —— 一条记录都没有。
    按"无记录就不销毁"处理，它会**永远占着配额**（账户 5 台上限）。
    """

    def test_ledger_path_is_independent_of_stopped_json(self):
        """账本路径不能从 STOPPED_JSON 推导。

        踩过：`_ledger_stop_ts` 曾用 `_stopped_path().parent.parent.parent` 拼账本路径，
        单测把 STOPPED_JSON 指到临时目录后账本就永远找不到 —— 兜底逻辑在测试里
        静默失效，真实环境才暴露。路径必须是独立常量。
        """
        self.assertNotEqual(L.LEDGER_JSON.parent, L.STOPPED_JSON.parent,
                            "两个路径不该有推导关系")
        self.assertEqual(L.LEDGER_JSON.name, "ledger.jsonl")

    def test_ledger_fallback_parses_real_ledger(self):
        """本地没记录时回查账本。用**真实账本里确实存在的**那台实例验。

        这台 `uhost-1xhk1c21vlfw` 在 2026-11-09 11:36:21 有一条 stop（实测存在）。
        断言写成"要么找到、要么明确说明账本里没有"，不静默跳过 ——
        静默跳过等于这条兜底没人验。
        """
        self.assertTrue(L.LEDGER_JSON.exists(), f"缺账本：{L.LEDGER_JSON}")
        text = L.LEDGER_JSON.read_text(encoding="utf-8", errors="replace")
        uid = "uhost-1xhk1c21vlfw"
        self.assertIn(uid, text, "账本里应有这台实例的历史事件（夹具前提）")
        ts = L.stopped_since(uid)
        self.assertGreater(ts, 1_700_000_000,
                           "有 stop 记录就该解析出 epoch 秒，不能返回 0")

    def test_ledger_fallback_disabled(self):
        self.assertEqual(L.stopped_since("uhost-nope"), 0.0)
        self.assertEqual(L.stopped_since("uhost-nope", ledger_fallback=False), 0.0)

    def test_stale_record_guard(self):
        """**关键安全边界**：停机起点早于本次开机 → 记录陈旧，不采信。

        "停过 → 被唤醒 → 又跑过 → 又停了" 的实例，本地记录还是老时间。
        按老时间算会得出"已过期"，一停机就被销毁 —— 误销毁不可逆。
        """
        L.record_stopped("u-stale", time.time() - 300 * 60)      # 5 小时前"停"的
        inst = {"UHostId": "u-stale", "Name": "t", "State": "Stopped",
                "StartTime": time.time() - 60 * 60}              # 但 1 小时前才开机
        rows = R.assess([inst], jobs_dir=self.root)
        self.assertEqual(rows[0].action, "keep", "陈旧记录不能导致销毁")
        self.assertIn("无停机记录", rows[0].action_reason)

    def test_fresh_record_wins_over_ledger(self):
        """本地记录比账本新时用本地的（账本只有 csctl stop 才写）。"""
        L.record_stopped("u1", 2_000_000_000.0)
        self.assertEqual(L.stopped_since("u1"), 2_000_000_000.0)

    def test_adoption_gives_untracked_stopped_instances_a_start(self):
        """无记录的停机实例要被"认领"：从第一次看到它停机的时刻起算保留期。

        不认领的后果是永久保留 —— 几台僵尸就能吃满 5 台配额。
        认领的起点晚于真实停机时刻，所以保留期只会更长、不会更短。
        """
        inst = {"UHostId": "u-orphan", "Name": "t", "State": "Stopped",
                "StartTime": time.time() - 60 * 60}
        fake = mock.MagicMock()
        said = []
        with mock.patch("compshare.cs.CS", return_value=fake, create=True):
            R.reap([inst], jobs_dir=self.root, dry_run=True, say=said.append)
        self.assertGreater(L.stopped_since("u-orphan"), 0,
                           "演练也要认领（否则永远算不出已过期）")
        self.assertTrue(any("无停机记录" in x for x in said))

    def test_adopted_instance_expires_next_round(self):
        """认领之后，下一轮就算得出"已过期" —— 这才是认领的意义。

        时间线要写对：开机（3.5 小时前）→ 停机（2.5 小时前，被认领记下）
        → 现在。认领时刻**晚于**开机，所以不是陈旧记录，能被采信。
        """
        now = time.time()
        L.record_stopped("u-orphan", now - 150 * 60)             # 2.5 小时前停的
        inst = {"UHostId": "u-orphan", "Name": "t", "State": "Stopped",
                "StartTime": now - 210 * 60}                       # 3.5 小时前开机
        rows = R.assess([inst], jobs_dir=self.root, now=now)
        self.assertEqual(rows[0].action, "terminate")

    def test_adoption_timestamp_after_start_is_trusted(self):
        """认领写在开机之后 → 采信；写在开机之前 → 按陈旧记录拦掉。

        这一正一反两条一起才说明守卫判的是"时间线是否自洽"，不是"有没有记录"。
        """
        now = time.time()
        inst = {"UHostId": "u-x", "Name": "t", "State": "Stopped",
                "StartTime": now - 120 * 60}                       # 2 小时前开机
        L.record_stopped("u-x", now - 30 * 60)                     # 0.5 小时前停机
        self.assertEqual(R.assess([inst], jobs_dir=self.root, now=now)[0].action, "keep")
        L.record_stopped("u-x", now - 200 * 60)                    # 写成开机之前
        rows = R.assess([inst], jobs_dir=self.root, now=now)
        self.assertEqual(rows[0].action, "keep", "陈旧记录不能导致销毁")
        self.assertIn("无停机记录", rows[0].action_reason)


# ================================================================ 豁免
class TestLabPrefixExempt(unittest.TestCase):
    """`lab-*` 的机器由 lab_runner 自己管，生命周期不归 reaper 判。

    `csctl watch` 一直有这个跳过逻辑。撤掉 watch、让 reaper 接手清理职责时，
    必须把豁免一起搬过来，否则**行为倒退**：原来不动的机器现在会被停机。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig = L.STOPPED_JSON
        L.STOPPED_JSON = self.root / "stopped_at.json"

    def tearDown(self):
        L.STOPPED_JSON = self._orig
        self.tmp.cleanup()

    def test_lab_instance_not_assessed(self):
        now = time.time()
        inst = {"UHostId": "u-lab", "Name": "lab-bench-1", "State": "Running",
                "StartTime": now - 9999 * 60}
        rows = R.assess([inst], jobs_dir=self.root, now=now)
        self.assertEqual(rows, [], "lab-* 不该产出 InstanceState")

    def test_normal_instance_still_assessed(self):
        """豁免只针对 lab-*，别把正常实例一起放过了（负向对照）。"""
        now = time.time()
        for name in ("p1-abc", "p2-def", "exp-offline-24h", ""):
            with self.subTest(name=name):
                inst = {"UHostId": "u-x", "Name": name, "State": "Running",
                        "StartTime": now - 9999 * 60}
                rows = R.assess([inst], jobs_dir=self.root, now=now)
                self.assertEqual(len(rows), 1, f"{name!r} 应被评估")
                self.assertEqual(rows[0].action, "stop")

    def test_prefix_must_match_from_start(self):
        """前缀匹配是 startswith，不是"包含"—— `my-lab-test` 不该被豁免。"""
        now = time.time()
        inst = {"UHostId": "u-x", "Name": "my-lab-test", "State": "Running",
                "StartTime": now - 9999 * 60}
        rows = R.assess([inst], jobs_dir=self.root, now=now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "stop")

    def test_skip_list_is_centralized(self):
        """豁免前缀集中在一个常量里，不要在 assess 里散写字符串。"""
        self.assertIn("lab-", R.SKIP_NAME_PREFIXES)


# ================================================================ run.py 接入
class TestRunWiring(unittest.TestCase):
    """run.py 的释放分支（静态断言 —— 真跑要建机）。"""

    def setUp(self):
        self.src = RUN_PY.read_text(encoding="utf-8")

    def _release_body(self) -> str:
        return self.src.split("def step_release")[1].split("\ndef ")[0]

    def test_long_task_does_not_destroy(self):
        """长任务分支必须在销毁之前 return，否则实例照旧被干掉。"""
        body = self._release_body()
        long_br = body.index("idle_s > 0")
        term = body.index("cs.terminate(uid)")
        self.assertLess(long_br, term, "长任务分支要在销毁之前")
        seg = body[long_br:term]
        self.assertIn("return", seg, "长任务分支必须提前 return")

    def test_release_reads_lifecycle_module(self):
        self.assertIn("from tools.p2 import lifecycle", self.src)
        self.assertIn("lifecycle.idle_for_job", self.src)

    def test_lifecycle_failure_does_not_break_release(self):
        """策略模块坏掉必须按短任务走，不能让释放阶段抛异常。

        释放阶段抛异常的后果是实例留在那儿继续计费 —— 比"空转窗口算错"严重得多。
        """
        body = self.src.split("def _idle_after_done")[1].split("\ndef ")[0]
        self.assertIn("except Exception", body)
        self.assertIn("return 0", body)


# ================================================================ 复用前提
class TestBorrowPrecondition(unittest.TestCase):
    """长任务的空转窗口只有配上「槽位复用」才有意义。

    窗口的成本逻辑是：空转 90s 花 ¥0.054，省下「停机再唤醒」的 ¥0.036。
    如果这台实例根本不会被复用，那 90 秒就是纯支出。

    而 `_try_borrow_slot` 的第一行是 `if not _registry_meta(job): return None` ——
    没有 `model.registry` 的作业（legacy）不参与复用。
    """

    def test_make_spec_always_sets_registry_meta(self):
        """UI 提交的作业一定带 registry meta → 能被复用 → 空转窗口有意义。

        `p20ctl._make_spec` 无条件把 registry_meta 放进 spec.model.registry，
        而 `webui.api.create_job` 一定走 `_make_spec`。所以 UI 提交的作业
        不存在"付了空转钱却拿不到复用"的情况。
        """
        from tools.p20 import p20ctl, registry
        r = registry.resolve("qwen_image_t2i", params={"prompt": "a red apple", "seed": 42})
        spec = p20ctl._make_spec(r, job_key="qwen_image_t2i", task_ref="x")
        meta = (spec.get("model") or {}).get("registry") or {}
        self.assertEqual(meta.get("workflow_key"), "qwen_image_t2i")
        self.assertEqual(meta.get("model_key"), "qwen_image")

    def test_registry_meta_is_unconditional(self):
        """`_make_spec` 对 registry 元数据不做条件判断（读源码锁死行为）。

        如果哪天有人把它改成"只对某些工作流设置"，UI 提交的作业就会
        静默掉进 legacy 分支：勾了长任务 → 付空转钱 → 不被复用。
        """
        import inspect
        from tools.p20 import p20ctl
        src = inspect.getsource(p20ctl._make_spec)
        self.assertIn('"registry": registry_meta', src)
        # registry_meta 的赋值里不该出现 if / 三元
        assign = src.split("registry_meta = {")[1].split("}")[0]
        self.assertNotIn(" if ", assign, "registry_meta 不能条件化")


# ================================================================ jobctl 落盘
class TestJobctlFields(unittest.TestCase):
    """job.json 要落下三条生命周期事实，reaper 只读这里。"""

    def setUp(self):
        sys.path.insert(0, str(_ROOT))
        from tools.p1 import jobctl
        self.jobctl = jobctl

    def test_short_fields(self):
        got = self.jobctl._lifecycle_fields({})
        self.assertEqual(got["task_class"], "short")
        self.assertEqual(got["task_class_source"], "default")
        self.assertIsNone(got["idle_tier"])
        self.assertEqual(got["idle_after_done_s"], 0)

    def test_long_fields(self):
        got = self.jobctl._lifecycle_fields({"task_class": "long", "idle_tier": 120})
        self.assertEqual(got["task_class"], "long")
        self.assertEqual(got["task_class_source"], "user")
        self.assertEqual(got["idle_tier"], 120)
        self.assertEqual(got["idle_after_done_s"], 120)

    def test_long_without_tier_gets_default(self):
        got = self.jobctl._lifecycle_fields({"task_class": "long"})
        self.assertIsNone(got["idle_tier"])
        self.assertEqual(got["idle_after_done_s"], 90)

    def test_invalid_tier_dropped(self):
        got = self.jobctl._lifecycle_fields({"task_class": "long", "idle_tier": 600})
        self.assertIsNone(got["idle_tier"], "非法档位不落盘")
        self.assertEqual(got["idle_after_done_s"], 90)


# ================================================================ schema 校验
class TestSchemaLifecycle(unittest.TestCase):
    """schema 要拦住"短任务带非 0 窗口"这种直接烧钱的错。"""

    def setUp(self):
        from tools.p1 import schema
        self.schema = schema

    def _job(self, **extra) -> dict:
        from tools.p1 import store
        model = {"key": "m", "workflow": "w", "checkpoint": "c",
                 "workflow_sha256": "a" * 64}
        params = {"prompt": "x"}
        expect = {"ext": "png", "min_bytes": 1000}
        d = {"schema": self.schema.JOB_SCHEMA, "job_id": "p1-20260101T0000-aaaaaa",
             "created_at": "2026-01-01T00:00:00+08:00", "branch": "b",
             "kind": "image", "model": model, "params": params,
             "params_hash": store.params_hash(model, params, expect),
             "expect": expect,
             "budget": {"max_minutes": 10, "max_cost": 1.0},
             "instance": {"required_gpu": ["4090"]},
             "state": "CREATED", "state_seq": 1}
        d.update(extra)
        return d

    def test_lifecycle_fields_optional(self):
        """定版前的作业没有这些字段，不能判成非法。"""
        self.assertEqual(self.schema.validate_job(self._job()), [])

    def test_valid_short_and_long(self):
        self.assertEqual(self.schema.validate_job(
            self._job(task_class="short", idle_after_done_s=0)), [])
        for sec in (60, 90, 120):
            with self.subTest(sec=sec):
                self.assertEqual(self.schema.validate_job(
                    self._job(task_class="long", idle_tier=sec,
                              idle_after_done_s=sec)), [])

    def test_short_with_nonzero_window_rejected(self):
        """短任务带 120 秒窗口 = 任务成本的 53%~84%，必须拦。"""
        errs = self.schema.validate_job(
            self._job(task_class="short", idle_after_done_s=120))
        self.assertTrue(any("短任务" in e for e in errs), errs)

    def test_long_with_off_tier_rejected(self):
        errs = self.schema.validate_job(
            self._job(task_class="long", idle_tier=300, idle_after_done_s=300))
        self.assertTrue(any("idle_tier" in e for e in errs), errs)
        self.assertTrue(any("idle_after_done_s" in e for e in errs), errs)

    def test_bad_task_class_rejected(self):
        errs = self.schema.validate_job(self._job(task_class="medium"))
        self.assertTrue(any("task_class" in e for e in errs), errs)


# ================================================================ webui 贯通
# 假 jobctl：真 jobctl 会算 hash、查重、落盘，要连 registry。
# 这里只要把 spec 里的生命周期字段原样抄进 job.json —— 这正是被测的那段。
FAKE_JOBCTL = r'''
import json, sys
sys.path.insert(0, {root!r})
from tools.p1 import store
from pathlib import Path
spec = json.load(open(sys.argv[sys.argv.index("--spec") + 1], encoding="utf-8"))
jid = "p1-20260101T0000-abc123"
d = Path({jobs!r}) / jid
d.mkdir(parents=True, exist_ok=True)
ph = store.params_hash(spec["model"], spec["params"], spec.get("expect") or {{}})
job = {{"schema": "p1.job/v1", "job_id": jid, "state": "CREATED",
        "created_at": "2026-01-01T00:00:00+08:00", "kind": spec.get("kind", "image"),
        "model": spec.get("model"), "params": spec.get("params"),
        "expect": spec.get("expect"), "params_hash": ph, "steps": {{}}}}
for k in ("task_class", "task_class_source", "idle_tier"):
    if k in spec:
        job[k] = spec[k]
(d / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
print("✓ 作业已创建 " + jid)
'''


class TestApiPassThrough(unittest.TestCase):
    """task_class / idle_tier 从 HTTP 一路进到 spec → job.json。"""

    def setUp(self):
        from webui import api
        self._api = api
        self._mp = api.maybe_publish
        api.maybe_publish = lambda *a, **k: None
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.jobs = self.root / "jobs"
        self.jobs.mkdir()
        self.fake = self.root / "fake_jobctl.py"
        self.fake.write_text(
            FAKE_JOBCTL.format(root=str(api.ROOT), jobs=str(self.jobs)), encoding="utf-8")

    def tearDown(self):
        self._api.maybe_publish = self._mp
        self.tmp.cleanup()

    def _create(self, **kw):
        return self._api.create_job(
            "qwen_image_t2i", {"prompt": "a red apple", "seed": 42},
            jobs_dir=self.jobs, spec_dir=self.root / "specs",
            jobctl=self.fake, **kw)

    def test_default_is_short(self):
        r = self._create()
        self.assertTrue(r["lifecycle"]["task_class"] == "short")
        self.assertEqual(r["lifecycle"]["idle_after_done_s"], 0)

    def test_long_with_tier(self):
        r = self._create(task_class="long", idle_tier=120)
        self.assertEqual(r["lifecycle"]["task_class"], "long")
        self.assertEqual(r["lifecycle"]["idle_after_done_s"], 120)

    def test_long_without_tier_uses_default(self):
        r = self._create(task_class="long")
        self.assertEqual(r["lifecycle"]["idle_after_done_s"], 90)

    def test_invalid_values_fall_back_silently(self):
        """非法值不 400 —— 为一个展示性选项丢掉整套参数不值得。"""
        r = self._create(task_class="nonsense", idle_tier=999)
        self.assertEqual(r["lifecycle"]["task_class"], "short")
        self.assertEqual(r["lifecycle"]["idle_after_done_s"], 0)

    def test_lifecycle_reaches_spec_and_job(self):
        """两个落点都要对：spec（交给 jobctl）和 job.json（reaper 读这里）。"""
        r = self._create(task_class="long", idle_tier=60)
        spec = json.loads(next((self.root / "specs").glob("*.json")).read_text("utf-8"))
        self.assertEqual(spec["task_class"], "long")
        self.assertEqual(spec["task_class_source"], "user")
        self.assertEqual(spec["idle_tier"], 60)
        # idle_after_done_s 由 jobctl 算（策略只在 lifecycle 一处），
        # 所以 spec 里没有它，job.json 里应该有 —— 这里断言后者的值正确。
        self.assertEqual(r["lifecycle"]["idle_after_done_s"], 60)
        j = json.loads((self.jobs / r["job_id"] / "job.json").read_text("utf-8"))
        self.assertEqual(j["task_class"], "long")
        self.assertEqual(j["idle_tier"], 60)

    def test_short_not_written_to_spec(self):
        """短任务不该在 spec 里留 idle_tier —— 没有档位可言。"""
        self._create(task_class="short", idle_tier=120)
        spec = json.loads(next((self.root / "specs").glob("*.json")).read_text("utf-8"))
        self.assertEqual(spec["task_class"], "short")
        self.assertNotIn("idle_tier", spec)


if __name__ == "__main__":
    unittest.main()
