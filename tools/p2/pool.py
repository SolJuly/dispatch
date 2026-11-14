#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 预热池（M5）：保留 N 台「已停机」实例，来活直接 start（~12s）而不是 create（70-85s）。

    python3 tools/p2/pool.py ensure --size 2 --gpu 4090     # 补齐到 2 台空闲
    python3 tools/p2/pool.py status
    python3 tools/p2/pool.py drain [--terminate]            # 清空（默认只停机，不删）

设计要点（见 reports/p2/M5-预热池-设计与验收.md）：
  - 停机实例：算力费 ¥0，系统盘 ≤100G 免费 → 持仓成本 0
  - 但**占配额**（Level1=5 台），所以有 max_total 上限，给在跑作业留槽
  - 实例必须是已烘焙镜像（带 /root/.p2_image.json），start 后 setup 走快路径
  - stop→start 后 entrypoint 会重新拉起 ComfyUI；acquire 时等 8188 就绪再交活
  - 超过 stale_hours 未使用的实例回收重建，避免长期漂移
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from compshare.cs import CS, CompShareError  # noqa: E402
from tools.p2 import config as p2cfg  # noqa: E402

POOL_JSON = _ROOT / "reports" / "p2" / "pool.json"
NAME_PREFIX = "p2-pool"
SSH_PORT = "23"
DEFAULT_MAX_TOTAL = 4      # 账户 5 台，留 1 个槽给「池空时 fallback 建机」
STALE_HOURS = 24

# 过渡期拒绝的重试参数。
#
# 为什么必须有：实例刚 start 完成、或正在处理上一次 stop 时，平台会拒绝新的
# 关机/删除请求，返回 226609（"实例正在进行关机相关操作"）或 8903
# （"UHost is in the task"）。这两者都是**可重试**的，十几秒后就会成功。
#
# 2026-11-14 真机踩到：release() 没有重试，一次 226609 就把实例标成 dead，
# 但云端仍在 Running —— 实例继续按 ¥1.41/h 烧钱，而池子再也不管它。
# 这是「孤儿实例」最典型的产生路径，也是直接的费用泄漏。
RETRY_TRANSIENT = 3
RETRY_TRANSIENT_WAIT = 10
# 命中这些子串即视为可重试（大小写不敏感）
TRANSIENT_MARKS = ("226609", "8903", "in the task",
                   "正在进行关机", "请稍后再试", "please try again")
# 归还不成功的实例不该标 dead（那等于放弃它），标 orphan 交给 reconcile 兜底销毁
STATE_ORPHAN = "orphan"


def _is_transient(err: BaseException) -> bool:
    """这个错误等一会儿重试有没有意义？"""
    msg = str(err).lower()
    return any(m.lower() in msg for m in TRANSIENT_MARKS)


def _stop_with_retry(cs: "CS", uid: str, tries: int | None = None,
                     wait_s: int | None = None) -> None:
    """调 stop 并在过渡期拒绝时重试。成功即返回，失败抛最后一次异常。

    注意：**不吞异常**。调用方要据此决定把实例标成什么状态 ——
    吞掉会让「没停成功」看起来像「停成功了」，正是孤儿实例的温床。
    """
    tries = RETRY_TRANSIENT if tries is None else tries
    wait_s = RETRY_TRANSIENT_WAIT if wait_s is None else wait_s
    last: BaseException | None = None
    for attempt in range(1, tries + 1):
        try:
            cs.stop(uid)
            return
        except Exception as e:                      # noqa: BLE001
            last = e
            if not _is_transient(e) or attempt == tries:
                raise
            _say(f"  过渡期拒绝（{attempt}/{tries}）：{str(e)[:60]}，{wait_s}s 后重试")
            time.sleep(wait_s)
    if last:
        raise last


def _terminate_with_retry(cs: "CS", uid: str, tries: int | None = None,
                          wait_s: int | None = None) -> None:
    """同 _stop_with_retry，但调 terminate。"""
    tries = RETRY_TRANSIENT if tries is None else tries
    wait_s = RETRY_TRANSIENT_WAIT if wait_s is None else wait_s
    last: BaseException | None = None
    for attempt in range(1, tries + 1):
        try:
            cs.terminate(uid)
            return
        except Exception as e:                      # noqa: BLE001
            last = e
            if not _is_transient(e) or attempt == tries:
                raise
            _say(f"  过渡期拒绝（{attempt}/{tries}）：{str(e)[:60]}，{wait_s}s 后重试")
            time.sleep(wait_s)
    if last:
        raise last



def _now() -> float:
    return time.time()


def load() -> dict:
    if POOL_JSON.exists():
        try:
            d = json.loads(POOL_JSON.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("instances"), list):
                return d
        except Exception:
            pass
    return {"schema": "p2.pool/v1", "gpu": config_default_gpu(), "size": 0,
            "image_id": "", "max_total": DEFAULT_MAX_TOTAL, "instances": []}


def config_default_gpu() -> str:
    try:
        return p2cfg.DEFAULT_GPU
    except Exception:
        return "4090"


def save(d: dict) -> None:
    POOL_JSON.parent.mkdir(parents=True, exist_ok=True)
    POOL_JSON.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [pool] {msg}", flush=True)


def _creds(cs: CS, uid: str):
    inst = cs.get(uid)
    return cs.public_ip(inst), SSH_PORT, cs.password_of(inst), inst


def _ssh(host: str, port: str, pwd: str, cmd: str, timeout=60) -> tuple[int, str]:
    try:
        p = subprocess.run(["sshpass", "-p", pwd, "ssh", "-p", str(port),
                            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                            "-o", "ConnectTimeout=8", f"root@{host}", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT({timeout}s)"


def _wait_sshd(host: str, port: str, pwd: str, timeout=120) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc, out = _ssh(host, port, pwd, "echo ready", timeout=20)
        if rc == 0 and "ready" in out:
            return True
        time.sleep(3)
    return False


def _wait_ready(host: str, port: str, pwd: str, timeout=150) -> bool:
    """等 ComfyUI 8188（复用镜像自带 python，不依赖 curl）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc, out = _ssh(host, port, pwd,
                       "/root/miniconda3/bin/python -c \"import urllib.request as u; "
                       "u.urlopen('http://127.0.0.1:8188/system_stats', timeout=3); print('UP')\"",
                       timeout=20)
        if rc == 0 and "UP" in out:
            return True
        time.sleep(3)
    return False


# ------------------------------------------------------------------ 创建/补齐
def _create_one(cs: CS, gpu: str, image_id: str, idx: int) -> str | None:
    img = cs.resolve_image(image_id) if image_id else cs.resolve_image(p2cfg.BASE_IMAGE_KEYWORD)
    cpu_n, mem_gb = p2cfg.GPU_SPEC.get(gpu, (16, 64))
    try:
        ids = cs.create(image_id=img["CompShareImageId"], gpu_type=gpu, gpu=1, cpu=cpu_n,
                        memory_gb=mem_gb, name=f"{NAME_PREFIX}-{idx}", charge_type="Postpay",
                        disk_gb=100, data_disk_gb=0, remark="p2 warm pool")
    except CompShareError as e:
        _say(f"✗ 建池实例失败：{str(e)[:160]}")
        return None
    uid = ids[0]
    _say(f"创建 {uid}，等 Running…")
    cs.wait_state(uid, {"running"}, timeout=900)
    return uid


def ensure(size: int, gpu: str | None = None, image_id: str | None = None,
           max_total: int = DEFAULT_MAX_TOTAL) -> dict:
    """把空闲实例补到 size 台（创建→自检→停机）。"""
    cs = CS()
    d = load()
    gpu = gpu or d.get("gpu") or config_default_gpu()
    image_id = image_id or d.get("image_id") or ""
    d.update({"gpu": gpu, "size": size, "image_id": image_id, "max_total": max_total})

    # 清理已不存在的
    alive = []
    for it in d["instances"]:
        try:
            st = (cs.get(it["uid"]).get("State") or "").lower()
            it["cloud_state"] = st
            alive.append(it)
        except Exception:
            _say(f"池内 {it['uid']} 已不存在，移除")
    d["instances"] = alive

    idle = [i for i in d["instances"] if i.get("state") in ("idle", "stopped")]
    need = max(0, size - len(idle))
    total = len(cs.instances())
    room = max(0, max_total - total)
    if need > room:
        _say(f"配额保护：需要 {need} 台，但只剩 {room} 个槽（max_total={max_total}，当前 {total} 台）")
        need = room

    for k in range(need):
        uid = _create_one(cs, gpu, image_id, len(d["instances"]) + 1)
        if not uid:
            continue
        host, port, pwd, _ = _creds(cs, uid)
        if not _wait_sshd(host, port, pwd):
            _say(f"⚠ {uid} sshd 未就绪，仍纳入池（start 时再等）")
        else:
            # 首次建好先跑一次 setup 快路径，确保 ComfyUI 能起（失败也不致命）
            _wait_ready(host, port, pwd, timeout=150)
        cs.stop(uid)
        cs.wait_state(uid, {"stopped"}, timeout=600)
        d["instances"].append({"uid": uid, "state": "idle", "job_id": None,
                               "created_at": _now(), "last_used": 0.0,
                               "hourly": 0.0, "cloud_state": "stopped"})
        _say(f"✓ {uid} 已入池（stopped）")

    save(d)
    return status(d)


# ------------------------------------------------------------------ 取用/归还
def acquire(job_id: str, wait_ready_s: int = 180) -> dict | None:
    """取一台空闲实例：start → 等 Running → 等 sshd → 等 8188。池空返回 None。"""
    cs = CS()
    d = load()
    cand = [i for i in d["instances"] if i.get("state") in ("idle", "stopped")]
    if not cand:
        return None
    it = sorted(cand, key=lambda x: x.get("last_used") or 0)[0]
    uid = it["uid"]
    t0 = time.time()
    try:
        cs.start(uid)
        cs.wait_state(uid, {"running"}, timeout=300)
        t_run = round(time.time() - t0, 1)
        host, port, pwd, inst = _creds(cs, uid)
        if not _wait_sshd(host, port, pwd, timeout=120):
            _say(f"✗ {uid} sshd 未就绪，标记 dead")
            it["state"] = "dead"
            save(d)
            return None
        ok = _wait_ready(host, port, pwd, timeout=wait_ready_s)
        t_ready = round(time.time() - t0, 1)
        it.update({"state": "busy", "job_id": job_id, "last_used": _now(),
                   "cloud_state": "running"})
        save(d)
        _say(f"✓ 复用 {uid}：Running {t_run}s · ComfyUI 就绪 {t_ready}s"
             f"{'' if ok else '（8188 未就绪，setup 会再拉）'}")
        return {"uhost_id": uid, "inst": inst, "host": host, "port": port, "password": pwd,
                "from_pool": True, "t_start": t_run, "t_ready": t_ready, "ready": ok}
    except Exception as e:
        _say(f"✗ 取用 {uid} 失败：{type(e).__name__}: {e}")
        it["state"] = "dead"
        save(d)
        return None


def release(uid: str, keep: bool = True) -> None:
    """作业结束：停机保留（默认）或销毁。

    **失败不标 dead**：dead 在 reconcile 里只被移除记录、不保证云端已销毁，
    等于放任实例继续烧钱。这里改成 orphan，让 reconcile 明确去 terminate。
    """
    cs = CS()
    d = load()
    it = next((i for i in d["instances"] if i["uid"] == uid), None)
    try:
        if keep:
            _stop_with_retry(cs, uid)
            cs.wait_state(uid, {"stopped"}, timeout=600)
            if it:
                it.update({"state": "idle", "job_id": None, "last_used": _now(),
                           "cloud_state": "stopped"})
            # 停机计时器：停机保留 2 小时 → 销毁，reaper 只认这一份记录
            try:
                from tools.p2 import lifecycle
                lifecycle.record_stopped(uid)
            except Exception:              # noqa: BLE001
                pass
            _say(f"↩ {uid} 已停机回池")
        else:
            _terminate_with_retry(cs, uid)
            if it:
                it["state"] = "dead"
            _say(f"✗ {uid} 已销毁")
    except Exception as e:
        _say(f"⚠ 归还 {uid} 失败：{type(e).__name__}: {e}")
        if it:
            # orphan = 「池子已经不管它了，但云端可能还活着」
            # reconcile 见到 orphan 会去 terminate，而不是只从表里删掉。
            it.update({"state": STATE_ORPHAN, "job_id": None,
                       "orphan_since": _now(), "orphan_reason": str(e)[:200]})
        _say(f"  → 标记 orphan，等 reconcile 兜底销毁")
    save(d)


# ------------------------------------------------------------------ 巡检/清空
def reconcile() -> dict:
    """回收 dead/orphan/超期实例；纠正 busy↔idle；扫描云端孤儿。

    三类回收的语义差异（踩过才分清的）：

      dead    池子主动销毁过，正常情况云端已经没了。若还在，补一次 terminate。
      orphan  归 还 失 败 —— 池子不管它了，但云端**极可能还在烧钱**。
              这是 2026-11-14 真机发现的问题：release 撞 226609 后标 dead，
              而实例仍是 Running，¥1.41/h 一直扣。必须强制 terminate。
      stale   闲置超过 STALE_HOURS，环境可能漂移，重建比复用便宜。
    """
    cs = CS()
    d = load()
    changed = []
    for it in list(d["instances"]):
        uid = it["uid"]
        try:
            st = (cs.get(uid).get("State") or "").lower()
        except Exception:
            # 云端已不存在：直接从池里摘掉（原来只置 dead 不删除，池会虚胖）
            d["instances"].remove(it)
            changed.append(f"{uid}:gone")
            continue
        it["cloud_state"] = st

        # orphan 优先处理：它可能是正在烧钱的实例
        if it["state"] == STATE_ORPHAN:
            try:
                _terminate_with_retry(cs, uid)
                changed.append(f"{uid}:orphan→terminated")
            except Exception as e:                  # noqa: BLE001
                # 销毁也失败 → 保留记录，下一轮再试。
                # **绝不在这里 remove**：记录一删，这台机器就彻底没人管了。
                it["orphan_reason"] = f"{str(e)[:160]}"
                changed.append(f"{uid}:orphan-retry-later")
                continue
            d["instances"].remove(it)
            continue

        if it["state"] == "dead" or (st == "stopped" and it["state"] == "busy"):
            if it["state"] == "dead":
                try:
                    _terminate_with_retry(cs, uid)
                except Exception:
                    pass
                d["instances"].remove(it)
                changed.append(f"{uid}:reaped")
                continue
            it.update({"state": "idle", "job_id": None})
            changed.append(f"{uid}:busy→idle")
        age_h = (_now() - (it.get("last_used") or it.get("created_at") or 0)) / 3600
        if it["state"] == "idle" and age_h > STALE_HOURS:
            try:
                _terminate_with_retry(cs, uid)
            except Exception:
                pass
            d["instances"].remove(it)
            changed.append(f"{uid}:stale({age_h:.0f}h)")

    # ---- 云端孤儿扫描：名字带池前缀、但池里没有记录的实例 ----
    # 为什么还要扫云端：release 失败时如果进程恰好被杀，pool.json 里连
    # orphan 记录都没留下，这台机器就成了完全隐形的费用泄漏。
    known = {i["uid"] for i in d["instances"]}
    try:
        for inst in cs.instances():
            name = str(inst.get("Name") or "")
            uid = inst.get("UHostId") or ""
            if not uid or uid in known:
                continue
            if not name.startswith(NAME_PREFIX):
                continue
            state = str(inst.get("State") or "").lower()
            if state in ("terminated", "terminating"):
                continue
            try:
                _terminate_with_retry(cs, uid)
                changed.append(f"{uid}:cloud-orphan({name})→terminated")
            except Exception as e:                  # noqa: BLE001
                changed.append(f"{uid}:cloud-orphan-failed:{str(e)[:40]}")
    except Exception as e:                          # noqa: BLE001
        changed.append(f"cloud-scan-skipped:{str(e)[:40]}")

    save(d)
    return {"changed": changed, **status(d)}


def drain(terminate: bool = False) -> dict:
    cs = CS()
    d = load()
    out = []
    for it in list(d["instances"]):
        uid = it["uid"]
        try:
            if terminate:
                cs.terminate(uid)
            else:
                cs.stop(uid)
            out.append(uid)
        except Exception as e:
            out.append(f"{uid}({type(e).__name__})")
    d["instances"] = []
    save(d)
    _say(f"已{'销毁' if terminate else '停机'} {len(out)} 台：{out}")
    return {"drained": out}


def status(d: dict | None = None) -> dict:
    d = d or load()
    insts = d.get("instances") or []
    by = {}
    for i in insts:
        by[i.get("state", "?")] = by.get(i.get("state", "?"), 0) + 1
    return {"size": d.get("size"), "gpu": d.get("gpu"), "total": len(insts),
            "by_state": by, "instances": insts, "max_total": d.get("max_total")}


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="pool", description="P2 预热池")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ensure", help="补齐到 N 台空闲")
    sp.add_argument("--size", type=int, required=True)
    sp.add_argument("--gpu", default=None)
    sp.add_argument("--image", default=None)
    sp.add_argument("--max-total", type=int, default=DEFAULT_MAX_TOTAL)
    sp.set_defaults(fn=lambda a: print(json.dumps(ensure(a.size, a.gpu, a.image, a.max_total),
                                                  ensure_ascii=False, indent=2)))

    sp = sub.add_parser("status", help="看池")
    sp.set_defaults(fn=lambda a: print(json.dumps(status(), ensure_ascii=False, indent=2)))

    sp = sub.add_parser("reconcile", help="回收 dead/超期")
    sp.set_defaults(fn=lambda a: print(json.dumps(reconcile(), ensure_ascii=False, indent=2)))

    sp = sub.add_parser("drain", help="清空（默认只停机）")
    sp.add_argument("--terminate", action="store_true")
    sp.set_defaults(fn=lambda a: print(json.dumps(drain(a.terminate), ensure_ascii=False, indent=2)))

    a = ap.parse_args(argv)
    return a.fn(a) or 0


if __name__ == "__main__":
    raise SystemExit(main())
