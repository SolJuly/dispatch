#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.2 实验：镜像体积 = 基础镜像 + 系统盘容量，还是只算实际用量？

做法：用官方 ComfyUI 0.36.0 基础镜像起一台 50GB 系统盘的实例（不安装任何东西），
直接存一张测试镜像，读 Size 后立即删除镜像与实例。

判读：
  ~150GB → 体积 ≈ 100GB 基础 + 50GB 盘 → 瘦身可用“小盘重建”
  ~100GB → 体积只算实际用量（50GB 盘装不下 30GB 内容则另说）
  ~210GB → 体积与内容/盘大小无关

用法：python3 tools/p21/image_size_test.py [--disk 50] [--keep]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "reports/p2/p21/evidence/image-size-test.json"
BASE_IMAGE = "compshareImage-1vjv4ef2vy94"   # ComfyUI基础镜像纯净版0.36.0（100GB）
GPU = "V100S"
CPU, MEM = 10, 64


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--disk", type=int, default=100)
    ap.add_argument("--base", default=BASE_IMAGE)
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from compshare.cs import CS  # noqa: PLC0415

    cs = CS()
    t0 = time.time()
    name = f"p22-size-test-{dt.datetime.now():%H%M%S}"
    rec: dict = {"started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "base_image": a.base, "disk_gb": a.disk, "name": name}

    bal = float(cs.balance().get("AmountAvailable") or 0)
    print(f"[0] 余额 ¥{bal:.2f}", flush=True)
    if bal < 3:
        return 2
    if cs.stock(GPU) <= 0:
        print(f"{GPU} 无现货")
        return 3

    uid = cs.create(image_id=a.base, gpu_type=GPU, gpu=1, cpu=CPU, memory_gb=MEM,
                    name=name, charge_type="Postpay", disk_gb=a.disk, data_disk_gb=0,
                    remark="p22 image size test")
    uid = uid[0]
    print(f"[1] 实例 {uid}（{a.disk}GB 盘），等待 running…", flush=True)
    try:
        cs.wait_state(uid, {"running"}, timeout=600)
        ok = True
    except Exception as e:  # noqa: BLE001
        print(f"[1] 启动失败：{e}")
        ok = False
    rec["boot_ok"] = ok

    img_id = ""
    if ok:
        host, pwd = cs.public_ip(cs.get(uid)), cs.password_of(cs.get(uid))
        import subprocess
        cmd = (f"sshpass -p {pwd!r} ssh -p 23 -o StrictHostKeyChecking=no "
               f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
               f"-c aes128-gcm@openssh.com root@{host} 'df -h / ; du -shx /root/ComfyUI 2>/dev/null'")
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=240)
        print("[2] 盘内观测:\n" + (p.stdout or "") + (p.stderr or ""), flush=True)
        rec["df"] = (p.stdout or "")[:500]

        print("[3] 保存测试镜像（不安装任何东西）…", flush=True)
        try:
            r = cs.save_image(uid, name, "p22 size test")
            img_id = r.get("CompShareImageId") or ""
            print(f"    {r}")
        except Exception as e:  # noqa: BLE001
            print(f"    保存失败：{e}")
            rec["save_error"] = str(e)

    if img_id:
        print("[4] 等待镜像 Available …", flush=True)
        for _ in range(60):
            row = next((i for i in cs.images() if i.get("CompShareImageId") == img_id), None)
            if row and row.get("Status") == "Available":
                rec["image"] = {"id": img_id, "size_mb": row.get("Size"),
                                "actual_mb": row.get("ActualSize")}
                print(f"    Size={row.get('Size')}MB Actual={row.get('ActualSize')}MB", flush=True)
                break
            if row and row.get("Status") in ("UnAvailable", "Failed"):
                print(f"    状态 {row.get('Status')}")
                rec["image_status"] = row.get("Status")
                break
            time.sleep(10)
        else:
            print("    超时未 Available")

    print("[5] 清理 …", flush=True)
    if img_id and not a.keep:
        try:
            cs.call("TerminateCompShareCustomImage", {"CompShareImageId": img_id})
            print("    已删除测试镜像")
        except Exception as e:  # noqa: BLE001
            print(f"    ⚠️ 删镜像失败：{e}")
            rec["delete_image_error"] = str(e)
    if not a.keep:
        try:
            cs.terminate(uid)
            print("    已删除实例")
        except Exception as e:  # noqa: BLE001
            print(f"    ⚠️ 删实例失败：{e}")
            rec["delete_instance_error"] = str(e)

    dur = time.time() - t0
    rec.update({"instance": uid, "duration_s": round(dur, 1),
                "est_cost": round(1.41 * dur / 3600, 3),
                "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[6] 用时 {dur/60:.1f} 分钟 · 估算 ¥{rec['est_cost']:.3f} · {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
