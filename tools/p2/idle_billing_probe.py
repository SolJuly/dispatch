#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""停机实例计费观察器：验证「关机后只留 100GB 系统盘 → ¥0/月」是否成立。

背景
----
智云文档写「关机后 CPU/GPU/内存停止收费，只保留云盘」+「系统盘 ≤100GB 免费」，
按这两条，一台停机实例应当是 **¥0/月**，而它的磁盘上就是完整环境
（ComfyUI + 10 个 custom_nodes + torch），**不需要花 ¥16.5/月 存镜像**。

这个推论很诱人，但没实证。M5 那次停机只待了 9 分钟就销毁，看不到一天的费用。
本工具每小时采样一次，把「实例状态 + 余额」记进独立账本，用来反证/证实。

**为什么单独一个账本**：不去污染主 ledger 的 settle 推断。这里记录的是
「指定实例在停机状态下，余额是否变化」，是一个受控实验。

判定逻辑
--------
    停机期间余额下降 Δ，且期间无其它实例在跑  →  停机计费 = Δ
    停机期间余额下降 Δ，但期间有作业开销 S    →  停机计费 = Δ - S
    停机期间余额不变                          →  停机计费 = 0  ✅ 方案成立

结论要等至少 24 小时（覆盖一次 01:10 日结）才有意义。

用法
----
    python3 tools/p2/idle_billing_probe.py start --uid uhost-xxx
    python3 tools/p2/idle_billing_probe.py sample          # cron 每小时一次
    python3 tools/p2/idle_billing_probe.py report          # 看结论
    python3 tools/p2/idle_billing_probe.py stop           # 结束实验
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROBE = ROOT / ".compshare" / "idle_probe.jsonl"
MAIN_LEDGER = ROOT / ".compshare" / "ledger.jsonl"
# 低于此额度的变化视为噪声
EPS = 0.005


def _now() -> dict:
    return {"ts": int(time.time()),
            "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def _append(rec: dict) -> None:
    PROBE.parent.mkdir(parents=True, exist_ok=True)
    with PROBE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load() -> list[dict]:
    if not PROBE.exists():
        return []
    out = []
    for line in PROBE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _balance() -> float | None:
    try:
        from compshare.cs import CS, CompShareError
    except Exception:
        return None
    try:
        return float(CS().balance().get("AmountAvailable") or 0)
    except CompShareError:
        return None
    except Exception:
        return None


def _instance(uid: str) -> dict | None:
    try:
        from compshare.cs import CS
        return CS().get(uid)
    except Exception:
        return None


def _other_instances_active(uid: str) -> list[str]:
    """除观测实例外，还有哪些实例在 Running（会污染余额差）。"""
    try:
        from compshare.cs import CS
        cs = CS()
        act = []
        for i in cs.instances():
            if i.get("UHostId") == uid:
                continue
            st = (i.get("State") or "").lower()
            if st in ("running", "starting", "rebooting", "installing"):
                act.append(i.get("UHostId") or "?")
        return act
    except Exception:
        return []


def cmd_start(a) -> int:
    inst = _instance(a.uid)
    if not inst:
        print(f"✗ 找不到实例 {a.uid}")
        return 1
    rec = {**_now(), "event": "start", "uid": a.uid,
           "name": inst.get("Name"), "gpu": inst.get("GpuType"),
           "disk_gb": inst.get("TotalDiskSpace"), "state": inst.get("State"),
           "balance": _balance(), "note": a.note}
    _append(rec)
    print(f"✓ 开始观测 {a.uid} ({inst.get('Name')})")
    print(f"  状态   {inst.get('State')}")
    print(f"  磁盘   {inst.get('TotalDiskSpace')} GB")
    print(f"  余额   ¥{rec['balance']}")
    print(f"  账本   {PROBE}")
    print("\n下一步：每小时跑一次 sample（加进 cron）")
    return 0


def cmd_sample(a) -> int:
    rows = load()
    starts = [r for r in rows if r.get("event") == "start"]
    ends = {r.get("uid") for r in rows if r.get("event") == "stop"}
    if not starts:
        print("没有进行中的实验（先跑 start --uid ...）")
        return 0
    active = [s for s in starts if s.get("uid") not in ends]
    if not active:
        print("实验已结束")
        return 0

    bal = _balance()
    for st in active:
        uid = st["uid"]
        inst = _instance(uid)
        state = (inst or {}).get("State") if inst else "MISSING"
        others = _other_instances_active(uid)
        rec = {**_now(), "event": "sample", "uid": uid, "state": state,
               "balance": bal, "others_running": others,
               "disk_gb": (inst or {}).get("TotalDiskSpace")}
        _append(rec)
        flag = "" if not others else f"  ⚠ 有 {len(others)} 台其它实例在跑，余额差会混入作业费"
        print(f"{rec['at']}  {uid}  state={state}  bal=¥{bal}{flag}")
    return 0


def cmd_report(a) -> int:
    rows = load()
    if not rows:
        print("账本为空")
        return 1
    starts = [r for r in rows if r.get("event") == "start"]
    samples = [r for r in rows if r.get("event") == "sample"]
    if not starts:
        print("没有 start 记录")
        return 1
    st = starts[-1]
    uid = st["uid"]
    mine = [s for s in samples if s.get("uid") == uid]
    print("═" * 60)
    print(f"停机计费实验 · {uid} ({st.get('name')})")
    print(f"磁盘 {st.get('disk_gb')} GB · 卡 {st.get('gpu')}")
    print(f"开始 {st.get('at')}  余额 ¥{st.get('balance')}")
    print("═" * 60)
    if not mine:
        print("\n还没有采样。先加 cron：每小时跑一次 sample")
        return 0

    first, last = mine[0], mine[-1]
    hours = (last["ts"] - st["ts"]) / 3600
    print(f"\n采样 {len(mine)} 次，跨度 {hours:.1f} 小时")
    print(f"{'时刻':<21}{'状态':<12}{'余额':>9}  其它实例")
    prev = None
    dirty = 0
    for s in mine:
        d = "" if prev is None or s["balance"] is None else f"  ({s['balance']-prev:+.2f})"
        others = s.get("others_running") or []
        if others:
            dirty += 1
        print(f"{s['at']:<21}{str(s.get('state')):<12}{s['balance'] if s['balance'] is not None else '?':>9}"
              f"{d}  {len(others) or ''}")
        prev = s["balance"]

    if st.get("balance") is None or last.get("balance") is None:
        print("\n余额缺失，无法判定")
        return 0
    delta = round(st["balance"] - last["balance"], 2)
    print("\n" + "─" * 60)
    print(f"余额变化  ¥{st['balance']:.2f} → ¥{last['balance']:.2f}   Δ = -¥{delta:.2f}")
    if dirty:
        print(f"⚠ {dirty}/{len(mine)} 次采样期间有其它实例在跑，差额可能混入作业费")
        print("  精确判定需要一段「只有这台停机实例」的窗口")
    print()
    if hours < 24:
        print(f"⏳ 才 {hours:.1f} 小时，不足 24h。要覆盖一次 01:10 日结才准。")
    elif delta <= EPS:
        print("✅ 停机实例 ¥0 —— 方案成立")
        print("   可以拿它当环境模板，省掉 ¥16.5/月 的镜像存储费")
    else:
        per_day = delta / hours * 24
        print(f"❌ 停机仍收费：{hours:.1f}h 掉 ¥{delta:.2f} → ¥{per_day:.2f}/天 = ¥{per_day*30:.0f}/月")
        print("   方案作废，继续用镜像")
    return 0


def cmd_stop(a) -> int:
    rows = load()
    starts = [r for r in rows if r.get("event") == "start"]
    ends = {r.get("uid") for r in rows if r.get("event") == "stop"}
    active = [s for s in starts if s.get("uid") not in ends]
    if not active:
        print("没有进行中的实验")
        return 0
    for s in active:
        _append({**_now(), "event": "stop", "uid": s["uid"], "balance": _balance()})
        print(f"✓ 结束观测 {s['uid']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="停机实例计费观察器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start"); p.add_argument("--uid", required=True)
    p.add_argument("--note", default=""); p.set_defaults(fn=cmd_start)

    p = sub.add_parser("sample"); p.set_defaults(fn=cmd_sample)
    p = sub.add_parser("report"); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("stop"); p.set_defaults(fn=cmd_stop)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
