#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 2：快速销毁（rapid destroy）

场景：
  A) 发起 `csctl up`，在「实例已创建」刚打印出来的瞬间就 `csctl stop`
     —— 此时实例还在 Install/Initializing，考验瞬态下的关机请求
  B) 发起 `csctl up`，在创建瞬间就 `csctl rm --yes`
     —— 考验瞬态下的删除请求（最容易撞 "UHost is in the task"）

验收：
  · 两个场景都要有明确结果（成功 / 被拒 / 超时），并记录完整输出
  · 结束后 csctl ls 里不得残留 test-rapid-* 实例

用法：
  python3 tests/test_rapid_destroy.py --dry-run
  python3 tests/test_rapid_destroy.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

TEST = "test_rapid_destroy"
PREFIX = "test-rapid-"
RESIDUE_PREFIX = "test-"


def plan(args) -> int:
    C.banner(f"{TEST} — dry-run 计划")
    print("场景 A：创建后立即关机")
    print(f"  python3 csctl.py up --gpu-type {args.gpu_type} --hours {args.hours} --name {PREFIX}stop   (后台启动)")
    print("  # 一旦 stdout 出现『实例已创建: uhost-xxx』，立刻：")
    print("  python3 csctl.py stop <uid>")
    print("  # 预期：可能被拒（实例仍在 Install），记录其 stderr 原文")
    print()
    print("场景 B：创建后立即删除")
    print(f"  python3 csctl.py up --gpu-type {args.gpu_type} --hours {args.hours} --name {PREFIX}rm     (后台启动)")
    print("  python3 csctl.py rm <uid> --yes")
    print("  # 预期：可能撞『UHost is in the task』，脚本会重试 3 次（间隔 10 秒）")
    print()
    print("收尾：")
    print(f"  · 两个场景的实例一律 rm --yes 兜底清理")
    print("  · csctl ls 校验无 test-rapid-* 残留")
    print("  · 费用写入 reports/test_cost_summary.md")
    print("\n--dry-run：未调用任何云端接口，未创建实例。")
    return 0


def _up_and_grab_uid(name: str, gpu: str, hours: float, fallback: str = "",
                     timeout: int = 600):
    """后台起 up，抓到 uid 就返回（不等就绪）。"""
    extra = ["--fallback", fallback] if fallback else []
    p = C.popen_csctl("up", "--gpu-type", gpu, "--hours", str(hours), "--name", name, *extra)
    w = C.StreamWatch(p)
    uid = w.wait_uid(timeout=timeout)
    return p, w, uid


def scenario_stop(args) -> dict:
    name = f"{PREFIX}stop"
    C.say(f"[A] 创建 {name} 并在创建瞬间关机")
    p, w, uid = _up_and_grab_uid(name, args.gpu_type, args.hours, args.fallback)
    rec = {"scenario": "A", "action": "stop", "name": name, "uid": uid}
    if not uid:
        rec["result"] = "up 未产出实例"
        rec["output"] = w.text()[-1200:]
        p.kill()
        return rec

    state_at = C.state_of(uid)
    rec["state_at_action"] = state_at
    C.say(f"  uid={uid} 当前状态={state_at} → 立即 stop")
    t0 = time.time()
    r = C.csctl("stop", uid, timeout=900)
    rec.update({"rc": r.rc, "dur": round(time.time() - t0, 1),
                "out": r.out.strip()[:600], "err": r.err.strip()[:600]})
    rec["outcome"] = "成功" if r.rc == 0 else "被拒/失败"
    C.say(f"  stop rc={r.rc} ({rec['dur']}s) → {rec['outcome']}")

    # up 进程此时可能因实例被关机而失败退出
    try:
        p.wait(timeout=180)
    except Exception:
        p.kill()
    rec["up_rc"] = p.returncode
    rec["state_after"] = C.state_of(uid)
    return rec


def scenario_rm(args) -> dict:
    name = f"{PREFIX}rm"
    C.say(f"[B] 创建 {name} 并在创建瞬间删除")
    p, w, uid = _up_and_grab_uid(name, args.gpu_type, args.hours, args.fallback)
    rec = {"scenario": "B", "action": "rm", "name": name, "uid": uid}
    if not uid:
        rec["result"] = "up 未产出实例"
        rec["output"] = w.text()[-1200:]
        p.kill()
        return rec

    state_at = C.state_of(uid)
    rec["state_at_action"] = state_at
    C.say(f"  uid={uid} 当前状态={state_at} → 立即 rm --yes")

    attempts = []
    for i in range(3):
        t0 = time.time()
        r = C.csctl("rm", uid, "--yes", timeout=900)
        attempts.append({"try": i + 1, "rc": r.rc, "dur": round(time.time() - t0, 1),
                         "out": r.out.strip()[:400], "err": r.err.strip()[:400]})
        C.say(f"  rm 第 {i+1} 次: rc={r.rc}")
        if r.rc == 0:
            break
        time.sleep(10)
    rec["attempts"] = attempts
    rec["outcome"] = "成功" if attempts and attempts[-1]["rc"] == 0 else "失败"

    try:
        p.wait(timeout=180)
    except Exception:
        p.kill()
    rec["up_rc"] = p.returncode
    ok, waited = C.wait_absent(uid, timeout=300)
    rec["absent"] = ok
    rec["absent_wait"] = round(waited, 1)
    C.say(f"  rm 结果: {rec['outcome']}，实例消失={ok}")
    return rec


def main() -> int:
    args = C.parse_args(f"{TEST}：快速销毁压力测试")
    if args.dry_run:
        return plan(args)

    C.banner(f"{TEST} — 快速销毁")
    t_all = time.time()
    recs: list[dict] = []
    try:
        # 幂等：先清残留
        stale = [i.get("Name") for i in C.test_instances(RESIDUE_PREFIX)
                 if (i.get("Name") or "").startswith(PREFIX)]
        if stale:
            C.say(f"发现残留 {stale}，先清理")
            C.cleanup(stale, log=C.say)

        recs.append(scenario_stop(args))
        recs.append(scenario_rm(args))
    finally:
        if not args.keep:
            C.say("兜底清理")
            C.cleanup([f"{PREFIX}stop", f"{PREFIX}rm"], log=C.say)

    residue = [] if args.keep else [
        i for i in C.test_instances(RESIDUE_PREFIX)
        if (i.get("Name") or "").startswith(PREFIX)]
    created = {r["uid"] for r in recs if r.get("uid")}
    total_cost, cost_rows = C.ledger_terminate_costs(created)
    C.record_cost(TEST, cost_rows)

    # ---------------- 报告
    secs = ["## 一、结论\n"]
    a = next((r for r in recs if r["scenario"] == "A"), {})
    b = next((r for r in recs if r["scenario"] == "B"), {})
    secs.append(C.table(["检查项", "结果"], [
        ["A. 创建后立即 stop", f"{a.get('outcome','-')}（rc={a.get('rc','-')}）"],
        ["B. 创建后立即 rm", f"{b.get('outcome','-')}（rc={b.get('attempts',[{}])[-1].get('rc','-') if b.get('attempts') else '-'}）"],
        ["无残留实例", "✅" if not residue else f"❌ {[i.get('Name') for i in residue]}"],
        ["本测试花费", f"¥{total_cost:.3f}"],
        ["总耗时", f"{time.time()-t_all:.0f}s"],
    ]))

    secs.append("\n## 二、场景 A：创建后立即关机\n")
    if a.get("uid"):
        secs.append(C.table(["项", "值"], [
            ["实例", f'`{a["uid"]}`（{a["name"]}）'],
            ["动手瞬间的状态", a.get("state_at_action", "-")],
            ["stop rc", a.get("rc", "-")],
            ["stop 耗时", f'{a.get("dur", "-")}s'],
            ["up 进程退出码", a.get("up_rc", "-")],
            ["关机后状态", a.get("state_after", "-")],
        ]))
        secs.append("\n**stop 输出**\n")
        secs.append(C.fence((a.get("out") or "") + ("\n" + a["err"] if a.get("err") else ""), "text"))
    else:
        secs.append(f"未能拿到实例：{a.get('result')}")
        secs.append(C.fence(a.get("output", ""), "text"))

    secs.append("\n## 三、场景 B：创建后立即删除\n")
    if b.get("uid"):
        secs.append(C.table(["项", "值"], [
            ["实例", f'`{b["uid"]}`（{b["name"]}）'],
            ["动手瞬间的状态", b.get("state_at_action", "-")],
            ["尝试次数", len(b.get("attempts", []))],
            ["最终结果", b.get("outcome", "-")],
            ["实例是否消失", f'{b.get("absent")}（等了 {b.get("absent_wait")}s）'],
            ["up 进程退出码", b.get("up_rc", "-")],
        ]))
        secs.append("\n**每次 rm 的返回**\n")
        secs.append(C.table(
            ["第几次", "rc", "耗时", "输出摘要"],
            [[x["try"], x["rc"], f'{x["dur"]}s',
              ((x.get("out") or "") + (x.get("err") or "")).replace("\n", " ")[:160] or "-"]
             for x in b.get("attempts", [])]))
    else:
        secs.append(f"未能拿到实例：{b.get('result')}")
        secs.append(C.fence(b.get("output", ""), "text"))

    secs.append("\n## 四、残留检查\n")
    secs.append(f"`csctl ls` 中 `{PREFIX}*` 实例："
                f"**{'0 台 ✅' if not residue else str(len(residue)) + ' 台 ❌'}**")

    secs.append("\n## 五、费用\n")
    if cost_rows:
        secs.append(C.table(["实例", "名称", "运行小时", "花费"],
                            [[f'`{r["uhost"]}`', r.get("name", "-"),
                              f'{float(r.get("run_h") or 0):.3f}', f'¥{float(r.get("cost") or 0):.3f}']
                             for r in cost_rows]))
    secs.append(f"\n本测试合计 **¥{total_cost:.3f}**")

    out = C.write_report(
        "test_rapid_destroy.md",
        "测试 2：快速销毁（rapid destroy）",
        {"测试": TEST, "卡型": args.gpu_type, "自动关机兜底": f"{args.hours}h", "模式": "真实执行"},
        secs)

    passed = (a.get("outcome") == "成功" or a.get("rc") is not None) and \
             (b.get("outcome") == "成功") and not residue
    C.banner(f"{TEST} {'通过 ✅' if passed else '未通过 ❌'}")
    print(f"报告: {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
