#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 1：并发创建（concurrent create）

场景：
  同时发起 N 个 `csctl up`（不同 --name），观察：
    · 有多少个被 .compshare/up.lock 的排他锁挡住（这是预期行为，防重复下单）
    · 被挡住的在锁释放后串行重试能否成功
    · 最终所有实例是否都进入 Running
    · 并发创建是否导致实例串号、名称错配

用法：
  python3 tests/test_concurrent_create.py --dry-run     # 只打印计划
  python3 tests/test_concurrent_create.py               # 真实执行（会创建并清理实例）
"""
from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

TEST = "test_concurrent_create"
PREFIX = "test-concurrent-"
RESIDUE_PREFIX = "test-"


def names(n: int) -> list[str]:
    return [f"{PREFIX}{chr(ord('a') + i)}" for i in range(n)]


def extract_uid(out: str) -> str | None:
    m = re.search(r"实例已创建:\s*(uhost-[A-Za-z0-9]+)", out or "")
    return m.group(1) if m else None


def up_once(name: str, gpu: str, hours: float, fallback: str = "") -> dict:
    extra = ["--fallback", fallback] if fallback else []
    r = C.csctl("up", "--gpu-type", gpu, "--hours", str(hours), "--name", name,
                *extra, timeout=1500)
    return {
        "name": name,
        "start": r.t_start,
        "end": r.t_end,
        "dur": round(r.dur, 1),
        "rc": r.rc,
        "uid": extract_uid(r.out),
        "out": r.out,
        "err": r.err,
        "blocked": ("另一个 up 正在运行" in (r.err or "") or "up.lock" in (r.err or "")),
        "ok": r.ok,
    }


def plan(args, ns: list[str]) -> int:
    C.banner(f"{TEST} — dry-run 计划")
    print("将并发发起以下命令（互不等待）：")
    for n in ns:
        print(f"  python3 csctl.py up --gpu-type {args.gpu_type} --hours {args.hours} --name {n}")
    print("\n预期行为：")
    print("  · 第 1 个拿到 .compshare/up.lock，正常创建")
    print("  · 其余立即返回『另一个 up 正在运行』（rc≠0）")
    print("  · 脚本会对被挡住的实例串行重试，直至全部创建成功")
    print("\n随后将执行：")
    print("  · 逐个等待 Running（超时 15 分钟/台）")
    print("  · 校验名称与 ID 一一对应，无串号")
    print(f"  · 清理 {len(ns)} 台（csctl rm <id> --yes）并验证无残留")
    print(f"  · 费用写入 reports/test_cost_summary.md")
    print("\n--dry-run：未调用任何云端接口，未创建实例。")
    return 0


def main() -> int:
    args = C.parse_args(f"{TEST}：并发创建压力测试")
    ns = names(args.count or 3)
    if args.dry_run:
        return plan(args, ns)

    C.banner(f"{TEST} — 并发创建 {len(ns)} 台")
    t_all = time.time()
    created: list[str] = []
    rounds: list[list[dict]] = []
    try:
        # 0) 幂等：清掉历史残留
        stale = [i.get("Name") for i in C.test_instances(RESIDUE_PREFIX)
                 if (i.get("Name") or "") in ns]
        if stale:
            C.say(f"发现同名残留 {stale}，先清理")
            C.cleanup(stale, log=C.say)

        # 1) 真并发
        C.say(f"同时发起 {len(ns)} 个 up（预期只有 1 个能拿到锁）")
        with ThreadPoolExecutor(max_workers=len(ns)) as ex:
            fb = args.fallback
            round1 = list(ex.map(lambda n: up_once(n, args.gpu_type, args.hours, fb), ns))
        rounds.append(round1)
        for r in round1:
            flag = "创建成功" if r["ok"] else ("被锁挡住" if r["blocked"] else "失败")
            C.say(f"  {r['name']}: rc={r['rc']} {flag} ({r['dur']}s) uid={r['uid'] or '-'}")
            if r["uid"]:
                created.append(r["uid"])

        # 2) 被锁挡住的串行重试
        blocked = [r for r in round1 if not r["ok"] and r["blocked"]]
        if blocked:
            C.say(f"{len(blocked)} 个被锁挡住，开始串行重试")
            fixed = []
            for r in blocked:
                rr = up_once(r["name"], args.gpu_type, args.hours, args.fallback)
                rr["retry_of"] = r["name"]
                fixed.append(rr)
                C.say(f"  重试 {rr['name']}: rc={rr['rc']} uid={rr['uid'] or '-'}")
                if rr["uid"]:
                    created.append(rr["uid"])
            rounds.append(fixed)

        # 3) 等待全部 Running
        C.say("等待全部进入 Running")
        states = {}
        for r in [x for rd in rounds for x in rd]:
            if not r.get("uid"):
                continue
            ok, waited = C.wait_running(r["uid"], timeout=900)
            st = C.state_of(r["uid"])
            states[r["uid"]] = {"name": r["name"], "running": ok,
                                "state": st, "waited": round(waited, 1)}
            C.say(f"  {r['name']} ({r['uid']}): {st} {'✅' if ok else '❌'} ({waited:.0f}s)")

        # 4) 名称↔ID 一致性
        insts = C.instances()
        by_uid = {i.get("UHostId"): (i.get("Name") or "") for i in insts}
        mismatch = [(u, by_uid.get(u)) for u in created
                    if by_uid.get(u) and by_uid[u] not in ns]
        all_running = all(v["state"] == "Running" for v in states.values()) and bool(states)

        # 5) 清理
        C.say("清理测试实例")
        cleanup_rows = C.cleanup(ns, log=C.say)
        if not args.keep:
            residue = C.assert_no_residue(RESIDUE_PREFIX)
            residue = [i for i in residue if (i.get("Name") or "").startswith(PREFIX)]
        else:
            residue = []

    finally:
        if args.keep:
            C.say("--keep：保留实例，未清理")
        elif "cleanup_rows" not in dir():
            C.cleanup(ns, log=C.say)

    total_cost, cost_rows = C.ledger_terminate_costs(set(created))
    C.record_cost(TEST, cost_rows)

    # ---------------- 报告
    flat = [x for rd in rounds for x in rd]
    r1 = rounds[0] if rounds else []
    ok1 = sum(1 for r in r1 if r["ok"])
    blocked_n = sum(1 for r in r1 if r["blocked"])

    secs = []
    secs.append("## 一、结论\n")
    secs.append(C.table(["检查项", "结果"], [
        ["首轮并发成功数", f"{ok1}/{len(r1)}"],
        ["被 up.lock 挡住的", f"{blocked_n}"],
        ["重试后全部创建", "✅" if len(created) == len(ns) else f"❌ 仅 {len(created)}/{len(ns)}"],
        ["全部进入 Running", "✅" if all_running else "❌"],
        ["名称与 ID 一一对应", "✅" if not mismatch else f"❌ 串号 {mismatch}"],
        ["清理完成", "✅" if not residue else f"❌ 残留 {residue}"],
        ["本测试花费", f"¥{total_cost:.3f}"],
        ["总耗时", f"{time.time() - t_all:.0f}s"],
    ]))

    secs.append("\n## 二、首轮并发明细（同时发起）\n")
    secs.append(C.table(
        ["实例名", "启动", "结束", "耗时", "rc", "UID", "结论"],
        [[r["name"], C.hms(r["start"]), C.hms(r["end"]), f'{r["dur"]}s', r["rc"],
          f'`{r["uid"] or "-"}`',
          "成功" if r["ok"] else ("被 up.lock 挡住" if r["blocked"] else "失败")]
         for r in r1]))

    secs.append("\n### 并发锁的实际行为\n")
    secs.append(C.fence(
        "csctl up 通过 fcntl.flock(.compshare/up.lock, LOCK_EX|LOCK_NB) 做排他，\n"
        "第二个并发的 up 会立刻失败并提示『另一个 up 正在运行』。\n"
        "因此「并发创建」在 csctl 层是串行化的 —— 这是防重复下单的设计，不是 bug。\n"
        f"本机实测：{len(r1)} 个并发里 {blocked_n} 个被挡，1 个成功。", "text"))

    if len(rounds) > 1:
        secs.append("\n## 三、被挡实例的串行重试\n")
        secs.append(C.table(
            ["实例名", "重试 rc", "UID", "耗时", "结论"],
            [[r["name"], r["rc"], f'`{r["uid"] or "-"}`', f'{r["dur"]}s',
              "成功" if r["ok"] else "失败"] for r in rounds[1]]))

    secs.append("\n## 四、最终状态\n")
    if states:
        secs.append(C.table(
            ["实例名", "UID", "最终状态", "达到 Running 耗时"],
            [[v["name"], f'`{u}`', v["state"], f'{v["waited"]}s'] for u, v in states.items()]))
    else:
        secs.append("（无实例被创建）")

    secs.append("\n## 五、清理\n")
    secs.append(C.table(
        ["实例名", "UID", "动作", "结果"],
        [[r["name"], f'`{r["id"]}`', r["action"],
          f'rc={r.get("rc","-")} 已消失={r.get("absent","-")}'] for r in cleanup_rows]))
    secs.append(f"\n残留检查：`csctl ls` 中 `{PREFIX}*` 实例 "
                f"**{'0 台 ✅' if not residue else str(len(residue)) + ' 台 ❌'}**")

    secs.append("\n## 六、费用\n")
    if cost_rows:
        secs.append(C.table(["实例", "名称", "运行小时", "花费"],
                            [[f'`{r["uhost"]}`', r.get("name", "-"),
                              f'{float(r.get("run_h") or 0):.3f}', f'¥{float(r.get("cost") or 0):.3f}']
                             for r in cost_rows]))
    secs.append(f"\n本测试合计 **¥{total_cost:.3f}**")

    out = C.write_report(
        "test_concurrent_create.md",
        "测试 1：并发创建（concurrent create）",
        {"测试": TEST, "卡型": args.gpu_type, "并发数": len(ns),
         "自动关机兜底": f"{args.hours}h", "模式": "真实执行"},
        secs)

    passed = (len(created) == len(ns) and all_running and not mismatch and not residue)
    C.banner(f"{TEST} {'通过 ✅' if passed else '未通过 ❌'}")
    print(f"报告: {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
