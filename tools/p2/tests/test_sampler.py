#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采样与实例快照的测试。

为什么现在才补：`tools/p2/instances.py` 是实例页/watchdog 的唯一数据源，
却一直没有任何测试。`b1d70e7` 撤掉写 `watch.log` 的 `csctl watch` 后，
采样静默停更 52 小时，页面看起来"一切正常"（因为老数据还在文件里，
只在文件完全为空时才提示），用户报"watchdog 不实时了"才被发现。

这里锁住三件事：
  1. 解析器认得 sampler 写出的行（格式对齐，不然新采样器等于没接上）
  2. 采样停更时必须出现在 notes 里（不能静默）
  3. sampler 只读：不停机、不销毁、不改实例状态
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools.p2 import instances as I
from tools.p2 import sampler as S


class _FakeQueue:
    """替身队列。

    注意 `mock.Mock(items=[])` **不能用** —— Mock 的构造关键字是"属性"，
    而 instances.snapshot 调的是 `queue.items()`（方法）。给属性赋一个 list
    再调用它，报 `TypeError: 'list' object is not callable`。
    """

    def items(self):
        return []


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


class TestParseSamplerLines(unittest.TestCase):
    """instances.py 的解析器必须认得 sampler.py 写出的行。

    两边是**同一个格式的两半**，谁改了格式都不会报错，只会让实例页
    静默变空 —— 所以要用真实的 sampler 输出反过来喂解析器。
    """

    def test_parser_reads_line_written_by_sampler(self):
        line = ("[14:05:33] uhost-1xkyan1hwwbf p1-7f562f        "
                "gpu=100% cpu=13% idle=0m run=0.42h 花=¥0.90")
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "watch.log", line + "\n")
            got = I.parse_watch_log(p, mtime=time.mktime(
                (2026, 11, 11, 14, 5, 33, 0, 0, -1)))
        self.assertIn("uhost-1xkyan1hwwbf", got)
        s = got["uhost-1xkyan1hwwbf"]
        self.assertEqual(s["gpu_util"], 100)
        self.assertEqual(s["cpu_util"], 13)
        self.assertEqual(s["name"], "p1-7f562f")
        self.assertAlmostEqual(s["cost_cny"], 0.90, places=2)

    def test_na_gpu_becomes_none_not_zero(self):
        """gpu=n/a 必须解析成 None。

        如果写成 0，实例页会显示"GPU 闲着"，看板会误判可以回收 ——
        而真相是"读不到监控数据"。
        """
        line = "[14:05:33] uhost-abc p2-x gpu=n/a cpu=n/a idle=0m run=0.02h 花=¥0.04"
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "watch.log", line + "\n")
            got = I.parse_watch_log(p, mtime=time.mktime(
                (2026, 11, 11, 14, 5, 33, 0, 0, -1)))
        self.assertIsNone(got["uhost-abc"]["gpu_util"])

    def test_empty_file_gives_empty_dict(self):
        with tempfile.TemporaryDirectory() as td:
            p = _write(Path(td) / "watch.log", "")
            self.assertEqual(I.parse_watch_log(p), {})

    def test_missing_file_gives_empty_dict(self):
        self.assertEqual(I.parse_watch_log(Path("/nonexistent/watch.log")), {})


class TestStaleSamplingWarns(unittest.TestCase):
    """采样停更必须在 notes 里说出来。

    52 小时静默那次，notes 是空的 —— 因为代码只在"完全为空"时提示，
    而文件里有 11-09 的老数据。用户看到的是空白表格，没有任何解释。
    """

    def _snapshot_with_age(self, age_min: float) -> dict:
        now = time.time()
        last = now - age_min * 60
        ts = time.strftime("%H:%M:%S", time.localtime(last))
        line = (f"[{ts}] uhost-abc p1-x gpu=0% cpu=5% idle=0m run=0.10h 花=¥0.20")

        with tempfile.TemporaryDirectory() as td:
            log = _write(Path(td) / "watch.log", line + "\n")
            # 必须以"最后一行 ≈ mtime"锚定日期，否则跨零点会算错
            import os
            os.utime(log, (last, last))
            with mock.patch.object(I, "WATCH_LOG", log), \
                 mock.patch.object(I, "WATCH_STATE", Path(td) / "nope.json"), \
                 mock.patch.object(I, "WATCHDOG_LOG", Path(td) / "nope.log"), \
                 mock.patch.object(I, "LEDGER", Path(td) / "nope.jsonl"), \
                 mock.patch.object(I, "_audit_rows", return_value=[]), \
                 mock.patch.object(I, "queue", _FakeQueue()):
                return I.snapshot(now=now)

    def test_fresh_sampling_has_no_stale_note(self):
        d = self._snapshot_with_age(3)
        self.assertFalse([n for n in d["notes"] if "停更" in n],
                         f"刚采过样不该告警：{d['notes']}")

    def test_stale_sampling_is_reported(self):
        d = self._snapshot_with_age(52 * 60)     # 52 小时，就是这次的真实值
        hits = [n for n in d["notes"] if "停更" in n]
        self.assertTrue(hits, f"采样停更 52 小时必须告警，实际 notes={d['notes']}")
        self.assertIn("sampler.py", hits[0])

    def test_threshold_is_not_too_twitchy(self):
        """5 分钟采样一次，20 分钟阈值 = 漏 4 轮才喊。抖一两轮不该报警。"""
        d = self._snapshot_with_age(10)
        self.assertFalse([n for n in d["notes"] if "停更" in n])

    def test_empty_log_still_reported(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(I, "WATCH_LOG", Path(td) / "none.log"), \
                 mock.patch.object(I, "WATCH_STATE", Path(td) / "nope.json"), \
                 mock.patch.object(I, "WATCHDOG_LOG", Path(td) / "nope.log"), \
                 mock.patch.object(I, "LEDGER", Path(td) / "nope.jsonl"), \
                 mock.patch.object(I, "_audit_rows", return_value=[]), \
                 mock.patch.object(I, "queue", _FakeQueue()):
                d = I.snapshot()
        self.assertTrue([n for n in d["notes"] if "缺失" in n or "不可用" in n])


class TestSamplerIsReadOnly(unittest.TestCase):
    """sampler 只能读云端 + 写日志。

    它的前身 `csctl watch` 会**停机**，撤掉它是对的判据，但采样功能必须留下。
    如果哪天有人给 sampler 加了 stop/terminate，就会变成第二套清理判据，
    和 reaper 打架 —— 那是 b1d70e7 修掉的问题重新长回来。
    """

    def test_source_has_no_mutating_calls(self):
        src = Path(S.__file__).read_text(encoding="utf-8")
        # 只检查可执行代码行，跳过注释和文档字符串里的说明
        code = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#"))
        for bad in ("cs.stop(", "cs.terminate(", ".stop(", ".terminate("):
            self.assertNotIn(bad, code,
                             f"sampler 不该调用 {bad} —— 停机归 p2-reaper")

    def test_no_instances_writes_nothing(self):
        with mock.patch("compshare.cs.CS") as CS:
            CS.return_value.instances.return_value = []
            with tempfile.TemporaryDirectory() as td:
                log = Path(td) / "watch.log"
                # 先放一行老数据，验证"没有实例"时不会把它清掉或追加东西
                log.write_text("[10:00:00] uhost-old p1-old gpu=0% cpu=0% "
                               "idle=0m run=0.01h 花=¥0.02\n", encoding="utf-8")
                before = log.read_text(encoding="utf-8")
                with mock.patch.object(S, "WATCH_LOG", log):
                    out = S.sample(say=lambda *_: None)
                self.assertEqual(out, [])
                self.assertEqual(log.read_text(encoding="utf-8"), before)

    def test_writes_one_line_per_instance(self):
        insts = [
            {"UHostId": "uhost-a", "Name": "p1-x", "State": "Running",
             "StartTime": time.time() - 3600},
            {"UHostId": "uhost-b", "Name": "p2-y", "State": "Stopped",
             "StartTime": time.time() - 3600},
        ]
        with mock.patch("compshare.cs.CS") as CS:
            CS.return_value.instances.return_value = insts
            CS.return_value.monitor.return_value = {"gpu": 55.0, "cpu": 12.0}
            with tempfile.TemporaryDirectory() as td:
                log = Path(td) / "watch.log"
                with mock.patch.object(S, "WATCH_LOG", log), \
                     mock.patch.object(S, "WATCH_STATE", Path(td) / "none.json"):
                    lines = S.sample(say=lambda *_: None)
                text = log.read_text(encoding="utf-8")
        self.assertEqual(len(lines), 2)
        self.assertIn("uhost-a", text)
        self.assertIn("uhost-b", text)
        # 停机的机器 run=0（关机不算力费），不能按开机时长算成 1 小时
        b = [ln for ln in lines if "uhost-b" in ln][0]
        self.assertIn("run=0.00h", b)
        self.assertIn("花=¥0.00", b)

    def test_cloud_failure_skips_quietly(self):
        """读不到云端就跳过这一轮，**不抛异常**。

        cron 每 5 分钟一次，网络抖一下就炸只会刷满邮件，信息量为零。
        """
        with mock.patch("compshare.cs.CS") as CS:
            CS.return_value.instances.side_effect = RuntimeError("boom")
            with tempfile.TemporaryDirectory() as td:
                log = Path(td) / "watch.log"
                with mock.patch.object(S, "WATCH_LOG", log):
                    out = S.sample(say=lambda *_: None)
                self.assertEqual(out, [])
                self.assertFalse(log.exists())


def _code_only(text: str) -> str:
    """只留可执行代码：去掉注释和 docstring。

    直接对源码做子串匹配会把**说明文字**也算进去 —— 我们在注释里写了
    "踩过 immutable"这段解释，却被自己的测试判成违规。被测的是行为，不是注释。
    """
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class TestStaticAssetCache(unittest.TestCase):
    """web/dist 是源码不是构建产物，文件名没有 hash。

    所以绝不能发 `immutable` —— 那会让浏览器跳过一切校验，改一次前端
    一年看不到（而且**刷新也无效**）。这个坑让"UI 改了还是老样子"
    被误判成"前端没同步"。
    """

    def _src(self) -> str:
        root = Path(__file__).resolve().parents[3]
        return (root / "fileserver.py").read_text(encoding="utf-8")

    def test_no_immutable_for_ui_assets(self):
        src = self._src()
        # 只检查 _ui_asset 函数体
        start = src.index("def _ui_asset")
        end = src.index("def _api_jobs", start)
        body = _code_only(src[start:end])
        self.assertNotIn("immutable", body,
                         "web/dist 文件名不带 hash，不能发 immutable")

    def test_no_cache_for_ui_assets(self):
        """no-cache = 可以缓存但要回源校验，这是对的；no-store = 完全禁缓存，太重。"""
        src = self._src()
        start = src.index("def _ui_asset")
        end = src.index("def _api_jobs", start)
        body = _code_only(src[start:end])
        # ast.unparse 统一用单引号，所以按值断言而不是按引号风格
        self.assertIn("no-cache", body)

    def test_etag_present(self):
        src = self._src()
        start = src.index("def _ui_asset")
        end = src.index("def _api_jobs", start)
        body = _code_only(src[start:end])
        self.assertIn("ETag", body, "没有 ETag 就只能靠 max-age 硬缓存")
        self.assertIn("If-None-Match", body, "要能回 304，否则每次都整包重传")


if __name__ == "__main__":
    unittest.main()
