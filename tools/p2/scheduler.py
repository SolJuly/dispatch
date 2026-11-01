#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 批量调度器：从 queue.db 领作业 → 起 `run.py --batch` 子进程 → 收尾/重排/回收。

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
TTL_MIN = 45
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
    d = time.strftime("%Y-%m-%d")
    total = 0.0
    try:
        with open(LEDGER, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(r.get("at", "")).startswith(d) and r.get("cost"):
                    total += float(r["cost"])
    except FileNotFoundError:
        pass
    return round(total, 3)


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


def reap_instances(ttl_min: int = TTL_MIN) -> list[str]:
    """超 TTL 且没有活跃队列项的 p2-* 实例 → terminate。"""
    active = {i["job_id"] for i in queue.items() if i["state"] in ("claimed", "running")}
    killed = []
    now = time.time()
    for inst in live_instances():
        name = str(inst.get("Name") or "")
        uid = inst.get("UHostId")
        created = inst.get("CreateTime") or 0
        try:
            age_min = (now - float(created)) / 60
        except (TypeError, ValueError):
            age_min = 0
        if age_min >= ttl_min and uid:
            terminate(uid)
            killed.append(uid)
    return killed


class Scheduler:
    def __init__(self, slots: int = 2, gpu: str = "4090", hours: float = 1.0,
                 daily_budget: float = 8.0, poll: float = SCAN_S, stagger: float = 10.0,
                 dry_run: bool = False, quota: int = 5):
        self.slots = max(1, slots)
        self.quota = quota          # 账户实例上限（Level1=5 / Level2=20，可 --quota 覆盖）
        self.stagger = stagger
        self.dry_run = dry_run
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
        jid = item["job_id"]
        log = open(self._log_path(jid), "ab")
        # --no-keep：批量无人值守，失败不得保留实例（否则 15 分钟空转烧钱）
        cmd = [sys.executable, str(_ROOT / "tools" / "p1" / "run.py"), jid,
               "--batch", "--no-keep", "--restart", "--gpu", self.gpu, "--hours", str(self.hours)]
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(_ROOT))
        self.children[jid] = p
        queue.mark_running(jid)
        _say(f"▶ {jid}（pid={p.pid}，槽位 {len(self.children)}/{self.slots}）")

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
                    if queue.requeue(jid, f"{reason} rc={rc}", exit_code=rc):
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
        while len(self.children) < self.slots:
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
            if len(self.children) < self.slots and self.stagger:
                time.sleep(self.stagger)   # 错峰下单，避免 226604 雪崩
        return claimed

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
