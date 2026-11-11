#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 实例视图：把散落的本地状态合成一份只读快照，供 UI 实例页使用。

数据源（全部本地文件 / 表，不调云 API，零费用、可离线）：

  .compshare/watch.log         cron 每 5min 采样：gpu%/cpu%/gpuprocs/idle/run 小时/累计花费
  .compshare/watch_state.json  每实例 idle_since / samples（watch --once 维护）
  .compshare/ledger.jsonl      账本：含 uhost 的事件（成本兜底来源）
  logs/watchdog.log            watchdog 每次运行的回收结果（"job_id:uhost_id" 字符串）
  reports/p2/queue.db          item.instance_id（实例 ↔ 作业的实时占用）+ audit_log（回收记录）

设计要点：
  - **不发明数据**：拿不到的字段一律不出现（GPU 型号/区域/单价不在本地，UI 显示「—」并标明来源）。
  - **窗口**：默认只看最近 6 小时活跃过的实例；更早的采样视为历史，不进列表（避免 300 条僵尸）。
  - **状态**：running（队列里挂着活跃作业）> reclaimed（窗口内有回收记录）> idle（窗口内有采样）。
  - 解析失败不抛异常，降级返回空表并带上 notes，UI 会显示"来源不可用"而不是白屏。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import jobqueue as queue

ROOT = Path(os.environ.get("P2_ROOT") or Path(__file__).resolve().parents[2])
COMP = ROOT / ".compshare"
WATCH_LOG = COMP / "watch.log"
WATCH_STATE = COMP / "watch_state.json"
LEDGER = COMP / "ledger.jsonl"
WATCHDOG_LOG = ROOT / "logs" / "watchdog.log"

WINDOW_S = int(os.environ.get("P2_INSTANCE_WINDOW_S") or 6 * 3600)
OFFLINE_S = int(os.environ.get("P2_INSTANCE_OFFLINE_S") or 900)   # 15 分钟没采样 = 离线
# 超过这个时长没有新采样，就在 notes 里明确告警。
# 采样是 5 分钟一次，20 分钟 = 连续漏了 4 轮，已经不能当"抖动"看。
STALE_MIN = int(os.environ.get("P2_INSTANCE_STALE_MIN") or 20)
LIMIT = 60

_TS_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]$")
_UHOST_RE = re.compile(r"uhost-[0-9a-z]+")


# ---------------------------------------------------------------- watch.log
def parse_watch_log(path: Path, mtime: float | None = None) -> dict[str, dict]:
    """解析采样日志，返回 {instance_id: 该实例的最新样本}。

    每行形如：
      [11:30:08] uhost-1x64s9glcmsv p2-bake-base gpu=0% cpu=11% gpuprocs=0 idle=0m run=0.02h 花=¥0.05
    行内只有 HH:MM:SS，没有日期 —— 以「最后一行 ≈ 文件 mtime」为锚点，从后往前倒推日界，
    这样跨零点的历史采样不会被算成"今天 18:30"（未来时间）。
    """
    if not path.exists():
        return {}
    mtime = mtime if mtime is not None else path.stat().st_mtime
    lt = time.localtime(mtime)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    rows: list[tuple[int, str, dict]] = []      # (当日秒数, instance_id, fields)
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("["):
            continue
        tok = line.split()
        if len(tok) < 3 or not tok[1].startswith("uhost-"):
            continue
        m = _TS_RE.match(tok[0])
        if not m:
            continue
        hh, mm, ss = (int(x) for x in m.groups())
        kv_at = next((i for i, t in enumerate(tok) if "=" in t), len(tok))
        fields = {}
        for t in tok[kv_at:]:
            if "=" in t:
                k, _, v = t.partition("=")
                fields[k] = v
        rows.append((hh * 3600 + mm * 60 + ss, tok[1], {
            "name": " ".join(tok[2:kv_at]),
            "gpu_util": _pct(fields.get("gpu")),
            "cpu_util": _pct(fields.get("cpu")),
            "gpuprocs": _int(fields.get("gpuprocs")),
            "idle_s": (_int(fields.get("idle")) or 0) * 60,
            "runtime_s": _hours_s(fields.get("run")),
            "cost_cny": _float(fields.get("花")),
        }))

    if not rows:
        return {}

    # 锚定最后一行：允许 5 分钟误差；仍"超前"就往前退天，直到落在 mtime 之前
    day = midnight
    last_sec = rows[-1][0]
    while day + last_sec > mtime + 300:
        day -= 86400

    out: dict[str, dict] = {}
    cur_day, cur_sec = day, last_sec
    for sec, uid, fields in reversed(rows):
        if sec > cur_sec:                        # 时间倒退 → 跨了零点
            cur_day -= 86400
        cur_sec = sec
        ts = cur_day + sec
        prev = out.get(uid)
        if prev:
            prev["samples"] += 1
            continue                             # 从后往前，先到的就是最新样本
        out[uid] = {"instance_id": uid, "at": ts, "samples": 1, **fields}
    return out


def _pct(v):
    if v is None:
        return None
    v = v.strip().rstrip("%")
    try:
        return int(float(v))
    except ValueError:
        return None


def _int(v):
    if v is None:
        return None
    try:
        return int(float(str(v).strip().rstrip("mh")))
    except ValueError:
        return None


def _float(v):
    if v is None:
        return None
    try:
        return round(float(str(v).strip().lstrip("¥")), 4)
    except ValueError:
        return None


def _hours_s(v):
    if v is None:
        return None
    try:
        return int(round(float(str(v).strip().rstrip("h")) * 3600))
    except ValueError:
        return None


# ---------------------------------------------------------------- watch_state.json
def parse_watch_state(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- watchdog.log
def parse_watchdog_log(path: Path) -> list[dict]:
    """watchdog.log 是「多行 pretty JSON 对象首尾相接」，用 raw_decode 逐段解析。"""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    dec = json.JSONDecoder()
    i, out = 0, []
    n = len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, j = dec.raw_decode(text, i)
        except ValueError:
            nl = text.find("\n", i)
            if nl == -1:
                break
            i = nl + 1
            continue
        i = j
        if not isinstance(obj, dict):
            continue
        for jid_uid in obj.get("reclaimed") or []:
            out.append(_wd_row(jid_uid, "watchdog 回收孤儿实例"))
        for jid_uid in obj.get("heartbeat_timeout") or []:
            out.append(_wd_row(jid_uid, "心跳超时，重排/终态"))
    return out


def _wd_row(s, reason: str) -> dict:
    if not isinstance(s, str):
        s = str(s)
    jid, _, uid = s.partition(":")
    return {"at": None, "job_id": jid, "instance_id": uid if uid.startswith("uhost-") else None,
            "reason": reason, "detail": s, "action": "reclaim", "source": "watchdog.log"}


# ---------------------------------------------------------------- ledger
def ledger_cost_by_instance(path: Path, limit_lines: int = 20000) -> dict[str, float]:
    """账本兜底成本：按 uhost 累加带 cost 的事件（watch.log 没有花= 时用）。"""
    if not path.exists():
        return {}
    acc: dict[str, float] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit_lines:]
    except OSError:
        return {}
    for ln in lines:
        if "uhost" not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        uid = d.get("uhost")
        if not uid:
            continue
        for k in ("cost", "cost_actual"):
            v = d.get(k)
            if isinstance(v, (int, float)):
                acc[uid] = round(acc.get(uid, 0.0) + float(v), 4)
    return acc


# ---------------------------------------------------------------- 审计里的回收记录
def _audit_rows(limit: int = 200) -> list[dict]:
    """队列审计里的回收记录。**队列禁用时返回空**。

    注意 `connect()` 必须在 try **里面**。原来的写法是：
        c = queue.connect()      ← 在 try 外
        try: ...
    队列禁用后 `connect()` 直接抛 `QueueDisabled`，而它不在 try 保护范围内，
    整条 `snapshot()` 就炸了 —— 实例页直接 500。
    这是加开关时漏掉的一处"直接调 connect"，不是 `queue.items()` 那种
    有 `is_enabled()` 保护的路径。
    """
    c = None
    try:
        c = queue.connect()
        rows = c.execute(
            "SELECT id, ts, job_id, instance_id, from_state, to_state, exit_code, detail "
            "FROM audit_log WHERE to_state='reclaimed' "
            "   OR detail LIKE '%watchdog%' OR detail LIKE '%timeout%' OR detail LIKE '%reclaim%' "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:                      # noqa: BLE001  队列禁用/库损坏都算"没有记录"
        return []
    finally:
        if c is not None:
            c.close()


def _reclaim_reason(row: dict) -> str:
    detail = (row.get("detail") or "").strip()
    if detail:
        return detail
    if row.get("to_state") == "reclaimed":
        return "watchdog 回收"
    return row.get("to_state") or "—"


# ---------------------------------------------------------------- 快照
def snapshot(window_s: int | None = None, now: float | None = None) -> dict:
    win = WINDOW_S if window_s is None else int(window_s)
    now = time.time() if now is None else now
    notes: list[str] = []

    samples = parse_watch_log(WATCH_LOG)
    if not samples:
        notes.append("watch.log 不可用或为空：GPU/CPU 采样缺失，实例页只能显示队列占用")
    else:
        # **采样停更必须说出来**。踩过：b1d70e7 撤掉 csctl watch 后 watch.log
        # 停更 52 小时，而这里只在"完全为空"时才提示 —— 文件里还有 11-09 的老数据，
        # 于是 notes 是空的，页面看起来一切正常，实际一个样本都没在更新。
        # 用户报"watchdog 不实时了"，代码这边一句告警都没有。
        newest = max((s.get("at") or 0) for s in samples.values())
        age_min = (now - newest) / 60 if newest else None
        if age_min is not None and age_min > STALE_MIN:
            notes.append(
                f"采样已停更 {int(age_min)} 分钟（最后一条 {_iso(newest)}）："
                f"cron 里的 tools/p2/sampler.py 可能没在跑")
    state = parse_watch_state(WATCH_STATE)
    ledger = ledger_cost_by_instance(LEDGER)
    wd_log = parse_watchdog_log(WATCHDOG_LOG)
    audit = _audit_rows()

    # 队列占用：instance_id ↔ 活跃作业
    busy: dict[str, dict] = {}
    for it in queue.items():
        uid = it.get("instance_id")
        if not uid:
            continue
        st = it.get("state")
        if st in queue.ACTIVE:
            busy[uid] = {"job_id": it["job_id"], "state": st,
                         "since": it.get("started_at") or it.get("enqueued_at"),
                         "model": it.get("model_key")}

    # 回收记录（审计优先，watchdog.log 兜底补时间未知的）
    watchdog: list[dict] = []
    seen_pairs: set[tuple] = set()
    for r in audit:
        uid = r.get("instance_id")
        if not uid:
            m = _UHOST_RE.search(r.get("detail") or "")
            uid = m.group(0) if m else None
        watchdog.append({
            "at": _iso(r.get("ts")),
            "job_id": r.get("job_id"),
            "instance_id": uid,
            "reason": _reclaim_reason(r),
            "action": "reclaim" if r.get("to_state") == "reclaimed" else "requeue",
            "source": "audit_log",
        })
        seen_pairs.add((r.get("job_id"), uid))
    for w in wd_log:
        if (w["job_id"], w["instance_id"]) in seen_pairs:
            continue
        watchdog.append(w)

    # 实例集合：队列占用 ∪ 窗口内采样 ∪ 窗口内回收记录
    ids: dict[str, dict] = {}
    for uid in busy:
        ids[uid] = {"instance_id": uid, "source": "queue"}
    for uid, s in samples.items():
        if now - s["at"] <= win:
            ids.setdefault(uid, {"instance_id": uid, "source": "watch.log"})
    reclaimed_ids: dict[str, float] = {}
    for w in watchdog:
        ts = _parse_iso(w.get("at"))
        uid = w.get("instance_id")
        if uid and ts and now - ts <= win:
            reclaimed_ids[uid] = max(reclaimed_ids.get(uid, 0), ts)
            ids.setdefault(uid, {"instance_id": uid, "source": "audit_log"})

    out = []
    for uid, meta in ids.items():
        s = samples.get(uid) or {}
        act = busy.get(uid)
        if act:
            st = "running"
        elif uid in reclaimed_ids:
            st = "reclaimed"
        elif s:
            # 采样很旧 = 机器大概率已经销毁。不能显示"空闲"，否则看板会骗人
            # （这正是"队列在排队、实例页却有空闲机器"那次误判的根因）。
            st = "idle" if (now - s["at"]) <= OFFLINE_S else "offline"
        else:
            st = "unknown"
        ws = state.get(uid) or {}
        out.append({
            "instance_id": uid,
            "name": s.get("name") or "",
            "state": st,
            "running_job_id": act["job_id"] if act else None,
            "running_job_state": act["state"] if act else None,
            "running_since": act["since"] if act else None,
            "running_s": _since_s(act["since"], now) if act else None,
            "runtime_s": s.get("runtime_s"),
            "idle_s": s.get("idle_s"),
            "gpu_util": s.get("gpu_util"),
            "cpu_util": s.get("cpu_util"),
            "gpuprocs": s.get("gpuprocs"),
            "cost_cny": s.get("cost_cny") if s.get("cost_cny") is not None else ledger.get(uid),
            "samples": s.get("samples"),
            "last_sample_at": _iso(s.get("at")),
            "reclaimed_at": _iso(reclaimed_ids.get(uid)) if uid in reclaimed_ids else None,
            "idle_since": _iso(ws.get("idle_since")),
            "source": meta["source"],
        })

    order = {"running": 0, "idle": 1, "offline": 2, "reclaimed": 3, "unknown": 4}
    out.sort(key=lambda r: r.get("last_sample_at") or "", reverse=True)   # 新的在前
    out.sort(key=lambda r: order.get(r["state"], 9))                      # 再按状态稳定排序
    out = out[:LIMIT]

    stats = {"total": len(out)}
    for r in out:
        stats[r["state"]] = stats.get(r["state"], 0) + 1
    cost_24h = round(sum(r["cost_cny"] or 0 for r in out), 4)
    return {
        "generated_at": _iso(now),
        "window_s": win,
        "stats": {**stats, "cost_cny_sum": cost_24h},
        "instances": out,
        "watchdog": watchdog[:100],
        "notes": notes,
    }


def _since_s(ts: str | None, now: float) -> int | None:
    t = _parse_iso(ts)
    return int(max(0, now - t)) if t else None


def _parse_iso(v) -> float | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            import datetime as _dt
            return _dt.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


def _iso(ts) -> str | None:
    t = _parse_iso(ts)
    if not t:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


if __name__ == "__main__":       # 手工自检：python3 -m tools.p2.instances
    print(json.dumps(snapshot(), ensure_ascii=False, indent=2)[:4000])
