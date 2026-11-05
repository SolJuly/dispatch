#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2.1 M0：GPU 共享库资产盘点（一次性，跑完立即销毁实例）。

确认三件事：
  1. SDXL / FLUX / Qwen 权重的确切文件名（命中 ≠ 可读，P1 踩过断链）
  2. inpaint / ControlNet / LoRA / IP-Adapter / upscale / 人脸检测 资产是否存在
  3. ComfyUI `/object_info` 里这些文件是否真的对 loader 可见

用法：
    python3 tools/p21/inventory.py                 # 起 4090 → 盘点 → 销毁
    python3 tools/p21/inventory.py --keep          # 保留实例（调试用，记得手动 rm）
    python3 tools/p21/inventory.py --id uhost-xxx  # 复用已存在的实例
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
BASE_IMAGE = "compshareImage-1xc7km2mb0ob"   # p2-comfy-base-v1（image.json base 层）
GPU = "4090"
CPU, MEM = 16, 64

INVENTORY_SH = r"""
set +e
echo "### host"; hostname; date -Is
for i in $(seq 1 30); do nvidia-smi -L >/dev/null 2>&1 && break; sleep 3; done
echo "### gpu"; nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>&1 | head -1
echo "### mount"; mountpoint -q /model && echo MOUNT_OK || echo NO_MOUNT

# 只做定点列举：/model 是 40T UPFS，find 会跑几分钟并把脚本拖死
for d in checkpoints unet diffusion_models vae loras controlnet Controlnet ipadapter clip_vision upscale_models ultralytics inpaint; do
  echo "### count $d $(ls /model/comfyui/models/$d 2>/dev/null | wc -l)"
done
echo "### ckpt_sdxl"; ls /model/comfyui/models/checkpoints 2>/dev/null | grep -iE 'juggernaut|sdxl|sd_xl|realvis' | head -10
echo "### ckpt_inpaint"; ls /model/comfyui/models/checkpoints 2>/dev/null | grep -iE 'inpaint|fill' | head -10
echo "### unet_qwen_flux"; ls /model/comfyui/models/unet 2>/dev/null | grep -iE 'qwen|flux|fill' | head -12
echo "### unet_subdirs"; for d in /model/comfyui/models/unet/*/ ; do echo "DIR $d"; ls "$d" 2>/dev/null | head -4; done | head -40
echo "### loras_sdxl"; ls /model/comfyui/models/loras/Sdxl 2>/dev/null | head -10
echo "### loras_qwen"; ls /model/comfyui/models/loras/Qwen 2>/dev/null | head -8
echo "### controlnet"; ls /model/comfyui/models/controlnet 2>/dev/null | head -20
echo "### controlnet_flux"; ls /model/comfyui/models/Controlnet/FLUX 2>/dev/null | head -12
echo "### ipadapter"; ls /model/comfyui/models/ipadapter 2>/dev/null | head -8
echo "### upscale"; ls /model/comfyui/models/upscale_models 2>/dev/null
echo "### ultralytics"; ls /model/comfyui/models/ultralytics 2>/dev/null
echo "### inpaint_dir"; ls /model/comfyui/models/inpaint 2>/dev/null
echo "### qwen_links"; ls -l /root/ComfyUI/models/controlnet/ 2>/dev/null | head -8
echo "### custom_nodes"; ls /root/ComfyUI/custom_nodes 2>/dev/null
echo "### comfy_proc"; pgrep -fa "main.py --listen" | head -2

echo "### comfy_wait"
for i in $(seq 1 40); do
  curl -s -m 3 -o /dev/null http://127.0.0.1:8188/object_info && { echo COMFY_UP; break; }
  sleep 3
done
if ! curl -s -m 3 -o /dev/null http://127.0.0.1:8188/object_info; then
  echo "### comfy_start"
  cd /root/ComfyUI && nohup python3 -u main.py --listen 0.0.0.0 --port 8188 >/root/comfy.out 2>&1 &
  for i in $(seq 1 40); do
    curl -s -m 3 -o /dev/null http://127.0.0.1:8188/object_info && { echo COMFY_UP_AFTER_START; break; }
    sleep 3
  done
fi
echo "### comfy_log"; tail -n 12 /root/comfy.out 2>/dev/null

echo "### object_info"
python3 - <<'PY'
import json, urllib.request
try:
    oi = json.load(urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=30))
except Exception as e:
    print("OBJECT_INFO_FAIL", repr(e)); raise SystemExit(0)
def req(node, key):
    try:
        return oi[node]["input"]["required"][key][0]
    except Exception:
        return []
ck = req("CheckpointLoaderSimple", "ckpt_name")
un = req("UNETLoader", "unet_name")
lx = req("LoraLoader", "lora_name")
cn = req("ControlNetLoader", "control_net_name")
up = req("UpscaleModelLoader", "model_name")
print("CKPT_N", len(ck))
print("CKPT_SDXL", json.dumps([c for c in ck if any(k in c.lower() for k in ("juggernaut","sdxl","sd_xl","realvis"))][:20], ensure_ascii=False))
print("CKPT_INPAINT", json.dumps([c for c in ck if any(k in c.lower() for k in ("inpaint","fill"))][:10], ensure_ascii=False))
print("UNET_N", len(un))
print("UNET_QWEN_FLUX", json.dumps([u for u in un if any(k in u.lower() for k in ("qwen","flux","fill"))][:20], ensure_ascii=False))
print("LORA_N", len(lx))
print("LORA_SDXL", json.dumps([x for x in lx if "sdxl" in x.lower()][:10], ensure_ascii=False))
print("CONTROLNET_N", len(cn))
print("CONTROLNET_INPAINT", json.dumps([x for x in cn if "inpaint" in x.lower() or "fill" in x.lower()][:10], ensure_ascii=False))
print("UPSCALE_N", len(up), json.dumps(up[:6], ensure_ascii=False))
print("NODES_HAS", json.dumps([n for n in ("VAEEncode","VAEEncodeForInpaint","SetLatentNoiseMask","LoraLoader","ControlNetLoader","IPAdapterModelLoader","ImageScaleBy","FaceDetailer","UpscaleModelLoader","InpaintModelConditioning") if n in oi], ensure_ascii=False))
PY
echo "### done"
"""


def sh(cmd: str, timeout: int = 90) -> tuple[int, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ssh_cmd(host: str, port: int, pwd: str, remote: str, timeout: int = 90) -> tuple[int, str]:
    cmd = (
        f"sshpass -p {pwd!r} ssh -p {port} -o StrictHostKeyChecking=no "
        f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
        f"-c aes128-gcm@openssh.com root@{host} " + repr(remote)
    )
    return sh(cmd, timeout=timeout)


def run_detached(host: str, port: int, pwd: str, script: str, timeout: int = 540) -> tuple[int, str]:
    """远端 nohup 跑盘点，再轮询取结果。

    同步 ssh 执行会被远端中断（首轮实测 Connection closed and 脚本被拖死），
    改后台跑 + 完成标记，ssh 断线不影响。
    """
    b64 = base64.b64encode((script + "\ntouch /root/inv.done\n").encode()).decode()
    ssh_cmd(host, port, pwd,
            f"rm -f /root/inv.done /root/inv.out; echo {b64} | base64 -d > /root/inv.sh", 120)
    ssh_cmd(host, port, pwd, "nohup bash /root/inv.sh > /root/inv.out 2>&1 & echo SPAWNED", 60)
    t0 = time.time()
    while time.time() - t0 < timeout:
        _rc, out = ssh_cmd(host, port, pwd,
                           "test -f /root/inv.done && echo DONE || echo RUNNING", 40)
        if "DONE" in out:
            break
        time.sleep(5)
    return ssh_cmd(host, port, pwd, "cat /root/inv.out", 120)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpu", default=GPU)
    ap.add_argument("--id", default="", help="复用已存在的 uhost id")
    ap.add_argument("--keep", action="store_true", help="跑完不销毁")
    ap.add_argument("--name", default="p21-inventory")
    a = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    from compshare.cs import CS  # noqa: PLC0415

    cs = CS()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    rec: dict = {"started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "gpu": a.gpu, "image": BASE_IMAGE}

    bal = float(cs.balance().get("AmountAvailable") or 0)
    if bal < 5:
        print(f"余额 ¥{bal:.2f} < ¥5，中止")
        return 2
    price = cs.price(a.gpu, 1, CPU, MEM, "Postpay", 100, 0, image_id=BASE_IMAGE)
    hourly = float(price.get("total") or 0)
    print(f"[0] 余额 ¥{bal:.2f} · {a.gpu} ¥{hourly:.2f}/h", flush=True)

    uid = a.id
    if not uid:
        if cs.stock(a.gpu) <= 0:
            print(f"{a.gpu} 无现货，退出")
            return 3
        ids = cs.create(image_id=BASE_IMAGE, gpu_type=a.gpu, gpu=1, cpu=CPU,
                        memory_gb=MEM, name=a.name, charge_type="Postpay",
                        disk_gb=100, data_disk_gb=0, remark="p21 inventory")
        uid = ids[0]
        print(f"[1] 已创建 {uid}，等待 running…", flush=True)
        inst = cs.wait_state(uid, {"running"}, timeout=900)
    else:
        inst = cs.get(uid)

    host, port, pwd = cs.public_ip(inst), "23", cs.password_of(inst)
    if not host:
        print("取不到公网 IP，退出")
        return 5

    try:
        cs.stop_at(uid, int((dt.datetime.now().astimezone() + dt.timedelta(hours=1)).timestamp()))
        print("[2] 已设 1 小时定时关机兜底", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[2] 定时关机设置失败（继续）：{e}")

    print(f"[3] ssh {host}:{port} 探活…", flush=True)
    ok = False
    for _ in range(30):
        rc, out = ssh_cmd(host, port, pwd, "echo ssh-ready", timeout=40)
        if rc == 0 and "ssh-ready" in out:
            ok = True
            break
        time.sleep(6)
    if not ok:
        print("ssh 未就绪，实例保留供排查：" + uid)
        return 4
    print("[3] ssh 就绪", flush=True)

    print("[4] 盘点共享库与 object_info（含等待 ComfyUI 就绪）…", flush=True)
    rc, out = run_detached(host, port, pwd, INVENTORY_SH, timeout=540)
    rec["raw"] = out
    rec["ssh_rc"] = rc

    counts = {m.group(1): int(m.group(2)) for m in re.finditer(r"^### count (\S+) (\d+)$", out, re.M)}

    def grab(pat: str, default=""):
        m = re.search(pat, out, re.M)
        return m.group(1) if m else default

    rec["derived"] = {
        "mount_ok": "MOUNT_OK" in out,
        "comfy_up": ("COMFY_UP" in out) or ("COMFY_UP_AFTER_START" in out),
        "counts": counts,
        "nodes_has": grab(r"^NODES_HAS (.*)$"),
        "ckpt_sdxl": grab(r"^CKPT_SDXL (.*)$"),
        "ckpt_inpaint": grab(r"^CKPT_INPAINT (.*)$"),
        "unet_qwen_flux": grab(r"^UNET_QWEN_FLUX (.*)$"),
        "controlnet_inpaint": grab(r"^CONTROLNET_INPAINT (.*)$"),
        "lora_n": int(grab(r"^LORA_N (\d+)$", "0")),
        "controlnet_n": int(grab(r"^CONTROLNET_N (\d+)$", "0")),
        "upscale_n": int(grab(r"^UPSCALE_N (\d+)$", "0").split()[0] or 0),
        "object_info_ok": "OBJECT_INFO_FAIL" not in out and "CKPT_N" in out,
    }

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
    path = OUT_DIR / f"inventory-{dt.date.today():%Y%m%d}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[6] 用时 {dur/60:.1f} 分钟 · 估算 ¥{rec['est_cost']:.3f} · 记录 {path.relative_to(ROOT)}")
    print(json.dumps(rec["derived"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
