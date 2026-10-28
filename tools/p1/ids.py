#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 唯一 ID：job_id / task_id 的生成与校验。

设计见 reports/p1/DESIGN-任务清单与唯一ID.md：
  job_id  = p1-YYYYMMDDTHHMM-<6hex>   运行期生成，字典序≈时间序
  task_id = P1-T<两位>[.<子号>]        人工排号，稳定可引用

两者前缀不同、空间不混：task 是开发任务，job 是一次 GPU 闭环。
"""
from __future__ import annotations

import datetime as dt
import re
import secrets

JOB_ID_RE = re.compile(r"^p1-\d{8}T\d{4}-[0-9a-f]{6}$")
TASK_ID_RE = re.compile(r"^P1-T\d{2}(?:\.\d+)?$")

PREFIX = "p1"
RAND_BYTES = 3          # 6 hex → 1677 万组合，单机单任务足够
MAX_RETRY = 5


def valid_job_id(s) -> bool:
    return isinstance(s, str) and bool(JOB_ID_RE.match(s))


def valid_task_id(s) -> bool:
    return isinstance(s, str) and bool(TASK_ID_RE.match(s))


def new_job_id(now: dt.datetime | None = None, rand: str | None = None) -> str:
    """生成一个 job_id。rand 仅用于测试注入。"""
    t = (now or dt.datetime.now()).strftime("%Y%m%dT%H%M")
    return f"{PREFIX}-{t}-{(rand or secrets.token_hex(RAND_BYTES))}"


def job_id_time(s: str) -> dt.datetime | None:
    """解析 job_id 的时间前缀，失败返回 None。"""
    if not valid_job_id(s):
        return None
    try:
        return dt.datetime.strptime(s[3:16], "%Y%m%dT%H%M")
    except ValueError:
        return None


def attempt_id(n: int) -> str:
    """作业内尝试号：attempt-01 起。"""
    return f"attempt-{int(n):02d}"
