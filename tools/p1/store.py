#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 存储层：路径约定、原子写、单任务锁、规范化哈希。

三条硬约定：
  1. 一切 JSON 落盘 = tmp → fsync → rename，绝不让半截文件被读到
  2. 单任务串行 = flock(reports/p1/.job.lock)；CLI 拿不到直接退出，控制台可带 wait 排队
  3. job 目录用 mkdir(exist_ok=False) 建，EEXIST 即 ID 冲突 → 重取
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
P1_DIR = ROOT / "reports" / "p1"
JOBS_DIR = P1_DIR / "jobs"
TASKS_JSON = P1_DIR / "tasks.json"
TASKS_MD = P1_DIR / "tasks.md"
STATUS_JSON = P1_DIR / "status.json"
LOCK_FILE = P1_DIR / ".job.lock"
# 提交锁：只保护「查重 → 建目录」这一段，与单任务执行锁（.job.lock）分开，
# 否则双击提交时两个进程都查到"没重复"、各建一个作业各开一台 GPU。
SUBMIT_LOCK_FILE = P1_DIR / ".submit.lock"
# jobctl 复用已有作业时打印的标记；webui / p20ctl 据此跳过重复 spawn run.py
DEDUP_REUSE_MARK = "DEDUP_REUSE"
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


def task_dedup_key(model: dict, params: dict) -> str:
    """「同一任务」的去重键：工作流 + 模型 + 输入参数。

    与 params_hash 的区别（别混）：
      - params_hash 含 expect/budget，用于「产物是否按这份 spec 生成」的完整性校验；
        校验口径一变（例如 freeze 分档、时长推导）hash 就变，不能拿来判重复。
      - task_dedup_key **只含会决定「跑什么」的字段**，不含 expect / budget /
        job_id / 时间戳 / seed 之外的运行参数。校验口径升级不影响它。

    老作业没有存这个键，扫描时按 job.json 里的 model+params 现算，完全向后兼容，
    不需要改 job schema，也不需要数据迁移。
    """
    key = {
        # registry 的 workflow_key 比图路径稳定（图换了路径仍是同一个玩法）
        "workflow": ((model or {}).get("registry") or {}).get("workflow_key")
                    or (model or {}).get("workflow"),
        "model": (model or {}).get("key"),
        "checkpoint": (model or {}).get("checkpoint"),
        "params": params or {},
    }
    return "sha256:" + hashlib.sha256(canonical_json(key).encode("utf-8")).hexdigest()


def find_same_task(model: dict, params: dict) -> list[tuple[str, str]]:
    """全库扫同任务作业，返回 [(job_id, state)]，按 job_id 排序。

    只扫 job.json（不查 DB），所以 CLI / webui / p20ctl 三个入口看到的是同一份事实。
    直接遍历 JOBS_DIR，不经 list_jobs()：那个函数会被测试/外部 mock 掉，
    而查重必须是「真实目录」的权威视图，不能被任何替身静默绕过。
    """
    want = task_dedup_key(model, params)
    out = []
    root = Path(JOBS_DIR)
    if not root.is_dir():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        j = read_json(d / "job.json")
        if not j:
            continue
        got = j.get("task_dedup_key") or task_dedup_key(
            j.get("model") or {}, j.get("params") or {})
        if got == want:
            out.append((d.name, j.get("state", "?")))
    return out


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
    root = Path(JOBS_DIR)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and (p / "job.json").exists())


class JobLock:
    """单任务锁：默认非阻塞 flock，拿不到直接抛 JobLocked（CLI 口径，立刻报错）。

    wait=N 时改为排队：最多等 N 秒，每 2 秒重试一次（控制台提交用，避免并发提交的
    作业永远停在 CREATED）。等锁期间照样持有一个 fd，超时才抛 JobLocked。
    """

    def __init__(self, path: Path | str = LOCK_FILE, wait: float = 0):
        self.path = Path(path)
        self.wait = max(0.0, float(wait or 0))
        self._fd: int | None = None

    def _try(self, fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EACCES):
                return False
            raise

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        if self.wait > 0:
            deadline = time.monotonic() + self.wait
            waited = 0.0
            while not self._try(fd):
                left = deadline - time.monotonic()
                if left <= 0:
                    os.close(fd)
                    raise JobLocked(
                        f"排队 {self.wait:.0f}s 仍拿不到单任务锁（{self.path}）。"
                        f"前面的作业还在跑，稍后重试即可。"
                    )
                time.sleep(min(2.0, left))
                waited += 2.0
                if int(waited) % 30 == 0:
                    print(f"  [queue] 等待前一个作业结束… 已等 {waited:.0f}s", flush=True)
        elif not self._try(fd):
            os.close(fd)
            raise JobLocked(
                f"已有作业在跑（{self.path}）。本轮只做单任务，等它结束或先收尾。"
            )
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
