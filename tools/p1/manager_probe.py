#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Manager 建机卡住问题的复现实验。

每次建机后观察 entrypoint 启动的 ComfyUI：
  · 45 秒内 8188 是否响应
  · 启动日志里是否出现拉 GitHub 的超时/失败
  · 进程 cmdline 是否带 --enable-manager
  · /start.d/comfyui.sh 的启动命令长什么样

用法：
    python3 tools/p1/manager_probe.py --rounds 5
每次建机约 2 分钟，建完立即销毁（不跑推理，控制成本）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compshare.cs import CS, CompShareError  # noqa: E402

IMAGE = "ComfyUI基础镜像纯净版0.36.0"
GPU_PREF = ("4090_48G", "5090", "4090")
# 每种卡的合法 CPU/内存组合（4090 传 96G 会报 8333 ratio 错误）
SPEC = {"4090_48G": (16, 96), "5090": (16, 96), "4090": (16, 64)}
OBSERVE_SEC = 45


def sh(host: str, pwd: str, cmd: str, timeout=60) -> tuple[int, str]:
    p = subprocess.run(["sshpass", "-p", pwd, "ssh", "-p", "23",
                        "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "ConnectTimeout=10", f"root@{host}", cmd],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def pick_gpu(cs: CS) -> str | None:
    for g in GPU_PREF:
        try:
            if cs.stock(g, False) > 0:
                return g
        except Exception:
            continue
    return None


def one_round(cs: CS, idx: int, outdir: Path) -> dict:
    rec = {"round": idx}
    gpu = pick_gpu(cs)
    if not gpu:
        rec["skip"] = "无现货"
        return rec
    rec["gpu"] = gpu

    t_create = time.time()
    try:
        img = cs.resolve_image(IMAGE)
        cpu, mem = SPEC.get(gpu, (16, 64))
        uid = cs.create(image_id=img["CompShareImageId"], gpu_type=gpu, gpu=1, cpu=cpu,
                        memory_gb=mem, name=f"p1-mgr{idx}", charge_type="Postpay",
                        disk_gb=100, data_disk_gb=0, remark="manager-probe")[0]
    except CompShareError as e:
        rec["skip"] = f"建机失败 {str(e)[:60]}"
        return rec

    rec["uid"] = uid
    inst = cs.wait_state(uid, {"running"}, timeout=900)
    rec["up_sec"] = round(time.time() - t_create, 1)
    host, pwd = cs.public_ip(inst), cs.password_of(inst)

    # 等 sshd
    for _ in range(30):
        rc, o = sh(host, pwd, "echo ok")
        if rc == 0 and "ok" in o:
            break
        time.sleep(4)

    # 观察窗口
    t0 = time.time()
    ready_at = None
    while time.time() - t0 < OBSERVE_SEC:
        rc, o = sh(host, pwd,
                   "source /root/miniconda3/bin/activate && python3 -c \""
                   "import urllib.request;"
                   "print(urllib.request.urlopen('http://127.0.0.1:8188/',timeout=3).getcode())\"",
                   timeout=20)
        if "200" in o:
            ready_at = round(time.time() - t0, 1)
            break
        time.sleep(3)

    rec["ready_sec"] = ready_at
    rec["stuck"] = ready_at is None

    # 证据
    rc, procs = sh(host, pwd, "pgrep -af '[m]ain.py' | head -2")
    rec["procs"] = procs.strip()[:200]
    rc, startd = sh(host, pwd, "cat /start.d/comfyui.sh 2>/dev/null | grep -E 'main.py' | head -1")
    rec["start_cmd"] = startd.strip()[:200]
    rc, logs = sh(host, pwd,
                  "grep -icE 'raw.githubusercontent|Connection timeout|ClientConnector' /root/ComfyUI/comfy.log 2>/dev/null || echo 0")
    rec["github_err_lines"] = logs.strip().splitlines()[-1] if logs.strip() else "0"
    rc, tail = sh(host, pwd, "tail -5 /root/ComfyUI/comfy.log 2>/dev/null | grep -vE '%\\|' | tail -3")
    rec["log_tail"] = tail.strip()[:300]

    (outdir / f"round{idx}.log").write_text(
        f"uid={uid} gpu={gpu}\nprocs={procs}\nstart_cmd={startd}\n"
        f"github_err_lines={logs}\n--- tail ---\n{tail}\n", encoding="utf-8")

    try:
        cs.terminate(uid)
        rec["terminated"] = True
    except CompShareError as e:
        rec["terminated"] = f"失败 {str(e)[:60]}"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(prog="manager_probe")
    ap.add_argument("--rounds", type=int, default=5)
    a = ap.parse_args()

    outdir = ROOT / "reports" / "p1" / "manager-probe"
    outdir.mkdir(parents=True, exist_ok=True)
    cs = CS()
    results = []
    for i in range(1, a.rounds + 1):
        print(f"\n===== 第 {i}/{a.rounds} 轮 =====", flush=True)
        rec = one_round(cs, i, outdir)
        results.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)

    stuck = sum(1 for r in results if r.get("stuck"))
    valid = [r for r in results if "uid" in r]
    (outdir / "summary.json").write_text(
        json.dumps({"rounds": a.rounds, "valid": len(valid), "stuck": stuck,
                    "results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n===== 汇总 =====")
    print(f"有效轮次 {len(valid)} · 卡住 {stuck} · 比例 {stuck/max(1,len(valid)):.0%}")
    for r in valid:
        print(f"  第{r['round']}轮 {r.get('gpu'):<9} 建机 {r.get('up_sec')}s · "
              f"就绪 {r.get('ready_sec')}s · GitHub错误行 {r.get('github_err_lines')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
