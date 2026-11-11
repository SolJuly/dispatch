#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实例运行采样器：只**看**，不动手。往 .compshare/watch.log 追加一行。

## 为什么要单独一个工具

`b1d70e7` 把 cron 里的 `csctl watch` 撤了，因为它按 GPU 利用率 15 分钟停机 ——
装模型阶段 GPU 也是 0%，会被误关；而 `p2-reaper` 用的是更准的判据
（作业状态 + 生命周期窗口），两套判据打架时 watch 会抢先关掉长任务的机器。

撤得对。但 watch 顺手还干了另一件事：**每 5 分钟往 watch.log 写一行采样**。
那行是全系统唯一的运行时观测数据源：

    · 实例页的 GPU%/CPU%/空闲时长/累计花费   ← tools/p2/instances.py 解析它
    · 看板上「watchdog」那张表的兜底时间线
    · arch.py 架构图里实例状态的数据来源

所以撤掉 watch 之后，实例页从 2026-11-09 18:30 起再无数据，
停更 52 小时，用户看到的是「watchdog 不显示了」。

正确做法是**拆开两件事**：判据（谁来停机）归 reaper，采样（谁记录）归这里。
本工具永远不停机、不销毁、不改任何状态，只读云端 + 追加一行日志。

## 采样失败不退出非零

cron 每 5 分钟跑一次。某一次读不到云端（限流、网络抖动）就跳过本轮，
下一轮补上即可。返回非零只会让 cron 发一堆邮件，而信息量为零。

## 用法

    python3 tools/p2/sampler.py            # 采一轮（cron 用）
    python3 tools/p2/sampler.py --dry-run  # 只打印会写什么
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

COMP = _ROOT / ".compshare"
WATCH_LOG = COMP / "watch.log"
WATCH_STATE = COMP / "watch_state.json"

# 与 csctl.py 的 _elapsed / _cost_so_far 同口径。
# 不 import csctl：那是脚本目录下的模块，且带一堆 argparse 副作用；
# 这里只有十几行，复制比耦合安全（两边都改了才叫口径变了，单边改是 bug）。
RATE_HOURLY = 2.15


def _elapsed_h(inst: dict, now: float) -> float:
    """本计费周期已运行小时数（关机状态为 0，关机不收算力费）。"""
    if inst.get("State") not in ("Running", "Rebooting"):
        return 0.0
    t = inst.get("StartTime") or inst.get("CreateTime") or 0
    return max(0.0, (now - float(t)) / 3600.0) if t else 0.0


def _load_state() -> dict:
    """每实例的 idle_since（csctl watch 时代维护的；reaper 也读同一份）。"""
    try:
        d = json.loads(WATCH_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _idle_min(state: dict, uid: str, now: float) -> int:
    """空闲分钟数。没有 idle_since（reaper 判定为"有活儿"）就是 0。"""
    rec = state.get(uid) or {}
    since = rec.get("idle_since")
    if not since:
        return 0
    try:
        return max(0, int((now - float(since)) / 60))
    except (TypeError, ValueError):
        return 0


def _fmt_pct(v) -> str:
    """GPU 百分比：读不到写 n/a（instances.py 的 _pct 会把 n/a 解析成 None）。"""
    return f"{v:.0f}%" if isinstance(v, (int, float)) else "n/a"


def sample(dry_run: bool = False, now: float | None = None, say=print) -> list[str]:
    """采一轮。返回写出的行（dry-run 时也返回，便于测试断言）。"""
    now = time.time() if now is None else now
    try:
        from compshare.cs import CS
    except Exception as e:                     # noqa: BLE001
        say(f"⚠ 取不到云凭证，本轮跳过：{type(e).__name__}: {e}")
        return []

    try:
        insts = CS().instances()
    except Exception as e:                     # noqa: BLE001
        say(f"⚠ 读不到实例列表，本轮跳过：{type(e).__name__}: {e}")
        return []

    state = _load_state()
    ts = dt.datetime.fromtimestamp(now).strftime("%H:%M:%S")
    lines: list[str] = []
    for i in insts:
        uid = str(i.get("UHostId") or "")
        if not uid:
            continue
        name = str(i.get("Name") or "")[:16]
        hours = _elapsed_h(i, now)
        cost = RATE_HOURLY * hours if hours else 0.0
        # gpu/cpu 走云端监控接口。读不到就写 n/a，**不写 0** ——
        # 0% 会被当成"GPU 确实闲着"，而 n/a 才是"不知道"。
        gpu = cpu = None
        try:
            m = CS().monitor(uid)
            gpu = m.get("gpu")
            cpu = m.get("cpu")
        except Exception:                      # noqa: BLE001  监控是附加信息
            pass
        line = (f"[{ts}] {uid} {name:<16} "
                f"gpu={_fmt_pct(gpu)} cpu={_fmt_pct(cpu)} "
                f"idle={_idle_min(state, uid, now)}m "
                f"run={hours:.2f}h 花=¥{cost:.2f}")
        lines.append(line)

    if not lines:
        # 没有实例也要留一行证据吗？不需要 —— watch.log 是"每台机器一行"的
        # 采样流，没有机器就没有可观测对象。instances.py 用 mtime 判断新鲜度，
        # 无实例期间页面本来就该是空的。
        say("没有实例，不写采样")
        return []

    if dry_run:
        for ln in lines:
            say(f"[dry-run] {ln}")
        return lines

    WATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with WATCH_LOG.open("a", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(ln + "\n")
    say(f"已采样 {len(lines)} 台实例")
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="sampler", description="实例运行采样（只读，不停机）—— 实例页/watchdog 的数据源")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写 watch.log")
    a = ap.parse_args(argv)
    sample(dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
