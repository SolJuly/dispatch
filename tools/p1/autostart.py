#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作业自动补位：`auto_run` 作业在并发腾出来之后自动启动。

## 为什么不是常驻调度器

本仓库有两个「跑作业」的机制，要分清：

  · `tools/p2/scheduler.py`（`batchctl run`）—— 从 **queue.db** 领活。
    但 `jobqueue.enqueue()` 没有调用方，UI 提交的作业根本不进 queue.db，
    所以它现在领不到任何东西。开了只会「队列为空，退出」+ systemd
    `Restart=always` 每 15 秒重启一次。

  · `webui.api.run_jobs()` —— 把 CREATED 作业 spawn 成 `run.py` 子进程。
    这才是 UI 提交的实际路径。

本模块走第二条路，但**不用常驻进程**：在作业进入终态时扫一遍，
把排在最前、且还有空槽的 `auto_run` 作业拉起来。事件驱动，零空转。

## auto_run 是什么

提交时勾了「立即执行」但因**并发已满**被降级成"只建作业不执行"的作业，
会带上 `auto_run: true` —— 记住"用户是想让它跑的"这个意图。
没勾「立即执行」的作业不带这个字段，它们就该老老实实等人点。

区别很重要：
  · 勾了执行 + 并发满  → auto_run=true  → 前面的跑完自动接着跑
  · 根本没勾执行        → 无此字段       → 一直等着，直到有人点「执行」

## 为什么在终态时触发，而不是定时轮询

定时轮询要常驻一个进程、要么写 cron（最小粒度 1 分钟，最长白等 60 秒）。
而"有空槽了"这件事**只可能发生在某个作业结束之后** —— 那是唯一让出槽位的事件。
挂在终态上，延迟是零。

例外：进程被 kill -9 时不会走终态，也就不会触发补位。那种情况由
`jobs_reconcile.py`（cron 每 15 分钟）收尾后触发；再漏的，用户点一下「执行」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p1 import state as jobstate  # noqa: E402

JOBS_DIR = _ROOT / "reports" / "p1" / "jobs"


def _say(msg: str) -> None:
    import time
    print(f"[{time.strftime('%H:%M:%S')}] [autostart] {msg}", flush=True)


def pending_jobs(jobs_dir: Path | None = None) -> list[dict]:
    """所有「想跑但还没跑」的作业，按创建时间升序（先来先跑）。

    只认 `state == CREATED` 且 `auto_run` 为真。
    """
    root = Path(jobs_dir) if jobs_dir else JOBS_DIR
    out = []
    for p in root.glob("*/job.json"):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if j.get("state") != jobstate.CREATED:
            continue
        if not j.get("auto_run"):
            continue
        out.append(j)
    out.sort(key=lambda j: str(j.get("created_at") or ""))
    return out


def start_pending(jobs_dir: Path | None = None, *, limit: int | None = None,
                  spawn=None, say=_say) -> list[str]:
    """把排队的 auto_run 作业拉起来，直到没空槽。

    返回真正启动的 job_id 列表。

    **并发判据用 `slots_used`（模型分组数），不是作业条数** —— 与提交时的
    `check_slot` 同一口径。同一模型的多个作业会挤在一台机器上串行，只占 1 槽；
    按条数判会把它们错当成"占满 2 槽"而拒绝启动。
    """
    import subprocess
    import sys as _sys
    from tools.p2 import concurrency as conc

    root = Path(jobs_dir) if jobs_dir else JOBS_DIR
    spawn = spawn or subprocess.Popen
    run_py = _ROOT / "tools" / "p1" / "run.py"

    try:
        cap = conc.max_parallel()
        used = conc.slots_used(root)
    except Exception as e:                  # noqa: BLE001
        say(f"⚠ 读不到并发状态，跳过补位：{type(e).__name__}: {e}")
        return []

    room = cap - used
    if room <= 0:
        return []

    started: list[str] = []
    for j in pending_jobs(root):
        if len(started) >= room or (limit is not None and len(started) >= limit):
            break
        jid = str(j.get("job_id") or "")
        if not jid:
            continue
        log = root / jid / "run.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log.open("ab") as fh:
                import datetime as _dt
                fh.write(f"\n--- 自动补位启动 {_dt.datetime.now().astimezone():%Y-%m-%dT%H:%M:%S} ---\n".encode())
                spawn([_sys.executable, str(run_py), jid, "--no-keep", "--batch",
                       "--wait-lock", "14400", "--wait-stock", "300"],
                      stdout=fh, stderr=subprocess.STDOUT,
                      start_new_session=True, cwd=str(_ROOT))
        except Exception as e:              # noqa: BLE001
            say(f"⚠ {jid} 启动失败：{type(e).__name__}: {e}")
            continue
        started.append(jid)
        say(f"▶ 自动启动 {jid}（并发 {used + len(started)}/{cap} 组）")
        # 启动后立刻多占一个槽；下一轮循环的判据要算上它
        if len(started) >= room:
            break
    return started


def after_terminal(job_id: str, jobs_dir: Path | None = None, *, say=_say) -> list[str]:
    """作业进入终态后调用：让出槽位，看看有没有人等着的活。

    幂等且失败安全 —— 补位失败绝不能影响调用方（它刚跑完一单，状态已经写好了）。
    """
    try:
        return start_pending(jobs_dir, say=say)
    except Exception as e:                  # noqa: BLE001
        say(f"⚠ 补位出错（不影响本作业）：{type(e).__name__}: {e}")
        return []


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--dry-run" in argv:
        room_pending = pending_jobs()
        from tools.p2 import concurrency as conc
        print(f"待跑 auto_run 作业：{len(room_pending)}")
        for j in room_pending:
            print(f"  {j['job_id']}  {j.get('created_at')}")
        print(f"并发：{conc.slots_used()}/{conc.max_parallel()}")
        return 0
    started = start_pending()
    if not started:
        print("没有需要补位的作业")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
