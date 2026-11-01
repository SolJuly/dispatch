#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 队列（SQLite WAL）：批量作业的排队、领取、心跳、重排队。

设计要点（见 P2 计划 §5.3/§5.4）：
  - 队列状态活在 queue.db，job 的 15 态状态机完全不动（批量只是调度壳）
  - claim 用 BEGIN IMMEDIATE 原子领取，天然防双领
  - 活跃态对 params_hash 建唯一索引，防止同一参数被跑两次（烧两份钱）
  - 租约 lease_until + 心跳，worker 崩了由 doctor/调度器复位
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "reports" / "p2" / "queue.db"

LEASE = 180          # 租约秒
ACTIVE = ("queued", "claimed", "running")

DDL = """
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

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
               CHECK (state IN ('queued','claimed','running','done','failed','cancelled','skipped')),
  worker       TEXT,
  lease_until  INTEGER,
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  enqueued_at  TEXT NOT NULL,
  started_at   TEXT,
  ended_at     TEXT,
  cost         REAL,
  last_error   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_item_params_active
  ON item(params_hash) WHERE state IN ('queued','claimed','running');
CREATE INDEX IF NOT EXISTS ix_item_pick ON item(state, priority DESC, enqueued_at);
CREATE INDEX IF NOT EXISTS ix_item_batch ON item(batch_id, state);

CREATE TABLE IF NOT EXISTS event (
  ts INTEGER, item_id TEXT, from_state TEXT, to_state TEXT, detail TEXT
);
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB), timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    return c


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _event(c, item_id, frm, to, detail=""):
    c.execute("INSERT INTO event(ts,item_id,from_state,to_state,detail) VALUES(?,?,?,?,?)",
              (int(time.time()), item_id, frm, to, detail[:500]))


def create_batch(batch_id: str, name: str, max_concurrency: int = 2,
                 budget_cny: float = 0.0, max_minutes: int = 0) -> None:
    c = connect()
    try:
        c.execute("INSERT OR REPLACE INTO batch(batch_id,name,created_at,max_concurrency,budget_cny,max_minutes) "
                  "VALUES(?,?,?,?,?,?)", (batch_id, name, _now(), max_concurrency, budget_cny, max_minutes))
    finally:
        c.close()


def submit(batch_id: str, job_id: str, params_hash: str, *, spec_sha256: str = "",
           priority: int = 0, model_key: str = "", required_gpu=None,
           est_cost: float = 0.0, max_attempts: int = 2) -> tuple[bool, str]:
    """入队。返回 (ok, 说明)。同参数活跃条目已存在时拒绝（防双花）。"""
    c = connect()
    try:
        dup = c.execute("SELECT job_id FROM item WHERE params_hash=? AND state IN ('queued','claimed','running')",
                        (params_hash,)).fetchone()
        if dup:
            return False, f"同参数已在队列/在跑：{dup['job_id']}"
        c.execute("INSERT INTO item(job_id,batch_id,spec_sha256,params_hash,priority,model_key,"
                  "required_gpu,est_cost,state,enqueued_at,max_attempts) "
                  "VALUES(?,?,?,?,?,?,?,?,'queued',?,?)",
                  (job_id, batch_id, spec_sha256, params_hash, priority, model_key,
                   json.dumps(required_gpu or []), est_cost, _now(), max_attempts))
        _event(c, job_id, None, "queued", f"batch={batch_id}")
        return True, "queued"
    except sqlite3.IntegrityError as e:
        return False, f"入队失败：{e}"
    finally:
        c.close()


def claim(worker_id: str, lease: int = LEASE) -> dict | None:
    """原子领取一条 queued 项，置为 claimed 并设租约。"""
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM item WHERE state='queued' "
                        "ORDER BY priority DESC, enqueued_at ASC LIMIT 1").fetchone()
        if not row:
            c.execute("COMMIT")
            return None
        job_id = row["job_id"]
        n = c.execute("UPDATE item SET state='claimed', worker=?, lease_until=?, attempts=attempts+1, "
                      "started_at=COALESCE(started_at,?) WHERE job_id=? AND state='queued'",
                      (worker_id, int(time.time()) + lease, _now(), job_id)).rowcount
        if n != 1:
            c.execute("ROLLBACK")
            return None
        c.execute("COMMIT")
        _event(c, job_id, "queued", "claimed", f"worker={worker_id}")
        return dict(c.execute("SELECT * FROM item WHERE job_id=?", (job_id,)).fetchone())
    finally:
        c.close()


def mark_running(job_id: str) -> None:
    c = connect()
    try:
        c.execute("UPDATE item SET state='running', lease_until=? WHERE job_id=?",
                  (int(time.time()) + LEASE, job_id))
        _event(c, job_id, "claimed", "running")
    finally:
        c.close()


def heartbeat(job_id: str, lease: int = LEASE) -> None:
    c = connect()
    try:
        c.execute("UPDATE item SET lease_until=? WHERE job_id=?", (int(time.time()) + lease, job_id))
    finally:
        c.close()


def finish(job_id: str, ok: bool, cost: float | None = None, error: str = "") -> None:
    c = connect()
    try:
        st = "done" if ok else "failed"
        c.execute("UPDATE item SET state=?, ended_at=?, cost=?, last_error=? WHERE job_id=?",
                  (st, _now(), cost, error[:500], job_id))
        _event(c, job_id, "running", st, error[:300])
    finally:
        c.close()


def requeue(job_id: str, error: str = "") -> bool:
    """失败重排：attempts 未到上限才回 queued。返回是否重排。"""
    c = connect()
    try:
        row = c.execute("SELECT attempts,max_attempts,state FROM item WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return False
        if row["attempts"] >= row["max_attempts"]:
            c.execute("UPDATE item SET state='failed', ended_at=?, last_error=? WHERE job_id=?",
                      (_now(), (error or "attempts exhausted")[:500], job_id))
            _event(c, job_id, row["state"], "failed", "attempts exhausted")
            return False
        c.execute("UPDATE item SET state='queued', worker=NULL, lease_until=NULL, last_error=? WHERE job_id=?",
                  (error[:500], job_id))
        _event(c, job_id, row["state"], "queued", f"requeue: {error[:200]}")
        return True
    finally:
        c.close()


def cancel(job_id: str, why: str = "manual") -> None:
    c = connect()
    try:
        c.execute("UPDATE item SET state='cancelled', ended_at=?, last_error=? "
                  "WHERE job_id=? AND state IN ('queued','claimed','running')", (_now(), why, job_id))
        _event(c, job_id, None, "cancelled", why)
    finally:
        c.close()


def expired(lease: int = LEASE) -> list[dict]:
    """租约过期仍未完成的项（worker 可能已死）。"""
    c = connect()
    try:
        rows = c.execute("SELECT * FROM item WHERE state IN ('claimed','running') "
                         "AND COALESCE(lease_until,0) < ?", (int(time.time()),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def reset_stale(lease: int = LEASE) -> list[str]:
    """把租约过期的 claimed/running 复位为 queued（或到上限则 failed）。"""
    out = []
    for row in expired(lease):
        ok = requeue(row["job_id"], "lease expired")
        out.append(f"{row['job_id']}:{'requeued' if ok else 'failed'}")
    return out


def items(batch_id: str | None = None, state: str | None = None) -> list[dict]:
    c = connect()
    try:
        q, args = "SELECT * FROM item", []
        where = []
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
    c = connect()
    try:
        rows = c.execute("SELECT state, COUNT(*) n, COALESCE(SUM(cost),0) c FROM item GROUP BY state").fetchall()
        d = {r["state"]: {"n": r["n"], "cost": round(r["c"], 3)} for r in rows}
        total = c.execute("SELECT COUNT(*) n FROM item").fetchone()["n"]
        return {"total": total, "by_state": d}
    finally:
        c.close()


def active_count() -> int:
    c = connect()
    try:
        return c.execute("SELECT COUNT(*) n FROM item WHERE state IN ('claimed','running')").fetchone()["n"]
    finally:
        c.close()
