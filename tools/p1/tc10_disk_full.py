#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TC-10：磁盘满导致拉取失败。

做法（真实链路，不 mock）：
  1. 在本地挂一个 64KB 的 tmpfs 当"产物目录"
  2. 在实例上造一个 200KB 的文件
  3. 用 _scp_get 去拉 → 写入时 ENOSPC
  4. 验证 step_fetch 把它包装成 RunError(FAILED_FETCH)
  5. 卸载 tmpfs、销毁实例

用法：
    python3 tools/p1/tc10_disk_full.py <uhost-id> <host> <password>
由调用方负责建机与销毁。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import run as R, state as S, store  # noqa: E402

TMPFS = Path("/tmp/p1-tc10-tmpfs")
SIZE = "64k"
REMOTE_FILE = "/root/ComfyUI/output/tc10_big.mp4"
REMOTE_SIZE_KB = 200


def sh(cmd: str, timeout=180) -> tuple[int, str]:
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    uid, host, pwd = sys.argv[1], sys.argv[2], sys.argv[3]

    print(f"目标实例 {uid} ({host})")
    results = []

    def rec(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"{'✓' if ok else '✗'} {name}  {'PASS' if ok else 'FAIL'}  {detail[:70]}")

    # ---------- 1. 远端造一个 200KB 文件 ----------
    rc, out = sh(f"sshpass -p '{pwd}' ssh -p 23 -o StrictHostKeyChecking=no "
                 f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 root@{host} "
                 f"'dd if=/dev/urandom of={REMOTE_FILE} bs=1024 count={REMOTE_SIZE_KB} 2>&1 | tail -1'")
    remote_ok = rc == 0
    rec("远端造 200KB 文件", remote_ok, out.strip()[:60])

    # ---------- 2. 本地挂 64KB tmpfs ----------
    sh(f"umount {TMPFS} 2>/dev/null; rm -rf {TMPFS}; mkdir -p {TMPFS}")
    rc, out = sh(f"mount -t tmpfs -o size={SIZE} tmpfs {TMPFS}")
    mounted = rc == 0
    rec(f"挂载 {SIZE} tmpfs", mounted, out.strip()[:60])
    if not mounted:
        print("无法挂 tmpfs，退出")
        return 1

    try:
        # 确认 tmpfs 真的只有 64KB
        rc, out = sh(f"df -h {TMPFS} | tail -1 | awk '{{print $2}}'")
        rec("tmpfs 容量确认", "64" in out or "63" in out, out.strip())

        # ---------- 3. 拉取 → 应触发 ENOSPC ----------
        jid = "p1-20261028T2200-tc1000"
        fake_jdir = TMPFS / "jobs" / jid
        (fake_jdir / "artifacts").mkdir(parents=True, exist_ok=True)

        orig_job_dir = store.job_dir
        store.job_dir = lambda j: TMPFS / "jobs" / j
        try:
            job = {"job_id": jid, "params": {}, "expect": {},
                   "model": {"key": "h3_t2v", "workflow": "workflows/h3_t2v_121f.api.json"}}
            ctx = {"host": host, "port": "23", "password": pwd}
            raised, fail_state, msg = False, None, ""
            t0 = time.time()
            try:
                R.step_fetch(job, None, ctx, [("", "tc10_big.mp4")])
            except R.RunError as e:
                raised, fail_state, msg = True, e.fail_state, str(e)
            elapsed = time.time() - t0

            ok_state = raised and fail_state == S.FAILED_FETCH
            rec("磁盘满时抛 FAILED_FETCH", ok_state, f"{elapsed:.1f}s · {msg[:60]}")

            # 残文件应被清掉（_scp_get 里 unlink missing_ok）
            leftovers = list((fake_jdir / "artifacts").glob("*"))
            rec("失败后残文件被清理", len(leftovers) == 0,
                f"残留 {len(leftovers)} 个" if leftovers else "无残留")
        finally:
            store.job_dir = orig_job_dir
    finally:
        sh(f"umount {TMPFS} 2>/dev/null; rm -rf {TMPFS}")
        sh(f"sshpass -p '{pwd}' ssh -p 23 -o StrictHostKeyChecking=no "
           f"-o UserKnownHostsFile=/dev/null root@{host} 'rm -f {REMOTE_FILE}' 2>/dev/null")

    ok_n = sum(1 for _, ok, _ in results if ok)
    print(f"\n合计 {len(results)} 项：PASS {ok_n} / FAIL {len(results) - ok_n}")
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
