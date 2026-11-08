#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从历史 ledger 回填 settle 事件（一次性迁移）。

背景：`tools/cost_snapshot.py` 原本只记 balance，导致「平台日结的镜像存储费」
在账本里完全隐形——它不产生 create/terminate/job 事件，只表现为每天 01:10
的一次余额阶跃。2026-11-08 的成本分析因此漏算 ¥16.25（13 天累计）。

本脚本把历史 balance 相邻差值补写成 settle 事件，并对每一笔做归因：
  - 期间有 job/create/terminate 事件 → 差额含作业开销（attribution=job）
  - 期间完全无活动               → 只可能是日结费用（attribution=idle）

幂等：已存在的 settle 位置不会重复写入（按 (at, event) 去重）。

用法：
    python3 scripts/backfill_settle.py                  # 预览
    python3 scripts/backfill_settle.py --apply          # 写入
    python3 scripts/backfill_settle.py --apply --ledger /tmp/x.jsonl
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / ".compshare" / "ledger.jsonl"
SETTLE_EPS = 0.005          # 低于此值视为浮点噪声
JOB_EVENTS = {"job", "create", "terminate", "cost", "start", "stop", "schedule_stop"}


def load(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def build_settles(rows: list[dict]) -> list[dict]:
    """扫描 balance 序列，为每段下降生成 settle 记录（纯函数）。"""
    # 按 ts 排序保证时序（账本是 append-only，正常已有序）
    balances = [r for r in rows if r.get("event") == "balance" and r.get("balance") is not None]
    other = [r for r in rows if r.get("event") not in (None, "balance", "settle")]

    out: list[dict] = []
    for i in range(1, len(balances)):
        prev, cur = balances[i - 1], balances[i]
        try:
            delta = round(float(prev["balance"]) - float(cur["balance"]), 4)
        except (TypeError, ValueError, KeyError):
            continue
        if delta <= SETTLE_EPS:
            continue
        # 归因：该时间窗内出现过哪些事件
        t0, t1 = int(prev.get("ts") or 0), int(cur.get("ts") or 0)
        kinds = sorted({
            str(r.get("event")) for r in other
            if t0 <= int(r.get("ts") or 0) <= t1
        })
        out.append({
            "ts": t1,
            "at": cur.get("at"),
            "event": "settle",
            "amount": delta,
            "from": round(float(prev["balance"]), 4),
            "to": round(float(cur["balance"]), 4),
            "prev_at": prev.get("at"),
            "gap_s": t1 - t0 if t0 else 0,
            "activity": kinds,
            "attribution": "job" if (set(kinds) & JOB_EVENTS) else "idle",
            "backfilled": True,
        })
    return out


def merge(rows: list[dict], settles: list[dict]) -> tuple[list[dict], int]:
    """把 settle 插到对应 balance 之后；已存在的 (at) 跳过。返回 (新行, 新增数)。"""
    existing = {r.get("at") for r in rows if r.get("event") == "settle"}
    todo = [s for s in settles if s.get("at") not in existing]
    if not todo:
        return rows, 0
    by_at: dict[str, list[dict]] = {}
    for s in todo:
        by_at.setdefault(str(s.get("at")), []).append(s)

    out: list[dict] = []
    for r in rows:
        out.append(r)
        if r.get("event") == "balance":
            for s in by_at.pop(str(r.get("at")), []):
                out.append(s)
    # 兜底：没找到对应 balance 的（异常账本）直接追加到末尾
    for leftover in by_at.values():
        out.extend(leftover)
    return out, len(todo)


def summarize(settles: list[dict]) -> dict:
    idle = [s for s in settles if s.get("attribution") == "idle"]
    return {
        "n": len(settles),
        "total": round(sum(s["amount"] for s in settles), 2),
        "idle_n": len(idle),
        "idle_total": round(sum(s["amount"] for s in idle), 2),
        "job_total": round(sum(s["amount"] for s in settles if s["attribution"] == "job"), 2),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把历史 balance 差值回填成 settle 事件")
    ap.add_argument("--apply", action="store_true", help="写入（默认只预览）")
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--no-backup", action="store_true", help="不备份原文件")
    a = ap.parse_args(argv)
    path = Path(a.ledger)

    rows = load(path)
    if not rows:
        print(f"账本为空或不存在：{path}")
        return 1

    settles = build_settles(rows)
    st = summarize(settles)
    print(f"账本 {len(rows)} 行，检出 {st['n']} 笔余额下降，合计 ¥{st['total']:.2f}")
    print(f"  归因=job  ¥{st['job_total']:.2f}（含作业开销）")
    print(f"  归因=idle ¥{st['idle_total']:.2f}（无活动，只能日结/存储费）")
    print()
    print("最近 15 笔：")
    for s in settles[-15:]:
        tag = "空闲" if s["attribution"] == "idle" else "有作业"
        print(f"  {s['at']}  -¥{s['amount']:.2f}  [{tag}] {s['prev_at']} → {s['at']}")

    merged, added = merge(rows, settles)
    if not a.apply:
        print(f"\n预览模式：将新增 {added} 条 settle。加 --apply 写入。")
        return 0
    if added == 0:
        print("\n无新增（已全部回填过）。")
        return 0

    if not a.no_backup:
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)
        print(f"\n已备份 → {bak}")
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)
    print(f"已写入 {added} 条 settle → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
