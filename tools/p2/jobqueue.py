#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 队列 + 审计（SQLite WAL）：排队、领取、心跳、退避重试、审计流水。

## ⚠ 本模块当前处于「禁用」状态（2026-11-12）

原因：**队列没有生产者**。`enqueue()` 有完整实现，但全库没有一处调用它 ——
UI 提交的作业直接写 `reports/p1/jobs/`，从不进队列。于是 queue.db 从
2026-11-01 起就没再写入过，只剩 8 条历史残留。

危害不是"多一个没用的模块"，而是它会**撒谎**：

    · UI 的「已完成花费 ¥7.35」来自 queue.db 的 8 条旧记录
    · 而「失败 26 / 完成 20」来自 job.json 的 47 条
    · 两个数字属于两套数据源，真实花费是 ¥3.86 —— 差了一倍

自动补位已经由 `tools/p1/autostart.py` 承担（事件驱动，不需要常驻队列），
所以这套队列短期内不会接通。

### 禁用是怎么回事

`ENABLED = False` 时：

    · 所有**读**接口返回空（stats → 0 条、items → []、claim → None）
    · 所有**写**接口抛 QueueDisabled
    · **绝不创建/打开数据库文件**

最后一条是关键。原来的 `connect()` 会 `_ensure_schema()` + `executescript(DDL)`，
即**任何一次只读调用都会把删掉的库重新建出来**（实测确认）。所以"删掉
queue.db 文件"根本不算禁用 —— 下次谁读一次它就回来了，还是个空库。

开关接通后（`ENABLED = True` 或环境变量 `P2_QUEUE_ENABLED=1`）行为完全不变，
969 行代码一行没删，随时可回滚。

设计（P0/P1 硬化后）：
  - P0-1 幂等：job_id 主键 + 提交前显式检查（同 id 活跃 → 拦截）；params_hash 活跃态唯一索引
  - P0-2 心跳：worker 每 15s heartbeat()，调度器每 30s scan，>60s 判定 FAILED
  - P0-3 审计：audit_log 逐条记录状态变更 + instance_id + exit_code + GPU 分钟
  - P1-4 重试：1s→5s→30s 退避，最多 3 次，超限 TERMINAL_FAILED
  - P1-5 回收：reclaim() 由 watchdog 调用，落 RECLAIMED

状态：queued → claimed → running → done / failed / terminal_failed / cancelled / skipped / reclaimed
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "reports" / "p2" / "queue.db"

# 队列总开关。为什么是默认关：见模块 docstring —— 没有生产者，
# 它唯一的实际作用是让界面上的数字互相矛盾。
# 接通生产者之后把这里改成 True（或设环境变量 P2_QUEUE_ENABLED=1）。
ENABLED = False


class QueueDisabled(RuntimeError):
    """队列已禁用时，写操作抛这个。

    读操作不抛 —— 它们返回空结果。理由：读方（reaper / instances / riskmon /
    status2）都是一次巡检里的附加信息，为一条读不到的队列让整轮巡检崩掉
    不值当。写方则必须立刻知道"这个调用没有生效"，静默丢弃会丢作业。
    """


def is_enabled() -> bool:
    """运行时判定。环境变量可以临时改回去（排障/回滚用）。"""
    raw = os.environ.get("P2_QUEUE_ENABLED")
    if raw is not None:
        return raw.strip() not in ("", "0", "false", "False", "no")
    return ENABLED


def _disabled_note() -> str:
    return ("队列已禁用（没有生产者）。真实作业状态看 reports/p1/jobs/*/job.json，"
            "自动补位见 tools/p1/autostart.py。要恢复请设 P2_QUEUE_ENABLED=1。")

SCHEMA_VERSION = 2
LEASE = 90               # 租约（> 心跳 15s × 若干次）
HEARTBEAT_S = 15         # worker 心跳间隔（P0-2）
TIMEOUT_S = 60           # 调度器判定超时（P0-2）
BACKOFF = (1, 5, 30)     # P1-4 退避阶梯（秒）
MAX_ATTEMPTS = 3         # P1-4 最大重试次数

ACTIVE = ("queued", "claimed", "running")
STATES = ("queued", "claimed", "running", "done", "failed",
          "terminal_failed", "cancelled", "skipped", "reclaimed")

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS batch (
  batch_id     TEXT PRIMARY KEY,
  name         TEXT,
  created_at   TEXT NOT NULL,
  max_concurrency INTEGER,
  budget_cny   REAL,
  max_minutes  INTEGER,
  state        TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS item (
  job_id       TEXT PRIMARY KEY,
  batch_id     TEXT,
  spec_sha256  TEXT,
  params_hash  TEXT NOT NULL,
  priority     INTEGER NOT NULL DEFAULT 0,
  model_key    TEXT,
  required_gpu TEXT,
  est_cost     REAL,
  state        TEXT NOT NULL DEFAULT 'queued'
               CHECK (state IN ('queued','claimed','running','done','failed',
                                'terminal_failed','cancelled','skipped','reclaimed')),
  worker       TEXT,
  instance_id  TEXT,
  lease_until  INTEGER,
  heartbeat_at INTEGER,
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  next_retry_at INTEGER,
  backoff_s    INTEGER,
  defers       INTEGER NOT NULL DEFAULT 0,
  enqueued_at  TEXT NOT NULL,
  started_at   TEXT,
  ended_at     TEXT,
  cost         REAL,
  gpu_minutes  REAL,
  exit_code    INTEGER,
  last_error   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_item_params_active
  ON item(params_hash) WHERE state IN ('queued','claimed','running');
CREATE INDEX IF NOT EXISTS ix_item_pick ON item(state, next_retry_at, priority DESC, enqueued_at);
CREATE INDEX IF NOT EXISTS ix_item_batch ON item(batch_id, state);

CREATE TABLE IF NOT EXISTS audit_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  job_id     TEXT NOT NULL,
  instance_id TEXT,
  from_state TEXT,
  to_state   TEXT NOT NULL,
  exit_code  INTEGER,
  gpu_minutes REAL,
  detail     TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_job ON audit_log(job_id, id);
"""


def _ensure_schema() -> None:
    """v1→v2 自动迁移：旧库缺 heartbeat_at 列就改名备份并重建（队列是运行态，job 事实在 job.json）。"""
    if not DB.exists():
        return
    try:
        c = sqlite3.connect(str(DB), timeout=5)
        cols = {r[1] for r in c.execute("PRAGMA table_info(item)")}
        c.close()
        if cols and "heartbeat_at" not in cols:
            bak = DB.with_name(f"queue.db.v1.{int(time.time())}.bak")
            DB.rename(bak)
            print(f"[jobqueue] 旧库已备份为 {bak.name}，重建 v{SCHEMA_VERSION} 库")
    except Exception:
        pass


# 增量列迁移：CREATE TABLE IF NOT EXISTS 不会给旧表补列，必须显式 ALTER
MIG_COLS = {
    "item": {
        "instance_id": "TEXT", "heartbeat_at": "INTEGER", "next_retry_at": "INTEGER",
        "backoff_s": "INTEGER", "defers": "INTEGER NOT NULL DEFAULT 0",
        "gpu_minutes": "REAL", "exit_code": "INTEGER",
    },
}


def _migrate_columns(c: sqlite3.Connection) -> None:
    for table, cols in MIG_COLS.items():
        try:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        except sqlite3.OperationalError:
            continue
        if not have:
            continue
        for name, decl in cols.items():
            if name not in have:
                try:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                except sqlite3.OperationalError:
                    pass


def connect() -> sqlite3.Connection:
    """打开队列库。禁用时抛 QueueDisabled，**不创建任何文件**。

    为什么把闸放在最底层：`connect()` 是每个读写的必经之路。放在这里，
    禁用后**不可能有任何一个函数绕过去建库**。放在各函数里逐个加的话，
    漏一个就会让"删掉的库"悄悄复活（实测过：删库后调一次 `stats()`
    文件就又有了）。

    副作用：禁用期间 `connect()` 不能直接用。所有面向调用方的接口
    （stats / items / claim …）都已加了 `is_enabled()` 判断，返回空结果。
    只有直接调 `connect()` 的代码会拿到异常 —— 那是内部用法，禁用时本就不该有。
    """
    if not is_enabled():
        raise QueueDisabled(_disabled_note())
    DB.parent.mkdir(parents=True, exist_ok=True)
    _ensure_schema()
    c = sqlite3.connect(str(DB), timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    _migrate_columns(c)
    c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    return c


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _audit(c, job_id, frm, to, *, instance_id=None, exit_code=None,
           gpu_minutes=None, detail=""):
    c.execute("INSERT INTO audit_log(ts,job_id,instance_id,from_state,to_state,exit_code,gpu_minutes,detail) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (int(time.time()), job_id, instance_id, frm, to, exit_code, gpu_minutes, (detail or "")[:500]))


# ---------------------------------------------------------------- 批次
def create_batch(batch_id: str, name: str, max_concurrency: int = 2,
                 budget_cny: float = 0.0, max_minutes: int = 0) -> None:
    c = connect()
    try:
        c.execute("INSERT OR REPLACE INTO batch(batch_id,name,created_at,max_concurrency,budget_cny,max_minutes) "
                  "VALUES(?,?,?,?,?,?)", (batch_id, name, _now(), max_concurrency, budget_cny, max_minutes))
    finally:
        c.close()


# ---------------------------------------------------------------- 提交（P0-1 幂等）
def submit(batch_id: str, job_id: str, params_hash: str, *, spec_sha256: str = "",
           priority: int = 0, model_key: str = "", required_gpu=None,
           est_cost: float = 0.0, max_attempts: int = MAX_ATTEMPTS) -> tuple[bool, str]:
    """入队。显式幂等：同 job_id 活跃 → 拦截；同 params_hash 活跃 → 拦截。"""
    c = connect()
    try:
        dup_id = c.execute("SELECT state FROM item WHERE job_id=?", (job_id,)).fetchone()
        if dup_id:
            if dup_id["state"] in ACTIVE:
                return False, f"job_id 幂等拦截：{job_id} 已处于 {dup_id['state']}"
            return False, f"job_id 已存在（{dup_id['state']}，不可重复入队）"
        dup = c.execute("SELECT job_id,state FROM item WHERE params_hash=? AND state IN ('queued','claimed','running')",
                        (params_hash,)).fetchone()
        if dup:
            return False, f"同参数已在队列/在跑：{dup['job_id']}（{dup['state']}）"
        c.execute("INSERT INTO item(job_id,batch_id,spec_sha256,params_hash,priority,model_key,"
                  "required_gpu,est_cost,state,enqueued_at,max_attempts) "
                  "VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                  (job_id, batch_id, spec_sha256, params_hash, priority, model_key,
                   json.dumps(required_gpu or []), est_cost, _now(), max_attempts))
        _audit(c, job_id, None, "queued", detail=f"batch={batch_id}")
        return True, "queued"
    except sqlite3.IntegrityError as e:
        return False, f"入队失败：{e}"
    finally:
        c.close()


# ---------------------------------------------------------------- 领取
def claim(worker_id: str, lease: int = LEASE) -> dict | None:
    if not is_enabled():
        return None
    """原子领取：只领 queued 且退避到期的项。"""
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        row = c.execute("SELECT * FROM item WHERE state='queued' "
                        "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                        "ORDER BY priority DESC, enqueued_at ASC LIMIT 1", (now,)).fetchone()
        if not row:
            c.execute("COMMIT")
            return None
        job_id = row["job_id"]
        n = c.execute("UPDATE item SET state='claimed', worker=?, lease_until=?, heartbeat_at=?, "
                      "attempts=attempts+1, started_at=COALESCE(started_at,?) "
                      "WHERE job_id=? AND state='queued'",
                      (worker_id, now + lease, now, _now(), job_id)).rowcount
        if n != 1:
            c.execute("ROLLBACK")
            return None
        c.execute("COMMIT")
        _audit(c, job_id, "queued", "claimed", detail=f"worker={worker_id} attempt={row['attempts'] + 1}")
        return dict(c.execute("SELECT * FROM item WHERE job_id=?", (job_id,)).fetchone())
    finally:
        c.close()


def mark_running(job_id: str) -> None:
    c = connect()
    try:
        now = int(time.time())
        c.execute("UPDATE item SET state='running', lease_until=?, heartbeat_at=? WHERE job_id=?",
                  (now + LEASE, now, job_id))
        _audit(c, job_id, "claimed", "running")
    finally:
        c.close()


def set_instance(job_id: str, uid: str) -> None:
    """worker 建机成功后登记实例，供 watchdog 回收孤儿实例。"""
    c = connect()
    try:
        c.execute("UPDATE item SET instance_id=? WHERE job_id=?", (uid, job_id))
    finally:
        c.close()


def clear_instance(job_id: str) -> None:
    c = connect()
    try:
        c.execute("UPDATE item SET instance_id=NULL WHERE job_id=?", (job_id,))
    finally:
        c.close()


def heartbeat(job_id: str, lease: int = LEASE) -> None:
    """worker 每 15s 调用（P0-2）。行不存在时静默（legacy 单跑场景）。"""
    c = connect()
    try:
        now = int(time.time())
        c.execute("UPDATE item SET heartbeat_at=?, lease_until=? WHERE job_id=? "
                  "AND state IN ('claimed','running')", (now, now + lease, job_id))
    finally:
        c.close()


# ---------------------------------------------------------------- 收尾 + 审计
def finish(job_id: str, ok: bool, cost: float | None = None, error: str = "",
           *, instance_id: str | None = None, exit_code: int | None = None,
           gpu_minutes: float | None = None) -> None:
    c = connect()
    try:
        st = "done" if ok else "failed"
        n = c.execute("UPDATE item SET state=?, ended_at=?, cost=?, last_error=?, instance_id=?, "
                      "exit_code=?, gpu_minutes=? WHERE job_id=? AND state != 'cancelled'",
                      (st, _now(), cost, (error or "")[:500], instance_id, exit_code, gpu_minutes, job_id)).rowcount
        if n == 0:
            # 已被 cancel：不复活状态，但成本/退出码必须如实记账（否则对账 C4 对不上）
            c.execute("UPDATE item SET cost=?, instance_id=?, exit_code=?, gpu_minutes=? WHERE job_id=?",
                      (cost, instance_id, exit_code, gpu_minutes, job_id))
            _audit(c, job_id, "cancelled", "cancelled",
                   instance_id=instance_id, exit_code=exit_code, gpu_minutes=gpu_minutes,
                   detail=f"worker finished after cancel: {st} ¥{cost or 0:.4f}")
            return
        _audit(c, job_id, "running", st, instance_id=instance_id, exit_code=exit_code,
               gpu_minutes=gpu_minutes, detail=(error or "")[:300])
    finally:
        c.close()


# ---------------------------------------------------------------- 重试（P1-4）
def requeue(job_id: str, error: str = "", exit_code: int | None = None,
            gpu_minutes: float | None = None, cost: float | None = None) -> bool:
    """失败重排：1s→5s→30s 退避，最多 MAX_ATTEMPTS 次；超限 → terminal_failed。"""
    c = connect()
    try:
        row = c.execute("SELECT attempts,max_attempts,state FROM item WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return False
        # 终态/取消不得复活（cancel 是被杀 worker 的崩溃路径再跑一遍时最容易踩的坑）
        if row["state"] in ("cancelled", "done", "reclaimed", "skipped"):
            return False
        if row["attempts"] >= row["max_attempts"]:
            c.execute("UPDATE item SET state='terminal_failed', ended_at=?, last_error=?, "
                      "exit_code=COALESCE(?, exit_code), "
                      "gpu_minutes=COALESCE(gpu_minutes,0)+COALESCE(?,0), "
                      "cost=COALESCE(cost,0)+COALESCE(?,0) WHERE job_id=?",
                      (_now(), (error or "attempts exhausted")[:500], exit_code,
                       gpu_minutes, cost, job_id))
            _audit(c, job_id, row["state"], "terminal_failed", exit_code=exit_code,
                   detail="attempts exhausted")
            return False
        backoff = BACKOFF[min(max(row["attempts"] - 1, 0), len(BACKOFF) - 1)]
        # 用 ceil 而不是 int：`int(now)` 会丢掉小数部分，让"退避 1 秒"实际变成
        # "退避 -1~1 秒"（同一秒内 int(now) 相同，下一毫秒就可能到期）。
        # 退避的语义是**至少等 N 秒**，向上取整才成立。
        # 实测这个截断会让瞬时故障（限流、无现货）立刻重试，把退避打穿。
        next_at = math.ceil(time.time()) + backoff
        c.execute("UPDATE item SET state='queued', worker=NULL, lease_until=NULL, heartbeat_at=NULL, "
                  "next_retry_at=?, backoff_s=?, last_error=?, "
                  "gpu_minutes=COALESCE(gpu_minutes,0)+COALESCE(?,0), "
                  "cost=COALESCE(cost,0)+COALESCE(?,0) WHERE job_id=?",
                  (next_at, backoff, (error or "")[:500], gpu_minutes, cost, job_id))
        _audit(c, job_id, row["state"], "queued", detail=f"retry in {backoff}s: {(error or '')[:200]}")
        return True
    finally:
        c.close()


def defer(job_id: str, seconds: int = 60, why: str = "", max_defers: int = 10) -> bool:
    """瞬时故障（无货/限流）退回队列：不消耗 attempts，延迟再试；超过 max_defers 交回 requeue。"""
    c = connect()
    try:
        row = c.execute("SELECT state,defers FROM item WHERE job_id=?", (job_id,)).fetchone()
        if not row or row["state"] in ("cancelled", "done", "reclaimed", "skipped"):
            return False
        if (row["defers"] or 0) >= max_defers:
            return False
        # 同 requeue：defer 的语义是"至少延迟 N 秒"，必须向上取整。
        now = math.ceil(time.time())
        c.execute("UPDATE item SET state='queued', worker=NULL, lease_until=NULL, heartbeat_at=NULL, "
                  "defers=defers+1, attempts=MAX(attempts-1,0), next_retry_at=?, last_error=? "
                  "WHERE job_id=?", (now + seconds, (why or "")[:500], job_id))
        _audit(c, job_id, row["state"], "queued", detail=f"defer {seconds}s: {(why or '')[:150]}")
        return True
    finally:
        c.close()


def release(job_id: str, why: str = "") -> None:
    """把刚 claim 但未真正执行的项退回 queued（attempts 回退，不消耗重试次数）。"""
    c = connect()
    try:
        n = c.execute("UPDATE item SET state='queued', worker=NULL, lease_until=NULL, heartbeat_at=NULL, "
                      "attempts=MAX(attempts-1,0) WHERE job_id=? AND state='claimed'", (job_id,)).rowcount
        if n:
            _audit(c, job_id, "claimed", "queued", detail=f"released: {(why or '')[:200]}")
    finally:
        c.close()


def cancel(job_id: str, why: str = "manual") -> None:
    c = connect()
    try:
        c.execute("UPDATE item SET state='cancelled', ended_at=?, last_error=? "
                  "WHERE job_id=? AND state IN ('queued','claimed','running')", (_now(), why, job_id))
        _audit(c, job_id, None, "cancelled", detail=why)
    finally:
        c.close()


def reclaim(job_id: str, why: str = "watchdog") -> None:
    """P1-5：watchdog 回收后落 RECLAIMED（实例已杀/已释放）。"""
    c = connect()
    try:
        c.execute("UPDATE item SET state='reclaimed', ended_at=?, last_error=? WHERE job_id=?",
                  (_now(), why, job_id))
        _audit(c, job_id, None, "reclaimed", detail=why)
    finally:
        c.close()


# ---------------------------------------------------------------- 心跳超时（P0-2）
def stale(seconds: int = TIMEOUT_S) -> list[dict]:
    if not is_enabled():
        return []
    """claimed/running 且心跳超时的项。"""
    c = connect()
    try:
        cutoff = int(time.time()) - int(seconds)
        rows = c.execute("SELECT * FROM item WHERE state IN ('claimed','running') "
                         "AND COALESCE(heartbeat_at,0) < ?", (cutoff,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def mark_timeout(job_id: str, seconds: int = TIMEOUT_S, detail: str = "") -> None:
    """心跳超时 → FAILED（P0-2）；是否重排交由 requeue()（P1-4）。"""
    c = connect()
    try:
        row = c.execute("SELECT state FROM item WHERE job_id=?", (job_id,)).fetchone()
        frm = row["state"] if row else None
        c.execute("UPDATE item SET state='failed', ended_at=?, last_error=? WHERE job_id=?",
                  (_now(), f"heartbeat timeout >{seconds}s {detail}"[:500], job_id))
        _audit(c, job_id, frm, "failed", detail=f"heartbeat timeout >{seconds}s {detail}")
    finally:
        c.close()


def expired(lease: int = LEASE) -> list[dict]:
    if not is_enabled():
        return []
    """兼容旧接口：租约过期即心跳过期。"""
    return stale(lease)


def reset_stale(seconds: int = TIMEOUT_S) -> list[str]:
    if not is_enabled():
        return []
    """心跳超时的项：先置 FAILED，再按退避重排（超限则 terminal_failed）。"""
    out = []
    for row in stale(seconds):
        jid = row["job_id"]
        mark_timeout(jid, seconds)
        ok = requeue(jid, f"heartbeat timeout >{seconds}s")
        out.append(f"{jid}:{'requeued' if ok else 'terminal_failed'}")
    return out


# ---------------------------------------------------------------- 查询
def batch_info(batch_id: str) -> dict | None:
    if not is_enabled():
        return None
    c = connect()
    try:
        r = c.execute("SELECT * FROM batch WHERE batch_id=?", (batch_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def batch_spent(batch_id: str) -> float:
    if not is_enabled():
        return 0.0
    """该批已花（item.cost 汇总；running 项按 est_cost 计入在途）。"""
    c = connect()
    try:
        spent = c.execute("SELECT COALESCE(SUM(cost),0) v FROM item WHERE batch_id=?", (batch_id,)).fetchone()["v"]
        inflight = c.execute("SELECT COALESCE(SUM(est_cost),0) v FROM item "
                             "WHERE batch_id=? AND state IN ('claimed','running')", (batch_id,)).fetchone()["v"]
        return round(float(spent) + float(inflight), 4)
    finally:
        c.close()


def cancelled_ids() -> set[str]:
    if not is_enabled():
        return set()
    c = connect()
    try:
        return {r["job_id"] for r in c.execute("SELECT job_id FROM item WHERE state='cancelled'")}
    finally:
        c.close()


def items(batch_id: str | None = None, state: str | None = None) -> list[dict]:
    if not is_enabled():
        return []
    c = connect()
    try:
        q, args, where = "SELECT * FROM item", [], []
        if batch_id:
            where.append("batch_id=?"); args.append(batch_id)
        if state:
            where.append("state=?"); args.append(state)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY enqueued_at"
        return [dict(r) for r in c.execute(q, args).fetchall()]
    finally:
        c.close()


def stats() -> dict:
    if not is_enabled():
        return {"total": 0, "by_state": {}}
    c = connect()
    try:
        rows = c.execute("SELECT state, COUNT(*) n, COALESCE(SUM(cost),0) c FROM item GROUP BY state").fetchall()
        d = {r["state"]: {"n": r["n"], "cost": round(r["c"], 3)} for r in rows}
        total = c.execute("SELECT COUNT(*) n FROM item").fetchone()["n"]
        return {"total": total, "by_state": d}
    finally:
        c.close()


def active_count() -> int:
    if not is_enabled():
        return 0
    c = connect()
    try:
        return c.execute("SELECT COUNT(*) n FROM item WHERE state IN ('claimed','running')").fetchone()["n"]
    finally:
        c.close()


def audit_since(since_id: int = 0, job_id: str | None = None, limit: int = 200) -> list[dict]:
    """增量取审计（id 游标），给 UI 轮询用。返回按 id 升序。"""
    if not is_enabled():
        return []
    c = connect()
    try:
        if job_id:
            rows = c.execute("SELECT * FROM audit_log WHERE id>? AND job_id=? ORDER BY id ASC LIMIT ?",
                             (int(since_id), job_id, int(limit))).fetchall()
        else:
            rows = c.execute("SELECT * FROM audit_log WHERE id>? ORDER BY id ASC LIMIT ?",
                             (int(since_id), int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def audit_rows(job_id: str | None = None, limit: int = 200) -> list[dict]:
    if not is_enabled():
        return []
    c = connect()
    try:
        if job_id:
            rows = c.execute("SELECT * FROM audit_log WHERE job_id=? ORDER BY id DESC LIMIT ?",
                             (job_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()
