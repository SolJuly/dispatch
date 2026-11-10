#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例生命周期执行器：保活 / 停机 / 销毁，三态。

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

## 三态（2026-11-10 定版）

以前只有「关 / 不关」两态。现在按生命周期分三态：

    keep       保活   —— 有作业在跑、在空转窗口内、停机保留期内、失败后待续跑
    stop       停机   —— 空闲超过窗口 → 关机（算力费 ¥0，磁盘环境还在，唤醒 62s）
    terminate  销毁   —— 停机超过保留期 → 销毁（腾配额）

空闲窗口不是固定 10 分钟了，**按作业的 task_class 取**（见 `lifecycle.py`）：

    短任务（默认）  0 秒    跑完立即进停机判定
    长任务（UI 勾）  60/90/120 秒，默认 90

为什么短任务恒 0：短任务本身只花 ¥0.085–0.139，给 120 秒空转等于任务成本的
53%~84%，那不是"多一点"，是翻倍。为什么长任务也只有三档：每一档都有明确经济
含义（60s 打平 / 90s 亏 ¥0.017 / 120s 亏 ¥0.035），开放任意值会让人填出
"300 秒"这种占长任务成本 42% 的数而不自知。

## 空闲时长从什么时候算

    idle_since = max(
        该实例上最后一个作业的「结束时间」,     ← 有作业时
        实例的 StartTime                        ← 没作业时（开机就在空转）
    )

不能用 `CreateTime`：一台实例可以活 8 小时跑 20 个作业，
按创建时间判会在第 45 分钟就把它杀掉（这正是 `reap_instances` 的 bug）。

**空转窗口从「最后一个作业结束」算，不是从"这一分钟"算。** 短任务窗口是 0，
所以第一次巡检（cron 每 5 分钟）就会把它停掉 —— 不需要"立刻"，因为停机是免费的，
而多烧 5 分钟是 ¥0.18。

## 停机计时器为什么要自己落盘

「停机超过 2 小时就销毁」需要"什么时候停的"。云端 Describe 不返回停机时间戳
（只有 StartTime，且停机后仍停在最后一次开机时间），账本里 `stop` 事件只有 3 条
（只有 csctl stop 会写，而大量停机走的是别的路径）。所以用
`reports/p2/stopped_at.json` 自己记，三个写入点：run.py 释放、reaper 关机、pool 归还。

**计时从「最后一次停机」算**：一台实例跑完 A 停机、被唤醒跑 B 又停机，
保留期必须从 B 停机那刻重新算 —— 否则 B 跑完立刻就被销毁。

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

# 失败分支：这些状态下的作业**还能续跑**（run.py --resume）。
# 实例必须多留一会儿，否则「保留实例等续跑」的设计被清理器悄悄推翻。
FAIL_BRANCH_STATES = frozenset({
    "FAILED_PROVISION", "FAILED_RUN", "FAILED_FETCH", "FAILED_VERIFY",
})

DEFAULT_IDLE_MIN = 10

# 基准实验室的机器（`lab-*`）由 lab_runner 自己管，生命周期不归我们判。
# lab_runner 虽已退役（见 tools/arch.py 的架构图标注），但 csctl watch 一直保留
# 这个跳过逻辑，riskmon 也还在按这个前缀分类。撤掉 watch 接手清理职责时，
# 必须把同样的豁免搬过来 —— 否则行为倒退：原来不动的机器现在会被停机。
SKIP_NAME_PREFIXES = ("lab-",)


def idle_minutes() -> int:
    """兜底空闲阈值（分钟）。**只在拿不到作业生命周期信息时用。**

    正常路径走 `lifecycle.long_idle_s()`（按 task_class 取 0/60/90/120 秒）。
    这个值是给"这台机器上没有我们知道的作业"那种情况兜底的：一台开着的机器
    既没作业、也没停机记录，只能按"开了多久"判 —— 那就用这个数。

    非法值回落默认，不返回 0 —— 0 会让所有实例立刻被关。
    """
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
    """一台实例的判定所需信息 + 判定结果。"""

    uid: str
    name: str = ""
    cloud_state: str = ""            # Running / Stopped / Stopping / ...
    start_time: float = 0.0          # 云端 StartTime（epoch 秒）
    jobs: list[str] = field(default_factory=list)        # 该实例上的作业 id
    busy: bool = False               # 是否有非终态作业
    queued_same_group: int = 0       # 队列里同分组待跑作业数
    idle_since: float = 0.0
    reason: str = ""                 # 判定理由（给人看）

    # ---- 三态判定的产物
    action: str = "keep"             # keep / stop / terminate
    action_reason: str = ""
    idle_window_s: int = 0           # 这台实例被允许的空转秒数（取最后一条作业的）
    idle_elapsed_s: float = 0.0      # 已经空转了多少秒
    idle_remaining_s: float = 0.0    # 窗口还剩多少秒（负数=已超）
    task_class: str = ""             # 最后一条作业的类型（short/long）
    stopped_since: float = 0.0       # 停机起点（0=不知道）
    stopped_remaining_min: float | None = None   # 停机保留期还剩几分钟（None=不可判）

    @property
    def idle_min(self) -> float:
        return round((time.time() - self.idle_since) / 60, 1) if self.idle_since else 0.0

    @property
    def stopped_label(self) -> str:
        if self.stopped_remaining_min is None:
            return "停机时长未知（无记录）"
        if self.stopped_remaining_min <= 0:
            return "停机保留期已过"
        h, m = divmod(int(self.stopped_remaining_min), 60)
        return f"停机保留期还剩 {h} 小时 {m} 分" if h else f"停机保留期还剩 {m} 分"


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
           jobs_dir: Path | None = None, now: float | None = None) -> list[InstanceState]:
    """给每台实例算出「保活 / 停机 / 销毁」和理由。纯计算，不动手。

    `instances` 缺省时去云平台取；测试可以注入假列表。
    """
    from tools.p2 import lifecycle

    now = now if now is not None else time.time()
    insts = instances if instances is not None else _live_instances()
    by_job = jobs_by_instance(jobs_dir)
    by_queue = queue_by_instance()
    qgroups = queued_groups()

    out: list[InstanceState] = []
    for i in insts:
        uid = str(i.get("UHostId") or "")
        if not uid:
            continue
        name = str(i.get("Name") or "")
        if any(name.startswith(pfx) for pfx in SKIP_NAME_PREFIXES):
            # 不由我们管的机器：直接跳过，连 InstanceState 都不产出。
            # 产出的话会在 reaper_cli 的表格里显示成"保活"，容易让人以为
            # 系统"看过并决定留着"，实际是"根本不归它管"。
            continue

        st = InstanceState(
            uid=uid,
            name=name,
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
        ends = [(_job_end_time(j, jobs_dir), j) for j in mine]
        ends = [(t, j) for t, j in ends if t]
        last_end = max((t for t, _ in ends), default=0.0)
        st.idle_since = max(last_end, st.start_time)

        # ④ 空转窗口：取「最后一条作业」的 task_class / idle_after_done_s
        #    没有作业的机器（开着但没用）走 idle_minutes() 兜底。
        if ends:
            last_job = max(ends, key=lambda x: x[0])[1]
            st.task_class = str(last_job.get("task_class") or "")
            st.idle_window_s = lifecycle.idle_for_job(last_job)
        else:
            st.task_class = ""
            st.idle_window_s = idle_minutes() * 60
        st.idle_elapsed_s = max(0.0, now - st.idle_since) if st.idle_since else 0.0
        if st.idle_since:
            st.idle_remaining_s = round(st.idle_window_s - st.idle_elapsed_s, 1)

        # ⑤ 规则 B 让路：队列里还有同分组待跑作业，别关
        model_key = ""
        for j in mine[:1]:
            model_key = ((j.get("model") or {}).get("key") or "")
        if model_key and qgroups.get(model_key):
            st.queued_same_group = qgroups[model_key]
            st.busy = True
            st.reason = st.reason or f"队列还有 {qgroups[model_key]} 条同模型作业待跑（规则 B 让路）"

        # ⑥ 失败分支：还有作业能续跑，别动（续跑窗口 = FAILED_KEEP_MIN）
        failing = [j for j in mine if (j.get("state") or "") in FAIL_BRANCH_STATES]
        if failing:
            st.busy = True
            # 覆盖掉第 ① 步那句"有 N 个作业未结束"—— FAILED_* 不是"在跑"，
            # 是"停了但能续跑"。两种含义混在一句话里会让人误判该不该等。
            st.reason = (f"作业 {failing[0].get('job_id')} 在 "
                         f"{failing[0].get('state')}，可续跑，保留")

        # ⑦ 停机计时器
        st.stopped_since = lifecycle.stopped_since(uid)
        # **陈旧记录保护**：停机起点必须晚于（或等于）这台机器最后一次开机时刻。
        # 否则说明"停过 → 被唤醒 → 又跑过 → 又停了"，而本地/账本记的还是老时间，
        # 按老时间算会得出"已过期"，一停机就被销毁 —— 误销毁不可逆。
        # 这种情况宁可当"时长未知"保留（停机 ¥0），等 reaper 下一次自己记上正确起点。
        if st.stopped_since and st.start_time and st.stopped_since < st.start_time - 60:
            st.stopped_since = 0.0
            st.stopped_remaining_min = None
        else:
            st.stopped_remaining_min = lifecycle.stopped_remaining_min(uid, now=now)

        _decide(st, now=now, lifecycle=lifecycle)
        out.append(st)
    return out


def _decide(st: InstanceState, *, now: float, lifecycle) -> None:
    """三态判定。顺序即优先级，每一条都说清理由。

    这个函数**只读 st 的字段、只写 action/action_reason** —— 不动手、不联网，
    所以能被单测穷举。
    """
    cloud = (st.cloud_state or "").lower()

    # ① 有活儿 → 保活。这是在跑、在排队、可续跑三种情况的合集，
    #    理由已由 assess 写进 st.reason。
    if st.busy:
        st.action = "keep"
        st.action_reason = st.reason or "有作业未结束"
        return

    # ② 云端不是 Running：要么已经停机，要么在过渡态（Starting/Stopping）。
    if cloud and cloud != "running":
        if cloud in ("stopping", "starting", "rebooting", "restarting"):
            st.action = "keep"
            st.action_reason = f"云端状态 {st.cloud_state}，过渡中不处理"
            return
        # 已停机：看保留期
        if st.stopped_remaining_min is None:
            # 没有停机记录 —— 不知道停了多久。**不销毁**：误销毁不可逆，
            # 而多留一台停机实例的成本是 ¥0（实测 11.5 小时 14 次采样全 +0.00）。
            st.action = "keep"
            st.action_reason = f"已停机但无停机记录，保留（停机不收费，销毁不可逆）"
            return
        if st.stopped_remaining_min <= 0:
            st.action = "terminate"
            st.action_reason = (f"停机已超保留期（{lifecycle.stopped_ttl_min() // 60} 小时），"
                                f"销毁回收配额")
            return
        st.action = "keep"
        st.action_reason = st.stopped_label
        return

    # ③ 云端 Running 且没活儿：看在不在空转窗口内
    if not st.idle_since:
        # 连开机时间都没有 —— 读不到云端时间。不猜，保留。
        st.action = "keep"
        st.action_reason = "拿不到空闲起点（云端未返回 StartTime），不处理"
        return

    if st.idle_remaining_s > 0:
        st.action = "keep"
        kind = "长任务" if st.task_class == "long" else "短任务"
        st.action_reason = (f"{kind}空转窗口内，剩 {int(st.idle_remaining_s)}s"
                            f"（窗口 {st.idle_window_s}s）")
        return

    st.action = "stop"
    if st.idle_window_s <= 0:
        st.action_reason = "短任务跑完即回收（空转 0 秒），停机后 ¥0"
    else:
        st.action_reason = (f"空转已 {int(st.idle_elapsed_s)}s，"
                            f"超过 {st.idle_window_s}s 窗口，停机后 ¥0")


def reapable(instances: list[dict] | None = None, *, idle_min: int | None = None,
             jobs_dir: Path | None = None, now: float | None = None) -> list[InstanceState]:
    """筛出「该停机」的实例（保持 terminate 单独一个函数，别混）。

    `idle_min` 传了就用它**覆盖**作业级窗口 —— 只给手动排障/演练用，
    正常清理不传（否则会推翻三档设计）。
    """
    rows = assess(instances, jobs_dir=jobs_dir, now=now)
    if idle_min is None:
        return [s for s in rows if s.action == "stop"]
    # 覆盖模式（手动排障/演练）：窗口统一按 `idle_min` 算，**不看 task_class**。
    # 覆盖时必须把"已经判 stop 的"也重新过一遍阈值 —— 否则短任务（窗口 0 秒，
    # 1 分钟就判 stop）会绕过显式给的阈值，`--idle-min 10` 等于没用。
    floor = idle_min * 60
    picked = []
    for s in rows:
        if s.busy or s.action in ("keep",) and (s.cloud_state or "").lower() != "running":
            continue
        if s.action == "terminate":
            continue                       # 该销毁的不受 idle_min 影响
        if not s.idle_since or s.idle_elapsed_s < floor:
            continue
        s.action = "stop"
        s.action_reason = (f"空闲 {s.idle_elapsed_s / 60:.1f} 分钟 ≥ "
                           f"阈值 {idle_min} 分钟（--idle-min 覆盖）")
        picked.append(s)
    return picked


def terminable(instances: list[dict] | None = None, *,
               jobs_dir: Path | None = None, now: float | None = None) -> list[InstanceState]:
    """筛出「该销毁」的实例（停机超保留期）。"""
    return [s for s in assess(instances, jobs_dir=jobs_dir, now=now)
            if s.action == "terminate"]


def reap(instances: list[dict] | None = None, *, idle_min: int | None = None,
         jobs_dir: Path | None = None, dry_run: bool = False,
         action: str = "auto", say=print, now: float | None = None) -> list[InstanceState]:
    """执行清理。

    `action` 语义（2026-11-10 起）：
      · `auto`（默认） 按三态判定：stop 的停机、terminate 的销毁
      · `stop`         只停机，不销毁（忽略 terminate 判定）
      · `terminate`    把「该停机」的也一并销毁（确认不要了才用）

    为什么默认是 `stop` 而不是 `terminate`：
      · 关机后算力费归零，系统盘 ≤100G 免费 → 成本约 0
      · 保住磁盘上的 ComfyUI + 权重软链，下次 start 只要 ~62s
      · terminate 不可逆，误杀要重装环境（20+ 分钟）

    但「停机超 2 小时」那一档必须真的销毁，否则配额（5 台）会被停机实例吃满。
    所以默认是 `auto` 而不是纯 `stop`。
    """
    if action == "terminate":
        targets = [s for s in assess(instances, jobs_dir=jobs_dir, now=now)
                   if s.action in ("stop", "terminate")]
    elif action == "stop":
        targets = reapable(instances, idle_min=idle_min, jobs_dir=jobs_dir, now=now)
    else:
        targets = [s for s in assess(instances, jobs_dir=jobs_dir, now=now)
                   if s.action in ("stop", "terminate")]
        if idle_min is not None:
            targets = reapable(instances, idle_min=idle_min, jobs_dir=jobs_dir, now=now)

    from tools.p2 import lifecycle

    # **认领无记录的停机实例**：第一次看到"已停机但拿不到停机时间"时，
    # 从这一刻开始算保留期。不认领就是永久保留 —— 账户只有 5 台配额，
    # 几台无记录的僵尸停机实例就能把配额吃满（新任务报 8903）。
    # 实测就有一台停了 21 小时、账本里的 stop 又是陈旧记录（早于本次开机）的。
    #
    # 为什么安全：起点 = 第一次被看到停机的时刻，**晚于**真实停机时刻，
    # 所以保留期只会更长、不会更短，不存在"刚认领就销毁"的误杀。
    # 代价是这类实例最多多留 stopped_ttl + 一个巡检周期（5 分钟）。
    #
    # 放在 dry_run 之前、targets 空判断之前：认领本身不花钱（只写本地 json），
    # 而且没有它，下一轮巡检永远算不出"已过期"，targets 永远是空。
    for s in assess(instances, jobs_dir=jobs_dir, now=now):
        if s.action == "keep" and (s.cloud_state or "").lower() not in ("running", "") \
                and s.stopped_remaining_min is None:
            lifecycle.record_stopped(s.uid, now)
            say(f"📝 {s.uid} 无停机记录，从现在起算保留期"
                f"（{lifecycle.stopped_ttl_min() // 60} 小时后销毁）")

    if dry_run:
        for s in targets:
            say(f"[dry-run] {s.action:9s} {s.uid} {s.name}（{s.action_reason}）")
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
            if s.action == "terminate":
                cs.terminate(s.uid)
                lifecycle.clear_stopped(s.uid)
                say(f"🗑 已销毁 {s.uid} {s.name}（{s.action_reason}）")
            else:
                cs.stop(s.uid)
                # 停机计时从这一刻算（覆盖旧值 —— 见 lifecycle.record_stopped 的注释）
                lifecycle.record_stopped(s.uid)
                say(f"💤 已停机 {s.uid} {s.name}（{s.action_reason}）")
            done.append(s)
        except Exception as e:             # noqa: BLE001
            say(f"⚠ {s.uid} 清理失败：{type(e).__name__}: {e}")
    return done
