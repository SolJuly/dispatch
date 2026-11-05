#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.2 镜像体积归因：起一台便宜实例，只读盘点 210GB 到底花在哪，跑完立即销毁。

不做任何删除/修改，只采集：
  - df / lsblk / overlay 层
  - du 全盘一级目录
  - du /root/ComfyUI、/root/miniconda3、~/.cache、site-packages
  - >1GB 大文件清单
  - /root/ComfyUI/models 软链 vs 实体

用法：
    python3 tools/p21/image_probe.py            # 建机 → 盘点 → 销毁
    python3 tools/p21/image_probe.py --keep     # 保留实例排查
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = ROOT / "reports/p2/p21/evidence"
IMAGE_ID = "compshareImage-1xc7km2mb0ob"   # p2-comfy-media-v3
GPU = "V100S"
CPU, MEM = 10, 64

PROBE_SH = r"""
set +e
echo "### date"; date -Is
echo "### df"; df -hT / /root /usr /var /opt 2>&1
echo "### du_top"; for d in /root /usr /opt /var /etc /home /srv /tmp; do printf "%s " "$d"; timeout 90 du -shx "$d" 2>/dev/null | cut -f1; done
echo "### du_root1"; timeout 90 du -xh --max-depth=1 /root 2>/dev/null | sort -rh | head -20
echo "### comfy1"; timeout 60 du -xh --max-depth=1 /root/ComfyUI 2>/dev/null | sort -rh | head -20
echo "### comfy_sub"; timeout 60 du -shx /root/ComfyUI/.git /root/ComfyUI/output /root/ComfyUI/input /root/ComfyUI/temp /root/ComfyUI/models /root/ComfyUI/custom_nodes /root/ComfyUI/comfy 2>/dev/null
echo "### conda1"; timeout 60 du -xh --max-depth=1 /root/miniconda3 2>/dev/null | sort -rh | head -15
echo "### pkgs"; timeout 30 du -shx /root/miniconda3/pkgs 2>/dev/null
echo "### site"; timeout 60 du -xhd1 /root/miniconda3/lib/python3.10/site-packages 2>/dev/null | sort -rh | head -15
echo "### cache"; timeout 40 du -shx /root/.cache/* 2>/dev/null | sort -rh | head -10
echo "### models_ls"; ls -la /root/ComfyUI/models 2>/dev/null | head -30
echo "### models_dir"; for d in /root/ComfyUI/models/*/; do printf "%s %s\n" "$(timeout 20 du -sh "$d" 2>/dev/null | cut -f1)" "$d"; done | sort -rh | head -20
echo "### big"; timeout 90 find /root -xdev -type f -size +300M -printf '%s %p\n' 2>/dev/null | sort -rn | head -25
echo "### uclfs"; ls /var/lib/uimgacc 2>/dev/null | head -3; timeout 25 du -shx /var/lib/uimgacc 2>/dev/null
echo "### done"
"""


def sh(cmd: str, timeout: int = 90) -> tuple[int, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ssh_cmd(host: str, pwd: str, remote: str, timeout: int = 90) -> tuple[int, str]:
    cmd = (
        f"sshpass -p {pwd!r} ssh -p 23 -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
        f"-c aes128-gcm@openssh.com root@{host} " + repr(remote)
    )
    return sh(cmd, timeout=timeout)


def run_detached(host: str, pwd: str, script: str, timeout: int = 600) -> tuple[int, str]:
    b64 = base64.b64encode((script + "\ntouch /root/probe.done\n").encode()).decode()
    ssh_cmd(host, pwd,
            f"rm -f /root/probe.done /root/probe.out; echo {b64} | base64 -d > /root/probe.sh", 120)
    ssh_cmd(host, pwd, "nohup bash /root/probe.sh > /root/probe.out 2>&1 & echo SPAWNED", 60)
    t0 = time.time()
    while time.time() - t0 < timeout:
        _rc, out = ssh_cmd(host, pwd, "test -f /root/probe.done && echo DONE || echo RUNNING", 40)
        if "DONE" in out:
            break
        time.sleep(5)
    return ssh_cmd(host, pwd, "cat /root/probe.out", 180)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", default=GPU)
    ap.add_argument("--cpu", type=int, default=CPU)
    ap.add_argument("--mem", type=int, default=MEM)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--id", default="")
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from compshare.cs import CS  # noqa: PLC0415

    cs = CS()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    rec: dict = {"started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "gpu": a.gpu, "image": IMAGE_ID}

    bal = float(cs.balance().get("AmountAvailable") or 0)
    if bal < 3:
        print(f"余额 ¥{bal:.2f} < ¥3，中止")
        return 2
    price = cs.price(a.gpu, 1, a.cpu, a.mem, "Postpay", 100, 0, image_id=IMAGE_ID)
    hourly = float(price.get("total") or 0)
    print(f"[0] 余额 ¥{bal:.2f} · {a.gpu} {a.cpu}C/{a.mem}G ¥{hourly:.2f}/h", flush=True)

    uid = a.id
    if not uid:
        if cs.stock(a.gpu) <= 0:
            print(f"{a.gpu} 无现货，退出")
            return 3
        ids = cs.create(image_id=IMAGE_ID, gpu_type=a.gpu, gpu=1, cpu=a.cpu,
                        memory_gb=a.mem, name="p22-image-probe", charge_type="Postpay",
                        disk_gb=100, data_disk_gb=0, remark="p22 image probe")
        uid = ids[0]
        print(f"[1] 已创建 {uid}，等待 running…", flush=True)
        inst = cs.wait_state(uid, {"running"}, timeout=900)
    else:
        inst = cs.get(uid)

    host, pwd = cs.public_ip(inst), cs.password_of(inst)
    if not host:
        print("取不到公网 IP，退出")
        return 5

    try:
        cs.stop_at(uid, int((dt.datetime.now().astimezone() + dt.timedelta(hours=1)).timestamp()))
        print("[2] 已设 1 小时定时关机兜底", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[2] 定时关机设置失败（继续）：{e}")

    print(f"[3] ssh {host}:23 探活…", flush=True)
    ok = False
    for _ in range(30):
        rc, out = ssh_cmd(host, pwd, "echo ssh-ready", timeout=40)
        if rc == 0 and "ssh-ready" in out:
            ok = True
            break
        time.sleep(6)
    if not ok:
        print("ssh 未就绪，实例保留供排查：" + uid)
        return 4
    print("[3] ssh 就绪", flush=True)

    print("[4] 只读盘点中（du / find，约 2–4 分钟）…", flush=True)
    rc, out = run_detached(host, pwd, PROBE_SH, timeout=600)
    rec["raw"] = out

    def grab(pat: str) -> str:
        m = re.search(pat, out, re.M)
        return m.group(1).strip() if m else ""

    rec["derived"] = {
        "root_total": grab(r"^### du_root_total_x\n(\S+)"),
        "root_apparent": grab(r"^### du_root_total_apparent\n(\S+)"),
        "df_root": grab(r"^### df\n(.*)"),
        "done": "### done" in out,
    }
    rec["ssh_rc"] = rc

    print("[5] 销毁实例 …", flush=True)
    if a.keep:
        print(f"    --keep：实例保留 {uid}")
        rec["kept"] = uid
    else:
        try:
            cs.terminate(uid)
            print("    已删除")
        except Exception as e:  # noqa: BLE001
            print(f"    ⚠️ 删除失败：{e}（务必手动 rm {uid}）")
            rec["delete_error"] = str(e)

    dur = time.time() - t0
    rec.update({"instance": uid, "host": host, "hourly": hourly, "duration_s": round(dur, 1),
                "est_cost": round(hourly * dur / 3600, 4),
                "finished_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    path = OUT_DIR / "image-probe.txt"
    path.write_text(out, encoding="utf-8")
    jpath = OUT_DIR / "image-probe.json"
    jpath.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[6] 用时 {dur/60:.1f} 分钟 · 估算 ¥{rec['est_cost']:.3f}")
    print(f"    原始输出 {path.relative_to(ROOT)} · 记录 {jpath.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
