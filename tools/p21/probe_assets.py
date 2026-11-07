#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：在临时实例上列 /model 里 H3 相关权重，确认模型卡里的文件名是否真实存在。

  python3 tools/p21/probe_assets.py --pattern nvfp4 --pattern audio_vae

只读操作：建机 → ssh find/ls → 立刻销毁。约 4 分钟 ≈ ¥0.1。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from compshare.cs import CS            # noqa: E402
from tools.p1 import run as R           # noqa: E402

IMAGE_ID = "compshareImage-1xdh3lcu5dvu"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", action="append", default=[], help="文件名片段，可多次")
    ap.add_argument("--dir", action="append", default=[], help="要 ls 的目录，可多次")
    ap.add_argument("--gpu", default="4090")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args(argv)

    pats = a.pattern or ["nvfp4", "audio_vae"]
    dirs = a.dir or ["/model/ModelScope/Comfy-Org/MiniMax-H3/text_encoders",
                     "/model/ModelScope/Comfy-Org/MiniMax-H3/vae"]

    cs = CS()
    ids = cs.create(image_id=IMAGE_ID, gpu_type=a.gpu, gpu=1, cpu=16, memory_gb=64,
                    name="probe-assets", charge_type="Postpay", disk_gb=100,
                    data_disk_gb=0, remark="asset probe")
    uid = ids[0] if isinstance(ids, (list, tuple)) else ids
    print(f"created {uid}", flush=True)
    try:
        info = cs.wait_state(uid, ["Running"], timeout=a.timeout)
        host, pwd = cs.public_ip(info), cs.password_of(info)
        for _ in range(30):
            _rc, out = R._ssh_run(host, "23", pwd, "echo ok", timeout=60)
            if "ok" in out:
                break
            time.sleep(5)
        print(f"up {host}", flush=True)

        for d in dirs:
            _rc, out = R._ssh_run(host, "23", pwd, f"ls -1 {d} 2>&1 | head -40", timeout=120)
            print(f"\n=== ls {d}\n{out.strip()}", flush=True)

        expr = " -o ".join(f"-iname '*{p}*'" for p in pats)
        cmd = f"find /model \\( {expr} \\) -printf '%p\\t%s\\n' 2>/dev/null | head -40"
        _rc, out = R._ssh_run(host, "23", pwd, cmd, timeout=300)
        print(f"\n=== find {' '.join(pats)}\n{out.strip()}", flush=True)
    finally:
        try:
            cs.terminate(uid)
            print(f"terminated {uid}", flush=True)
        except Exception as e:      # noqa: BLE001
            print(f"terminate ERR {e}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
