#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L4 任务控制层的缺口测试：并发闸门 / 孤儿兜底 / 成本口径 / 运行中预算。

覆盖 2026-11-09 实证出来的 6 处真缺口（审计报告 13:20 写的，13:28 有个
93ae85f 修了一批，所以报告里有些结论已经过期 —— 下面的用例都是**实测**出来的）：

  A 并发无上限       UI 提交 N 个 = 建 N 台机，¥2.15/h/台，账户 5 台配额
  B 孤儿实例         run.py 无信号处理，kill -9 后实例留 Running 继续计费
  C 熔断器失明       today_cost() 只认 cost 字段，真实扣费在 settle.amount
  D 僵尸调度服务     p2-dispatch 的 cwd 指向已删除的 /root/wt-ui
  E 运行中无复查     max_cost 建机前查一次，跑起来就不管了

用法：python3 -m unittest tools.p2.tests.test_l4_gaps
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

from tools.p2 import concurrency as conc  # noqa: E402
from tools.p2 import spend  # noqa: E402

RUN_PY = _ROOT / "tools" / "p1" / "run.py"
API_PY = _ROOT / "webui" / "api.py"


def _mk_job(root: Path, jid: str, state: str) -> Path:
    d = root / jid
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.json").write_text(json.dumps({
        "schema": "p1.job/v1", "job_id": jid, "state": state,
        "created_at": "2026-01-01T00:00:00+08:00", "kind": "video",
        "model": {"key": "m", "registry": {"workflow_key": "w"}},
        "params": {"prompt": "x"}, "expect": {}, "steps": {}}, ensure_ascii=False),
        encoding="utf-8")
    return d


class TestConcurrency(unittest.TestCase):
    """A：并发闸门。以前没有任何上限 —— 点五次"提交并执行"就开五台机。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_limit_is_conservative(self):
        """默认必须是"不会因为误操作烧穿余额"的数，不是账户配额 5。

        5 台 × ¥2.15/h = ¥10.75/h，而余额长期 ¥8 上下。配额不是预算。
        """
        old = conc.os.environ.pop("P21_MAX_PARALLEL", None)
        try:
            cap = conc.max_parallel()
            self.assertEqual(cap, 2)
            self.assertLess(cap, 5, "默认上限不该等于账户配额 —— 配额不是预算")
        finally:
            if old is not None:
                conc.os.environ["P21_MAX_PARALLEL"] = old

    def test_env_override(self):
        old = conc.os.environ.get("P21_MAX_PARALLEL")
        try:
            conc.os.environ["P21_MAX_PARALLEL"] = "4"
            self.assertEqual(conc.max_parallel(), 4)
        finally:
            if old is None:
                conc.os.environ.pop("P21_MAX_PARALLEL", None)
            else:
                conc.os.environ["P21_MAX_PARALLEL"] = old

    def test_bad_env_falls_back_not_zero(self):
        """非法值回落默认，不能变 0 —— 0 会锁死整个系统。"""
        old = conc.os.environ.get("P21_MAX_PARALLEL")
        try:
            for bad in ("abc", "0", "-3", ""):
                conc.os.environ["P21_MAX_PARALLEL"] = bad
                with self.subTest(bad=bad):
                    self.assertEqual(conc.max_parallel(), conc.DEFAULT_MAX_PARALLEL)
        finally:
            if old is None:
                conc.os.environ.pop("P21_MAX_PARALLEL", None)
            else:
                conc.os.environ["P21_MAX_PARALLEL"] = old

    def test_counts_only_non_terminal(self):
        """终态作业不占槽，非终态才占。"""
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING")
        _mk_job(self.root, "p1-20260101T0001-bbbbbb", "DONE")
        _mk_job(self.root, "p1-20260101T0002-cccccc", "FAILED")
        _mk_job(self.root, "p1-20260101T0003-dddddd", "CREATED")
        self.assertEqual(conc.slots_used(self.root), 2)   # RUNNING + CREATED

    def test_orphan_job_still_occupies_slot(self):
        """孤儿作业（进程死了、状态停在 RUNNING）**必须**算占槽。

        它对应的实例可能还在云端计费。宁可让人手动清一个假占位，
        也不要因为自动判定"它已经死了"而多开一台机。
        """
        _mk_job(self.root, "p1-20260101T0000-oooooo", "RUNNING")
        self.assertEqual(conc.slots_used(self.root), 1)
        ok, why = conc.check_slot(self.root, limit=2)
        self.assertTrue(ok)
        _mk_job(self.root, "p1-20260101T0001-pppppp", "RUNNING")
        ok2, why2 = conc.check_slot(self.root, limit=2)
        self.assertFalse(ok2, "两个孤儿占满 2 个槽，第三个应该被拒")
        self.assertIn("并发已满", why2)

    def test_check_slot_reports_job_ids(self):
        """拒绝时要说清是谁在占槽，不能只说"满了"。"""
        _mk_job(self.root, "p1-20260101T0000-aaaaaa", "RUNNING")
        ok, why = conc.check_slot(self.root, limit=1)
        self.assertFalse(ok)
        self.assertIn("p1-20260101T0000-aaaaaa", why)

    def test_batch_needs_all_slots(self):
        """批量提交要一次算总账，不能逐个判。

        逐个判时前两个都看到"还有空槽"（它们还没写 RUNNING），
        第三个才发现满了 —— 而那时两台机已经在下单路上了。
        """
        ok, why = conc.check_slot(self.root, limit=2, extra=3)
        self.assertFalse(ok, "空槽 2 个，要 3 个，必须拒绝")
        ok2, _ = conc.check_slot(self.root, limit=3, extra=3)
        self.assertTrue(ok2)

    def test_check_provision_readable_when_platform_down(self):
        """平台读不到实例数时**放行**并说明 —— 这是最后兜底闸，不是主闸。

        网络抖动把所有作业堵死的代价，比偶尔多开一台机更大。
        """
        orig = conc.running_instances
        conc.running_instances = lambda: None
        try:
            ok, why = conc.check_provision(limit=2)
            self.assertTrue(ok)
            self.assertIn("读不到平台", why)
        finally:
            conc.running_instances = orig

    def test_check_provision_blocks_at_limit(self):
        """云端 running 数到上限就拒 —— 本地 job.json 会撒谎，这个不撒谎。"""
        orig = conc.running_instances
        conc.running_instances = lambda: 2
        try:
            ok, why = conc.check_provision(limit=2)
            self.assertFalse(ok)
            self.assertIn("云端已有 2 台", why)
        finally:
            conc.running_instances = orig


class TestCostAccounting(unittest.TestCase):
    """C：熔断器失明。today_cost() 只认 `cost` 字段，真实扣费在 `settle.amount`。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows: list[dict]) -> None:
        self.ledger.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                               encoding="utf-8")

    def test_sees_settle_amount(self):
        """这是本轮最要紧的一条：熔断器看不见真实扣费 = 永远不会熔断。

        2026-11-09 实测：账本今日 settle 合计 ¥0.86，老 today_cost() 返回 0.0。
        """
        self._write([
            {"at": "2026-11-09 18:30:01", "event": "balance", "balance": 8.38},
            {"at": "2026-11-09 18:40:02", "event": "settle", "amount": 0.23},
            {"at": "2026-11-09 19:00:00", "event": "settle", "amount": 0.63},
        ])
        self.assertAlmostEqual(spend.spend_on("2026-11-09", ledger=self.ledger)["settle"], 0.86)
        self.assertAlmostEqual(spend.today_cost(self.ledger) if False else
                               spend.spend_on("2026-11-09", ledger=self.ledger)["total"], 0.86)

    def test_ignores_non_spend_events(self):
        """balance / create / stop 这些事件不带钱，不能计入。"""
        self._write([
            {"at": "2026-11-09 10:00:00", "event": "balance", "balance": 8.38},
            {"at": "2026-11-09 10:00:01", "event": "create", "uhost": "u1"},
            {"at": "2026-11-09 10:00:02", "event": "settle", "amount": 0.5},
        ])
        d = spend.spend_on("2026-11-09", ledger=self.ledger)
        self.assertAlmostEqual(d["settle"], 0.5)
        self.assertEqual(d["settle_n"], 1)

    def test_job_cost_counted_when_no_settle(self):
        """作业刚结束、还没到结算点时，job 自报的 cost 是唯一的数。"""
        self._write([{"at": "2026-11-09 10:00:00", "event": "job", "cost": 0.75}])
        d = spend.spend_on("2026-11-09", ledger=self.ledger)
        self.assertAlmostEqual(d["job"], 0.75)
        self.assertAlmostEqual(d["total"], 0.75)

    def test_takes_max_not_sum(self):
        """settle 和 job 会重叠（settle 通常已包含那些 job 的花费）。

        相加会重复计数一倍，取 max 才不会高估。
        """
        self._write([
            {"at": "2026-11-09 10:00:00", "event": "job", "cost": 0.80},
            {"at": "2026-11-09 10:05:00", "event": "settle", "amount": 0.82},
        ])
        d = spend.spend_on("2026-11-09", ledger=self.ledger)
        self.assertAlmostEqual(d["total"], 0.82, msg="取 max 不是相加")
        self.assertNotAlmostEqual(d["total"], 1.62)

    def test_other_day_excluded(self):
        self._write([
            {"at": "2026-11-08 10:00:00", "event": "settle", "amount": 5.0},
            {"at": "2026-11-09 10:00:00", "event": "settle", "amount": 0.1},
        ])
        self.assertAlmostEqual(spend.spend_on("2026-11-09", ledger=self.ledger)["total"], 0.1)

    def test_missing_ledger_is_zero_not_crash(self):
        self.assertEqual(spend.today_cost(Path(self.tmp.name) / "nope.jsonl"), 0.0)

    def test_corrupt_lines_skipped(self):
        self.ledger.write_text('{"event":"settle","amount":0.2,"at":"2026-11-09 10:00:00"}\n'
                               'not json\n'
                               '{"event":"settle","amount":0.3,"at":"2026-11-09 11:00:00"}\n',
                               encoding="utf-8")
        self.assertAlmostEqual(spend.spend_on("2026-11-09", ledger=self.ledger)["total"], 0.5)

    def test_three_callers_agree(self):
        """scheduler / csctl / spend 三处口径必须一致。

        修之前是三份不同实现：scheduler 累加"有 cost 字段的任何行"，
        csctl 只认 event=="cost"（账本里根本没这个 event），riskmon 再抄一份。
        """
        from tools.p2 import scheduler
        import csctl
        self.assertAlmostEqual(scheduler.today_cost(), spend.today_cost(), places=4)
        self.assertAlmostEqual(csctl.ledger_today_cost(), spend.today_cost(), places=4)


class TestOrphanGuard(unittest.TestCase):
    """B：孤儿实例。run.py 以前没有任何信号处理。"""

    def setUp(self):
        self.src = RUN_PY.read_text(encoding="utf-8")

    def test_signal_handlers_registered(self):
        for s in ("SIGTERM", "SIGINT"):
            with self.subTest(sig=s):
                self.assertIn(s, self.src, f"缺 {s} 处理")

    def test_handler_prefers_stop_at_over_terminate(self):
        """优雅信号优先「定时关机」而不是「立刻销毁」。

        立刻销毁会把正在跑的推理和已产出但没取回的文件一起干掉。
        定时关机是可续跑的（--resume 还能接上）。

        判据：在信号处理器函数体内，stop_at 出现在 try 分支、terminate 出现在
        它的 except 分支。不能全文比 index —— terminate 在 step_release 里也有。
        """
        body = self.src.split("def _on_signal")[1].split("_OPTS = None")[0]
        self.assertIn("stop_at", body, "处理器里要调 stop_at")
        idx_try = body.find("stop_at")
        idx_exc = body.find("terminate")
        self.assertGreater(idx_exc, idx_try,
                           "terminate 只能出现在 stop_at 失败的降级分支里")

    def test_note_instance_called_on_both_provision_paths(self):
        """两条建机路径（预热池 / 新建）都要登记实例，否则兜底拿不到 uid。"""
        self.assertEqual(self.src.count("_note_instance(uid)"), 2,
                         "预热池分支和新建分支各要一次")

    def test_guard_installed_at_run_entry(self):
        self.assertIn("_install_orphan_guard(job_id, opts)", self.src)

    def test_keep_minutes_configurable(self):
        self.assertIn("P1_ORPHAN_KEEP_MINUTES", self.src)


class TestRuntimeBudget(unittest.TestCase):
    """E：运行中预算复查。max_cost 以前只在建机前查一次。"""

    def setUp(self):
        self.src = RUN_PY.read_text(encoding="utf-8")

    def test_wait_loop_is_cost_aware(self):
        """step_wait 要按 hourly × 已跑时间折算，钱先到就先停。"""
        body = self.src.split("def step_wait")[1].split("_COMFY_PROC_RE")[0]
        self.assertIn("cost_limit", body, "step_wait 里应有成本折算的上限")
        self.assertIn("hourly", body, "要用实际每小时价折算")

    def test_cost_timeout_message_distinct(self):
        """预算超时和时间超时要分开报，否则用户不知道该调哪个参数。"""
        self.assertIn("预算超时", self.src)
        self.assertIn("推理超时", self.src)

    def test_provision_gate_still_present(self):
        """建机前的 cap 闸门不能被运行中复查替代掉（两个都要有）。"""
        self.assertIn("est_real > cap", self.src)

    def test_concurrency_gate_in_provision(self):
        """建机时要查云端 running 实例数 —— 本地 job.json 会撒谎。"""
        body = self.src.split("def step_provision")[1].split("def run_setup")[0]
        self.assertIn("check_provision", body)


class TestApiConcurrencyGate(unittest.TestCase):
    """A（续）：API 层的并发闸门。"""

    def setUp(self):
        self.src = API_PY.read_text(encoding="utf-8")

    def test_create_job_checks_slot(self):
        self.assertIn("conc.check_slot", self.src)

    def test_run_jobs_computes_room_before_loop(self):
        """批量执行要先算总空槽，不能逐个判。"""
        body = self.src.split("def run_jobs")[1].split("def maybe_publish")[0]
        self.assertIn("room", body, "应先算 room 再逐个启动")
        self.assertIn("max_parallel", body)

    def test_full_degrades_to_created_not_reject(self):
        """满了要降级成"建作业但不执行"，不能 429 丢掉用户填好的参数。

        这比"作业躺在 CREATED 等人点执行"难受得多 —— 后者零风险、可随时启动。
        """
        self.assertIn("defer_reason", self.src)
        self.assertNotIn('ApiError(f"{why}（本次未提交', self.src)


class TestDispatchUnitFile(unittest.TestCase):
    """D：僵尸调度服务。原 unit 的 WorkingDirectory 指向已删除的 worktree。"""

    def test_unit_file_in_repo(self):
        f = _ROOT / "deploy" / "systemd" / "p2-dispatch.service"
        self.assertTrue(f.is_file(), "修好的 unit 要进仓库，否则下次重装又用错的")

    def test_unit_points_at_deploy_tree(self):
        """判据看 **WorkingDirectory 那一行**，不看全文 ——
        注释里提"原来的路径是 /root/wt-ui"是正常的（解释为什么改）。
        """
        f = (_ROOT / "deploy" / "systemd" / "p2-dispatch.service").read_text(encoding="utf-8")
        cwd_lines = [ln for ln in f.splitlines()
                     if ln.strip().startswith("WorkingDirectory=")]
        self.assertEqual(len(cwd_lines), 1, "应当恰好一行 WorkingDirectory")
        self.assertEqual(cwd_lines[0].strip(), "WorkingDirectory=/root/智云视频工作流")
        self.assertNotIn("wt-ui", cwd_lines[0])

    def test_unit_has_daily_budget(self):
        """要有当日预算熔断，否则调度器会一直领作业烧到余额见底。"""
        f = (_ROOT / "deploy" / "systemd" / "p2-dispatch.service").read_text(encoding="utf-8")
        self.assertIn("--daily-budget", f)


if __name__ == "__main__":
    unittest.main()
