#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在保活实例上执行远端 bash（脚本走 base64，避免引号地狱）。

    from tools.p21.remote import inst, run
    host, pwd = inst("uhost-xxx")
    print(run(host, pwd, "ls /root"))
"""
from __future__ import annotations

import base64
import subprocess

IMAGE_ID = "compshareImage-1xdh3lcu5dvu"


def inst(uid: str):
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from compshare.cs import CS  # noqa: PLC0415

    cs = CS()
    i = cs.get(uid)
    return cs.public_ip(i), cs.password_of(i)


def run(host: str, pwd: str, script: str, timeout: int = 300, quiet: bool = False) -> str:
    b64 = base64.b64encode(script.encode()).decode()
    inner = f"echo {b64} | base64 -d | bash"
    cmd = (f"sshpass -p {pwd!r} ssh -p 23 -o StrictHostKeyChecking=no "
           f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
           f"-c aes128-gcm@openssh.com root@{host} " + repr(inner))
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "") + (p.stderr or "")
    if not quiet:
        print(out, flush=True)
    return out
