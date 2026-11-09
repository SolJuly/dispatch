#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 单任务执行器：把一个 job 从 CREATED 串行推到 DONE。

    python3 tools/p1/run.py <job_id>                 # 真跑（会建机、花钱）
    python3 tools/p1/run.py <job_id> --dry-run       # 只走状态机，不建机不花钱
    python3 tools/p1/run.py <job_id> --gpu 4090_48G  # 指定卡型

九步串行，每一步都是「先转移状态（守卫在此拦截）→ 再执行动作」：
    CREATED → PROVISIONING → READY → SUBMITTED → RUNNING
            → FETCHING → VERIFYING → PERSISTED → RELEASING → DONE

三条硬规矩：
    G1  只有 PERSISTED 才允许 RELEASING（产物不随实例消失）
    G2  DONE 必须 manifest.verdict == PASS
    8903 过渡期拒绝 → 释放重试 3 次（U2 的规避）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import store, state  # noqa: E402
from tools.p1 import verify as verify_mod  # noqa: E402

COMFY_API = "http://127.0.0.1:8188"
IMAGE_KEYWORD = "ComfyUI基础镜像纯净版0.36.0"

# 卡型 → (CPU 核, 内存 GB)：统一放 p2/config.py，避免两处漂移
try:
    from tools.p2.config import GPU_SPEC  # noqa: E402
except Exception:  # 极端情况下回退
    GPU_SPEC = {"4090": (16, 64), "4090_48G": (16, 96), "3090": (16, 64), "5090": (16, 96),
                "V100S": (10, 64), "A100": (16, 64), "A800": (16, 240), "H20": (16, 240)}
RETRY_8903 = 3
CREATE_RETRY = 4
RETRY_8903_WAIT = 10


# ---------------------------------------------------------------- BYOK 入口
# 2026-11-09 复核后的商业模型：客户用**自己的**智云账号付算力费，
# 我们只收调度服务费。代码上唯一的差别是「用谁的凭证建机」。
#
# `CS(cfg)` 本来就接受 isolated credentials（compshare/cs.py:79），所以
# 不需要改 34 个调用点里的任何一个 —— 只需要让它们都经由这一个函数拿实例。
#
# 凭证来源（优先级从高到低）：
#   1. 显式传入的 creds（webui 按用户查出来的，加密存储）
#   2. 环境变量 CS_PUBLIC_KEY / CS_PRIVATE_KEY（load_config 已支持）
#   3. compshare/credentials.env（自用时的默认）
def _cs(creds: dict[str, str] | None = None):
    """取一个 CS 实例。creds 非空时走 BYOK，不读本地 credentials.env。"""
    from compshare.cs import CS
    if creds and creds.get("CS_PUBLIC_KEY") and creds.get("CS_PRIVATE_KEY"):
        return CS(creds)
    return CS()


class RunError(Exception):
    """执行失败，带目标失败态。"""

    def __init__(self, msg: str, fail_state: str):
        super().__init__(msg)
        self.fail_state = fail_state


# ---------------------------------------------------------------- 孤儿实例兜底
# 为什么需要（审计 §1 L4 没列，但它是「并发」失控的真正来源）：
#
#   run.py 以前没有任何信号处理。worker 被 systemd 重启、Ctrl-C、kill -9、
#   调度器超时强杀 —— 无论哪种，进程消失后**实例还在 Running**，按 ¥2.15/h 继续计费。
#   本地 job.json 冻在 RUNNING，于是：
#     · 并发闸看本地 → 只有 1 个作业 → 放行 → 又建一台
#     · 用户看面板 → 显示"1 个作业在跑" → 实际 3 台机在烧钱
#
#   2026-10-28 的实例 uhost-1xe255rs9xyp 就是这么留下的（v4-sweep 跑完 terminate
#   走的是正常路径，但 trial.py 中途被打断那次没有）。
#
# 兜底策略分两层，因为「优雅」和「暴力」能做的事不同：
#
#   ① SIGTERM / SIGINT：还能执行代码。设定时关机（stop_at，N 分钟后自动停），
#      而不是立刻销毁 —— 立刻销毁会把「正在跑的推理」和「已产出但没取回的文件」
#      一起干掉。定时关机是「先记账后停机」，代价可控且可续跑。
#   ② 进程直接消失（kill -9 / OOM / 断电）：Python 收不到任何信号。
#      这种只能靠**下次启动时对账**：并发闸查云端 running 数（见 concurrency.py），
#      以及 tools/p2/status2.py 的实例巡检把它们停掉。
#
# 注意：兜底只在「本进程建了机」时注册。--dry-run / salvage 不建机，不该触发。

_ORPHAN_UID: str | None = None
_ORPHAN_HANDLED = False


def _orphan_guard_minutes() -> int:
    """信号中断后实例再活多久。默认 10 分钟 —— 够续跑，不够烧钱。"""
    try:
        return max(1, int(os.environ.get("P1_ORPHAN_KEEP_MINUTES", "10")))
    except ValueError:
        return 10


def _on_signal(signum: int, frame) -> None:
    """收到终止信号：把实例定在「N 分钟后关机」，然后退出。

    不销毁的理由同上：销毁是不可逆的，而停机是可续跑的（--resume 还能接着跑）。
    钱的事交给定时关机（停机后算力费归零，系统盘 ≤100G 免费）。
    """
    global _ORPHAN_HANDLED
    name = {2: "SIGINT", 15: "SIGTERM"}.get(signum, str(signum))
    if _ORPHAN_HANDLED or not _ORPHAN_UID:
        _say(f"⏹ 收到 {name}，无实例需要处理，退出")
        raise SystemExit(130)
    _ORPHAN_HANDLED = True
    mins = _orphan_guard_minutes()
    try:
        cs = _cs(getattr(_OPTS, "creds", None))
        cs.stop_at(_ORPHAN_UID, int(time.time() + mins * 60))
        _say(f"⏹ 收到 {name}：实例 {_ORPHAN_UID} 已设定 {mins} 分钟后自动关机（可 --resume 续跑）")
    except Exception as e:                 # noqa: BLE001
        # 设不上定时关机时退化为直接销毁 —— 宁可丢一次续跑机会，不留计时炸弹
        _say(f"⏹ 收到 {name}：定时关机失败（{type(e).__name__}: {e}），尝试直接销毁")
        try:
            _cs(getattr(_OPTS, "creds", None)).terminate(_ORPHAN_UID)
            _say(f"   已销毁 {_ORPHAN_UID}")
        except Exception as e2:            # noqa: BLE001
            _say(f"   ⚠ 销毁也失败（{e2}）—— 实例 {_ORPHAN_UID} 可能仍在计费，请手动处理")
    raise SystemExit(130)


_OPTS = None                               # 信号处理器里拿 creds 用


def _install_orphan_guard(job_id: str, opts) -> None:
    """注册信号处理器。幂等：重复调用不会挂多个。"""
    global _OPTS, _ORPHAN_HANDLED
    _OPTS = opts
    if _ORPHAN_HANDLED:
        return
    try:
        import signal as _sig
        for s in (_sig.SIGTERM, _sig.SIGINT, _sig.SIGHUP):
            try:
                _sig.signal(s, _on_signal)
            except (ValueError, OSError):
                # ValueError：非主线程（测试里常见）；OSError：平台不支持 SIGHUP
                pass
    except Exception:                      # noqa: BLE001
        pass


def _note_instance(uid: str | None) -> None:
    """记住本进程当前占用的实例，供信号兜底使用。"""
    global _ORPHAN_UID
    _ORPHAN_UID = uid


def _ledger(job_id: str, cost: float, **kw) -> None:
    rec = {"ts": int(time.time()),
           "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "event": "job", "job": job_id, "cost": round(cost, 4), **kw}
    try:
        store.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(store.LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        _say(f"  ⚠ 账本写入失败：{e}")


def _mark_step(job_id: str, name: str, **detail) -> None:
    """把「某一步已完成」写进 job.json，供 --resume 判断断点。

    只记续跑需要的 ID（uhost_id / prompt_id / 文件列表），不记密码。
    """
    j = state.load_job(job_id)
    steps = j.setdefault("steps", {})
    steps[name] = {"done": True,
                   "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                   **{k: v for k, v in detail.items() if v is not None}}
    j["steps"] = steps
    store.atomic_write_json(store.job_file(job_id), j)


def _step_done(job_id: str, name: str) -> bool:
    j = store.read_json(store.job_file(job_id)) or {}
    return bool((j.get("steps") or {}).get(name, {}).get("done"))


_LOG_PREFIX = ""


def _say(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {_LOG_PREFIX}{msg}", flush=True)


def _start_heartbeat(job_id: str, opts) -> None:
    """P0-2：批量模式起心跳线程，每 15s 向队列打点（daemon，进程退出自动结束）。"""
    if not getattr(opts, "batch", False):
        return
    try:
        import threading
        from tools.p2 import jobqueue
    except Exception:
        return

    def loop():
        while True:
            try:
                jobqueue.heartbeat(job_id)
            except Exception:
                pass
            time.sleep(jobqueue.HEARTBEAT_S)

    threading.Thread(target=loop, daemon=True, name=f"hb-{job_id}").start()


def _lock(job_id: str, opts):
    """批量模式用作业级锁（不同 job 可并行）；legacy 单机模式仍用全局锁。"""
    if getattr(opts, "batch", False):
        return store.per_job_lock(job_id)
    return store.JobLock(wait=getattr(opts, "wait_lock", 0) or 0)


def _render_workflow(wf: dict, params: dict, prefix: str) -> dict:
    """把 {{PROMPT}} 等占位符换成真实值（深拷贝，不改原文件）。"""
    text = json.dumps(wf, ensure_ascii=False)
    mapping = {
        "{{PROMPT}}": params.get("prompt", ""),
        "{{NEG_PROMPT}}": params.get("neg_prompt", ""),
        "{{SEED}}": params.get("seed"),
        "{{FIRST_FRAME}}": params.get("image", ""),
        "{{OUTPUT_PREFIX}}": prefix,
    }
    for k, v in mapping.items():
        if v is None:
            continue
        text = text.replace(f'"{k}"', json.dumps(v, ensure_ascii=False))
    out = json.loads(text)
    # {{SEED}} 在 JSON 里是字符串，替换后要还原成数字
    for node in out.values():
        ins = node.get("inputs") or {}
        if isinstance(ins.get("seed"), str) and ins["seed"].isdigit():
            ins["seed"] = int(ins["seed"])
    return out


def _http_json(url: str, payload=None, timeout=60):
    if payload is None:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _ssh_run(host: str, port: str, password: str, cmd: str, timeout=600) -> tuple[int, str]:
    base = ["sshpass", "-p", password, "ssh", "-p", str(port),
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10", "-c", "aes128-gcm@openssh.com",
            f"root@{host}", cmd]
    p = subprocess.run(base, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


SCP_TIMEOUT = 300      # 单文件 5 分钟：100MB@10MB/s 只需 10s，留足余量
SCP_RETRY = 2          # 失败重试次数（网络抖动很常见）


def _scp_put(host: str, port: str, password: str, local: Path, remote: str,
             timeout: int = 180) -> bool:
    """把本地文件推到实例（img2img / inpaint 的输入图）。"""
    cmd = ["sshpass", "-p", password, "scp", "-q", "-P", str(port),
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           str(local), f"root@{host}:{remote}"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        _say(f"  scp 上传失败：{(p.stderr or '')[:160]}")
    return p.returncode == 0


def _scp_get(host: str, port: str, password: str, remote: str, local: Path,
             timeout: int = SCP_TIMEOUT, retries: int = SCP_RETRY) -> bool:
    """scp 拉单个文件。失败会重试；仍失败返回 False（由调用方决定状态）。"""
    local.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["sshpass", "-p", password, "scp", "-q", "-P", str(port),
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=15", "-c", "aes128-gcm@openssh.com",
           f"root@{host}:{remote}", str(local)]
    for attempt in range(1, retries + 1):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _say(f"  scp 超时（{timeout}s，第 {attempt}/{retries} 次）")
            if attempt < retries:
                time.sleep(5)
            continue
        if p.returncode == 0 and local.exists():
            return True
        _say(f"  scp 失败（第 {attempt}/{retries} 次）：{(p.stderr or '')[:160]}")
        # 半截文件必须删掉，否则下次重试会误判成功
        if local.exists() and p.returncode != 0:
            local.unlink(missing_ok=True)
        if attempt < retries:
            time.sleep(5)
    return False


# ---------------------------------------------------------------- 九步
def _pick_image(cs, opts, job: dict) -> dict:
    """选镜像（D2 两层）：--image > image.json.layers[video→media/image→base] > 平台关键字。

    任何一环缺失/解析失败都回落基础镜像关键字，保证不会因为镜像登记问题挡住闭环。
    """
    if getattr(opts, "image", None):
        try:
            return cs.resolve_image(opts.image)
        except Exception:
            pass
    try:
        from tools.p2 import config as p2cfg
        d = json.loads(p2cfg.IMAGE_JSON.read_text(encoding="utf-8"))
        layer = "media" if job.get("kind") == "video" else "base"
        iid = ((d.get("layers") or {}).get(layer) or {}).get("image_id")
        if iid:
            return cs.resolve_image(iid)
    except Exception:
        pass
    return cs.resolve_image(IMAGE_KEYWORD)


def _gpu_candidates(job: dict, opts) -> list[str]:
    """候选卡型：--gpu 指定则只它一个，否则按 required_gpu 顺序去重。"""
    if getattr(opts, "gpu", None):
        return [opts.gpu]
    raw = (job.get("instance") or {}).get("required_gpu") or ["4090_48G"]
    out: list[str] = []
    for g in raw:
        if g and g not in out:
            out.append(str(g))
    return out or ["4090_48G"]


def _pick_gpu(cs, job: dict, opts) -> str:
    """按候选顺序挑第一个有现货的卡；全没货时最多等 --wait-stock 秒。

    以前是 required_gpu[0] 定死：4090_48G 一旦没货，即使列表里还有 4090 也直接失败
    （2026-11-07 的 wan21_i2v 作业就是这么死的）。
    """
    cands = _gpu_candidates(job, opts)
    wait_s = int(getattr(opts, "wait_stock", 0) or 0)
    stocks: dict[str, int] = {}
    for g in cands:
        try:
            stocks[g] = cs.stock(g)
        except Exception as e:                      # noqa: BLE001
            _say(f"  {g} 库存查询失败（{type(e).__name__}: {e}），跳过预检")
            stocks[g] = 1
        if stocks[g] > 0:
            if g != cands[0]:
                _say(f"{cands[0]} 无现货 → 改用 {g}（{stocks[g]} 台可用）")
            return g
        if len(cands) > 1:
            _say(f"  {g} 无现货，看下一个候选")
    if wait_s > 0:
        _say(f"候选卡都没货，最多等 {wait_s}s…")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            for g in cands:
                try:
                    if cs.stock(g) > 0:
                        _say(f"{g} 有货了（等了 {time.time() - t0:.0f}s）")
                        return g
                except Exception:                   # noqa: BLE001
                    pass
            time.sleep(15)
        raise RunError(f"{'/'.join(cands)} 等了 {wait_s}s 仍无现货（226604）",
                       state.FAILED_PROVISION)
    raise RunError(f"{'/'.join(cands)} 当前无现货（226604）。"
                   f"换 --gpu，或加 --wait-stock 秒数排队等货",
                   state.FAILED_PROVISION)


def _try_borrow_slot(job: dict, opts) -> dict | None:
    """先看有没有可以复用的同模型实例（规则 B：同工作流串行，不建新机）。

    复用的收益是**装载模型**的时间 —— 实测 SDXL ~21s、Qwen ~53s。
    同一个工作流连跑 5 条，只付一次。

    判定交给 `instance_slots.find_compatible()`：
      · 同 model_key（权重共用是主要成本）
      · 镜像里有需要的节点类（缺节点的换机器也没用）
      · 这台机器上验证过的资产覆盖参数指到的文件

    **--no-borrow 可强制不用**：调试/对比基准时想要干净机器。
    池里的实例（from_pool）不走这条路 —— 池有自己的生命周期。
    """
    if getattr(opts, "no_borrow", False):
        return None
    reg = _registry_meta(job)
    if not reg:
        return None                    # legacy 作业不参与槽位复用
    mk = reg.get("model_key") or ""
    wk = reg.get("workflow_key") or ""
    if not (mk and wk):
        return None
    try:
        from tools.p2 import instance_slots as slots
    except Exception:                  # noqa: BLE001
        return None

    slot, why = slots.find_compatible(str(mk), str(wk))
    if not slot:
        _say(f"  槽位：{why}")
        return None
    # 在跑的作业太多就别再往里塞（串行语义：一个实例同时只跑一条）
    if slot.busy_jobs and not getattr(opts, "slot_parallel", False):
        _say(f"  槽位 {slot.uid} 正忙（{', '.join(slot.busy_jobs)}），本单另建")
        return None

    cs = _cs(getattr(opts, "creds", None))
    try:
        inst = cs.get(slot.uid)
    except Exception as e:             # noqa: BLE001
        _say(f"  槽位 {slot.uid} 查不到（{type(e).__name__}），本单另建")
        return None
    state_now = str(inst.get("State") or "").lower()
    if state_now != "running":
        # 停机槽位：start 一下（~12s）继续用，比重建省得多
        _say(f"  槽位 {slot.uid} 当前 {state_now}，尝试启动复用")
        try:
            cs.start(slot.uid)
            cs.wait_state(slot.uid, {"running"}, timeout=300)
            inst = cs.get(slot.uid)
        except Exception as e:         # noqa: BLE001
            _say(f"  槽位 {slot.uid} 启动失败（{type(e).__name__}），本单另建")
            return None

    host, port = cs.public_ip(inst), "23"
    pwd = cs.password_of(inst)
    # sshd 探活：启动后要几秒（复用路径同样需要）
    for i in range(20):
        rc, out = _ssh_run(host, port, pwd, "echo ssh-ready", timeout=20)
        if rc == 0 and "ssh-ready" in out:
            break
        time.sleep(3)
    else:
        _say(f"  槽位 {slot.uid} sshd 未就绪，本单另建")
        return None

    price = cs.price(str(inst.get("GpuType") or "4090"), 1, 16, 64, "Postpay", 100, 0,
                     image_id=str(inst.get("CompShareImageId") or ""))
    hourly = float(price.get("total") or 0)
    _say(f"  ✓ 复用实例 {slot.uid}（{slot.model_key}，已验证 {len(slot.workflows_ok)} 个工作流）"
         f" · {hourly:.2f}/h")

    slots.touch(slot.uid, job_id=job["job_id"], busy=True)
    return {"uhost_id": slot.uid, "inst": inst, "hourly": hourly,
            "host": host, "port": port, "password": pwd, "borrowed": True}


def step_provision(job: dict, opts) -> dict:
    """建机并等 Running。

    先试复用（规则 B），没有再建。复用的实例在 release 阶段只更新空闲时间，
    不销毁 —— 见 `step_release` 的 `borrowed` 分支。
    """
    from compshare.cs import CompShareError
    from compshare.cs import CS  # noqa: F401  (保留给异常路径的 import 语义)

    borrowed = _try_borrow_slot(job, opts)
    if borrowed:
        # 登记实例（信号兜底要用），但**不**覆盖 up_at：
        # 那是计费起点，复用的机器早就开着，按现在记会把成本算少
        j = state.load_job(job["job_id"])
        j.setdefault("instance", {}).update({
            "uhost_id": borrowed["uhost_id"],
            "gpu": (borrowed["inst"] or {}).get("GpuType"),
            "zone": (borrowed["inst"] or {}).get("Zone"),
            "borrowed": True,
            "up_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        store.atomic_write_json(store.job_file(job["job_id"]), j)
        _note_instance(borrowed["uhost_id"])
        return borrowed

    cs = _cs(getattr(opts, "creds", None))
    hours = opts.hours
    img = _pick_image(cs, opts, job)
    gpu = _pick_gpu(cs, job, opts)          # 按 required_gpu 顺序挑第一个有货的卡
    cpu_n, mem_gb = GPU_SPEC.get(gpu, (16, 64))
    price = cs.price(gpu, 1, cpu_n, mem_gb, "Postpay", 100, 0, image_id=img["CompShareImageId"])
    hourly = float(price.get("total") or 0)
    est = hourly * hours
    budget = job.get("budget") or {}
    cap = float(budget.get("max_cost") or 0)
    max_min = float(budget.get("max_minutes") or 20)
    # 真正会花多久：--hours 是定时关机兜底，max_minutes 是推理超时，
    # 实际成本按较紧的那个估。以前只按 hours 打印，40 分钟能跑完的活
    # 也会按 1 小时报，而且 cap 从头到尾没参与判断。
    will_run_h = min(float(hours or 0) or max_min / 60, max_min / 60)
    est_real = hourly * will_run_h

    _say(f"计划 {gpu} ×1 · 镜像 {img.get('Name')} · ≈{hourly:.2f}/h")
    _say(f"预算上限 ¥{cap:.2f}（预计跑 {will_run_h * 60:.0f} 分钟 ≈ ¥{est_real:.2f}）")

    # ---- 预算闸门（2026-11-09 审计：cap 以前只打印不校验）----
    # 这是唯一能拦住"误提交烧钱"的地方：超过上限直接拒绝建机。
    if cap > 0 and est_real > cap:
        raise RunError(
            f"预估 ¥{est_real:.2f}（{hourly:.2f}/h × {will_run_h * 60:.0f} 分钟）"
            f"超过作业预算上限 ¥{cap:.2f}。请调小帧数/分辨率，或截短驱动视频，"
            f"或显式提高 spec.budget.max_cost。",
            state.FAILED_PROVISION)

    bal = float(cs.balance().get("AmountAvailable") or 0)
    if bal < 5:
        raise RunError(f"余额 ¥{bal:.2f} < ¥5，中止建机", state.FAILED_PROVISION)
    # 余额必须够跑完这一单，否则跑到一半没钱会留下孤儿实例
    if cap > 0 and bal < est_real:
        raise RunError(
            f"余额 ¥{bal:.2f} 不足以覆盖本作业预估 ¥{est_real:.2f}，中止建机",
            state.FAILED_PROVISION)

    # ---- 并发闸门（§1 L4「并发」的另一半）----
    # 上一轮把全局串行锁改成了作业级锁，并行度一下从 1 变成"想开几台开几台"。
    # 账户只有 5 台配额，5 × ¥2.15/h = ¥10.75/h，而余额长期 ¥8 上下 ——
    # 这不是理论风险，是点五次"提交并执行"就能触发的事。
    # 判据用**云端 running 实例数**，不用本地 job.json：孤儿作业（进程被 kill、
    # 状态卡在 RUNNING）在本地看是一个作业，在云端是一台正在计费的机器。
    from tools.p2 import concurrency as conc
    slot_ok, slot_why = conc.check_provision()
    if not slot_ok:
        raise RunError(f"{slot_why}", state.FAILED_PROVISION)
    _say(f"并发检查通过：{slot_why}")

    # 预热池优先：池里有停机实例就直接 start（~12s），不消耗现货
    if getattr(opts, "from_pool", False):
        got = None
        try:
            from tools.p2 import pool as poolmod
            got = poolmod.acquire(job["job_id"])
        except Exception as e:
            _say(f"⚠ 预热池不可用（{type(e).__name__}: {e}），回落新建")
        if got:
            uid = got["uhost_id"]
            j = state.load_job(job["job_id"])
            j.setdefault("instance", {})
            j["instance"].update({
                "uhost_id": uid, "gpu": gpu, "zone": cs.zone, "from_pool": True,
                "up_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
            store.atomic_write_json(store.job_file(job["job_id"]), j)
            _note_instance(uid)          # 信号兜底：进程被打断时把实例停掉
            _say(f"复用预热池实例 {uid}（start {got.get('t_start')}s · 就绪 {got.get('t_ready')}s）")
            return {"uhost_id": uid, "inst": got["inst"], "hourly": float(price.get("total") or 0),
                    "host": got["host"], "port": got["port"], "password": got["password"],
                    "from_pool": True}
        _say("池内无空闲实例，回落到新建")

    # 库存检查通过≠下单成功：热门卡会被别人抢（226604 竞态），重试几次
    ids, last_err = None, None
    for attempt in range(1, CREATE_RETRY + 1):
        try:
            ids = cs.create(image_id=img["CompShareImageId"], gpu_type=gpu, gpu=1,
                            cpu=cpu_n, memory_gb=mem_gb, name=f"p1-{job['job_id'][-6:]}",
                            charge_type="Postpay", disk_gb=100, data_disk_gb=0,
                            remark="p1 single-task")
            break
        except CompShareError as e:
            last_err = str(e)
            if "226604" in last_err and attempt < CREATE_RETRY:
                _say(f"  下单被抢（{attempt}/{CREATE_RETRY}），20s 后重试")
                time.sleep(20)
                continue
            raise RunError(f"建机失败：{last_err[:200]}", state.FAILED_PROVISION) from e
    if not ids:
        raise RunError(f"建机失败：{str(last_err)[:200]}", state.FAILED_PROVISION)
    uid = ids[0]
    _say(f"已创建 {uid}，等 Running…")
    inst = cs.wait_state(uid, {"running"}, timeout=900)

    # Running ≠ sshd 就绪（实测还要 20–40s），不探活就 push 必然失败
    host, port = cs.public_ip(inst), "23"
    pwd = cs.password_of(inst)
    for i in range(30):
        rc, out = _ssh_run(host, port, pwd, "echo ssh-ready", timeout=25)
        if rc == 0 and "ssh-ready" in out:
            _say(f"sshd 就绪（{i * 5}s）")
            break
        time.sleep(5)
    else:
        raise RunError("sshd 60 次探活仍未就绪", state.FAILED_PROVISION)
    opts_ctx = {"host": host, "port": port, "password": pwd}

    j = state.load_job(job["job_id"])
    j.setdefault("instance", {})
    j["instance"].update({"uhost_id": uid, "gpu": gpu, "zone": cs.zone,
                          "up_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    store.atomic_write_json(store.job_file(job["job_id"]), j)
    _note_instance(uid)              # 信号兜底：进程被打断时把实例停掉
    return {"uhost_id": uid, "inst": inst, "hourly": float(price.get("total") or 0),
            **opts_ctx}


def run_setup(uid: str, need: str = "all", ctx: dict | None = None) -> None:
    """建机后配环境：挂载 / 禁 Manager / 软链 / 启动 / 自检（按任务类型收敛）。"""
    script = ROOT / "tools" / "p1" / "instance_setup.sh"
    if not script.exists():
        raise RunError("instance_setup.sh 缺失", state.FAILED_PROVISION)
    _say(f"配置实例环境（挂载 + 禁 Manager + 软链 + 启动），资产需求={need}")
    env = dict(os.environ)
    env.update({"P1_HOST": ctx.get("host", ""), "P1_PORT": str(ctx.get("port", "23")),
                "P1_PASSWORD": ctx.get("password", "")})
    p = subprocess.run(["bash", str(script), uid, f"--need={need}"],
                       capture_output=True, text=True, timeout=900, env=env)
    tail = ((p.stdout or "") + (p.stderr or ""))[-800:]
    if p.returncode != 0:
        raise RunError(f"setup 失败：{tail}", state.FAILED_PROVISION)
    _say("setup 完成，自检通过")


def _registry_meta(job: dict) -> dict:
    """registry 作业的元数据；legacy 作业返回 {}（所有新分支都以它为准，保证向后兼容）。"""
    return ((job.get("model") or {}).get("registry") or {})


def _apply_model_links(job: dict, ctx: dict) -> None:
    """registry 模型可在配置里声明软链（setup.links）；legacy 作业零操作。"""
    reg = _registry_meta(job)
    if not reg:
        return
    from tools.p20 import assets as p20assets, registry as p20registry
    model = p20registry.get_model(reg["model_key"], version=reg.get("model_version"))
    ssh = lambda cmd, timeout=60: _ssh_run(ctx["host"], ctx["port"],   # noqa: E731
                                          ctx["password"], cmd, timeout=timeout)

    # 1) 配置里显式声明的软链（旧口径，保留兼容）
    links = p20assets.apply_links(model, ssh)
    bad = [x for x in links if not x.get("ok")]
    if links:
        _say(f"模型软链 {len(links) - len(bad)}/{len(links)} 就绪")
    if bad:
        raise RunError(f"模型软链失败：{bad[0].get('link')}（{bad[0].get('out')}）",
                       state.FAILED_PROVISION)

    # 2) 自动补链：按模型卡里的文件名去 /model 找，再链进 ComfyUI/models/<类型>/
    #    静态表 compat_links.txt 覆盖不到新导入的 pack_* 模型，这里兜底。
    try:
        missing = p20assets.link_model_files(model, ssh)
    except Exception as e:                      # noqa: BLE001
        _say(f"  ⚠ 自动补链跳过（{type(e).__name__}: {e}）")
        return
    if missing:
        _say(f"  ⚠ 这些权重在 /model 里没找到：{', '.join(missing[:6])}")
    else:
        _say("  自动软链已就绪（按文件名匹配 /model）")


def step_ready(job: dict, opts, ctx: dict) -> None:
    """等 ComfyUI 就绪 + 模型资产可见。

    registry 作业：资产清单来自 registry/models/*.json（不再写死 H3/Qwen 文件名）；
    legacy 作业：保持原逻辑一字不动。
    """
    host, port, pwd = ctx["host"], ctx["port"], ctx["password"]
    reg = _registry_meta(job)
    if reg:
        from tools.p20 import assets as p20assets, registry as p20registry
        try:
            model = p20registry.get_model(reg["model_key"],
                                          version=reg.get("model_version"))
        except Exception as e:
            raise RunError(f"registry 模型读取失败：{e}", state.FAILED_PROVISION) from e
        ok, missing, detail = p20assets.run_check(
            model, lambda cmd, timeout=90: _ssh_run(host, port, pwd, cmd, timeout=timeout))
        if ok:
            _say(f"ComfyUI 就绪，{len(p20assets.asset_rules(model))} 项模型资产可见"
                 f"（第 {detail.get('attempt')} 次探活）")
            return
        why = detail.get("why") or detail.get("error") or ""
        raise RunError(f"模型资产不可见：{missing or why}（{str(why)[:200]}）",
                       state.FAILED_PROVISION)

    check = ("source /root/miniconda3/bin/activate && python3 -c \""
             "import json,urllib.request;"
             "oi=json.load(urllib.request.urlopen('http://127.0.0.1:8188/object_info',timeout=30));"
             "ul=oi['UNETLoader']['input']['required']['unet_name'][0];"
             "print('OK' if any('dasiwaHybridV2' in u for u in ul) or any('qwen_image' in u for u in ul) else 'NO_MODEL')\"")
    for i in range(20):
        rc, out = _ssh_run(host, port, pwd, check, timeout=90)
        if "OK" in out:
            _say("ComfyUI 就绪，模型可见")
            return
        time.sleep(6)
    raise RunError("ComfyUI 20 次探活未就绪", state.FAILED_PROVISION)


def step_submit(job: dict, opts, ctx: dict) -> None:
    """渲染 workflow 并提交，拿 prompt_id。

    registry 作业：节点/参数绑定全部来自配置（render_job），run.py 不认识任何节点名；
    legacy 作业：仍走写死 5 个占位符的 _render_workflow，行为不变。
    """
    wf_path = ROOT / job["model"]["workflow"]
    if not wf_path.exists():
        raise RunError(f"workflow 不存在：{wf_path}", state.FAILED_RUN)
    reg = _registry_meta(job)
    if reg:
        from tools.p20 import render as p20render
        try:
            graph, meta = p20render.render_job(job)
        except Exception as e:
            raise RunError(f"registry 渲染失败：{type(e).__name__}: {e}", state.FAILED_RUN) from e
        store.atomic_write_json(store.job_file(job["job_id"], "render.json"),
                                {"schema": "p20.render/v1", "job_id": job["job_id"],
                                 "meta": meta, "graph": graph})
        _say(f"registry 渲染：{meta['workflow_key']} v{meta['workflow_version']} × "
             f"{meta['model_key']} v{meta['model_version']} · {meta['nodes']} 节点")
        # 记下 output 节点：fetch 阶段要把它的产物排到主产物位
        try:
            from tools.p20 import registry as p20registry
            wf_rec = p20registry.get_workflow(reg["workflow_key"],
                                             version=reg.get("workflow_version"))
            ctx["output_node"] = str((wf_rec.get("output") or {}).get("node") or "")
        except Exception as e:
            _say(f"  ⚠ 取 output 节点失败：{e}")
            ctx["output_node"] = ""
    else:
        wf = json.loads(wf_path.read_text(encoding="utf-8"))
        graph = _render_workflow(wf, job.get("params") or {}, job["job_id"])

    host, port, pwd = ctx["host"], ctx["port"], ctx["password"]

    # img2img / inpaint：把本地输入图推到实例 input/（job.params.image_local 由 spec 归一化写入）
    params = job.get("params") or {}
    _ssh_run(host, port, pwd, "mkdir -p /root/ComfyUI/input", timeout=60)
    for key in [k for k in params if re.fullmatch(r"(?:image|mask|audio|video)(?:_\d+)?", k)]:
        remote_name = params.get(key)
        if not remote_name:
            continue
        local_path = ROOT / "reports/p2/p21/inputs" / remote_name
        if not local_path.is_file():
            continue
        if not _scp_put(host, port, pwd, local_path, f"/root/ComfyUI/input/{remote_name}"):
            raise RunError(f"输入文件上传失败：{remote_name}", state.FAILED_RUN)
        _say(f"输入已上传：{key}={remote_name}")

    payload = json.dumps({"prompt": graph, "client_id": "p1"}, ensure_ascii=False)
    cmd = ("source /root/miniconda3/bin/activate && python3 -c \""
           "import json,sys,urllib.request;"
           f"p=json.loads(sys.argv[1]);"
           "r=urllib.request.Request('http://127.0.0.1:8188/prompt',data=json.dumps(p).encode(),"
           "headers={'Content-Type':'application/json'});"
           "print(urllib.request.urlopen(r,timeout=120).read().decode())\" "
           + "'" + payload.replace("'", "'\"'\"'") + "'")
    rc, out = _ssh_run(host, port, pwd, cmd, timeout=180)
    from tools.p20 import comfy as p20comfy
    pid = p20comfy.parse_prompt_id(out)
    if not pid:
        raise RunError(f"提交失败：{out[-400:]}", state.FAILED_RUN)
    ctx["prompt_id"] = pid
    _say(f"已提交 prompt_id={pid}")


def step_wait(job: dict, opts, ctx: dict) -> list[tuple[str, str]]:
    """轮询 history 直到完成，返回远端文件列表。

    两条上限同时在跑，取先到的那个：

      · `max_minutes` —— 推理超时。**已经有效**（本函数下面那个 while）。
      · `max_cost`    —— 钱的上限。**以前不生效**：建机前查过一次预估，
        建机之后就不再管了。于是「预估 20 分钟 ¥0.72，实际卡了 40 分钟 ¥1.43」
        这种情况会静默超支 —— 建机前的闸门只看"能不能开始"，管不到"跑多久"。

    为什么按时间折算而不去查余额：查余额要一次 HTTP 往返，而这里是 5 秒一轮的
    轮询；而且余额被多条作业共享，查出来的差额不一定是"本作业"花的。
    按 `hourly × 已跑分钟` 折算只依赖自己知道的信息，可复算、可解释。
    """
    host, port, pwd = ctx["host"], ctx["port"], ctx["password"]
    pid = ctx["prompt_id"]
    budget = job.get("budget") or {}
    limit = int(budget.get("max_minutes") or 20) * 60
    cap = float(budget.get("max_cost") or 0)
    hourly = float(ctx.get("hourly") or 0)
    # 钱能买多少秒：cap / 每小时价 × 3600。cap<=0（不限）或 hourly<=0（未知）时不管。
    cost_limit = int(cap / hourly * 3600) if (cap > 0 and hourly > 0) else 0
    if cost_limit:
        _say(f"预算 {cap:.2f} / {hourly:.2f} 每小时 → 最多跑 {cost_limit // 60} 分钟")
        # 钱的上限比时间上限更紧时，超时口径跟着钱走
        if cost_limit < limit:
            limit = cost_limit
    t0 = time.time()

    cmd = ("source /root/miniconda3/bin/activate && python3 -c \""
           "import json,sys,urllib.request;"
           f"h=json.load(urllib.request.urlopen('http://127.0.0.1:8188/history/'+sys.argv[1],timeout=60));"
           f"pid=sys.argv[1];"
           "e=h.get(pid);"
           "print(json.dumps(e if e else {}))\" " + pid)
    fail_streak = 0
    last_note = 0.0
    while time.time() - t0 < limit:
        rc, out = _ssh_run(host, port, pwd, cmd, timeout=120)
        if rc == 0:
            fail_streak = 0
            from tools.p20 import comfy as p20comfy
            ent = p20comfy.parse_history(out)
            if ent:
                st = p20comfy.status_of(ent)
                if st == "success":
                    files = p20comfy.extract_files(ent)
                    if not files:
                        raise RunError("任务成功但无输出文件", state.FAILED_FETCH)
                    # 文件名 → 产出节点：主产物判定要用 output 节点，不能靠文件名排序
                    try:
                        by_node = p20comfy.extract_files_by_node(ent)
                        ctx["file_nodes"] = {fn: nid for nid, _sub, fn in by_node}
                    except Exception:
                        ctx["file_nodes"] = {}
                    _say(f"推理完成：{len(files)} 个文件，耗时 {int(time.time()-t0)}s")
                    return sorted(files)
                if st == "error":
                    raise RunError(f"推理失败：{p20comfy.extract_error(ent)}", state.FAILED_RUN)
        else:
            # 查询失败可能是 ssh 抖动，也可能是 ComfyUI 挂了（OOM/崩溃）。
            # 以前这里只会一直等到 max_minutes（45 分钟）白烧 GPU：
            # 每 30s 连续失败就去问一次进程还在不在。
            fail_streak += 1
            if fail_streak % 6 == 1:
                alive = _comfy_alive(host, port, pwd)
                if alive is False:
                    raise RunError(
                        f"ComfyUI 进程已退出（第 {int(time.time()-t0)}s，疑似显存不足/崩溃）",
                        state.FAILED_RUN)
        elapsed = int(time.time() - t0)
        if elapsed - last_note >= 120:
            spent = hourly * elapsed / 3600 if hourly > 0 else None
            money = f" · 已花 ≈¥{spent:.2f}/{cap:.2f}" if (spent is not None and cap > 0) else ""
            _say(f"  推理中… 已等 {elapsed}s{money}（每 2 分钟一次心跳）")
            last_note = elapsed
        time.sleep(5)
    # 分两种超时：钱先到 vs 时间先到。混成一句会让用户不知道调哪个参数。
    if cost_limit and cost_limit <= int(budget.get("max_minutes") or 20) * 60:
        raise RunError(
            f"预算超时：按 ¥{hourly:.2f}/h 折算，¥{cap:.2f} 只够跑 {cost_limit // 60} 分钟，"
            f"已用尽（推理仍未完成）。请调小帧数/分辨率，或提高 budget.max_cost。",
            state.FAILED_RUN)
    raise RunError(f"推理超时（{limit}s）", state.FAILED_RUN)


_COMFY_PROC_RE = re.compile(r"[m]ain\.py")


def _parse_comfy_alive(out: str, rc: int) -> bool | None:
    """从一次探活的输出里判断 ComfyUI 死活。

    三个坑（2026-11-07 修）：
      1. 命令串自己会进远端 `bash -c` 的 cmdline，`pgrep -af 'ComfyUI.*main.py'`
         会匹配到这条 shell，于是恒真 —— 模式写成 `[m]ain\.py` 才躲得掉自匹配；
      2. 真实启动命令是 `python -u main.py --listen ...`，不带 ComfyUI 路径，
         所以模式里不能依赖 ComfyUI 字样；
      3. 只看进程不看端口，会被同名脚本骗过去，所以补一发 /system_stats。

    True=活着；False=确实没了；None=这台机器 ssh 都不通，别判死刑。
    """
    text = out or ""
    proc, marker, code = text.partition("---PROC---")
    for line in proc.splitlines():
        # 探活命令自己会出现在远端 bash 的 cmdline 里，跳过这些行再找进程
        if "pgrep" in line or "bash -c" in line or "system_stats" in line:
            continue
        if _COMFY_PROC_RE.search(line):
            return True
    if code.strip().startswith("200"):
        return True
    # 看到 ---PROC--- 就说明远端命令真的跑完了（curl 连不上会返回 7，别把退出码当"未知"）
    if marker:
        return False
    if rc == 0:
        return False
    return None


def _comfy_alive(host: str, port: str, pwd: str) -> bool | None:
    """ComfyUI 进程还在吗？True/False；ssh 都连不上就 None（别乱判死刑）。"""
    rc, out = _ssh_run(host, port, pwd,
                       "pgrep -af '[m]ain\\.py --listen' | head -5; echo '---PROC---'; "
                       "curl -s -m 3 -o /dev/null -w '%{http_code}' "
                       "http://127.0.0.1:8188/system_stats; true",
                       timeout=60)
    return _parse_comfy_alive(out, rc)


def step_fetch(job: dict, opts, ctx: dict, files: list[tuple[str, str]]) -> list[Path]:
    """拉产物到 jobs/<id>/artifacts/。"""
    jdir = store.job_dir(job["job_id"])
    art = jdir / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    local = []
    primary_node = str(ctx.get("output_node") or "")
    file_nodes = ctx.get("file_nodes") or {}
    for idx, (sub, fn) in enumerate(files, 1):
        remote = f"/root/ComfyUI/output/{sub}/{fn}" if sub else f"/root/ComfyUI/output/{fn}"
        # 00- 前缀保证 output 节点的产物排在最前，verify 以它为唯一主产物
        if primary_node and str(file_nodes.get(fn, "")) == primary_node:
            dst = art / f"00-primary-{fn}"
        else:
            dst = art / f"{idx:02d}-{fn}"
        _say(f"拉取 {fn}（{idx}/{len(files)}）…")
        if not _scp_get(ctx["host"], ctx["port"], ctx["password"], remote, dst):
            raise RunError(f"拉取失败（已成功 {len(local)}/{len(files)}）：{remote}",
                           state.FAILED_FETCH)
        local.append(dst)
    return local


def step_verify(job: dict, opts, ctx: dict, arts: list[Path]) -> dict:
    """跑复验项。

    registry 作业用「渲染后的图 + 配置里的绑定」回读参数（不再依赖 verify.py 的节点白名单）；
    legacy 作业仍用模板图 + 内置 _PARAM_NODES。
    """
    jdir = store.job_dir(job["job_id"])
    reg = _registry_meta(job)
    if reg:
        from tools.p20 import registry as p20registry
        wf = p20registry.get_workflow(reg["workflow_key"], version=reg.get("workflow_version"))
        rr = store.read_json(jdir / "render.json") or {}
        graph = rr.get("graph") or {}
        res = verify_mod.verify_job(job, jdir / "artifacts", None,
                                    rendered_graph=graph, wf_config=wf)
    else:
        wf_path = ROOT / job["model"]["workflow"]
        res = verify_mod.verify_job(job, jdir / "artifacts", wf_path)
    for a in res.get("artifacts", []):
        _say(f"  {a['file']}: {a['verdict']}  failed={a['failed'] or '—'}")
    if res["verdict"] != "PASS":
        bad = [f"{a['file']}: {','.join(a['failed'])}"
               for a in (res.get("artifacts") or []) if a["verdict"] != "PASS"]
        raise RunError(f"校验未通过：{'; '.join(bad) or res.get('reason') or '?'}",
                       state.FAILED_VERIFY)

    # ---- 质量评分（L6 补完：七项只证明"文件成功"，这里才回答"质量成功"）----
    # 默认 mode=report：只记录分数进 render.json，**不改 PASS/FAIL**。
    # 理由：现存 30 条 verified 都是按七项判的，把质量分设成默认门禁等于
    # 全部重判一次；那需要人一条条看，不是一次改动能决定的。
    res["quality"] = _score_quality(job, res, arts)
    return res


def _score_quality(job: dict, res: dict, arts: list[Path]) -> dict:
    """给产物打质量分。失败不影响作业结论 —— 评分器自己不该成为故障源。"""
    try:
        from tools.p2 import quality as q
        cfg = q.from_expect(job.get("expect"))
        # 主产物排首位（fetch 阶段已保证）；只评主产物 + 至多 2 个附加产物，
        # 避免一份作业里 8 路输出各跑一遍 ffmpeg
        targets = list(arts)[:3]
        kind = "image" if res.get("kind") == "image" else "video"
        rep = q.assess_job(targets, kind=kind, **cfg)
        d = rep.as_dict()
        grade = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🔴"}.get(d["grade"], "⚪")
        _say(f"  质量 {grade} {d['overall']:.1f}（{d['grade']}）· 模式 {d['mode']}")
        for a in d["artifacts"]:
            for i in a["issues"]:
                _say(f"    ! {a['file']}: {i}")
        if d["mode"] == "gate" and d["verdict"] != "PASS":
            raise RunError(f"质量门禁未通过：{d['overall']:.1f} / {d['grade']}",
                           state.FAILED_VERIFY)
        return d
    except RunError:
        raise
    except Exception as e:                      # noqa: BLE001
        _say(f"  ⚠ 质量评分跳过（{type(e).__name__}: {e}）")
        return {"error": f"{type(e).__name__}: {e}"}


def step_persist(job: dict, opts, ctx: dict, res: dict, arts: list[Path]) -> dict:
    """写 manifest.json（原子写 + 读回校验）。"""
    import hashlib

    jdir = store.job_dir(job["job_id"])
    job = state.load_job(job["job_id"])      # 重新加载：up_at 是 provision 时写入的
    inst = job.get("instance") or {}
    now = dt.datetime.now().astimezone()
    up_at = inst.get("up_at")
    minutes = 0.0
    if up_at:
        try:
            minutes = round((now - dt.datetime.fromisoformat(up_at)).total_seconds() / 60, 1)
        except ValueError:
            pass
    hourly = float(ctx.get("hourly") or 0)

    mans = []
    for a in res.get("artifacts", []):
        p = jdir / "artifacts" / a["file"]
        data = p.read_bytes()
        mans.append({
            "rel": f"artifacts/{a['file']}",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "probe": a["details"].get("decode", {}),
            "match": {k: bool(v) for k, v in a["checks"].items() if v is not None},
        })

    first = res["artifacts"][0]
    checks = {k: bool(v) for k, v in first["checks"].items() if v is not None}
    manifest = {
        "schema": "p1.manifest/v1",
        "job_id": job["job_id"],
        "params_hash": job["params_hash"],
        "persisted_at": now.isoformat(timespec="seconds"),
        "instance": {"uhost_id": inst.get("uhost_id"), "gpu": inst.get("gpu"),
                     "zone": inst.get("zone"), "up_at": up_at,
                     "release_at": None, "minutes": minutes},
        "artifacts": mans,
        "checks": checks,
        "verdict": "PASS",
        # 质量评分（L6）：七项技术判据之上的内容层结论。
        # 与 checks 并列而不是并入：checks 是 PASS/FAIL，quality 是 0-100 分，
        # 混在一起会让"PASS 但 32 分"这种关键信号被吞掉。
        "quality": res.get("quality") or {},
        "cost": {"hourly": round(hourly, 2), "minutes": minutes,
                 "amount": round(hourly * minutes / 60, 4)},
        "state_history": [e.get("to") for e in state.read_events(job["job_id"]) if e.get("to")],
    }
    reg = _registry_meta(job)
    if reg:
        rr = store.read_json(jdir / "render.json") or {}
        meta = rr.get("meta") or {}
        manifest["registry"] = {
            "workflow_key": reg.get("workflow_key"),
            "workflow_version": reg.get("workflow_version"),
            "model_key": reg.get("model_key"),
            "model_version": reg.get("model_version"),
            "compat": (meta.get("compat") or {}).get("status") or reg.get("compat"),
            "compat_basis": (meta.get("compat") or {}).get("basis") or reg.get("compat_basis"),
            "graph_sha256": meta.get("graph_sha256"),
            "template_sha256": meta.get("template_sha256"),
            "registry_fingerprint": meta.get("registry_fingerprint"),
        }
    man_path = jdir / "manifest.json"
    store.atomic_write_json(man_path, manifest)
    back = store.read_json(man_path)
    if not back or back.get("verdict") != "PASS":
        raise RunError("manifest 读回校验失败", state.FAILED_VERIFY)
    _say(f"manifest 已落盘（{len(mans)} 个产物，{minutes} 分钟）")
    return manifest


def step_release(job: dict, opts, ctx: dict) -> None:
    """销毁实例，8903 过渡期拒绝时重试。"""
    from compshare.cs import CompShareError

    uid = ctx.get("uhost_id")
    if not uid:
        _say("无实例可销毁")
        return

    # ---- 借来的实例（规则 B：同工作流串行）----
    # 不销毁，只更新槽位的空闲时间。下一单进来会接着用这台机器
    # （装载的模型还在显存里，省 21~53 秒）。
    # 真正的回收交给 tools/p2/reaper.py：空闲超阈值才关机。
    if ctx.get("borrowed"):
        try:
            from tools.p2 import instance_slots as slots
            slots.touch(uid, job_id=job["job_id"], busy=False)
            _say(f"{uid} 是复用实例，已归还槽位（不销毁，等下一单或空闲回收）")
        except Exception as e:                      # noqa: BLE001
            _say(f"  ⚠ 归还槽位失败（{type(e).__name__}: {e}），实例保留")
        return

    # 预热池实例：停机回池（下次 12s 起），不销毁
    try:
        jk = state.load_job(job["job_id"])
        from_pool = bool((jk.get("instance") or {}).get("from_pool"))
    except Exception:
        from_pool = bool(ctx.get("from_pool"))
    if from_pool:
        try:
            from tools.p2 import pool as poolmod
            poolmod.release(uid, keep=True)
            _say(f"{uid} 已停机回池（保留复用）")
            return
        except Exception as e:
            _say(f"⚠ 回池失败（{type(e).__name__}: {e}），改为销毁")

    cs = _cs(getattr(opts, "creds", None))
    last = None
    for attempt in range(1, RETRY_8903 + 1):
        try:
            cs.terminate(uid)
            _say(f"已提交销毁 {uid}")
            return
        except CompShareError as e:
            msg = str(e)
            last = msg
            if "8903" in msg or "in the task" in msg.lower():
                _say(f"  8903 过渡期拒绝（{attempt}/{RETRY_8903}），{RETRY_8903_WAIT}s 后重试")
                time.sleep(RETRY_8903_WAIT)
                continue
            raise RunError(f"销毁失败：{msg[:200]}", state.FAILED)
    raise RunError(f"销毁连续 {RETRY_8903} 次被拒：{last[:200]}", state.FAILED)


def step_done(job: dict, opts, ctx: dict, manifest: dict) -> None:
    """补 manifest 的 release_at，收尾。

    顺带把「这台机器上跑成功过这个工作流」记进槽位表 ——
    这是「已验证集」的来源，规则 B 靠它判断能不能复用。
    """
    jdir = store.job_dir(job["job_id"])
    m = store.read_json(jdir / "manifest.json") or manifest
    m.setdefault("instance", {})["release_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    m["state_history"] = [e.get("to") for e in state.read_events(job["job_id"]) if e.get("to")]
    store.atomic_write_json(jdir / "manifest.json", m)

    # 登记槽位（只有 registry 作业有 workflow_key）
    reg = _registry_meta(job)
    uid = ctx.get("uhost_id")
    if not (reg and uid and m.get("verdict") == "PASS"):
        return
    try:
        from tools.p2 import instance_slots as slots
        wk = str(reg.get("workflow_key") or "")
        mk = str(reg.get("model_key") or "")
        nodes, assets = slots.workflow_needs(wk, mk)
        slot = slots.get(str(uid))
        if not slot:
            inst = ctx.get("inst") or {}
            slot = slots.Slot(uid=str(uid), model_key=mk,
                              gpu_type=str(inst.get("GpuType") or "4090"),
                              state="running", started_at=time.time())
            slots.upsert(slot)
        slots.mark_workflow_ok(str(uid), wk, nodes=nodes, assets=assets)
    except Exception as e:                      # noqa: BLE001
        _say(f"  ⚠ 槽位登记失败（{type(e).__name__}: {e}）")


# ------------------------------------------------------------ 编排
STEP_STATE_MAP = {
    "provision": state.PROVISIONING, "setup": None, "ready": state.READY,
    "submit": state.SUBMITTED, "wait": state.RUNNING, "fetch": state.FETCHING,
    "verify": state.VERIFYING, "persist": state.PERSISTED, "release": state.RELEASING,
}


def _need(job: dict) -> str:
    """setup 资产档位：registry 作业读模型配置，legacy 仍按模型 key 猜。"""
    reg = _registry_meta(job)
    if reg.get("setup_need"):
        return reg["setup_need"]
    return "qwen" if str(job["model"]["key"]).startswith("qwen") else "h3"


FAIL_STATE_OF_STEP = {
    "provision": state.FAILED_PROVISION, "setup": state.FAILED_PROVISION,
    "ready": state.FAILED_PROVISION, "submit": state.FAILED_RUN,
    "wait": state.FAILED_RUN, "fetch": state.FAILED_FETCH,
    "verify": state.FAILED_VERIFY, "persist": state.FAILED_VERIFY,
    "release": state.FAILED,
}


def _run_posts(job: dict, job_id: str) -> None:
    """执行 job.post 的本地步骤（拼接），产物进 artifacts/，manifest 追加 derived。

    D5：位置在 PERSISTED 之后、RELEASING 之前；本地零 GPU，不影响 G1（已落盘）。
    post 失败不改变主产物 verdict，只记录告警。
    """
    posts = job.get("post") or []
    if not posts:
        return
    try:
        from tools.p2 import concat as concat_mod
    except Exception as e:  # 工具链缺失不应拖垮闭环
        _say(f"  ⚠ post 跳过（concat 不可用：{e}）")
        return
    jdir = store.job_dir(job_id)
    _say(f"post：{len(posts)} 个本地步骤")
    results = concat_mod.run_posts(job, jdir / "artifacts")
    man = store.read_json(store.job_file(job_id, "manifest.json")) or {}
    if man:
        man["derived"] = results
        store.atomic_write_json(store.job_file(job_id, "manifest.json"), man)
    for r in results:
        flag = "ok" if r.get("ok") else f"FAIL：{r.get('reason')}"
        _say(f"  {r.get('op')} → {r.get('out')} {flag}")


def _do_step(name: str, job_id: str, job: dict, opts, ctx: dict) -> None:
    """执行单步动作（不含状态转移）。"""
    # 测试钩子：P1_FAIL_AT=fetch 可在指定步骤注入失败，用于验证续跑
    if os.environ.get("P1_FAIL_AT") == name:
        raise RunError(f"注入失败（测试）：{name}",
                       FAIL_STATE_OF_STEP.get(name, state.FAILED))
    if name == "provision":
        ctx.update(step_provision(job, opts))
        if getattr(opts, "batch", False) and ctx.get("uhost_id"):
            try:
                from tools.p2 import jobqueue
                jobqueue.set_instance(job_id, ctx["uhost_id"])
            except Exception:
                pass
        _mark_step(job_id, name, uhost_id=ctx.get("uhost_id"),
                   gpu=(ctx.get("inst") or {}).get("GpuType"), hourly=ctx.get("hourly"))
    elif name == "setup":
        run_setup(ctx["uhost_id"], _need(job), ctx)
        _apply_model_links(job, ctx)
        _mark_step(job_id, name)
    elif name == "ready":
        step_ready(job, opts, ctx)
        _mark_step(job_id, name)
    elif name == "submit":
        step_submit(job, opts, ctx)
        _mark_step(job_id, name, prompt_id=ctx.get("prompt_id"))
    elif name == "wait":
        ctx["files"] = step_wait(job, opts, ctx)
        _mark_step(job_id, name, files=[list(f) for f in ctx["files"]])
    elif name == "fetch":
        ctx["arts"] = step_fetch(job, opts, ctx, ctx["files"])
        _mark_step(job_id, name, local=[a.name for a in ctx["arts"]])
    elif name == "verify":
        ctx["res"] = step_verify(job, opts, ctx, ctx["arts"])
        _mark_step(job_id, name, verdict=ctx["res"].get("verdict"))
    elif name == "persist":
        ctx["man"] = step_persist(job, opts, ctx, ctx["res"], ctx["arts"])
        _run_posts(job, job_id)
        _mark_step(job_id, name)
    elif name == "release":
        if ctx.get("salvage"):
            _say("salvage：实例早已销毁，跳过销毁动作")
        else:
            step_release(job, opts, ctx)
        _mark_step(job_id, name)
    else:
        raise RunError(f"未知步骤 {name}", state.FAILED)


def _execute(job_id: str, job: dict, opts, ctx: dict, start_at: str = "provision") -> dict:
    """从 start_at 开始顺序执行剩余步骤（每步先转移状态再执行）。"""
    names = state.STEP_ORDER[state.STEP_ORDER.index(start_at):]
    for name in names:
        st = STEP_STATE_MAP.get(name)
        if st:
            cur = state.load_job(job_id).get("state")
            if cur != st:
                state.transition(job_id, st, reason=f"步骤 {name}")
        _do_step(name, job_id, job, opts, ctx)
    return ctx


def _next_step(job: dict) -> str | None:
    steps = job.get("steps") or {}
    for name in state.STEP_ORDER:
        if not (steps.get(name) or {}).get("done"):
            return name
    return None


def _rebuild_ctx(job: dict, creds: dict[str, str] | None = None) -> dict:
    """续跑时重建上下文：从 uhost_id 反查 host/密码，从 steps 取 prompt_id。"""
    inst = job.get("instance") or {}
    uid = inst.get("uhost_id")
    if not uid:
        return {}
    cs = _cs(creds)
    info = cs.get(uid)
    steps = job.get("steps") or {}
    ctx = {
        "uhost_id": uid,
        "host": cs.public_ip(info),
        "port": "23",
        "password": cs.password_of(info),
        "inst": info,
        "hourly": (steps.get("provision") or {}).get("hourly"),
        "prompt_id": (steps.get("submit") or {}).get("prompt_id"),
    }
    # 恢复中间产物：断点靠后的步骤要依赖前面步骤的产出
    wf = (steps.get("wait") or {}).get("files")
    if wf:
        ctx["files"] = [tuple(f) for f in wf]
    fl = (steps.get("fetch") or {}).get("local")
    if fl:
        jdir = store.job_dir(job["job_id"])
        ctx["arts"] = [jdir / "artifacts" / n for n in fl]
    return ctx


KEEP_MINUTES = 15          # 失败后实例保留时长（到点自动关机）


def _keep_instance(job_id: str, ctx: dict, opts) -> bool:
    """失败后保留实例：设定时关机兜底，提示可续跑/可 abort。

    返回 True 表示实例被保留（调用方不要再销毁）。
    """
    uid = (ctx or {}).get("uhost_id")
    if not uid or getattr(opts, "abort", False) or getattr(opts, "no_keep", False):
        return False
    try:
        cs = _cs(getattr(opts, "creds", None))
        ts = int(time.time() + KEEP_MINUTES * 60)
        cs.stop_at(uid, ts)
        # 说明：Describe 不返回 SchedulerStopTime，所以查实例看到的该字段永远是 None，
        # 不代表没设上。2026-10-28 实测：设 23:45:42 → 23:46:59 State 变为 Stopped。
        _say(f"实例 {uid} 已保留（{KEEP_MINUTES} 分钟后自动关机兜底）")
        _say(f"  续跑：python3 tools/p1/run.py {job_id} --resume")
        _say(f"  放弃：python3 tools/p1/run.py {job_id} --abort")
        return True
    except Exception as e:
        _say(f"  ⚠ 保留实例失败（将走清理路径）：{e}")
        return False


def _resume(job_id: str, job: dict, opts) -> int:
    """从 FAILED_* 续跑：RESUMING → 重放到断点状态 → 继续执行。"""
    cur = job.get("state")
    if cur in (state.DONE, state.FAILED, state.ABORTED):
        _say(f"✗ 作业已是终态 {cur}，无需续跑")
        return 0 if cur == state.DONE else 1
    if cur not in state.FAIL_BRANCH and cur not in state.MAIN_CHAIN:
        _say(f"✗ 当前状态 {cur} 不支持续跑")
        return 1

    nxt = _next_step(job)
    if nxt is None:
        _say("所有步骤已完成，无需续跑（可 --abort 清理实例）")
        return 0

    target = STEP_STATE_MAP.get(nxt)
    _say(f"续跑：{cur} → RESUMING → {target or 'READY'}（断点步骤 {nxt}）")

    with _lock(job_id, opts):
        ctx = _rebuild_ctx(job, getattr(opts, "creds", None))
        if not ctx.get("uhost_id"):
            _say("✗ job 里没有 uhost_id，无法续跑（请重新 new 一个作业）")
            return 1
        try:
            # FAILED_* 走 RESUMING 中转；已在中间状态（如进程被杀）则直接继续
            if cur in state.FAIL_BRANCH:
                state.transition(job_id, state.RESUMING, reason=f"续跑，断点 {nxt}")
            _execute(job_id, job, opts, ctx, start_at=nxt)
            # 收尾
            if state.load_job(job_id)["state"] == state.RELEASING:
                pass
            state.transition(job_id, state.DONE, reason="续跑完成")
            man = store.read_json(store.job_file(job_id, "manifest.json")) or {}
            step_done(job, opts, ctx, man)
            _say("✓ 续跑完成")
            _ledger(job_id, (man.get("cost") or {}).get("amount", 0),
                    uhost=ctx.get("uhost_id"), model=job["model"]["key"], resumed=True)
            return 0
        except RunError as e:
            _say(f"✗ 续跑失败：{e}")
            cur2 = state.load_job(job_id).get("state")
            if cur2 not in state.TERMINAL:
                try:
                    state.transition(job_id, e.fail_state, reason=str(e)[:200])
                except Exception:
                    pass
            _keep_instance(job_id, ctx, opts)
            return 1
        except Exception as e:
            # 非 RunError（程序 bug / 网络异常）也要落到失败态，别把 job 卡在中间状态
            _say(f"✗ 续跑异常：{type(e).__name__}: {e}")
            cur2 = state.load_job(job_id).get("state")
            if cur2 not in state.TERMINAL:
                fs = FAIL_STATE_OF_STEP.get(nxt, state.FAILED_RUN)
                try:
                    state.transition(job_id, fs, reason=f"{type(e).__name__}: {str(e)[:150]}")
                except Exception:
                    pass
            _keep_instance(job_id, ctx, opts)
            return 1


def _restart(job_id: str, job: dict, opts) -> int | None:
    """批量重试用：实例已丢，必须从 provision 整条重跑。

    合法路径：crashed(PROVISIONING/READY/SUBMITTED/RUNNING/FETCHING/VERIFYING)
              → 对应 FAILED_* → RESUMING → PROVISIONING（清空断点）
    返回 None 表示「已重置，继续走正常流程」。
    """
    cur = job.get("state")
    if cur == state.CREATED:
        return None                              # 首次运行
    if cur == state.DONE:
        _say("作业已完成，无需 restart")
        return 0
    if cur == state.ABORTED:
        _say("作业已中止，不 restart")
        return 1
    crash_map = {
        state.PROVISIONING: state.FAILED_PROVISION, state.READY: state.FAILED_PROVISION,
        state.SUBMITTED: state.FAILED_RUN, state.RUNNING: state.FAILED_RUN,
        state.FETCHING: state.FAILED_FETCH, state.VERIFYING: state.FAILED_VERIFY,
    }
    if cur in crash_map:
        state.transition(job_id, crash_map[cur], reason=f"restart: crashed at {cur}")
        cur = crash_map[cur]
    if cur in state.FAIL_BRANCH or cur == state.FAILED:
        state.transition(job_id, state.RESUMING, reason="restart")
        j = state.load_job(job_id)
        j["steps"] = {}                          # 清断点，强制重做
        store.atomic_write_json(store.job_file(job_id), j)
        state.transition(job_id, state.PROVISIONING, reason="restart from scratch")
        _say(f"restart：{job.get('state')} → PROVISIONING（清空断点，全量重跑）")
        return None
    _say(f"当前状态 {cur} 不支持 restart（产物可能已落盘）")
    return 1


def _abort(job_id: str, job: dict, opts) -> int:
    """放弃续跑：销毁保留的实例，作业标 FAILED。"""
    cur = job.get("state")
    if cur in (state.DONE, state.FAILED, state.ABORTED):
        _say(f"作业已是终态 {cur}，无需 abort")
        return 0
    ctx = _rebuild_ctx(job, getattr(opts, "creds", None))
    with _lock(job_id, opts):
        if cur in state.FAIL_BRANCH:
            try:
                state.transition(job_id, state.RELEASING, reason="abort：放弃续跑")
            except Exception as e:
                _say(f"  ⚠ 转移到 RELEASING 失败：{e}")
        elif cur not in state.TERMINAL:
            # 正在跑的作业 abort 后必须落到 ABORTED，否则状态永远停在 RUNNING
            try:
                state.transition(job_id, state.ABORTED, reason="abort：人工中止")
            except Exception as e:
                _say(f"  ⚠ 转移到 ABORTED 失败：{e}")
        released = False
        for attempt in (1, 2):
            try:
                step_release(job, opts, ctx)
                released = True
                break
            except Exception as e:
                _say(f"  ⚠ 销毁异常（第 {attempt} 次）：{e}")
                if attempt == 1:
                    time.sleep(5)
        if state.load_job(job_id).get("state") == state.RELEASING:
            state.transition(job_id, state.FAILED, reason="abort 收尾")
        if released:
            _say("✓ 已放弃并清理")
        else:
            _say("✗ 作业已中止，但实例释放失败：请到控制台手动销毁，避免继续计费")
    return 0


def _refresh_expect(job_id: str, job: dict) -> dict:
    """按当前工作流配置重算 expect（只对 registry 作业）。

    作业 spec 是提交那一刻生成的：如果之后修了期望口径（例如 fps/duration_s），
    老作业的 expect 会一直是错的。salvage 前先按现在的配置重算一次再校验。
    """
    reg = _registry_meta(job)
    if not reg:
        return job
    try:
        from tools.p20 import registry as p20registry, render as p20render
        wf = p20registry.get_workflow(reg["workflow_key"], version=reg.get("workflow_version"))
        new = p20render.expect_for(wf, job.get("params") or {})
    except Exception as e:
        _say(f"  ⚠ 重算 expect 失败（沿用原值）：{type(e).__name__}: {e}")
        return job
    old = job.get("expect") or {}
    if new == old:
        return job
    _say(f"  重算 expect：时长 {old.get('duration_s')} → {new.get('duration_s')}")
    job["expect"] = new
    job["expect_refreshed_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    store.atomic_write_json(store.job_file(job_id), job)
    return job


def _salvage(job_id: str, job: dict, opts) -> int:
    """产物已在 artifacts/：重新校验 + 落 manifest + 收尾，全程不建机、不销毁。

    用途：校验口径本身错了（例如 expect 没跟着 frames 重算）导致 FAILED，
    但 mp4/png 是好的。重跑一整单要再花一遍钱，salvage 只补状态，零 GPU 成本。
    """
    cur = job.get("state")
    if cur not in state.FAIL_BRANCH and cur != state.FAILED:
        _say(f"✗ salvage 只用于 FAILED/FAILED_*（当前 {cur}）")
        return 1
    arts = sorted(p for p in (store.job_dir(job_id) / "artifacts").glob("*") if p.is_file())
    if not arts:
        _say("✗ artifacts/ 里没有产物，无从收尾（只能重跑）")
        return 1
    steps = job.get("steps") or {}
    job = _refresh_expect(job_id, job)
    ctx = {
        "uhost_id": (job.get("instance") or {}).get("uhost_id"),
        "hourly": (steps.get("provision") or {}).get("hourly"),
        "arts": arts,
        "salvage": True,
    }
    with _lock(job_id, opts):
        if cur in state.FAIL_BRANCH or cur == state.FAILED:
            state.transition(job_id, state.RESUMING, reason="salvage：仅重跑校验后收尾")
        job = state.load_job(job_id)
        start = "persist" if (steps.get("verify") or {}).get("done") else "verify"
        _say(f"salvage：{len(arts)} 个产物已在本地，从 {start} 起重放")
        try:
            _execute(job_id, job, opts, ctx, start_at=start)
        except RunError as e:
            _say(f"✗ salvage 失败：{e}")
            cur2 = state.load_job(job_id).get("state")
            if cur2 not in state.TERMINAL:
                try:
                    state.transition(job_id, state.FAILED, reason=f"salvage：{str(e)[:150]}")
                except Exception:
                    pass
            return 1
        if state.load_job(job_id).get("state") != state.DONE:
            state.transition(job_id, state.DONE, reason="salvage 完成")
        man = store.read_json(store.job_file(job_id, "manifest.json")) or {}
        step_done(job, opts, ctx, man)
        _say("✓ salvage 完成（未新建实例、未产生新费用）")
        _ledger(job_id, (man.get("cost") or {}).get("amount", 0),
                uhost=ctx.get("uhost_id"), model=job["model"]["key"], salvage=True)
    return 0


def run(job_id: str, opts) -> int:
    global _LOG_PREFIX
    if getattr(opts, "batch", False):
        _LOG_PREFIX = f"[{job_id}] "
    job = state.load_job(job_id)
    _say(f"作业 {job_id} · 模型 {job['model']['key']} · 预算 ¥{job['budget'].get('max_cost')}")
    _start_heartbeat(job_id, opts)
    _install_orphan_guard(job_id, opts)

    if opts.dry_run:
        _say("--dry-run：只走状态机，不建机、不花钱")
        reg = _registry_meta(job)
        if reg:
            from tools.p20 import render as p20render
            g, meta = p20render.render_job(job)
            _say(f"  registry 渲染 OK：{meta['workflow_key']} v{meta['workflow_version']} × "
                 f"{meta['model_key']} v{meta['model_version']} · 兼容性 "
                 f"{meta['compat']['status']}")
        else:
            wf = json.loads((ROOT / job["model"]["workflow"]).read_text(encoding="utf-8"))
            g = _render_workflow(wf, job.get("params") or {}, job_id)
        seeds = []
        prompts = []
        for n in g.values():
            ins = n.get("inputs") or {}
            for k in ("seed", "noise_seed"):
                if k in ins:
                    seeds.append(ins[k])
            for k in ("prompt", "text"):
                if isinstance(ins.get(k), str):
                    prompts.append(ins[k])
        _say(f"  占位符渲染 OK：seed={seeds} · prompt 长度={[len(p) for p in prompts]}")
        _say(f"  节点数 {len(g)}，输出目录 {store.rel(store.job_dir(job_id))}")
        _say("  （真实执行请去掉 --dry-run）")
        return 0

    if getattr(opts, "abort", False):
        return _abort(job_id, job, opts)

    if getattr(opts, "salvage", False):
        return _salvage(job_id, job, opts)

    if opts.resume:
        return _resume(job_id, job, opts)

    if getattr(opts, "restart", False):
        r = _restart(job_id, job, opts)
        if r is not None:
            return r
        job = state.load_job(job_id)             # 重置后重新加载

    with _lock(job_id, opts):
        ctx: dict = {}
        try:
            _execute(job_id, job, opts, ctx)
            state.transition(job_id, state.DONE, reason="闭环完成")
            step_done(job, opts, ctx, ctx["man"])
            _say("✓ 闭环完成")
            _ledger(job_id, (ctx["man"].get("cost") or {}).get("amount", 0),
                    uhost=ctx.get("uhost_id"), model=job["model"]["key"])
            return 0

        except RunError as e:
            _say(f"✗ {e}")
            cur = state.load_job(job_id).get("state")
            if cur not in state.TERMINAL:
                try:
                    state.transition(job_id, e.fail_state, reason=str(e)[:200])
                except Exception as e2:
                    _say(f"  进入 {e.fail_state} 失败：{e2}")
            # 断点续跑：失败时**保留实例**，靠定时关机兜底，等 --resume 或 --abort
            kept = _keep_instance(job_id, ctx, opts)
            if kept:
                return 1
            # 未保留（例如建机都没成功）→ 走原有清理路径
            cur = state.load_job(job_id).get("state")
            if cur in state.FAIL_BRANCH:
                try:
                    state.transition(job_id, state.RELEASING, reason="失败后清理")
                    step_release(job, opts, ctx)
                    state.transition(job_id, state.FAILED, reason="失败收尾")
                except Exception as e3:
                    _say(f"  清理失败：{e3}")
            return 1
        except Exception as e:
            from compshare.cs import CompShareError
            if isinstance(e, CompShareError):
                _say(f"✗ 平台错误：{str(e)[:200]}")
                cur2 = state.load_job(job_id).get("state")
                if cur2 not in state.TERMINAL:
                    try:
                        state.transition(job_id, state.FAILED_PROVISION, reason=str(e)[:150])
                        state.transition(job_id, state.RELEASING, reason="失败清理")
                        step_release(job, opts, ctx)
                        state.transition(job_id, state.FAILED, reason="失败收尾")
                    except Exception as e2:
                        _say(f"  失败收尾异常：{e2}")
                return 1
            _say(f"✗ 未预期异常：{type(e).__name__}: {e}")
            try:
                step_release(job, opts, ctx)
            except Exception:
                pass
            return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run", description="P1 单任务执行器")
    ap.add_argument("job_id")
    ap.add_argument("--dry-run", action="store_true", help="只走状态机，不建机不花钱")
    ap.add_argument("--gpu", help="覆盖卡型，如 4090_48G")
    ap.add_argument("--image", help="镜像关键字")
    ap.add_argument("--hours", type=float, default=1.0, help="定时关机兜底小时数")
    ap.add_argument("--wait-stock", type=int, default=0, help="无货时最多等待秒数")
    ap.add_argument("--resume", action="store_true",
                    help="从 FAILED_* 续跑：按 job.json 的 steps 从断点继续")
    ap.add_argument("--abort", action="store_true", help="放弃续跑，销毁保留的实例")
    ap.add_argument("--no-keep", action="store_true", help="失败时不保留实例（旧行为）")
    ap.add_argument("--from-pool", action="store_true",
                    help="优先复用预热池停机实例（start ~12s），池空回落新建")
    ap.add_argument("--restart", action="store_true",
                    help="实例丢失后整条重跑：crashed/FAILED_* → PROVISIONING（清断点）")
    ap.add_argument("--batch", action="store_true",
                    help="批量模式：用作业级锁（不同 job 可并行），日志加 job 前缀")
    ap.add_argument("--no-borrow", action="store_true",
                    help="不复用现成实例（调试/对比基准时想要干净机器）")
    ap.add_argument("--slot-parallel", action="store_true",
                    help="允许把作业塞进正忙的槽位（同实例并行，需显存够 —— 默认关）")
    ap.add_argument("--wait-lock", type=int, default=0,
                    help="排队等单任务锁的最大秒数（0=拿不到立刻退出；控制台提交用 14400）")
    ap.add_argument("--salvage", action="store_true",
                    help="产物已在 artifacts/：只重跑校验+收尾，不建机、不销毁")
    # BYOK：让客户用自己的云账号付费。env 变量优先于 credentials.env，
    # 所以这两个参数只是"把凭证送进环境"的显式入口。
    ap.add_argument("--public-key", help="BYOK：客户自己的 CS_PUBLIC_KEY")
    ap.add_argument("--private-key", help="BYOK：客户自己的 CS_PRIVATE_KEY")
    a = ap.parse_args(argv)
    a.creds = None
    if a.public_key and a.private_key:
        a.creds = {"CS_PUBLIC_KEY": a.public_key, "CS_PRIVATE_KEY": a.private_key}
    return run(a.job_id, a)


if __name__ == "__main__":
    raise SystemExit(main())
