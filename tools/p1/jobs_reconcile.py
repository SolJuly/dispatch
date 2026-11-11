#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""作业状态对账：把「进程已死、实例已没、状态却停在中间态」的僵尸作业收尾。

## 要解决什么（2026-11-11 实测）

工作台的「进行中」里挂着两个作业，其中一个创建于 **11-08**，卡了 3 天：

    p1-20261109T1803-ddd004  RUNNING   11-08 14:00 建，release 都做完了
    p1-20261111T0736-6b6821  RUNNING   今天 07:36 建，停在 wait 步骤

查下去：**`ps` 里没有 run.py 进程，云端一台实例都没有**。
状态机的收尾完全依赖 `run.py` 进程活着 —— 它被 kill -9 / OOM / 断电带走时，
`job.json` 就永远停在 RUNNING。而 UI 的「进行中」正是按 `state not in TERMINAL` 算的。

后果不止难看：

  · `concurrency.slots_used()` 把僵尸算成占槽 → 实际能并行的少了
  · `reaper` 看到「有作业未结束」就不敢停机/销毁 → 多烧钱
  · 用户以为还在跑，实际早就死了

## 判据：三条证据，全部要查

绝不单凭「静默多久」判死 —— 一条跑 40 分钟的视频作业中间可能 30 分钟没写日志，
按静默时长判会把**正在跑的**作业误杀成 FAILED（而且它会继续跑、继续烧钱）。

    ① 进程   有没有 run.py 在处理这个 job_id（ps 全表匹配）
    ② 实例   job.json 里的 uhost_id 在云端还在不在
    ③ 进度   最后一条 event 的时间距今多久（只用来兜底，不作主判据）

决策表：

    进程在                     → keep（无论多慢，它在跑）
    进程没了 + 实例还在         → 查云端状态：Running 说明可能真在推理（run.py 死了但
                                  ComfyUI 还活着），标 FAILED_RUN 保留实例供排查
    进程没了 + 实例没了 + 步骤走完 → DONE（产物都在，只是没来得及写终态）
    进程没了 + 实例没了 + 有产物 → DONE 或 FAILED_VERIFY（按 manifest 的 verdict）
    进程没了 + 实例没了 + 无产物 → FAILED（没东西可救）

## 为什么不直接改 job.json

走 `state.transition()`，这样 `state_seq` / `events.jsonl` 都保持一致 ——
手改 json 会让状态机和事件流对不上，下次续跑或审计时出怪事。

## 用法

    python3 tools/p1/jobs_reconcile.py --dry-run     # 只看会怎么判（¥0）
    python3 tools/p1/jobs_reconcile.py               # 真收尾
    python3 tools/p1/jobs_reconcile.py --json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JOBS_DIR = ROOT / "reports" / "p1" / "jobs"

# 与 tools/p1/state.py 保持一致
TERMINAL = frozenset({"DONE", "FAILED", "ABORTED"})

# 静默超过这个时长才进入候选。60 分钟：
# 实测最长的一条作业（LTX 2.5 视频 121 帧）推理 226s、整条闭环 5.1 分钟；
# 留 10 倍余量，避免把真正在跑的慢作业当成僵尸。
STALE_MIN = 60


@dataclass
class Verdict:
    job_id: str
    state: str
    has_proc: bool
    uhost: str
    inst_alive: bool
    idle_h: float
    action: str          # keep | done | fail_run | fail_verify | fail
    reason: str

    @property
    def needs_fix(self) -> bool:
        return self.action != "keep"


def _load(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _proc_owners() -> set[str]:
    """当前在跑的 run.py 处理哪些 job_id。

    用 `ps -eo args` 全表匹配而不是 `pgrep -f`：后者在某些环境下会匹配到自己，
    而这个函数的结论直接决定「要不要把作业判死」，多算一个都不行。
    """
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    found = set()
    for line in out.splitlines():
        if "run.py" not in line or "jobs_reconcile" in line:
            continue
        for tok in line.split():
            if tok.startswith("p1-") and "T" in tok and "-" in tok:
                found.add(tok)
    return found


def _cloud_instances() -> dict[str, str] | None:
    """{uhost_id: state}。读不到返回 None（**不是空字典** —— 空字典会被当成
    "实例全没了"，从而把所有僵尸判死；读不到时应当保守跳过）。"""
    try:
        from compshare.cs import CS
        return {str(i.get("UHostId")): str(i.get("State") or "")
                for i in CS().instances()}
    except Exception:                      # noqa: BLE001
        return None


def _last_activity(d: Path, job: dict) -> float:
    ev = d / "events.jsonl"
    if ev.exists():
        last = 0.0
        try:
            for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                for k in ("ts", "at"):
                    v = e.get(k)
                    if isinstance(v, (int, float)):
                        last = max(last, float(v))
                    elif isinstance(v, str):
                        try:
                            import datetime as dt
                            last = max(last, dt.datetime.fromisoformat(v).timestamp())
                        except ValueError:
                            pass
        except OSError:
            pass
        if last:
            return last
    try:
        return d.stat().st_mtime
    except OSError:
        return time.time()


def assess_one(d: Path, procs: set[str], insts: dict[str, str] | None,
               *, stale_min: int = STALE_MIN) -> Verdict | None:
    job = _load(d / "job.json")
    if not job:
        return None
    st = str(job.get("state") or "")
    if st in TERMINAL:
        return None

    jid = str(job.get("job_id") or d.name)
    uid = str((job.get("instance") or {}).get("uhost_id") or "")
    idle_h = round((time.time() - _last_activity(d, job)) / 3600, 1)
    has_proc = jid in procs

    # 实例是否还在。**云端读不到时不能算"在"，也不能算"没了"** ——
    # 两种都不对：算"在"会把网格抖动的作业判成 fail_run（改坏数据），
    # 算"没了"会把正在跑的判成 fail（丢作业、还可能继续烧钱）。
    # 正确做法是单独记一个"未知"，由下面的判据直接 keep。
    if insts is None:
        inst_alive = True        # 仅用于展示，配合 inst_unknown 走 keep 分支
        inst_unknown = True
    else:
        inst_alive = bool(uid) and uid in insts
        inst_unknown = False

    steps = job.get("steps") or {}
    # **必须对着 STEP_ORDER 全表查**，不能只查 steps 里出现过的那几个。
    # 踩过：`6b6821` 只跑了 provision/setup/ready/submit 四步就死了，
    # `all(steps.values(), done)` 返回 True —— 把"跑了一半"判成"走完了"。
    from tools.p1.state import STEP_ORDER
    all_steps_done = bool(steps) and all(
        (steps.get(name) or {}).get("done") for name in STEP_ORDER)
    man = _load(d / "manifest.json")
    arts = list((d / "artifacts").glob("*")) if (d / "artifacts").is_dir() else []

    def v(action: str, reason: str) -> Verdict:
        return Verdict(jid, st, has_proc, uid, inst_alive, idle_h, action, reason)

    # ① 进程还在 → 无论多慢都别动
    if has_proc:
        return v("keep", "run.py 进程还在处理它")

    # 静默不够久 → 可能刚被拉起，保守保留
    if idle_h * 60 < stale_min:
        return v("keep", f"静默 {idle_h:.1f}h < {stale_min}min 阈值，先不动")

    # 读不到云端实例列表 → 证据不足，一律保留。
    # 一次网络抖动就把整个作业表标成失败，代价远大于"僵尸多留一轮"。
    if inst_unknown:
        return v("keep", "读不到云端实例列表，证据不足，保守保留")

    # ② 进程没了，实例还在 → ComfyUI 可能仍在推理，实例留着供排查
    if inst_alive:
        cloud = (insts or {}).get(uid, "?")
        return v("fail_run",
                 f"run.py 已退出但实例 {uid} 还在（云端 {cloud}）——"
                 f"推理可能仍在进行，标记失败并保留实例供排查")

    # ③ 进程没了，实例也没了 → 按已有的证据收尾
    #
    # **判 done 必须先看产物，不能只看步骤**。踩过：`ddd004` 的九个步骤
    # 全都 done=True、fetch 还记录了本地文件名，但 `artifacts/` 是空的 ——
    # 产物被删过（那是 11-08 做 UI 验证的夹具）。按 `all_steps_done` 判 done
    # 会把「什么都拿不出来」的作业写成 DONE，等于伪造成功。
    #
    # 所以：步骤状态只是**辅助**，产物文件才是事实。
    if insts is not None and uid and uid not in insts:
        if man.get("verdict") == "PASS" and arts:
            return v("done", f"实例已销毁、manifest PASS、{len(arts)} 个产物——补写 DONE")
        if arts and man:
            # 有产物但 manifest 没 PASS：可能是校验未过
            return v("fail_verify",
                     f"实例已销毁，有 {len(arts)} 个产物但 manifest verdict="
                     f"{man.get('verdict')!r}")
        if arts and not man:
            return v("fail_verify",
                     f"实例已销毁，有 {len(arts)} 个产物但**没有 manifest**"
                     f"（persist 未完成，无从判质量）")
        # 无产物：不管步骤走到哪，这条作业没产出任何东西
        if all_steps_done:
            # 步骤全走完（含 release、manifest 也写过）但产物没了。
            # 真实成因实测是"产物被人工删除"（11-08 的 UI 夹具就是），
            # 也可能是磁盘清理。无论哪种，现在拿不出东西 → 不能算成功。
            return v("fail",
                     "九步全 done 但 artifacts/ 为空——产物已被删除或未落盘，"
                     "现在拿不出任何东西，不能算成功")
        return v("fail", "实例已销毁、无产物、步骤未走完")

    # ④ 没有 uhost_id（建机都没成功）且静默很久
    if not uid:
        if arts:
            return v("done", "无实例记录但有产物，按 DONE 收尾")
        return v("fail", "无实例记录、无产物")

    return v("keep", "证据不足，保守保留")


def collect(*, stale_min: int = STALE_MIN) -> list[Verdict]:
    procs = _proc_owners()
    insts = _cloud_instances()
    out = []
    for path in sorted(glob.glob(str(JOBS_DIR / "*/job.json"))):
        d = Path(path).parent
        r = assess_one(d, procs, insts, stale_min=stale_min)
        if r:
            out.append(r)
    return out


def _path_to(cur: str, target: str) -> list[str] | None:
    """在 TRANSITIONS 图上找一条从 cur 到 target 的最短路径（不含 cur）。

    为什么要找路径而不是硬写转移：状态机有守卫（G1: PERSISTED 之前不许 RELEASE；
    G2: DONE 要求 manifest PASS）。**直接跳态会被守卫拦住** ——
    实测 `RUNNING → FAILED` 报 `非法转移（允许 ABORTED/FAILED_RUN/FETCHING）`。

    正规路径长这样：

        RUNNING → FAILED_RUN → RELEASING → FAILED

    硬跳既过不了守卫，也破坏了事件流的可审计性。所以按图搜路，逐跳走。
    """
    from tools.p1.state import TRANSITIONS

    if cur == target:
        return []
    seen = {cur}
    queue: list[tuple[str, list[str]]] = [(cur, [])]
    while queue:
        node, path = queue.pop(0)
        for nxt in sorted(TRANSITIONS.get(node, ())):
            if nxt in seen:
                continue
            if nxt == target:
                return path + [nxt]
            seen.add(nxt)
            queue.append((nxt, path + [nxt]))
    return None


# 判定 → **终态**。注意 fail_run / fail_verify 都落到 FAILED：
# 这两个是「可续跑的中转态」，不是终态 —— 停在它们上面的作业仍然算
# 「进行中」（UI 的进行中 = state not in TERMINAL）。
# 对账的目的是把僵尸作业收进终态，所以目标一律是 DONE / FAILED。
# 踩过：目标写成 FAILED_VERIFY 时，作业本来就在 FAILED_VERIFY，
# `cur == target` 直接返回"无需修改"，于是它永远挂在进行中。
TARGET_OF = {"done": "DONE", "fail_run": "FAILED",
             "fail_verify": "FAILED", "fail": "FAILED"}


def apply(v: Verdict, *, say=print) -> bool:
    """把判定落成状态转移。返回是否真的改了。"""
    from tools.p1 import state

    name = TARGET_OF.get(v.action)
    if name is None:
        return False
    target = getattr(state, name)
    try:
        cur = state.load_job(v.job_id).get("state")
        if cur == target:
            return False
        path = _path_to(cur, target)
        if path is None:
            say(f"  ✗ {v.job_id} 找不到 {cur} → {target} 的合法路径")
            return False
        # 判 DONE 的路径上不许经过失败态。
        # 搜路只在图上找最短，`RUNNING → DONE` 会经过 FAILED_RUN —— 那等于
        # "为了标成功先标一次失败"，事件流读起来像作弊，审计时会误导。
        # 这种情况说明这条作业不该判 done，交回人工看。
        if target == state.DONE and any(
                nxt.startswith("FAILED") for nxt in path):
            say(f"  ✗ {v.job_id} 判 DONE 需经过失败态（{' → '.join(path)}），"
                f"不自动改，请人工确认")
            return False
        for nxt in path:
            state.transition(v.job_id, nxt, reason=f"对账：{v.reason}"[:180])
        say(f"  ✓ {v.job_id} {cur} → {' → '.join(path)}")
        return True
    except Exception as e:                 # noqa: BLE001
        say(f"  ✗ {v.job_id} 收尾失败：{type(e).__name__}: {e}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="作业状态对账（僵尸作业收尾）")
    ap.add_argument("--dry-run", action="store_true", help="只看会怎么判，不改")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--stale-min", type=int, default=STALE_MIN,
                    help=f"静默多少分钟才进入候选（默认 {STALE_MIN}）")
    ap.add_argument("--job", help="只处理一个 job_id")
    a = ap.parse_args(argv)

    rows = collect(stale_min=a.stale_min)
    if a.job:
        rows = [r for r in rows if r.job_id == a.job]

    if a.json:
        print(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("没有非终态作业，状态干净")
        return 0

    print(f"非终态作业 {len(rows)} 条：\n")
    print(f"  {'job_id':<28}{'state':<22}{'静默':>7}  {'进程':<6}{'实例':<6} 判定")
    print("  " + "-" * 104)
    for r in sorted(rows, key=lambda x: -x.idle_h):
        print(f"  {r.job_id:<28}{r.state:<22}{r.idle_h:>6.1f}h  "
              f"{'在' if r.has_proc else '没了':<6}{'在' if r.inst_alive else '没了':<6} "
              f"{r.action:<11}{r.reason}")

    todo = [r for r in rows if r.needs_fix]
    if not todo:
        print("\n没有需要收尾的作业")
        return 0

    if a.dry_run:
        print(f"\n--dry-run：{len(todo)} 条会被收尾（未真的改）")
        return 0

    print(f"\n收尾 {len(todo)} 条：")
    ok = sum(1 for r in todo if apply(r))
    print(f"\n完成 {ok}/{len(todo)}")
    return 0 if ok == len(todo) else 1


if __name__ == "__main__":
    raise SystemExit(main())
