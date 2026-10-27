#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 3：状态过渡期查询（state transitions）

场景：
  创建实例后，每 2 秒查询一次状态，持续 30 秒，记录完整的观察序列：
    · 实际观察到哪些状态值（API 原始 State 字段）
    · 每个状态持续了多久
    · 状态之间怎么跳（Install → Running？有没有出现中间态？）
    · 与 compshare/cs.py 的 STATE_CN 文档枚举比对，标记未文档化的状态

用法：
  python3 tests/test_state_transitions.py --dry-run
  python3 tests/test_state_transitions.py --duration 30 --interval 2
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C  # noqa: E402

TEST = "test_state_transitions"
PREFIX = "test-state-"
RESIDUE_PREFIX = "test-"
NAME = f"{PREFIX}tx"


def plan(args, duration: int, interval: float) -> int:
    C.banner(f"{TEST} — dry-run 计划")
    print(f"1. 后台创建实例 {NAME}")
    print(f"   python3 csctl.py up --gpu-type {args.gpu_type} --hours {args.hours} --name {NAME}")
    print()
    print(f"2. 抓到『实例已创建: uhost-xxx』后立即开始观察")
    print(f"   每 {interval}s 查询一次状态，共 {duration}s（约 {int(duration/interval)} 个采样点）")
    print("   查询方式：只读调用 ComfyUI 无关的实例列表 API（不触碰实例内部）")
    print()
    print("3. 记录并分析")
    print("   · 状态序列（采样时刻 / State / 中文）")
    print("   · 各状态驻留时长")
    print("   · 状态跳转路径")
    print(f"   · 与 STATE_CN 的 {len(C.DOCUMENTED_STATES)} 个文档化状态比对，标记未文档化者")
    print()
    print(f"4. 收尾：rm --yes 删除 {NAME}，校验无 test-state-* 残留")
    print("\n--dry-run：未调用任何云端接口，未创建实例。")
    return 0


def main() -> int:
    args = C.parse_args(f"{TEST}：状态过渡期查询")
    duration = int(args.count) if args.count else 30
    interval = 2.0
    if args.dry_run:
        return plan(args, duration, interval)

    C.banner(f"{TEST} — 观察 {duration}s / 每 {interval}s")
    t_all = time.time()
    samples: list[dict] = []
    transitions: list[str] = []
    uid = None
    p = None
    w = None
    try:
        stale = [i.get("Name") for i in C.test_instances(RESIDUE_PREFIX)
                 if (i.get("Name") or "").startswith(PREFIX)]
        if stale:
            C.say(f"发现残留 {stale}，先清理")
            C.cleanup(stale, log=C.say)

        C.say(f"后台创建 {NAME}")
        extra = ["--fallback", args.fallback] if args.fallback else []
        p = C.popen_csctl("up", "--gpu-type", args.gpu_type, "--hours", str(args.hours),
                          "--name", NAME, *extra)
        w = C.StreamWatch(p)
        uid = w.wait_uid(timeout=600)
        if not uid:
            C.say("未能从 up 输出拿到 uid，改为按名称查询")
            inst = C.by_name(NAME)
            uid = (inst or {}).get("UHostId")
        if not uid:
            C.say("创建失败，测试中止")
            print(w.text()[-1500:])
            return 1
        C.say(f"uid={uid}，开始采样")

        t0 = time.time()
        prev = None
        while time.time() - t0 < duration:
            inst = C.by_id(uid)
            raw = (inst or {}).get("State") or ("MISSING" if inst is None else "UNKNOWN")
            cn = C.STATE_CN.get(raw, "未文档化")
            el = round(time.time() - t0, 1)
            samples.append({"t": el, "state": raw, "cn": cn,
                            "documented": raw in C.DOCUMENTED_STATES})
            if raw != prev:
                transitions.append(f"{raw}" if prev is None else f"{prev} → {raw}")
                C.say(f"  [{el:05.1f}s] {raw} ({cn})")
                prev = raw
            time.sleep(interval)
    finally:
        if p is not None and p.poll() is None:
            C.say("停止后台 up 进程")
            p.kill()
        if not args.keep:
            C.say("清理")
            C.cleanup([NAME], log=C.say)

    residue = [] if args.keep else [
        i for i in C.test_instances(RESIDUE_PREFIX)
        if (i.get("Name") or "").startswith(PREFIX)]
    total_cost, cost_rows = C.ledger_terminate_costs({uid} if uid else set())
    C.record_cost(TEST, cost_rows)

    # ---------------- 分析
    observed = []
    for s in samples:
        if s["state"] not in observed:
            observed.append(s["state"])
    undocumented = [s for s in observed if s not in C.DOCUMENTED_STATES]

    # 每个状态的驻留时长
    dwell: dict[str, float] = {}
    for i, s in enumerate(samples):
        nxt = samples[i + 1]["t"] if i + 1 < len(samples) else round(time.time() - t_all, 1)
        dwell[s["state"]] = dwell.get(s["state"], 0.0) + max(0.0, nxt - s["t"])

    secs = ["## 一、结论\n"]
    secs.append(C.table(["检查项", "结果"], [
        ["采样点数", len(samples)],
        ["观察到的状态", "、".join(f"`{s}`" for s in observed) or "-"],
        ["状态跳转路径", " → ".join(transitions) or "-"],
        ["未文档化状态", "、".join(f"`{s}`" for s in undocumented) if undocumented else "无 ✅"],
        ["无残留实例", "✅" if not residue else f"❌ {[i.get('Name') for i in residue]}"],
        ["脚本判定", "通过 ✅" if (samples and not residue) else "未通过 ❌"],
        ["本测试花费", f"¥{total_cost:.3f}"],
        ["总耗时", f"{time.time()-t_all:.0f}s"],
    ]))

    secs.append("\n## 二、状态序列（逐采样）\n")
    secs.append(C.table(
        ["时刻(s)", "State", "中文", "是否文档化"],
        [[f'{s["t"]:.1f}', f'`{s["state"]}`', s["cn"],
          "✅" if s["documented"] else "**未文档化**"] for s in samples]))

    secs.append("\n## 三、状态驻留时长\n")
    secs.append(C.table(["State", "中文", "累计驻留(s)"],
                        [[f'`{k}`', C.STATE_CN.get(k, "未文档化"), f"{v:.1f}"]
                         for k, v in sorted(dwell.items(), key=lambda kv: -kv[1])]))

    secs.append("\n## 四、与文档枚举的比对\n")
    secs.append("`compshare/cs.py` 的 `STATE_CN` 定义了以下状态：\n")
    secs.append(C.table(["State", "中文"],
                        [[f'`{k}`', v] for k, v in C.STATE_CN.items()]))
    secs.append("\n**本次观察到的**：" + ("、".join(f"`{s}`" for s in observed) or "无"))
    if undocumented:
        secs.append("\n**未文档化的状态**：" + "、".join(f"`{s}`" for s in undocumented))
        secs.append("\n> 这些状态值未出现在 `STATE_CN` 里，`csctl ls` 会把它显示成原始英文。"
                    "建议补进映射表，否则状态列会出现英文或 `None`。")
    else:
        secs.append("\n**未文档化的状态**：无。所有观察到的状态都能在 `STATE_CN` 中查到。")

    secs.append("\n## 五、清理与费用\n")
    secs.append(f"`csctl ls` 中 `{PREFIX}*` 残留："
                f"**{'0 台 ✅' if not residue else str(len(residue)) + ' 台 ❌'}**")
    if cost_rows:
        secs.append("")
        secs.append(C.table(["实例", "名称", "运行小时", "花费"],
                            [[f'`{r["uhost"]}`', r.get("name", "-"),
                              f'{float(r.get("run_h") or 0):.3f}', f'¥{float(r.get("cost") or 0):.3f}']
                             for r in cost_rows]))
    secs.append(f"\n本测试合计 **¥{total_cost:.3f}**")
    secs.append("\n> 费用口径：以 `.compshare/ledger.jsonl` 的 terminate 记账为准。"
                "若实例存活时间极短（< 1 分钟）且 API 的 `StartTime` 尚未回填，"
                "`csctl rm` 会按 `run_h=0` 记账，此时显示 ¥0.00 属正常，实际扣费以账单为准（通常延迟数分钟）。")

    out = C.write_report(
        "test_state_transitions.md",
        "测试 3：状态过渡期查询（state transitions）",
        {"测试": TEST, "卡型": args.gpu_type, "观察时长": f"{duration}s",
         "采样间隔": f"{interval}s", "模式": "真实执行"},
        secs)

    # 未文档化状态是「发现」而非「失败」——测试的价值就在于把它找出来
    passed = bool(samples) and not residue
    C.banner(f"{TEST} {'通过 ✅' if passed else '未通过 ❌'}")
    print(f"报告: {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
