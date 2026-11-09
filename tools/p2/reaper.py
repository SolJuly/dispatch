#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例清理：把「空闲超过阈值」的实例停机，把「该复用的」留住。

## 为什么要有这个文件（先把现状说清楚）

仓库里**已经有三套清理相关的东西**，但没有一套是完整的：

    ① csctl.py watch --once        cron 每 5 分钟，GPU≤5% 且 gpuprocs==0
                                   连续 15 分钟 → `stop`（关机）
                                   实测真跑过 3 次（.compshare/watch.log 可查）
    ② batchctl.py watchdog         cron 每 5 分钟 → reclaim_orphans()
                                   遍历 queue.db 的 instance_id
    ③ batchctl doctor --reap       调 reap_instances()，但**不在 cron 里**

各自的问题：

| 谁 | 问题 | 后果 |
|---|---|---|
| ① watch | 判据是 **GPU 利用率**，不是"有没有作业" | 作业刚提交、正在装模型（GPU 还是 0%）时会被误判空闲，15 分钟后被 stop 中断 |
| ① watch | 只 `stop` 不 `terminate` | 实例占配额（账户 5 台上限），频繁建机撞配额报 8903 |
| ② watchdog | 只看 `queue.db`，而 queue.db 里 `instance_id` 全是 None | 实测 queue.db 8 条 / job.json 5 条 / **交集 0 条** —— 它看不见任何真实作业 |
| ③ doctor --reap | 不在 cron 里 | 等于永远没人敲 |
| ③ reap_instances | 注释说"且没有活跃队列项"，实际 `active` 变量算完不用 | 判据退化成"创建超 45 分钟就杀"，跑 50 分钟的长作业会被腰斩 |

所以这个文件的定位是：**用"作业状态"做判据，替掉"GPU 利用率"这个代理指标。**

## 判据怎么定

不看 GPU。GPU 利用率是个**后果**，不是**意图** —— 一台机器 GPU 闲着，可能是
在装模型、在等下一条作业、或者真的没人用。前两种不该关。

看**作业状态**，三源合一，任一说"在忙"就不动：

    ① job.json          reports/p1/jobs/*/job.json 的 state 和 instance.uhost_id
                        这是真实作业的唯一可靠来源（实测 queue.db 是空的）
    ② queue.db          jobqueue.items() 里 claimed/running 且 instance_id 匹配
                        队列接通后用得上，现在为空也没关系
    ③ 云平台状态        实例本身是 Running 还是 Stopped/Stopping

**为什么不看 watch.log 的 idle_since**：那是 GPU 时长，正是要替掉的错误判据。
但 `watch_state.json` 里的时间戳可以当**兜底参考** —— 见 `_idle_since()`。

## 空闲时长从什么时候算

    idle_since = max(
        该实例上最后一个作业的「结束时间」,     ← 有作业时
        实例的 StartTime                        ← 没作业时（开机就在空转）
    )

不能用 `CreateTime`：一台实例可以活 8 小时跑 20 个作业，
按创建时间判会在第 45 分钟就把它杀掉（这正是 `reap_instances` 的 bug）。

## 和规则 B（同工作流复用）的关系

清理必须给排队让路：

    队列里还有属于这个实例分组（同 model_key）的待跑作业 → 不关

否则"作业 A 跑完 → 作业 B 还没被领取"的那个瞬间会把机器关掉，
B 起来要重新建机 + 装模型（SDXL 21s / Qwen 53s），比多空转一会儿贵得多。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JOBS_DIR = ROOT / "reports" / "p1" / "jobs"
WATCH_STATE = ROOT / ".compshare" / "watch_state.json"

# 与 tools/p1/state.py 的 TERMINAL 一致。不 import 它：state 模块导入时会建目录，
# 而清理脚本可能在只读环境下跑。
TERMINAL_STATES = frozenset({"DONE", "FAILED", "ABORTED"})

# 空闲多久算"可以关"。你说 10 分钟，我按 10 定，环境变量可覆盖。
#
# 为什么不是更短：单条作业的建机+装载约 3 分钟（建机 ~2min + 装模型 21~53s）。
# 空转 10 分钟 = 10 × ¥0.036 = ¥0.36；重建一次 ≈ ¥0.19 但要重新等 3 分钟。
# 10 分钟是"宁可多空转一会儿，也别打断刚热好的机器"的保守取值。
#
# 注意别和 P2_INSTANCE_OFFLINE_S（15 分钟，采样离线判定）混淆 —— 那是
# "多久没采样就认为实例没了"，和"空闲多久该关"是两件事。
DEFAULT_IDLE_MIN = 10


def idle_minutes() -> int:
    """空闲阈值（分钟）。非法值回落默认，不返回 0 —— 0 会让所有实例立刻被关。"""
    raw = os.environ.get("P2_IDLE_REAP_MIN")
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    return DEFAULT_IDLE_MIN


@dataclass
class InstanceState:
    """一台实例的判定所需信息。"""

    uid: str
    name: str = ""
    cloud_state: str = ""            # Running / Stopped / Stopping / ...
    start_time: float = 0.0          # 云端 StartTime（epoch 秒）
    jobs: list[str] = field(default_factory=list)        # 该实例上的作业 id
    busy: bool = False               # 是否有非终态作业
    queued_same_group: int = 0       # 队列里同分组待跑作业数
    idle_since: float = 0.0
    reason: str = ""                 # 判定理由（给人看）

    @property
    def idle_min(self) -> float:
        return round((time.time() - self.idle_since) / 60, 1) if self.idle_since else 0.0


def _load_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------- 数据源
def jobs_by_instance(jobs_dir: Path | None = None) -> dict[str, list[dict]]:
    """{uhost_id: [job, ...]}，来自 job.json。

    这是**真实作业的唯一可靠来源**：实测 queue.db 里 instance_id 全是 None，
    而 job.json 里 uhost_id 100% 有值（5/5）。
    """
    root = Path(jobs_dir) if jobs_dir else JOBS_DIR
    if not root.is_dir():
        return {}
    out: dict[str, list[dict]] = {}
    for d in root.iterdir():
        if not d.is_dir():
            continue
        j = _load_json(d / "job.json")
        if not j:
            continue
        uid = ((j.get("instance") or {}) or {}).get("uhost_id")
        if uid:
            out.setdefault(str(uid), []).append(j)
    return out


def queue_by_instance() -> dict[str, list[dict]]:
    """{instance_id: [item, ...]}，来自 queue.db（队列接通后才有内容）。"""
    try:
        from tools.p2 import jobqueue as queue
        rows = queue.items()
    except Exception:                    # noqa: BLE001  无 queue.db 属于正常情况
        return {}
    out: dict[str, list[dict]] = {}
    for it in rows:
        uid = it.get("instance_id")
        if uid:
            out.setdefault(str(uid), []).append(it)
    return out


def queued_groups() -> dict[str, int]:
    """{model_key: 待跑作业数}。用于「队列还有同组作业就别关机器」（规则 B 让路）。"""
    try:
        from tools.p2 import jobqueue as queue
        rows = queue.items(state="queued")
    except Exception:                    # noqa: BLE001
        return {}
    out: dict[str, int] = {}
    for it in rows:
        mk = it.get("model_key") or ""
        out[mk] = out.get(mk, 0) + 1
    return out


def _job_end_time(job: dict, jobs_dir: Path | None = None) -> float:
    """作业的结束时间（从 events 或 job.json 推断），拿不到返回 0。

    `jobs_dir` 必须能覆盖：`assess(jobs_dir=...)` 传了临时目录时，
    这里还用模块级 `JOBS_DIR` 就会读错地方 —— 实测会静默返回 0，
    于是 idle 退化成"开机时长"，正是要修的那个判据。
    """
    jid = job.get("job_id")
    if not jid:
        return 0.0
    root = Path(jobs_dir) if jobs_dir else JOBS_DIR
    ev = root / str(jid) / "events.jsonl"
    if ev.exists():
        last = 0.0
        try:
            for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = e.get("ts")
                if ts:
                    last = max(last, float(ts))
        except OSError:
            pass
        if last:
            return last
    # 退回 job.json 里的 steps 时间戳
    steps = job.get("steps") or {}
    ts = []
    for v in steps.values():
        at = (v or {}).get("at")
        if at:
            try:
                import datetime as dt
                ts.append(dt.datetime.fromisoformat(at).timestamp())
            except (ValueError, TypeError):
                pass
    return max(ts) if ts else 0.0


def _live_instances() -> list[dict]:
    """云平台上的实例列表。读不到返回空表（不抛 —— 清理是兜底，不该自己成为故障源）。"""
    try:
        from compshare.cs import CS
        return CS().instances()
    except Exception:                      # noqa: BLE001
        return []


# ---------------------------------------------------------------- 判定
def assess(instances: list[dict] | None = None, *,
           jobs_dir: Path | None = None) -> list[InstanceState]:
    """给每台实例算出「该不该关」和理由。纯计算，不动手。

    `instances` 缺省时去云平台取；测试可以注入假列表。
    """
    insts = instances if instances is not None else _live_instances()
    by_job = jobs_by_instance(jobs_dir)
    by_queue = queue_by_instance()
    qgroups = queued_groups()

    out: list[InstanceState] = []
    for i in insts:
        uid = str(i.get("UHostId") or "")
        if not uid:
            continue
        st = InstanceState(
            uid=uid,
            name=str(i.get("Name") or ""),
            cloud_state=str(i.get("State") or ""),
            start_time=float(i.get("StartTime") or 0),
        )

        # ① 作业（job.json）
        mine = by_job.get(uid, [])
        st.jobs = [str(j.get("job_id")) for j in mine if j.get("job_id")]
        running = [j for j in mine if (j.get("state") or "") not in TERMINAL_STATES]
        if running:
            st.busy = True
            st.reason = f"有 {len(running)} 个作业未结束（{running[0].get('job_id')}）"

        # ② 队列（queue.db）—— 队列接通后才会有内容
        qmine = by_queue.get(uid, [])
        qactive = [it for it in qmine if it.get("state") in ("claimed", "running")]
        if qactive:
            st.busy = True
            st.reason = st.reason or f"队列有 {len(qactive)} 条在跑（{qactive[0].get('job_id')}）"

        # ③ 空闲起点：最后一个作业的结束时间，没有就用开机时间
        ends = [_job_end_time(j, jobs_dir) for j in mine]
        last_end = max(ends) if ends else 0.0
        st.idle_since = max(last_end, st.start_time)

        # ④ 规则 B 让路：队列里还有同分组待跑作业，别关
        model_key = ""
        for j in mine[:1]:
            model_key = ((j.get("model") or {}).get("key") or "")
        if model_key and qgroups.get(model_key):
            st.queued_same_group = qgroups[model_key]
            st.busy = True
            st.reason = st.reason or f"队列还有 {qgroups[model_key]} 条同模型作业待跑（规则 B 让路）"

        # ⑤ 云端已不在 Running 的不用管（watch 已经 stop 过）
        if st.cloud_state and st.cloud_state.lower() != "running":
            st.busy = True
            st.reason = st.reason or f"云端状态 {st.cloud_state}，无需处理"

        out.append(st)
    return out


def reapable(instances: list[dict] | None = None, *, idle_min: int | None = None,
             jobs_dir: Path | None = None) -> list[InstanceState]:
    """筛出「空闲超阈值且没作业」的实例。"""
    limit = idle_min if idle_min is not None else idle_minutes()
    return [s for s in assess(instances, jobs_dir=jobs_dir)
            if not s.busy and s.idle_min >= limit]


def reap(instances: list[dict] | None = None, *, idle_min: int | None = None,
         jobs_dir: Path | None = None, dry_run: bool = False,
         action: str = "stop", say=print) -> list[InstanceState]:
    """执行清理。默认 stop（关机）而不是 terminate（销毁）。

    为什么默认 stop：
      · 关机后算力费归零，系统盘 ≤100G 免费 → 成本约 0
      · 保住磁盘上的 ComfyUI + 权重软链，下次 start 只要 ~12s
      · terminate 不可逆，误杀要重装环境（20+ 分钟）

    `action="terminate"` 只用于「确认不要了」的场景（比如实例被标记 dead）。
    """
    targets = reapable(instances, idle_min=idle_min, jobs_dir=jobs_dir)
    if dry_run:
        for s in targets:
            say(f"[dry-run] 可清理 {s.uid} {s.name}（空闲 {s.idle_min} 分钟）")
        return targets

    if not targets:
        return []
    try:
        from compshare.cs import CS
        cs = CS()
    except Exception as e:                 # noqa: BLE001
        say(f"⚠ 取不到云凭证，跳过清理：{type(e).__name__}: {e}")
        return []

    done = []
    for s in targets:
        try:
            if action == "terminate":
                cs.terminate(s.uid)
                say(f"🗑 已销毁 {s.uid} {s.name}（空闲 {s.idle_min} 分钟）")
            else:
                cs.stop(s.uid)
                say(f"💤 已关机 {s.uid} {s.name}（空闲 {s.idle_min} 分钟）")
            done.append(s)
        except Exception as e:             # noqa: BLE001
            say(f"⚠ {s.uid} 清理失败：{type(e).__name__}: {e}")
    return done
