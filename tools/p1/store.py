#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 存储层：路径约定、原子写、单任务锁、规范化哈希。

三条硬约定：
  1. 一切 JSON 落盘 = tmp → fsync → rename，绝不让半截文件被读到
  2. 单任务串行 = flock(reports/p1/.job.lock)，拿不到直接退出，不排队
  3. job 目录用 mkdir(exist_ok=False) 建，EEXIST 即 ID 冲突 → 重取
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
P1_DIR = ROOT / "reports" / "p1"
JOBS_DIR = P1_DIR / "jobs"
TASKS_JSON = P1_DIR / "tasks.json"
TASKS_MD = P1_DIR / "tasks.md"
STATUS_JSON = P1_DIR / "status.json"
LOCK_FILE = P1_DIR / ".job.lock"
LEDGER = ROOT / ".compshare" / "ledger.jsonl"


class JobExists(Exception):
    """job 目录已存在：ID 冲突。"""


class JobLocked(Exception):
    """已有作业在跑：本轮口径是单任务，不排队。"""


def canonical_json(obj) -> str:
    """规范化 JSON：键排序、紧凑分隔、保留中文。params_hash 的唯一依据。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def params_hash(model: dict, params: dict, expect: dict) -> str:
    """幂等键：只覆盖会改变产物的字段，不含 job_id/时间。

    model 只取 key / workflow_sha256 / checkpoint —— 换模型、换工作流、换权重都算新作业。
    """
    key = {
        "model": {k: (model or {}).get(k) for k in ("key", "workflow_sha256", "checkpoint")},
        "params": params or {},
        "expect": expect or {},
    }
    return "sha256:" + hashlib.sha256(canonical_json(key).encode("utf-8")).hexdigest()


def is_params_hash(s) -> bool:
    return isinstance(s, str) and s.startswith("sha256:") and len(s) == 71


def atomic_write_json(path: Path, obj, *, indent: int = 2) -> None:
    """写 JSON：同目录 tmp → fsync → rename → fsync 目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def ensure_job_dir(job_id: str) -> Path:
    """建作业目录；已存在说明 ID 撞了，抛 JobExists 由调用方重取。"""
    d = JOBS_DIR / job_id
    try:
        d.mkdir(parents=True, exist_ok=False)
    except FileExistsError as e:
        raise JobExists(job_id) from e
    return d


def job_dir(job_id: str) -> Path:
    return JOBS_DIR / job_id


def job_file(job_id: str, name: str = "job.json") -> Path:
    return job_dir(job_id) / name


def list_jobs() -> list[str]:
    """按 job_id 字典序（≈时间序）返回全部作业。"""
    if not JOBS_DIR.is_dir():
        return []
    return sorted(p.name for p in JOBS_DIR.iterdir()
                  if p.is_dir() and (p / "job.json").exists())


class JobLock:
    """单任务锁：非阻塞 flock，拿不到直接抛 JobLocked（不排队，符合单任务口径）。"""

    def __init__(self, path: Path = LOCK_FILE):
        self.path = path
        self._fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EACCES):
                raise JobLocked(
                    f"已有作业在跑（{self.path}）。本轮只做单任务，等它结束或先收尾。"
                ) from e
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False


class PerJobLock:
    """作业级锁：同一 job 不能并发跑，不同 job 互不影响（P2 批量模式用）。

    路径 reports/p1/jobs/<jid>/.lock，非阻塞：拿不到抛 JobLocked（调用方跳过/重排）。
    与全局 JobLock 的区别：global 是「同一时刻只允许一个作业」，per-job 只防「同一作业被跑两次」。
    """

    def __init__(self, job_id: str):
        self.path = job_dir(job_id) / ".lock"
        self._fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EAGAIN, errno.EACCES):
                raise JobLocked(f"作业已在跑（{self.path}）。") from e
            raise
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return self

    def __exit__(self, *exc):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        return False


def per_job_lock(job_id: str) -> PerJobLock:
    return PerJobLock(job_id)


def rel(p: Path) -> str:
    """相对仓库根，用于报告与 manifest。"""
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)
