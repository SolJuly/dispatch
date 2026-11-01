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
WORKER = f"{socket.gethostname()[-10:]}-{os.getpid()}"
TTL_MIN = 45


def _say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [sched] {msg}", flush=True)


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
                 daily_budget: float = 8.0, poll: float = 10.0, stagger: float = 10.0):
        self.slots = max(1, slots)
        self.stagger = stagger
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
               "--batch", "--no-keep", "--gpu", self.gpu, "--hours", str(self.hours)]
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
            try:
                j = state.load_job(jid)
                st = j.get("state")
                man = store.read_json(store.job_file(jid, "manifest.json")) or {}
                cost = float((man.get("cost") or {}).get("amount") or 0)
            except Exception:
                st, cost, man = "?", 0.0, {}
            if rc == 0 and st == state.DONE:
                queue.finish(jid, True, cost)
                _say(f"✓ {jid} done  ¥{cost:.3f}")
            else:
                # run.py 失败后会走 FAILED_* → RELEASING → FAILED，终态看不出原因，
                # 所以要翻 events 找有没有「可重试的失败态」（G1 清理后 state 恒为 FAILED）。
                reason = _fail_reason(jid, st)
                if reason in RETRYABLE and queue.requeue(jid, f"{reason} rc={rc}"):
                    _say(f"↻ {jid} 重排（{reason}）")
                else:
                    queue.finish(jid, False, cost, f"{reason or st} rc={rc}")
                    _say(f"✗ {jid} failed（{reason or st}）")

    # ---------------------------------------------------------------- 资源
    def _budget_ok(self) -> tuple[bool, str]:
        spent = today_cost()
        if self.daily_budget and spent >= self.daily_budget:
            return False, f"当日已花 ¥{spent:.2f} ≥ cap ¥{self.daily_budget:.2f}"
        return True, f"当日 ¥{spent:.2f} / ¥{self.daily_budget:.2f}"

    def _quota_left(self) -> int | None:
        try:
            from compshare.cs import CS
            cs = CS()
            insts = cs.instances()
            running = sum(1 for i in insts if (i.get("State") or "").lower() == "running")
            # Level1 上限 5 台；保守用 5 作分母
            return max(0, 5 - running)
        except Exception:
            return None

    # ---------------------------------------------------------------- 主循环
    def once(self) -> int:
        """跑一轮：回收 + 领取 + 补槽。返回本轮领取数。"""
        self._reap_children()
        queue.reset_stale()
        ok, msg = self._budget_ok()
        if not ok:
            _say(f"⏸ 熔断：{msg}")
            return 0
        left = self._quota_left()
        if left is not None and left <= 0:
            _say("⏸ 账户配额已满（无空闲槽）")
            return 0
        claimed = 0
        while len(self.children) < self.slots:
            item = queue.claim(WORKER)
            if not item:
                break
            self._spawn(item)
            claimed += 1
            if len(self.children) < self.slots and self.stagger:
                time.sleep(self.stagger)   # 错峰下单，避免 226604 雪崩
        return claimed

    def run(self) -> int:
        _say(f"启动：slots={self.slots} gpu={self.gpu} hours={self.hours} "
             f"daily_cap=¥{self.daily_budget} poll={self.poll}s")
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
