#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当前余额写进账本（cron 每 10 分钟一次），并在余额下降时补一条 settle 事件。

为什么要有 settle：平台的「镜像存储费」不产生任何 API 事件，只在每天 01:10
做一次余额阶跃。只记 balance 的话，这种支出在账本里完全隐形——
2026-11-08 的成本分析就因此漏算了 ¥16.25 的镜像费（13 天累计）。

settle 记录的是「相邻两次余额采样之间的下降额」，所以它是**毛额**，
包含同期作业开销。区分办法：
  - 有 job 事件覆盖该时间段 → 差额主要是作业
  - 无任何 create/job/terminate 事件 → 差额只可能是日结费用（镜像存储等）
要精确拆开，用 scripts/attribute_settle.py（按事件归因）。

用法：
    python3 tools/cost_snapshot.py                # 采样一次
    python3 tools/cost_snapshot.py --dry-run      # 只打印不写
    python3 tools/cost_snapshot.py --no-settle    # 只写 balance（旧行为）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from compshare.cs import CS, CompShareError  # noqa: E402

LEDGER = ROOT / ".compshare" / "ledger.jsonl"
# 低于这个额度的变化视为浮点噪声（平台返回两位小数，正常抖动 < ¥0.005）
SETTLE_EPS = 0.005
# 单次采样间隔上限：超过这个时长说明 cron 停过，差额不可归因到某一天，
# 仍然记录但打上 gap 标记，供回填脚本跳过
MAX_GAP_S = 3 * 3600


def _last_balance(path: Path) -> dict | None:
    """从文件尾部倒着找最后一条 balance，避免把整个账本读进内存。"""
    if not path.exists():
        return None
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        # 尾部 64KB 足够覆盖几百条记录；不够就扩大
        chunk = 65536
        while True:
            start = max(0, size - chunk)
            f.seek(start)
            block = f.read(size - start)
            lines = [ln for ln in block.split(b"\n") if ln.strip()]
            # 若没读到文件头，第一条可能是半行，丢掉
            if start > 0 and lines:
                lines = lines[1:]
            for raw in reversed(lines):
                try:
                    r = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                if r.get("event") == "balance" and r.get("balance") is not None:
                    return r
            if start == 0:
                return None
            chunk *= 4  # 扩大窗口重试


def _has_activity(path: Path, since_ts: int) -> list[str]:
    """返回 since_ts 之后出现过的事件类型（去重），用于归因提示。"""
    kinds: set[str] = set()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if int(r.get("ts") or 0) >= since_ts and r.get("event") not in (None, "balance"):
                    kinds.add(str(r.get("event")))
    except OSError:
        return []
    return sorted(kinds)


def build_records(bal: float, prev: dict | None, *, now: float | None = None,
                  activity: list[str] | None = None) -> list[dict]:
    """生成待写记录（纯函数，便于单测）。

    返回 [balance_rec, settle_rec?]。settle 只在余额**下降**且超过 EPS 时产生。
    """
    now = now if now is not None else time.time()
    at = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
    # 平台返回两位小数，但浮点减法会带出 8.459999999999999 这种尾巴
    bal = round(float(bal), 4)
    recs = [{"ts": int(now), "at": at, "event": "balance", "balance": bal}]
    if not prev:
        return recs
    try:
        prev_bal = float(prev.get("balance"))
        prev_ts = int(prev.get("ts") or 0)
    except (TypeError, ValueError):
        return recs
    delta = round(prev_bal - bal, 4)
    if delta <= SETTLE_EPS:
        return recs
    gap = int(now) - prev_ts if prev_ts else 0
    kinds = activity if activity is not None else []
    rec = {
        "ts": int(now), "at": at, "event": "settle",
        "amount": delta, "from": round(prev_bal, 4), "to": round(bal, 4),
        "prev_at": prev.get("at"), "gap_s": gap,
        # 归因线索：有没有人在这段时间动过这台机器
        "activity": kinds,
        "attribution": _attribute(kinds),
    }
    if gap > MAX_GAP_S:
        rec["gap"] = True
    recs.append(rec)
    return recs


# 归因分类：把「钱花在什么上」分成 4 类，而不是二元 job/idle。
#
# ## 为什么必须改
#
# 原来只有两种：`job`（有 job/create/terminate/cost 事件）或 `idle`（其余全部）。
# 但账本里 `ssh_exec` 有 1731 条，是 `job`（83 条）的 20 倍 —— 手动排障、
# 验证工作流、调环境，全程都在 ssh，却一条都不算"有活动"。
#
# 实测后果（2026-11-12）：一天做了 4 轮真机验证，花了 ¥4.68，
# 其中 `job` 只有 ¥0.18，其余全被记成 `idle`。
# 看起来像"96% 空转"，实际是实验开销。
# 交接单里那个"65% 的钱花在空转上"的结论，很可能也被同样高估了。
#
# ## 四类的区别（关系到能不能省）
#
#   job      跑作业            该花的，是产出
#   operator 人工操作（ssh/拉取）  该花的，是调试/实验成本
#   provision 建机/停机/定时关机   该花的，是生命周期开销
#   idle     真的什么都没干       **唯一可省的**
#
# `01:10` 每天固定的镜像存储费会落进 `idle`（那段时间确实没活动），
# 但它是**必然支出**，不是浪费 —— 靠 `gap_s` 和金额能区分（固定 ¥0.55 左右）。
_ATTR_JOB = ("job", "cost")
_ATTR_OPERATOR = ("ssh_exec", "pull")
_ATTR_PROVISION = ("create", "terminate", "stop", "schedule_stop")


def _attribute(kinds: list[str]) -> str:
    """按"谁在动这台机器"归类。多类同时出现时按 job > operator > provision 取。"""
    ks = set(kinds or ())
    if ks & set(_ATTR_JOB):
        return "job"
    if ks & set(_ATTR_OPERATOR):
        return "operator"
    if ks & set(_ATTR_PROVISION):
        return "provision"
    return "idle"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="采样余额，并在下降时补一条 settle 事件")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写账本")
    ap.add_argument("--no-settle", action="store_true", help="只写 balance（旧行为）")
    ap.add_argument("--ledger", default=str(LEDGER), help="账本路径（测试用）")
    a = ap.parse_args(argv)
    path = Path(a.ledger)

    try:
        bal = float(CS().balance().get("AmountAvailable") or 0)
    except CompShareError as e:
        print("balance failed:", e)
        return 1

    prev = _last_balance(path)
    activity = _has_activity(path, int(prev.get("ts") or 0)) if prev else []
    recs = build_records(bal, prev, activity=activity)
    if a.no_settle:
        recs = [r for r in recs if r["event"] == "balance"]

    if a.dry_run:
        for r in recs:
            print(json.dumps(r, ensure_ascii=False))
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print("balance", bal)
    for r in recs:
        if r["event"] == "settle":
            print(f"settle  -¥{r['amount']:.2f}  ({r['prev_at']} → {r['at']}, "
                  f"归因={r['attribution']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
