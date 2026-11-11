#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""队列「禁用」行为的测试。

## 为什么是禁用而不是删除

队列本身没坏：`enqueue()` 实现完整、`claim()` 原子、心跳/退避/审计都有测试。
坏的是**没有生产者** —— UI 提交的作业从不进队列，于是 queue.db 从 2026-11-01
起就没再写入过，只剩 8 条历史残留。

它唯一还在起作用的地方是**让界面数字互相矛盾**：
「已完成花费 ¥7.35」来自 queue.db 的 8 条，「失败 26 / 完成 20」来自 job.json
的 47 条 —— 真实花费是 ¥3.86。

用户决定：不删，禁用。969 行一行没动，随时可回滚。

## 最容易踩的坑：删文件 ≠ 禁用

`connect()` 原本会 `_ensure_schema()` + `executescript(DDL)`，
**任何一次只读调用都会把删掉的库重新建出来**（实测确认）。所以
"删掉 queue.db" 根本不算禁用 —— 下次谁读一次它就回来了，还是个空库。

这也是为什么闸必须放在 `connect()` 里，而不是各函数各自判断。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import jobqueue as Q  # noqa: E402


class DisabledBase(unittest.TestCase):
    def setUp(self):
        # 强制禁用（环境变量优先级最高，测试不受模块常量影响）
        self._env = mock.patch.dict("os.environ", {"P2_QUEUE_ENABLED": "0"})
        self._env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_db = Q.DB
        Q.DB = Path(self.tmp.name) / "queue.db"

    def tearDown(self):
        Q.DB = self._orig_db
        self.tmp.cleanup()
        self._env.stop()


class TestReadsReturnEmpty(DisabledBase):
    """读接口返回空结果，**不抛异常**。

    读方（reaper / instances / riskmon / status2）都把队列当成一次巡检里的
    附加信息，为一条读不到的队列让整轮巡检崩掉不值当。
    """

    def test_stats_empty(self):
        self.assertEqual(Q.stats(), {"total": 0, "by_state": {}})

    def test_items_empty(self):
        self.assertEqual(Q.items(), [])

    def test_active_count_zero(self):
        self.assertEqual(Q.active_count(), 0)

    def test_claim_returns_none(self):
        self.assertIsNone(Q.claim("worker-1"))

    def test_stale_expired_empty(self):
        self.assertEqual(Q.stale(), [])
        self.assertEqual(Q.expired(), [])
        self.assertEqual(Q.reset_stale(), [])

    def test_batch_helpers(self):
        self.assertIsNone(Q.batch_info("b-1"))
        self.assertEqual(Q.batch_spent("b-1"), 0.0)

    def test_cancelled_ids_empty(self):
        self.assertEqual(Q.cancelled_ids(), set())

    def test_audit_empty(self):
        self.assertEqual(Q.audit_rows(), [])
        self.assertEqual(Q.audit_since(), [])


class TestWritesRaise(DisabledBase):
    """写接口必须**明确报错**。

    静默丢弃会丢作业 —— 调用方以为提交成功了，实际什么都没记下来。
    """

    def test_submit_raises(self):
        with self.assertRaises(Q.QueueDisabled):
            Q.submit("b-1", "j1", "sha256:" + "a" * 64)

    def test_state_transitions_raise(self):
        for name, fn in [
            ("mark_running", lambda: Q.mark_running("j1")),
            ("finish", lambda: Q.finish("j1", True)),
            ("cancel", lambda: Q.cancel("j1")),
            ("release", lambda: Q.release("j1")),
            ("reclaim", lambda: Q.reclaim("j1")),
            ("heartbeat", lambda: Q.heartbeat("j1")),
            ("requeue", lambda: Q.requeue("j1")),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(Q.QueueDisabled):
                    fn()

    def test_batch_write_raises(self):
        with self.assertRaises(Q.QueueDisabled):
            Q.create_batch("b-1", "n")

    def test_error_message_says_where_to_look(self):
        """报错信息要指路，不能只说"禁用"。

        看到这条错的人多半是来排障的，得知道去哪看真实状态。
        """
        with self.assertRaises(Q.QueueDisabled) as cm:
            Q.submit("b-1", "j1", "h")
        msg = str(cm.exception)
        self.assertIn("autostart", msg)
        self.assertIn("P2_QUEUE_ENABLED", msg)


class TestNoFileCreated(DisabledBase):
    """禁用时**绝不创建数据库文件** —— 这是"真禁用"的判据。

    如果 `/tmp/queue.db` 又出现了，说明有代码绕过闸建了库，
    下次那个空库就会开始骗人。
    """

    def test_reads_do_not_create_db(self):
        for fn in (Q.stats, Q.items, Q.active_count, Q.audit_rows,
                   lambda: Q.claim("w"), Q.stale, Q.expired):
            fn()
        self.assertFalse(Q.DB.exists(), f"读操作把库建出来了：{Q.DB}")

    def test_writes_do_not_create_db(self):
        try:
            Q.create_batch("b-1", "n")
        except Q.QueueDisabled:
            pass
        self.assertFalse(Q.DB.exists())

    def test_connect_itself_raises(self):
        """`connect()` 是唯一能建库的入口，必须自己就挡住。"""
        with self.assertRaises(Q.QueueDisabled):
            Q.connect()
        self.assertFalse(Q.DB.exists())


class TestToggleOn(unittest.TestCase):
    """启用开关必须真的能把功能打开 —— 否则"随时可回滚"是空话。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_db = Q.DB
        Q.DB = Path(self.tmp.name) / "queue.db"

    def tearDown(self):
        Q.DB = self._orig_db
        self.tmp.cleanup()

    def test_env_can_enable(self):
        with mock.patch.dict("os.environ", {"P2_QUEUE_ENABLED": "1"}):
            self.assertTrue(Q.is_enabled())
            Q.create_batch("b-1", "test", 2, 1.0)
            self.assertTrue(Q.DB.exists(), "启用后应该真的建库")
            stats = Q.stats()
            self.assertEqual(stats["total"], 0)   # 新库，没有条目
            ok, _ = Q.submit("b-1", "j1", "sha256:" + "a" * 64)
            self.assertTrue(ok, "启用后提交应该成功")
            self.assertEqual(Q.stats()["total"], 1)

    def test_env_off_overrides_module_constant(self):
        """环境变量优先于模块常量 —— 排障时要能不改进程就切换。"""
        with mock.patch.dict("os.environ", {"P2_QUEUE_ENABLED": "0"}):
            self.assertFalse(Q.is_enabled())
        with mock.patch.dict("os.environ", {"P2_QUEUE_ENABLED": "false"}):
            self.assertFalse(Q.is_enabled())
        with mock.patch.dict("os.environ", {"P2_QUEUE_ENABLED": ""}):
            self.assertEqual(Q.is_enabled(), Q.ENABLED,
                             "空字符串视为未设置，回落到模块常量")


class TestModuleDefault(unittest.TestCase):
    def test_default_is_disabled(self):
        """模块常量必须是 False —— 这是产品的决定，不是临时状态。

        谁要翻成 True，先得把生产者（enqueue 的调用方）接上，
        否则就是让界面继续显示两套互相矛盾的账。
        """
        self.assertFalse(Q.ENABLED,
                         "队列默认必须是禁用的；要启用请先接上 enqueue 的生产者")


class TestServerEndpoint(unittest.TestCase):
    """`/api/p2/queue` 要如实报告"已禁用"，不能返回一堆 0 让人以为是空的。"""

    def test_endpoint_reports_disabled(self):
        src = (_ROOT / "fileserver.py").read_text(encoding="utf-8")
        idx = src.index('if sub == ["queue"]:')
        block = src[idx:idx + 1600]
        self.assertIn("is_enabled()", block)
        self.assertIn('"enabled": False', block)
        self.assertIn("autostart", block, "要说清楚自动补位在哪")


class TestFrontendStopsFetching(unittest.TestCase):
    """前端不该再请求 /queue —— 数据永远为空，纯浪费一轮往返。

    更重要的原因：只要 S.queue 里还有字段，下次就可能有人拿它算点什么，
    重演「已完成花费 ¥7.35」（真实 ¥3.86）那个 bug。
    """

    def test_no_active_queue_fetch(self):
        src = (_ROOT / "web" / "dist" / "app.js").read_text(encoding="utf-8")
        active = [ln for ln in src.splitlines()
                  if "get('/queue')" in ln and not ln.strip().startswith("//")]
        self.assertEqual(active, [], f"还有未注释的 /queue 请求：{active}")

    def test_dispatch_still_fetched(self):
        src = (_ROOT / "web" / "dist" / "app.js").read_text(encoding="utf-8")
        active = [ln for ln in src.splitlines()
                  if "get('/dispatch')" in ln and not ln.strip().startswith("//")]
        self.assertTrue(active, "dispatch 是活的功能（调度器状态），必须保留")


class TestNoBareConnect(unittest.TestCase):
    """禁用后，任何模块都**不许裸调** `queue.connect()`。

    这是加开关时漏掉的一处真 bug：`instances._audit_rows()` 原本写的是

        c = queue.connect()      ← 在 try 外
        try: ...

    `connect()` 抛 `QueueDisabled` 时不受 try 保护，整条 `snapshot()` 就炸了
    —— 实例页直接 500。它不是 `queue.items()` 那种有 `is_enabled()` 保护的路径。

    这类"绕过闸"的调用只有靠静态检查才能防住：运行时测试覆盖不到所有调用点，
    而漏一个就是一个页面的 500。
    """

    # 允许裸调 connect 的地方：jobqueue 自己、以及测试
    ALLOW = {"jobqueue.py", "test_queue.py", "test_idempotency.py",
             "test_scheduler.py", "test_queue_disabled.py"}

    def test_no_module_calls_connect_outside_try(self):
        """裸调 `connect()` 且**不在 try 内**才算违规。

        为什么要看 try 边界：`c = queue.connect()` 本身没问题 —— 只要它在
        try 里，`QueueDisabled` 就会被 `except` 接住。`instances.py` 的正确
        写法就是这样。真正危险的是"在 try 外调"（原来那版的 bug）。

        为什么要用 AST 而不是文本匹配：docstring 里写着示例代码
        `c = queue.connect()  ← 在 try 外`，文本匹配会把它当成真实调用误报。
        测试自己制造假阳性，就是在教人忽略它。
        """
        import ast

        offenders = []
        for py in sorted((_ROOT / "tools").rglob("*.py")):
            if py.name in self.ALLOW or "__pycache__" in str(py):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # 收集本函数内 try 语句覆盖的行号区间
                guarded = set()
                for node in ast.walk(fn):
                    if isinstance(node, ast.Try):
                        for sub in ast.walk(node):
                            if hasattr(sub, "lineno"):
                                guarded.add(sub.lineno)
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    f = node.func
                    if not (isinstance(f, ast.Attribute) and f.attr == "connect"):
                        continue
                    base = f.value
                    name = getattr(base, "id", "") or getattr(base, "attr", "")
                    if name not in ("queue", "jobqueue"):
                        continue
                    if node.lineno not in guarded:
                        offenders.append(f"{py.relative_to(_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "这些地方在 try 之外调 connect()，队列禁用时会抛异常：\n  "
                         + "\n  ".join(offenders))

    def test_instances_survives_disabled(self):
        """实例页在队列禁用时必须还能出数据。"""
        from tools.p2 import instances as I
        d = I.snapshot()
        self.assertIsInstance(d, dict)
        self.assertIn("instances", d)
        self.assertIn("watchdog", d)

    def test_riskmon_distinguishes_disabled_from_empty(self):
        """riskmon 要能区分"队列禁用"和"队列空着"。"""
        import importlib
        import tools.riskmon as rm
        importlib.reload(rm)
        with mock.patch.dict("os.environ", {"P2_QUEUE_ENABLED": "0"}):
            lvl, _, desc, _ = rm.c_queue_terminal(None)
        self.assertEqual(lvl, "info")
        self.assertIn("禁用", desc)

    def test_status2_marks_disabled(self):
        """status2.json 的 queue 字段要带 enabled 标记。

        不带的话，arch.py 和 /monitor 页面会照实显示"队列 0 项"，
        和"队列空着待命"长得一模一样。
        """
        src = (_ROOT / "tools" / "p2" / "status2.py").read_text(encoding="utf-8")
        self.assertIn("is_enabled()", src)
        self.assertIn('"enabled": False', src)

    def test_arch_shows_disabled(self):
        src = (_ROOT / "tools" / "arch.py").read_text(encoding="utf-8")
        self.assertIn("队列：已禁用", src)

    def test_monitor_page_shows_disabled(self):
        src = (_ROOT / "fileserver.py").read_text(encoding="utf-8")
        self.assertIn("队列已禁用", src)


if __name__ == "__main__":
    unittest.main()
