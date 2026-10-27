#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 基准自动化流水线：等现货 → 建机 → 跑多模型/多配置 → 回收产物 → 销毁 → 出报告。

设计要点
- 以「一台机器 = 一个 group」为单位，一台机器上连跑多个模型/配置，省掉反复建机的钱与时间；
- 同一 group 内对比「模型 × 配置」，跨 group 对比「卡型 × 地域」；
- 每台机器：建机即设定时关机兜底，跑完立刻销毁（异常也销毁）；
- 全局预算上限 + 单机时长上限 + STOP 文件急停；
- 等货期间零成本（只轮询库存接口）。

用法
  python3 lab_runner.py --dry-run                 # 只打印计划与现货，不建机
  python3 lab_runner.py --budget 8 --max-hours 6  # 挂机等货，抓到就跑
"""
from __future__ import annotations

import argparse
import datetime as dt
import signal
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from compshare.cs import CS, CompShareError, load_config, load_state, save_state  # noqa: E402
from tools.transfer import Transfer  # noqa: E402

LAB = ROOT / "lab"
LAB.mkdir(exist_ok=True)
STATE_FILE = LAB / "state.json"
LEDGER = ROOT / ".compshare" / "ledger.jsonl"
STOP_FILE = LAB / "STOP"
OUT = ROOT / "reports" / "lab"
OUT.mkdir(parents=True, exist_ok=True)

PROMPT = ("a cinematic photo of a small wooden cabin by a calm lake at golden hour, "
          "pine forest, mist over water, ultra detailed, 35mm")
NEGATIVE = "blurry, low quality, watermark, text, deformed"

IMAGE_NEW = "ComfyUI基础镜像纯净版0.36.0"          # 4090/3090/A100/V100S…
IMAGE_5090 = "ComfyUI基础镜像纯净版0.36.0-5090"    # Blackwell 需要新 torch
IMAGE_OLD = "ComfyUI_v0.3.40"                      # 已验证可用（CUDA 12.4 / torch 2.4）

# ---------------------------------------------------------------- 负载定义
def W(name, model, **kw):
    base = dict(workload=name, model=model, prompt=PROMPT, negative=NEGATIVE,
                sampler="euler", scheduler="normal", repeats=2, seed=20261026,
                prefix=name)
    base.update(kw)
    return base


WORKLOADS_ALL = [
    W("sd15_base", "sd15", ckpt="ghostmix_v12.safetensors", width=512, height=512,
      steps=20, cfg=7.0),
    W("sd15_fast", "sd15", ckpt="ghostmix_v12.safetensors", width=512, height=512,
      steps=8, cfg=7.0),
    W("sd15_big", "sd15", ckpt="ghostmix_v12.safetensors", width=768, height=768,
      steps=20, cfg=7.0),
    W("sdxl_base", "sdxl", ckpt="juggernautXL_juggernautX.safetensors", width=1024,
      height=1024, steps=20, cfg=5.0, sampler="dpmpp_2m", scheduler="karras"),
    W("sdxl_fast", "sdxl", ckpt="juggernautXL_juggernautX.safetensors", width=1024,
      height=1024, steps=8, cfg=5.0, sampler="dpmpp_2m", scheduler="karras"),
]
WORKLOADS_HI = [
    W("flux_base", "flux", unet="flux1-dev.sft", clip="clip_l.safetensors",
      clip2="t5xxl_fp16.safetensors", vae="ae.sft", width=1024, height=1024,
      steps=20, cfg=1.0, guidance=3.5, sampler="euler", scheduler="simple"),
    W("flux_fast", "flux", unet="flux1-dev.sft", clip="clip_l.safetensors",
      clip2="t5xxl_fp16.safetensors", vae="ae.sft", width=1024, height=1024,
      steps=10, cfg=1.0, guidance=3.5, sampler="euler", scheduler="simple"),
]
WORKLOADS_VIDEO = [
    W("wan_i2v", "wan_i2v", unet="Wan2.1_i2v_720p_scaled_fp8_e4m3_lightx2v.safetensors",
      clip="umt5_xxl_fp8_e4m3fn_scaled.safetensors", vae="wan_2.1_vae.safetensors",
      width=480, height=480, steps=8, cfg=1.0, frames=25, fps=8,
      start_image="lab_start.png", repeats=1, sampler="euler", scheduler="simple"),
]

HI_END = {"4090", "5090", "A100", "A800", "H20", "4090_48G"}
VIDEO_OK = {"4090", "5090"}


def build_groups() -> list[dict]:
    zones = [("cn-wlcb", "cn-wlcb-01", 1), ("cn-sh2", "cn-sh2-02", 7),
             ("cn-sh2", "cn-sh2-01", 8), ("cn-bj2", "cn-bj2-03", 9)]
    groups = []
    # 4090：四个地域都排上（抢到哪个算哪个，用来对比地域）
    for region, zone, prio in zones:
        groups.append({"id": f"4090@{zone}", "gpu": "4090", "gpu_count": 1, "region": region,
                       "zone": zone, "priority": prio,
                       "workloads": WORKLOADS_ALL + WORKLOADS_HI + WORKLOADS_VIDEO})
    # 5090：只在主可用区
    groups.append({"id": "5090@cn-wlcb-01", "gpu": "5090", "gpu_count": 1,
                   "region": "cn-wlcb", "zone": "cn-wlcb-01", "priority": 20,
                   "workloads": WORKLOADS_ALL + WORKLOADS_HI + WORKLOADS_VIDEO})
    for gpu, prio in (("3090", 30), ("V100S", 40), ("4090_48G", 60)):
        groups.append({"id": f"{gpu}@cn-wlcb-01", "gpu": gpu, "gpu_count": 1,
                       "region": "cn-wlcb", "zone": "cn-wlcb-01", "priority": prio,
                       "workloads": WORKLOADS_ALL + (WORKLOADS_HI if gpu in HI_END else [])})
    return sorted(groups, key=lambda g: g["priority"])


_SPEC_CACHE: dict = {}


def legal_spec(cs: CS, gpu: str, count: int) -> tuple[int, int]:
    """按 DescribeAvailableCompShareInstanceTypes 取该卡型 + 卡数下最小的合法 CPU/内存。"""
    key = (cs.region, cs.zone, gpu, count)
    if key in _SPEC_CACHE:
        return _SPEC_CACHE[key]
    best = None
    try:
        t = cs.call("DescribeAvailableCompShareInstanceTypes",
                    {"Region": cs.region, "Zone": cs.zone})
        for it in t.get("AvailableInstanceTypes") or []:
            if it.get("Name") != gpu:
                continue
            for ms in it.get("MachineSizes") or []:
                if ms.get("Gpu") != count:
                    continue
                for col in ms.get("Collection") or []:
                    if best is None or col["Cpu"] < best[0]:
                        best = (col["Cpu"], col["Memory"][0])
    except CompShareError:
        pass
    out = best or (16, 64)
    _SPEC_CACHE[key] = out
    return out


def image_for(gpu: str, cs: CS, zone: str) -> dict | None:
    """选镜像：5090 用 50 系专用镜像，其它卡用 0.3.40（已验证 CUDA 12.4），绝不混用。"""
    try:
        all_imgs = [i for i in cs.images(zone=zone)
                    if i.get("ImageType") in ("App", "System") and "ComfyUI" in (i.get("Name") or "")]
    except CompShareError:
        all_imgs = []
    if not all_imgs:
        return None

    def is_50(n: str) -> bool:
        return ("5090" in n) or ("50系" in n)

    def newest(lst):
        return sorted(lst, key=lambda i: -(i.get("CreateTime") or 0))

    tiers: list[list[dict]] = []
    if gpu == "5090":
        tiers = [[i for i in all_imgs if is_50(i["Name"]) and "0.36" in i["Name"]],
                 [i for i in all_imgs if is_50(i["Name"])]]
    elif gpu in ("4090", "4090_48G"):
        # 0.36 镜像里没有 /model 的 checkpoint 软链，模型一个都看不到 → 统一用 0.3.40 保证可比
        tiers = [[i for i in all_imgs if i["Name"] == "ComfyUI_v0.3.40"],
                 [i for i in all_imgs if i["Name"].startswith("ComfyUI基础镜像纯净版0.3")
                  and not is_50(i["Name"])]]
    else:
        tiers = [[i for i in all_imgs if i["Name"] == "ComfyUI_v0.3.40"],
                 [i for i in all_imgs if i["Name"].startswith("ComfyUI基础镜像纯净版0.3")
                  and not is_50(i["Name"])]]
    for t in tiers:                      # 逐级优先，同级取最新
        if t:
            return newest(t)[0]
    return None


# ---------------------------------------------------------------- SSH / SCP
def _cred(cs: CS, inst: dict):
    login = (cs.ssh_of(inst) or "").split()
    ip, user, port = cs.public_ip(inst), "root", "22"
    for i, tok in enumerate(login):
        if tok == "-p" and i + 1 < len(login):
            port = login[i + 1]
        if "@" in tok:
            user, ip = tok.split("@")[0] or "root", tok.split("@")[1] or ip
    return user, ip, port, cs.password_of(inst)


def ssh(cs: CS, inst: dict, cmd: str, timeout: int = 120) -> str:
    user, ip, port, pw = _cred(cs, inst)
    r = subprocess.run(["sshpass", "-p", pw, "ssh", "-p", port,
                        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                        f"{user}@{ip}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (("\nERR:" + r.stderr) if r.returncode else "")


def wait_ssh(cs: CS, inst: dict, timeout: int = 300) -> bool:
    """实例 Running ≠ sshd 就绪：等 SSH 真能跑命令再上传（0.3.40 镜像实测要再多等几十秒）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            out = ssh(cs, inst, "echo SSHD_OK", timeout=20)
            if "SSHD_OK" in out:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def scp_to(cs: CS, inst: dict, local: str, remote: str, timeout: int = 120) -> bool:
    user, ip, port, pw = _cred(cs, inst)
    for attempt in range(3):
        r = subprocess.run(["sshpass", "-p", pw, "scp", "-P", port,
                            "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null",
                            "-o", "ConnectTimeout=10",
                            "-r", local, f"{user}@{ip}:{remote}"],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True
        print(f"    scp→ 重试{attempt + 1}/3: {r.stderr.strip().splitlines()[-1][:120]}")
        time.sleep(5)
    return False


def scp_from(cs: CS, inst: dict, remote: str, local: str, timeout: int = 180) -> bool:
    user, ip, port, pw = _cred(cs, inst)
    for attempt in range(3):
        r = subprocess.run(["sshpass", "-p", pw, "scp", "-P", port,
                            "-o", "StrictHostKeyChecking=no",
                            "-o", "UserKnownHostsFile=/dev/null",
                            "-o", "ConnectTimeout=10",
                            "-r", f"{user}@{ip}:{remote}", local],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True
        print(f"    scp← 重试{attempt + 1}/3: {r.stderr.strip().splitlines()[-1][:120]}")
        time.sleep(5)
    return False


# ---------------------------------------------------------------- 单组执行
def detect_env(cs: CS, inst: dict) -> dict:
    out = ssh(cs, inst, "pid=$(pgrep -f 'main.py' | head -1); "
                        "echo PID=$pid; echo CWD=$(readlink /proc/$pid/cwd 2>/dev/null); "
                        "echo EXE=$(readlink /proc/$pid/exe 2>/dev/null); "
                        "echo CMD=$(tr '\\0' ' ' < /proc/$pid/cmdline 2>/dev/null)", timeout=60)
    env = {"raw": out.strip()}
    for line in out.splitlines():
        if line.startswith("CWD="):
            env["comfy_dir"] = line[4:].strip()
        elif line.startswith("EXE="):
            env["python"] = line[4:].strip()
        elif line.startswith("CMD="):
            env["cmdline"] = line[4:].strip()
    env.setdefault("comfy_dir", "/root/ComfyUI")
    # pgrep 可能匹配到 `bash -c "python main.py"` 这种 shell 包装 → 必须确认是 python
    py = env.get("python") or ""
    if not py.startswith("/") or "python" not in os.path.basename(py).lower():
        env["python"] = ""
    if not env.get("python"):
        probe = ssh(cs, inst, "for p in /root/miniconda3/envs/comfyui/bin/python "
                              "/opt/conda/bin/python /usr/bin/python3; do "
                              "[ -x $p ] && $p -c 'import torch' 2>/dev/null && echo GOOD=$p && break; done")
        good = [l.split("=", 1)[1].strip() for l in probe.splitlines() if l.startswith("GOOD=")]
        env["python"] = good[0] if good else "/usr/bin/python3"
    return env


def run_group(cs: CS, g: dict, price: dict, budget_left: float, args) -> dict:
    gid = g["id"]
    t0 = time.time()
    bal0 = float(cs.balance().get("AmountAvailable") or 0)
    img = image_for(g["gpu"], cs, g["zone"])
    if not img:
        return {"status": "skipped", "reason": "该可用区找不到 ComfyUI 镜像"}
    print(f"  镜像: {img['Name']} ({img['CompShareImageId']})")
    cpu, mem = legal_spec(cs, g["gpu"], g["gpu_count"])
    print(f"  规格: {cpu}C/{mem}G")
    rec = {"id": gid, "gpu": g["gpu"], "zone": g["zone"], "region": g["region"],
           "image": img["Name"], "started": int(t0), "workloads": {}}
    inst = None
    try:
        t_create0 = time.time()
        ids = cs.create(image_id=img["CompShareImageId"], gpu_type=g["gpu"],
                        gpu=g["gpu_count"], cpu=cpu, memory_gb=mem,
                        name=f"lab-{g['gpu'].lower()}", charge_type="Postpay",
                        disk_gb=100, zone=g["zone"])
        uid = ids[0]
        rec["uhost"] = uid
        print(f"  建机 {uid} …")
        try:
            cs.stop_at(uid, int(time.time() + args.cell_minutes * 60))
        except CompShareError as e:
            print(f"  ⚠ 定时关机兜底失败（继续，靠 finally 销毁）: {e}")
        inst = cs.get(uid)                      # 先拿到实例，避免中途异常漏销毁
        inst = cs.wait_state(uid, {"running"}, timeout=900)
        rec["t_to_running_s"] = round(time.time() - t_create0, 1)

        if not wait_ssh(cs, inst, timeout=args.ssh_timeout):
            raise RuntimeError("sshd 未就绪（%ds 内无法执行命令）" % args.ssh_timeout)
        # 等 ComfyUI 自身就绪（0.3.40 启动要 20-40s）
        for _ in range(36):
            probe = ssh(cs, inst, "ls /root/ComfyUI/main.py 2>/dev/null; "
                                  "pgrep -f 'main.py --listen' | head -1", timeout=20)
            if "main.py" in probe or probe.strip().isdigit():
                break
            time.sleep(5)
        env = detect_env(cs, inst)
        rec["env"] = {k: v for k, v in env.items() if k != "raw"}
        print(f"  ComfyUI 目录 {env['comfy_dir']} / python {env['python']}")

        ssh(cs, inst, "mkdir -p /root/lab /root/lab_out", timeout=60)
        ok_upload = Transfer(cs, inst, quiet=True).push(str(LAB / "bench_remote.py"),
                                                       "/root/lab/bench_remote.py")
        if not ok_upload or "bench_remote.py" not in ssh(cs, inst, "ls /root/lab", timeout=30):
            # 再给一次机会（sshd/scp 子系统在刚开机时偶发握手失败）
            time.sleep(10)
            ok_upload = Transfer(cs, inst, quiet=True).push(str(LAB / "bench_remote.py"),
                                                       "/root/lab/bench_remote.py")
        if not ok_upload:
            raise RuntimeError("上传 bench 脚本失败")

        for w in g["workloads"]:
            if time.time() - t0 > args.cell_minutes * 60:
                rec["workloads"][w["workload"]] = {"status": "skipped", "reason": "单机时长到上限"}
                continue
            if STOP_FILE.exists() or STOPPING:
                rec["workloads"][w["workload"]] = {"status": "skipped", "reason": "STOP"}
                continue
            if w["model"] == "wan_i2v" and g["gpu"] not in VIDEO_OK:
                rec["workloads"][w["workload"]] = {"status": "skipped", "reason": "该卡不支持 fp8 视频"}
                continue
            spec = dict(w)
            spec["cell_id"] = f"{gid}/{w['workload']}"
            spec["prefix"] = f"{g['gpu'].lower()}_{w['workload']}"
            spec["comfy_dir"] = env["comfy_dir"]
            spec["output_dir"] = os.path.join(env["comfy_dir"], "output")
            # 视频需要起始图：先把 sd15 的产物塞进 input
            if w["model"] == "wan_i2v":
                pngs = []
                for d in sorted((OUT / gid).glob("sd15*")):
                    pngs += sorted(d.glob("*.png"))
                if not pngs:
                    rec["workloads"][w["workload"]] = {"status": "skipped", "reason": "没有起始图"}
                    continue
                scp_to(cs, inst, str(pngs[0]), "/root/ComfyUI/input/lab_start.png")
            sp = LAB / f"spec_{gid}_{w['workload']}.json"
            sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            scp_to(cs, inst, str(sp), f"/root/lab/{sp.name}")
            print(f"  ▸ {w['workload']} ({w['model']} {w.get('width')}x{w.get('height')} "
                  f"steps={w.get('steps')})", flush=True)
            t1 = time.time()
            remote_cmd = (f"{env['python']} /root/lab/bench_remote.py --spec /root/lab/{sp.name} "
                          f"--outdir /root/lab_out/{w['workload']}")
            try:
                out = ssh(cs, inst, remote_cmd, timeout=args.workload_timeout)
                if not any(l.startswith("{") for l in out.strip().splitlines()):
                    # 刚开机时 sshd 会偶发断连，重试一次
                    print("    ↻ 无返回，15s 后重试")
                    time.sleep(15)
                    if not scp_to(cs, inst, str(sp), f"/root/lab/{sp.name}"):
                        pass
                    out = ssh(cs, inst, remote_cmd, timeout=args.workload_timeout)
                tail = [l for l in out.strip().splitlines() if l.startswith("{")]
                res = json.loads(tail[-1]) if tail else {"status": "failed",
                                                         "reason": out.strip()[-300:]}
            except subprocess.TimeoutExpired:
                res = {"status": "failed", "reason": f"工作负载超时 {args.workload_timeout}s"}
            try:
                (OUT / gid).mkdir(parents=True, exist_ok=True)
                (OUT / gid / f"{w['workload']}.remote.log").write_text(str(out)[-8000:],
                                                                       encoding="utf-8")
            except Exception:
                pass
            res["wall_s"] = round(time.time() - t1, 1)
            dest = OUT / gid / w["workload"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            Transfer(cs, inst, quiet=True).pull(f"/root/lab_out/{w['workload']}", str(dest.parent))
            rec["workloads"][w["workload"]] = res
            print(f"    → {res.get('status')} {('%.1fs' % res['t_cold_s']) if res.get('t_cold_s') else ''}"
                  f" {res.get('reason', '')[:80]}", flush=True)
            write_reports(state_all(), current=gid)
        rec["status"] = "ok"
    except CompShareError as e:
        msg = str(e)
        # 可重试：镜像同步中 / 库存瞬间被抢
        if ("镜像正在同步" in msg) or ("out of resources" in msg) or ("226628" in msg) \
                or ("226604" in msg) or ("8333" in msg):
            rec.update({"status": "retry", "reason": msg[:200]})
            print(f"  ↻ 可重试（{msg[:80]}），稍后再来")
            return rec
        # 镜像与卡型不匹配时，退回老镜像再试一次
        if img["Name"] != IMAGE_OLD and ("镜像" in msg or "image" in msg.lower()
                                        or "220" in msg or "230" in msg or "240" in msg):
            print(f"  ⚠ 建机失败（{msg[:90]}），改用 {IMAGE_OLD} 重试")
            try:
                old = next((i for i in cs.images(IMAGE_OLD, zone=g["zone"])
                            if i.get("Name") == IMAGE_OLD), None)
                if old:
                    rec["image"] = old["Name"]
                    ids = cs.create(image_id=old["CompShareImageId"], gpu_type=g["gpu"],
                                    gpu=g["gpu_count"], cpu=cpu, memory_gb=mem,
                                    name=f"lab-{g['gpu'].lower()}", charge_type="Postpay",
                                    disk_gb=100, zone=g["zone"])
                    uid = ids[0]
                    rec["uhost"] = uid
                    inst = cs.get(uid)
                    try:
                        cs.stop_at(uid, int(time.time() + args.cell_minutes * 60))
                    except CompShareError:
                        pass
                    inst = cs.wait_state(uid, {"running"}, timeout=900)
                    rec["t_to_running_s"] = round(time.time() - t_create0, 1)
                    env = detect_env(cs, inst)
                    rec["env"] = {k: v for k, v in env.items() if k != "raw"}
                    ssh(cs, inst, "mkdir -p /root/lab /root/lab_out", timeout=60)
                    scp_to(cs, inst, str(LAB / "bench_remote.py"), "/root/lab/bench_remote.py")
                    for w in g["workloads"]:
                        if w["model"] == "wan_i2v" and g["gpu"] not in VIDEO_OK:
                            continue
                        spec = dict(w)
                        spec["cell_id"] = f"{gid}/{w['workload']}"
                        spec["prefix"] = f"{g['gpu'].lower()}_{w['workload']}"
                        spec["comfy_dir"] = env["comfy_dir"]
                        spec["output_dir"] = os.path.join(env["comfy_dir"], "output")
                        sp = LAB / f"spec_{gid}_{w['workload']}.json"
                        sp.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
                        scp_to(cs, inst, str(sp), f"/root/lab/{sp.name}")
                        out = ssh(cs, inst, f"{env['python']} /root/lab/bench_remote.py "
                                            f"--spec /root/lab/{sp.name} "
                                            f"--outdir /root/lab_out/{w['workload']}",
                                  timeout=args.workload_timeout)
                        tail = [l for l in out.strip().splitlines() if l.startswith("{")]
                        rec["workloads"][w["workload"]] = (json.loads(tail[-1]) if tail
                                                           else {"status": "failed",
                                                                 "reason": out[-200:]})
                        dest = OUT / gid / w["workload"]
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        Transfer(cs, inst, quiet=True).pull(f"/root/lab_out/{w['workload']}",
                                                             str(dest.parent))
                    rec["status"] = "ok"
                else:
                    rec.update({"status": "failed", "reason": msg[:300]})
            except Exception as e2:
                rec.update({"status": "failed", "reason": f"重试失败 {type(e2).__name__}: {e2}"[:300]})
        else:
            rec["status"] = "failed"
            rec["reason"] = msg[:300]
        if rec.get("status") == "failed":
            print(f"  ✗ 失败: {rec.get('reason')}")
    except Exception as e:
        rec["status"] = "failed"
        rec["reason"] = f"{type(e).__name__}: {e}"[:300]
        print(f"  ✗ 异常: {rec['reason']}")
    finally:
        rec["ended"] = int(time.time())
        rec["wall_min"] = round((time.time() - t0) / 60, 1)
        try:
            bal1 = float(cs.balance().get("AmountAvailable") or 0)
            rec["cost_actual"] = round(bal0 - bal1, 3)
        except CompShareError:
            rec["cost_actual"] = None
        rec["cost_estimate"] = round(price["total"] * (time.time() - t0) / 3600, 3)
        try:
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            with LEDGER.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": int(time.time()), "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "event": "lab_group", "uhost": rec.get("uhost"), "gid": gid,
                    "name": f"lab-{g['gpu'].lower()}", "gpu": g["gpu"], "zone": g["zone"],
                    "cost": rec.get("cost_estimate"), "cost_actual": rec.get("cost_actual"),
                    "ok": sum(1 for x in (rec.get("workloads") or {}).values()
                              if x.get("status") == "ok"),
                    "bad": sum(1 for x in (rec.get("workloads") or {}).values()
                               if x.get("status") == "failed"),
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if inst:
            try:
                cs.terminate(uid)
                print(f"  已销毁 {uid}（花费≈¥{rec['cost_estimate']}，实扣¥{rec.get('cost_actual')}）")
            except CompShareError as e:
                print(f"  ⚠ 销毁失败，需要人工处理: {e}")
                rec["destroy_failed"] = str(e)[:200]
    return rec


# ---------------------------------------------------------------- 报告
def state_all() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"groups": {}}


def write_reports(state: dict, current: str | None = None) -> None:
    groups = state.get("groups", {})
    rows, cards = [], []
    by_model: dict[str, list] = {}
    for gid, r in groups.items():
        for wname, w in (r.get("workloads") or {}).items():
            model = (w.get("model") or wname.split("_")[0])
            by_model.setdefault(model, [])
            ok = w.get("status") == "ok"
            rows.append([gid, wname, w.get("status"),
                         ("%.1f" % w["t_cold_s"]) if w.get("t_cold_s") else "-",
                         ("%.1f" % w["t_warm_s"]) if w.get("t_warm_s") else "-",
                         (w.get("vram") or {}).get("vram_peak_mib", "-"),
                         w.get("reason", "")[:40]])
            for a in w.get("artifacts") or []:
                p = OUT / gid / wname / a["file"]
                if p.exists():
                    rel = os.path.relpath(p, OUT)   # 相对 gallery.html 的路径
                    by_model[model].append({
                        "gpu": r.get("gpu"), "zone": r.get("zone"), "workload": wname,
                        "src": str(rel), "w": a.get("width"), "h": a.get("height"),
                        "t_cold": w.get("t_cold_s"), "file": a["file"],
                    })
    md = ["# GPU 基准报告（自动生成）", "",
          f"- 更新：{dt.datetime.now():%Y-%m-%d %H:%M:%S}",
          f"- 进度：{len([g for g in groups.values() if g.get('status') == 'ok'])} 台完成 / "
          f"{len(groups)} 台计划", ""]
    if rows:
        md += ["| 机器 | 负载 | 状态 | 冷启(s) | 预热(s) | 显存峰值(MiB) | 备注 |",
               "|---|---|---|---|---|---|---|"]
        md += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
        md.append("")
    total_cost = sum((r.get("cost_actual") or 0) or 0 for r in groups.values())
    md.append(f"累计实扣：¥{total_cost:.2f}")
    (OUT / "REPORT.md").write_text("\n".join(md), encoding="utf-8")

    # 画廊交给 tools/gallery.py（缩略图 + 指标表，避免整页拉 60MB 原图）
    try:
        subprocess.run([sys.executable, str(ROOT / "tools" / "gallery.py")],
                       capture_output=True, text=True, timeout=300)
    except Exception as e:
        print(f"  画廊生成失败: {e}")
    subprocess.run(["bash", str(ROOT / "scripts" / "sync_share.sh")],
                   capture_output=True, text=True, timeout=120)


# ---------------------------------------------------------------- 主循环
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, default=8.0, help="总预算（元），用完即停")
    ap.add_argument("--max-hours", type=float, default=6.0, help="等货总时长上限")
    ap.add_argument("--poll", type=int, default=60, help="库存轮询秒")
    ap.add_argument("--cell-minutes", type=int, default=35, help="单机最长运行分钟")
    ap.add_argument("--workload-timeout", type=int, default=600, help="单个负载超时秒")
    ap.add_argument("--ssh-timeout", type=int, default=300, help="等 sshd 就绪上限秒")
    ap.add_argument("--retries", type=int, default=1, help="失败机器重试次数")
    ap.add_argument("--only-gpu", default="", help="只跑指定卡型，逗号分隔")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    clients: dict[str, CS] = {}

    def cs_for(region: str, zone: str) -> CS:
        key = f"{region}/{zone}"
        if key not in clients:
            clients[key] = CS({**cfg, "CS_REGION": region, "CS_ZONE": zone})
        return clients[key]

    # 清掉上次异常退出遗留的 lab-* 实例（避免无人看管还在计费）
    try:
        cs0 = cs_for("cn-wlcb", "cn-wlcb-01")
        for inst in cs0.instances():
            if (inst.get("Name") or "").startswith("lab-") and inst.get("State") in (
                    "Running", "Stopped", "Initializing", "Install"):
                print(f"清理遗留实例 {inst['UHostId']} {inst.get('Name')} …")
                try:
                    cs0.terminate(inst["UHostId"])
                except CompShareError as e:
                    print(f"  清理失败: {e}")
    except CompShareError as e:
        print(f"遗留实例检查失败: {e}")

    state = state_all()
    state.setdefault("groups", {})
    groups = build_groups()
    if args.only_gpu:
        keep = {x.strip() for x in args.only_gpu.split(",") if x.strip()}
        groups = [g for g in groups if g["gpu"] in keep]

    print(f"[{dt.datetime.now():%H:%M:%S}] 计划 {len(groups)} 台机器，预算 ¥{args.budget}，"
          f"等货上限 {args.max_hours}h")
    spent = sum((r.get("cost_actual") or 0) or 0 for r in state["groups"].values())
    deadline = time.time() + args.max_hours * 3600
    last_report = 0.0

    while time.time() < deadline:
        if STOP_FILE.exists() or STOPPING:
            print("停止信号 / lab/STOP → 退出")
            break
        pending = []
        for g in groups:
            r = state["groups"].get(g["id"])
            if r and r.get("status") in ("ok",):
                continue
            if r and r.get("status") == "skipped" and "镜像" in (r.get("reason") or ""):
                continue
            fails = (r or {}).get("attempts", 0)
            if r and r.get("status") == "failed" and fails > args.retries:
                continue
            pending.append(g)
        if not pending:
            print("全部机器已处理完")
            break

        progressed = False
        for g in pending:
            if time.time() >= deadline or STOP_FILE.exists() or STOPPING:
                break
            if spent >= args.budget:
                print(f"预算用尽（已花 ¥{spent:.2f} / 预算 ¥{args.budget}）")
                break
            cs = cs_for(g["region"], g["zone"])
            try:
                stock = cs.stock(g["gpu"], False, g["zone"])
            except CompShareError as e:
                print(f"  {g['id']} 库存查询失败: {e}")
                continue
            if stock <= 0:
                if args.dry_run:
                    print(f"  {g['id']}: 现货 0")
                continue
            sc, sm = legal_spec(cs, g["gpu"], g["gpu_count"])
            p = cs.price(g["gpu"], g["gpu_count"], sc, sm, "Postpay", 100, zone=g["zone"])
            projected = p["total"] * args.cell_minutes / 60
            if projected > max(0.5, (args.budget - spent) * 0.7):
                print(f"  {g['id']} 预估 ¥{projected:.2f} 超出剩余预算安全线，跳过")
                state["groups"][g["id"]] = {"status": "skipped",
                                           "reason": f"预估 ¥{projected:.2f} 超预算"}
                continue
            print(f"\n[{dt.datetime.now():%H:%M:%S}] {g['id']} 现货 {stock}，"
                  f"单价 ¥{p['total']}/h（本机上限 {args.cell_minutes}min ≈ ¥{projected:.2f}）→ 开跑")
            if args.dry_run:
                print("  dry-run：不建机")
                continue
            prev = state["groups"].get(g["id"], {})
            rec = run_group(cs, g, p, args.budget - spent, args)
            rec["attempts"] = prev.get("attempts", 0) + (0 if rec.get("status") == "retry" else 1)
            state["groups"][g["id"]] = rec
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            spent += rec.get("cost_actual") or 0
            progressed = True
            write_reports(state)
        if not progressed:
            if args.dry_run:
                break
            print(f"[{dt.datetime.now():%H:%M:%S}] 暂无可用现货，{args.poll}s 后再试"
                  f"（已花 ¥{spent:.2f}）")
            time.sleep(args.poll)
    write_reports(state_all())
    print(f"\n结束：共花 ¥{spent:.2f}；报告 {OUT/'REPORT.md'}")
    return 0


STOPPING = False


def _on_term(signum, frame):
    print("收到停止信号，尽快收尾（执行中的机器会在 finally 里销毁）", flush=True)
    global STOPPING
    STOPPING = True


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    sys.exit(main())
