#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 批量调度器：从 queue.db 领作业 → 起 `run.py --batch` 子进程 → 收尾/重排/回收。

## ⚠ 本模块当前处于「不可用」状态（2026-11-12）

**队列已禁用**（`tools/p2/jobqueue.py` 的 `ENABLED = False`），所以本调度器
启动后会立刻报 `QueueDisabled` 退出。

### 为什么禁

队列没有生产者：`jobqueue.enqueue()` 有完整实现，但全库零调用方 ——
UI 提交的作业直接写 `reports/p1/jobs/`，从不进 queue.db。
于是 queue.db 从 2026-11-01 起就没再写入过。

它唯一还起作用的地方是**让界面数字互相矛盾**：
「已完成花费 ¥7.35」来自 queue.db 的 8 条历史残留，
「失败 26 / 完成 20」来自 job.json 的 47 条 —— 真实花费是 ¥3.86。

### 自动补位已经由别的模块承担

`tools/p1/autostart.py` 做同样的事，但**事件驱动**而不是常驻轮询：
作业进入终态时扫一遍，把排队的 `auto_run` 作业拉起来。
延迟是 0 秒（实测：前一条 DONE 的同一秒启动了后一条），且不需要任何常驻进程。

### 要恢复需要三步

1. 接上生产者：在 `webui/api.py` 的 `create_job()` 里，`jobctl new` 之后
   调 `queue.submit(...)`
2. 把 `jobqueue.ENABLED` 改成 `True`（或设环境变量 `P2_QUEUE_ENABLED=1`）
3. **先修这个 unit 的坑**：`deploy/systemd/p2-dispatch.service` 配的是
   `Restart=always` + `RestartSec=15`，而本调度器"队列空就退出" ——
   两者叠加会变成每 15 秒重启一次的死循环。改成 `on-failure` 或让它空闲不退出。

设计（见 P2 计划 §5.5）：
  - 一实例一 job：每个 worker = 一次 run.py 子进程（subprocess 隔离，最稳）
  - 槽位 = min(--slots, 账户剩余配额)，不硬上
  - 预算熔断：当日账本 + 在跑燃烧 超 cap 就停止领取
  - 失败分流：FAILED_PROVISION/实例失联 → 重排；FAILED_VERIFY → 不重排
  - 僵尸回收：租约过期复位 + 超 TTL 的 p2-* 实例 terminate
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import store, state  # noqa: E402
from tools.p2 import jobqueue as queue  # noqa: E402

LEDGER = _ROOT / ".compshare" / "ledger.jsonl"
RETRYABLE = {"FAILED_PROVISION", "FAILED_RUN", "FAILED_FETCH"}
# run.py 的终态。worker 被 kill 时 job 会停在这些之外（READY/RUNNING/…），那属于「崩了」，
# 必须当可重试处理——否则心跳丢了的任务会被直接判死，永远不重试。
TERMINAL_JOB_STATES = {"DONE", "FAILED", "ABORTED", "FAILED_PROVISION", "FAILED_RUN",
                       "FAILED_FETCH", "FAILED_VERIFY"}
WORKER = f"{socket.gethostname()[-10:]}-{os.getpid()}"
# 空闲多久算可清理。**统一由 reaper 定义**（默认 10 分钟，P2_IDLE_REAP_MIN 可调）。
# 以前这里是硬编码 45，和 reaper 的默认值会漂移成两个数。
TTL_MIN = None          # 延迟到调用时取 reaper.idle_minutes()
SCAN_S = 30          # P0-2：调度器每 30s 扫一次
TIMEOUT_S = 60       # P0-2：>60s 无心跳判定死锁


def _say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [sched] {msg}", flush=True)


def _metrics(job: dict, man: dict) -> tuple[float | None, float | None]:
    """GPU 分钟 + 成本：成功看 manifest，失败从 instance.up_at 推算（审计要 100% 有值）。"""
    m = man.get("cost") or {}
    if m.get("minutes") is not None:
        return float(m["minutes"]), float(m.get("amount") or 0)
    inst = job.get("instance") or {}
    up_at = inst.get("up_at")
    hourly = ((job.get("steps") or {}).get("provision") or {}).get("hourly")
    minutes = None
    if up_at:
        try:
            from datetime import datetime
            up = datetime.fromisoformat(up_at)
            now = datetime.now().astimezone()
            minutes = round(max(0.0, (now - up).total_seconds() / 60), 1)
        except Exception:
            minutes = None
    cost = round(float(hourly) * minutes / 60, 4) if (hourly and minutes is not None) else None
    return minutes, cost


def _fail_reason(jid: str, final_state: str) -> str:
    """从事件流里找失败原因（FAILED_PROVISION / FAILED_RUN / FAILED_FETCH …）。"""
    try:
        evs = state.read_events(jid)
    except Exception:
        return final_state
    seen = [e.get("to") for e in evs if e.get("to")]
    for s_ in reversed(seen):
        if s_ in RETRYABLE:
            return s_
    return final_state


def today_cost() -> float:
    """今天花了多少（熔断器用）。口径统一在 tools/p2/spend.py。

    修之前这里只累加 `r.get("cost")`，而平台的真实扣费记在 `settle` 事件的
    `amount` 字段里 —— 于是熔断器看到 ¥0.00，永远不熔断。2026-11-09 实测：
    账本今日 settle 合计 ¥0.86，这个函数返回 0.0。
    """
    from tools.p2 import spend
    return spend.today_cost()


def live_instances() -> list[dict]:
    """平台侧 p2-* 实例（用于对账与回收）。无凭证/断网时返回空。"""
    try:
        from compshare.cs import CS
        cs = CS()
        return [i for i in cs.instances() if str(i.get("Name") or "").startswith(("p2-", "p1-"))]
    except Exception as e:
        _say(f"⚠ 读实例失败：{type(e).__name__}: {e}")
        return []


def terminate(uid: str) -> None:
    try:
        from compshare.cs import CS
        CS().terminate(uid)
        _say(f"已销毁超期实例 {uid}")
    except Exception as e:
        _say(f"⚠ 销毁 {uid} 失败：{e}")


def reap_instances(ttl_min: int | None = None, *, dry_run: bool = False) -> list[str]:
    """清理空闲实例。**已改判据**（2026-11-09）。

    修之前这个方法有三个问题（实测）：

      ① 注释说"且没有活跃队列项"，但 `active` 变量算完从不使用 ——
         函数体里它只出现 1 次，就是赋值那行。`name` 同样算了不用。
      ② 判据退化成「CreateTime 超过 ttl_min 就 terminate」：
         跑 50 分钟的长作业会被腰斩，而没作业的机器可能因为别的原因留着。
      ③ 只看 `queue.db`，而 queue.db 里 instance_id 实测全是 None
         → 它看不见任何真实作业。

    现在委托给 `tools/p2/reaper.py`：判据换成「作业状态 + 空闲时长」，
    数据源三合一（job.json / queue.db / 云端状态），默认 `stop` 而非 `terminate`
    （关机后算力费归零、保住磁盘环境、下次 start 只要 ~12s）。

    保留这个函数名是因为 `batchctl doctor --reap` 和测试都在调它。
    """
    from tools.p2 import reaper
    done = reaper.reap(idle_min=ttl_min or reaper.idle_minutes(), dry_run=dry_run,
                       say=lambda m: _say(m) if not dry_run else print(m))
    return [s.uid for s in done]


class Scheduler:
    def __init__(self, slots: int = 2, gpu: str = "4090", hours: float = 1.0,
                 daily_budget: float = 8.0, poll: float = SCAN_S, stagger: float = 10.0,
                 dry_run: bool = False, quota: int = 5, from_pool: bool = False):
        self.slots = max(1, slots)
        self.quota = quota          # 账户实例上限（Level1=5 / Level2=20，可 --quota 覆盖）
        self.stagger = stagger
        self.dry_run = dry_run
        self.from_pool = from_pool
        self.gpu = gpu
        self.hours = hours
        self.daily_budget = daily_budget
        self.poll = poll
        self.children: dict[str, subprocess.Popen] = {}
        self.stop = False

    # ---------------------------------------------------------------- 子进程
    def _log_path(self, jid: str) -> Path:
        d = store.job_dir(jid)
        d.mkdir(parents=True, exist_ok=True)
        return d / "run.log"

    def _spawn(self, item: dict) -> None:
        """起一个 `run.py --batch` 子进程跑这条作业。

        **槽位模型（2026-11-09 改）**：`self.slots` 现在限制的是
        「同时有几个**模型分组**在跑」，不是「同时有几个作业」。

        作业之间的串行复用由 `run.py` 自己的 `_try_borrow_slot()` 决定 ——
        同一 model_key 的第二条作业会自动挂到第一台机上排队，不再建新机。
        所以调度器这一层不用管"复用"，只需要控制**分组数**别超上限。

        为什么不在调度器里持有实例：`run.py` 是独立进程，它才知道
        「这台机器上现在能不能接活」（要问 slot.busy_jobs）。
        调度器盲管一层只会让状态分裂成两份。
        """
        jid = item["job_id"]
        log = open(self._log_path(jid), "ab")
        # --no-keep：批量无人值守，失败不得保留实例（否则 15 分钟空转烧钱）
        cmd = [sys.executable, str(_ROOT / "tools" / "p1" / "run.py"), jid,
               "--batch", "--no-keep", "--restart", "--gpu", self.gpu, "--hours", str(self.hours)]
        if self.from_pool:
            cmd.append("--from-pool")
        if getattr(self, "no_borrow", False):
            cmd.append("--no-borrow")
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(_ROOT))
        self.children[jid] = p
        queue.mark_running(jid)
        _say(f"▶ {jid}（pid={p.pid}，模型 {item.get('model_key') or '?'}，"
             f"在跑 {len(self.children)} 条 / 分组上限 {self.slots}）")

    def _reap_children(self) -> None:
        for jid, p in list(self.children.items()):
            rc = p.poll()
            if rc is None:
                queue.heartbeat(jid)
                continue
            self.children.pop(jid, None)
            if jid in queue.cancelled_ids():
                _say(f"⛔ {jid} 已取消（worker 已终止），不再重排")
                continue
            j, man = {}, {}
            try:
                j = state.load_job(jid)
                st = j.get("state")
                man = store.read_json(store.job_file(jid, "manifest.json")) or {}
            except Exception:
                st = "?"
            inst = (j.get("instance") or {}).get("uhost_id")
            gpu_min, cost = _metrics(j, man)
            cost = float(cost or 0)
            if rc == 0 and st == state.DONE:
                queue.finish(jid, True, cost, instance_id=inst, exit_code=rc, gpu_minutes=gpu_min)
                _say(f"✓ {jid} done  ¥{cost:.3f}  gpu={gpu_min}m")
            else:
                # run.py 失败后会走 FAILED_* → RELEASING → FAILED，终态看不出原因，
                # 所以要翻 events 找有没有「可重试的失败态」（G1 清理后 state 恒为 FAILED）。
                reason = _fail_reason(jid, st)
                crashed = st not in TERMINAL_JOB_STATES
                if crashed:
                    reason = f"worker died (state={st})"
                if reason == "FAILED_PROVISION" and queue.defer(jid, 60, "provision failed（无货/配额？）"):
                    _say(f"⏳ {jid} 建机失败（疑似无货），60s 后再试（不消耗尝试）")
                elif crashed or reason in RETRYABLE:
                    if queue.requeue(jid, f"{reason} rc={rc}", exit_code=rc,
                                     gpu_minutes=gpu_min, cost=cost):
                        _say(f"↻ {jid} 重排（{reason}）")
                    else:
                        # requeue 已把它落成 terminal_failed，不能再 finish 覆盖审计
                        _say(f"✗ {jid} 终态失败（{reason}，重试已到上限）")
                else:
                    queue.finish(jid, False, cost, f"{reason or st} rc={rc}",
                                 instance_id=inst, exit_code=rc, gpu_minutes=gpu_min)
                    _say(f"✗ {jid} failed（{reason or st}）")

    # ---------------------------------------------------------------- 资源
    def _budget_ok(self, batch_id: str | None = None) -> tuple[bool, str]:
        spent = today_cost()
        if self.daily_budget and spent >= self.daily_budget:
            return False, f"当日已花 ¥{spent:.2f} ≥ cap ¥{self.daily_budget:.2f}"
        if batch_id:
            info = queue.batch_info(batch_id) or {}
            cap = float(info.get("budget_cny") or 0)
            bspent = queue.batch_spent(batch_id)
            if cap and bspent >= cap:
                return False, f"批次 {batch_id} 已花 ¥{bspent:.2f} ≥ 批预算 ¥{cap:.2f}（该批暂停）"
            return True, f"当日 ¥{spent:.2f} / ¥{self.daily_budget:.2f} · 批 ¥{bspent:.2f} / ¥{cap:.2f}"
        return True, f"当日 ¥{spent:.2f} / ¥{self.daily_budget:.2f}"

    def _quota_left(self) -> int | None:
        try:
            from compshare.cs import CS
            insts = CS().instances()
            running = sum(1 for i in insts if (i.get("State") or "").lower() == "running")
            return max(0, self.quota - running)
        except Exception:
            return None

    # ---------------------------------------------------------------- 主循环
    def once(self) -> int:
        """跑一轮：回收 + 心跳扫描 + 取消清理 + 领取 + 补槽。dry-run 只报计划不动手。"""
        if self.dry_run:
            self._report_plan()
            return 0
        self._reap_children()
        self._kill_cancelled_children()
        timed_out = queue.reset_stale(TIMEOUT_S)
        if timed_out:
            _say(f"⏱ 心跳超时：{timed_out}")
        self._reap_instances()
        ok, msg = self._budget_ok()
        if not ok:
            _say(f"⏸ 熔断：{msg}")
            return 0
        left = self._quota_left()
        if left is not None and left <= 0:
            _say("⏸ 账户配额已满（无空闲槽）")
            return 0
        claimed, skip = 0, set()
        # ---- 分组并发（规则 C）----
        # `self.slots` 限制的是「同时有几个**模型分组**在跑」，不是几个作业。
        # 同一条队列里可能有 5 份 qwen_image 的活 —— 它们该排在一台机器上
        # 串行，所以只占 1 个槽。
        #
        # 这里不主动"把同组作业并发起来"：`run.py` 的 `_try_borrow_slot()`
        # 会让第二条挂到第一台上排队（槽位表里 busy_jobs 非空就另建/等）。
        # 调度器只要保证「不同分组数」不超上限即可。
        while len(self._active_groups()) < self.slots:
            item = queue.claim(WORKER)
            if not item:
                break
            bid = item.get("batch_id")
            if bid in skip:
                queue.release(item["job_id"], "batch budget skip")
                break
            ok2, msg2 = self._budget_ok(bid)
            if not ok2:
                skip.add(bid)
                queue.release(item["job_id"], f"budget: {msg2}")
                _say(f"⏸ 批次 {bid} 暂停：{msg2}")
                continue
            self._spawn(item)
            claimed += 1
            if len(self._active_groups()) < self.slots and self.stagger:
                time.sleep(self.stagger)   # 错峰下单，避免 226604 雪崩
        return claimed

    def _active_groups(self) -> set[str]:
        """当前正在跑的模型分组集合（从 queued/claimed/running 的 item 里拿）。"""
        groups = set()
        try:
            for it in queue.items():
                if it.get("state") in ("claimed", "running"):
                    groups.add(str(it.get("model_key") or it.get("job_id") or ""))
        except Exception:                   # noqa: BLE001
            return {str(j) for j in self.children}
        return groups

    def _kill_cancelled_children(self) -> None:
        if not self.children:
            return
        cancelled = queue.cancelled_ids()
        for jid, p in list(self.children.items()):
            if jid in cancelled:
                try:
                    p.kill()
                except Exception:
                    pass
                # 把 job 也置 ABORTED（否则 job.json 会冻结在 RUNNING，和队列对不上）
                try:
                    cur = (state.load_job(jid) or {}).get("state")
                    if cur not in state.TERMINAL:
                        state.transition(jid, state.ABORTED, reason="cancel by user")
                except Exception:
                    pass
                _say(f"⛔ {jid} 已取消，终止 worker")

    def _report_plan(self) -> None:
        ok, msg = self._budget_ok()
        left = self._quota_left()
        _say(f"[dry-run] 预算 {msg} · 配额剩余 {left} · 槽位 {self.slots}（不领取、不建机）")
        qs = queue.items(state="queued")
        if not qs:
            _say("  [dry-run] 队列为空，无事可做")
            return
        for it in qs[:20]:
            ok2, msg2 = self._budget_ok(it.get("batch_id"))
            _say(f"  将跑 {it['job_id']}（{it.get('model_key') or '-'}）"
                 f"{'✓' if ok2 else '✗ ' + msg2}")
        if len(qs) > 20:
            _say(f"  …另有 {len(qs) - 20} 项")

    def _reap_instances(self) -> None:
        """P1-5：心跳超时/失败但仍占用实例的项 → 杀进程 + 释放（watchdog 的进程内版本）。"""
        try:
            reclaim_orphans(self.dry_run)
        except Exception as e:
            _say(f"⚠ watchdog 异常：{type(e).__name__}: {e}")

    def run(self) -> int:
        _say(f"启动：slots={self.slots} gpu={self.gpu} hours={self.hours} "
             f"daily_cap=¥{self.daily_budget} quota={self.quota} poll={self.poll}s"
             f"{' [dry-run]' if self.dry_run else ''}")
        if self.dry_run:
            self.once()
            return 0
        while not self.stop:
            n = self.once()
            if not self.children and n == 0:
                st = queue.stats()["by_state"]
                if not st.get("queued"):
                    _say("队列已空，退出")
                    break
            time.sleep(self.poll)
        for jid, p in list(self.children.items()):
            _say(f"等待在跑的 {jid} 结束…")
            p.wait()
            self._reap_children()
        return 0


def doctor() -> dict:
    """对账：队列 vs 平台实例 vs 账本。"""
    stale = queue.reset_stale()
    insts = live_instances()
    active_items = {i["job_id"] for i in queue.items() if i["state"] in ("claimed", "running")}
    orphan_inst = [i.get("UHostId") for i in insts]
    return {"reset_stale": stale, "active_items": sorted(active_items),
            "live_instances": orphan_inst, "today_cost": today_cost(),
            "stats": queue.stats()}


# ---------------------------------------------------------------- watchdog（P1-5）
def _instance_state(uid: str) -> str | None:
    try:
        from compshare.cs import CS
        return (CS().get(uid).get("State") or "").lower()
    except Exception:
        return None


def _instance_alive(uid: str) -> bool:
    return _instance_state(uid) in ("running", "stopped", "stopping", "starting")


def _ssh_kill(uid: str) -> None:
    """SSH 到实例杀僵尸 Python/ComfyUI（方括号技巧防误杀执行进程）。"""
    try:
        from compshare.cs import CS
        cs = CS()
        inst = cs.get(uid)
        ip, port, pwd = cs.public_ip(inst), "23", cs.password_of(inst)
        if not ip or not pwd:
            return
        cmd = ("pkill -f '[m]ain.py --listen' 2>/dev/null; "
               "pkill -f '[c]omfyui' 2>/dev/null; "
               "sleep 2; pgrep -fa '[m]ain.py --listen' | head -3; echo killed")
        subprocess.run(["sshpass", "-p", pwd, "ssh", "-p", str(port),
                        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=8", f"root@{ip}", cmd],
                       capture_output=True, text=True, timeout=15)
    except Exception as e:
        _say(f"⚠ SSH kill {uid} 失败：{type(e).__name__}: {e}")


def reclaim_orphans(dry_run: bool = False) -> list[str]:
    """扫描仍有实例引用、但已无活跃 worker 的项：杀僵尸进程 → 释放实例 → 落 RECLAIMED。"""
    done = []
    for it in queue.items():
        uid = it.get("instance_id")
        if not uid or it["state"] == "running":
            continue
        jid = it["job_id"]
        if not _instance_alive(uid):
            queue.clear_instance(jid)
            continue
        if dry_run:
            _say(f"[dry-run] 回收孤儿 {jid} / {uid}（{it['state']}）")
            done.append(f"{jid}:{uid}")
            continue
        # 只在实例真的 running 时才值得 SSH（stopping/deleting 只会白等超时）
        if _instance_state(uid) == "running":
            _ssh_kill(uid)
        terminate(uid)
        if it["state"] in ("failed", "terminal_failed"):
            queue.reclaim(jid, f"watchdog reclaimed orphan {uid}")
            queue.clear_instance(jid)
            _say(f"🧹 {jid} 孤儿实例 {uid} 已回收 → RECLAIMED")
        else:
            queue.clear_instance(jid)
            _say(f"🧹 {jid} 孤儿实例 {uid} 已释放（保留 {it['state']}）")
        done.append(f"{jid}:{uid}")
    return done
