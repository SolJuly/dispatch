#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发闸门：同时在跑的作业超过上限就**在花钱之前**拒绝，而不是排队等着烧钱。

## 为什么需要（审计 §1 L4 的「并发：全局串行」的另一半）

上一轮修掉了「全局串行」（`93ae85f`：UI 提交加 `--batch`，改成作业级锁，
不同 job 可以并行）。那把锁一撤，问题就翻了个面：

    以前：第 2 个人提交 → 静默排队 4 小时 → 超时失败（难受但不花钱）
    现在：第 2 个人提交 → 立刻建第 2 台机 → ¥2.15/h 起跳

账户只有 **5 台配额**（Level1）。UI 上点五次「提交并执行」，五台机器同时开，
单这一下就是 ¥10.75/h。余额 ¥8.15 —— 两小时就打穿。

所以「能不能并行」和「最多并行几个」是两件事。前者已经解决，这里做后者。

## 两道闸，位置不同，理由不同

    ① 提交闸（webui/api.py，create_job / run_jobs）
       花**逻辑秒**：数 job.json 里的非终态作业。快、无网络、能在 HTTP 层回 429。
       为什么先在这里拦：让用户看到「已有 2 个作业在跑，排队还是稍后再来」，
       而不是提交成功、10 秒后 run.log 里冒出 FAILED_PROVISION。

    ② 建机闸（tools/p1/run.py，step_provision）
       花**真实秒**：问平台要 running 实例数。慢、要网络，但它是唯一的事实来源。
       为什么还要这一道：job.json 会撒谎 —— 有孤儿实例（run.py 被 kill -9、
       作业卡在 RUNNING）时，本地看只有 1 个非终态作业，云端却真实有 3 台机在跑。
       只信本地就会持续超建。

## 上限定多少

`MAX_PARALLEL` 默认 2，不是 5。理由：

  · 账户配额 5 台，但**配额不是预算**。5 台 × ¥2.15/h = ¥10.75/h，
    而余额长期在 ¥8 上下。留 3 个槽给预热池和手动排障。
  · 单任务锁时代实测：两条视频作业并行时，4090 上的 ComfyUI 会抢显存，
    第二条 OOM 失败（`FAILED_RUN`），钱照扣。并行度「能开」不等于「该开」。

要放宽就显式设 `P21_MAX_PARALLEL=3`，别改这里的默认值 ——
默认值应当是「不会因为一个误操作烧掉余额」的那个数。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JOBS_DIR = ROOT / "reports" / "p1" / "jobs"

# 与 tools/p1/state.py 的 TERMINAL 保持一致。这里不 import 它，是为了让本模块
# 能被 webui / run.py / 测试三方引用而不引入 state 的副作用（它启动时会建目录）。
TERMINAL_STATES = frozenset({"DONE", "FAILED", "ABORTED"})

DEFAULT_MAX_PARALLEL = 2


def max_parallel() -> int:
    """并发上限。环境变量可覆盖，非法值回落到默认（不改成 0 —— 0 会锁死整个系统）。"""
    raw = os.environ.get("P21_MAX_PARALLEL")
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    return DEFAULT_MAX_PARALLEL


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def active_jobs(jobs_dir: Path | None = None) -> list[dict]:
    """非终态作业（还占着槽位的那些）。

    判据只看 job.json 的 state，不看有没有 run.log、不看进程在不在 ——
    孤儿作业（进程死了但状态停在 RUNNING）**必须**算占槽：它对应的实例可能还在跑。
    宁可让人手动清理一个假占位，也不要因为自动判定"它已经死了"而多开一台机。
    """
    root = Path(jobs_dir) if jobs_dir else JOBS_DIR
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        j = _load(d / "job.json")
        if not j:
            continue
        st = j.get("state") or ""
        if st and st not in TERMINAL_STATES:
            out.append(j)
    return out


def slot_key(job: dict) -> str:
    """槽位键：一个槽 = 一个「可以去同一台实例上排队的工作流组」。

    按 **model_key** 分组，不是按作业数、也不是按 workflow_key：

      · 按作业数（旧口径）→ 提交 5 份同工作流会直接撞上限，
        而它们本该共用一台机器排队
      · 按 workflow_key  → 太碎。实测 sdxl_juggernaut 那 5 个工作流
        （t2i / t2i_lora / img2img / inpaint / detail_polish）**全部共用同一份**
        juggernautXL_v8Rundiffusion_V8.safetensors，分开建机纯浪费

    按 model_key 分组的前提是"同模型的权重共用"，这一条实测成立。

    节点集差异由 `instance_slots.find_compatible()` 在**排队时**再检查一遍
    （同模型的不同工作流节点集能差 6-9 类，见该模块注释），
    这里只管"需要几个槽"，不管"能不能塞进同一台机"。
    """
    mk = ((job.get("model") or {}).get("key") or "").strip()
    return mk or f"job:{job.get('job_id') or '?'}"


def groups_used(jobs_dir: Path | None = None) -> dict[str, list[str]]:
    """{槽位键: [job_id, ...]} —— 在跑的分组明细。"""
    out: dict[str, list[str]] = {}
    for j in active_jobs(jobs_dir):
        out.setdefault(slot_key(j), []).append(str(j.get("job_id") or "?"))
    return out


def slots_used(jobs_dir: Path | None = None) -> int:
    """占用的槽位数 = **不同模型的分组数**，不是作业数。

    这样：5 份 qwen_image_t2i = 1 个槽（共用一台机排队）
          3 份不同模型       = 3 个槽
    """
    return len(groups_used(jobs_dir))


def check_slot(jobs_dir: Path | None = None, *, limit: int | None = None,
               extra: int = 0) -> tuple[bool, str]:
    """提交闸：还有没有空槽。返回 (ok, 人话原因)。

    `extra` 用于批量提交：一次要建 3 个，就得确认空槽 ≥ 3，
    而不是逐个检查（逐个检查时前 2 个都看到"有空槽"，第 3 个才发现满了）。
    """
    cap = limit if limit is not None else max_parallel()
    groups = groups_used(jobs_dir)
    used = len(groups)
    need = max(1, int(extra or 1))
    if used + need <= cap:
        return True, f"并发 {used}/{cap} 组"
    lines = [f"{k}（{len(v)} 个作业）" for k, v in list(groups.items())[:4]]
    more = "…" if len(groups) > 4 else ""
    return False, (f"并发已满：{used}/{cap} 组在跑（{', '.join(lines)}{more}）。"
                   f"本次需要 {need} 个槽。等它们结束，或用 P21_MAX_PARALLEL 调大上限。")


def running_instances(exclude: set[str] | None = None) -> int | None:
    """平台侧真实在跑的 p1-/p2- 实例数。读不到返回 None（不是 0 —— 0 会被当成"没占槽"）。

    `exclude`：不算进来的 UHostId 集合。给**续跑**用 —— 见 `check_provision` 的说明。
    """
    try:
        from compshare.cs import CS
        cs = CS()
        skip = {str(x) for x in (exclude or ()) if x}
        return sum(1 for i in cs.instances()
                   if (i.get("State") or "").lower() == "running"
                   and str(i.get("Name") or "").startswith(("p1-", "p2-"))
                   and str(i.get("UHostId") or "") not in skip)
    except Exception:                      # noqa: BLE001  无凭证/断网都属于"读不到"
        return None


def check_provision(*, limit: int | None = None,
                    exclude: set[str] | None = None) -> tuple[bool, str]:
    """建机闸：以云端 running 实例数为准（本地 job.json 会撒谎）。

    `exclude` 存在的理由（实测 bug，2026-11-11）：
      `p1-20261111T0058-8302f5` 推理失败后 restart，状态走
      `FAILED_RUN → RESUMING → PROVISIONING`。这三步之间只隔 3 秒，
      它**上一轮那台实例还没销毁完**，仍是 Running 状态。
      于是本闸数出「云端已有 2 台」，把这次重启拒了 —— 而那 2 台里
      **有一台是它自己**。重启变成自我否决，作业直接判死。

      续跑时调用方应把自己已登记的 uhost_id 传进来排除。
      注意只排除**自己的**：别人的实例必须照数，否则并发上限就没意义了。
    """
    cap = limit if limit is not None else max_parallel()
    n = running_instances(exclude=exclude)
    if n is None:
        return True, f"读不到平台实例数，按放行处理（上限 {cap}）"
    if n < cap:
        return True, f"云端在跑 {n}/{cap}"
    return False, (f"云端已有 {n} 台 p1-/p2- 实例在跑（上限 {cap}）。"
                   f"超建会按 ¥2.15/h/台 计费，且可能 OOM。等已有作业结束，"
                   f"或用 P21_MAX_PARALLEL 调大上限。")
