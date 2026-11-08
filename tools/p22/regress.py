#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在指定实例上跑一张 ComfyUI API 图，用于镜像回归（不经过 job 框架）。

    python3 tools/p22/regress.py --uid uhost-x --graph workflows/zimage_turbo_t2i.api.json
    python3 tools/p22/regress.py --uid uhost-x --graph workflows/qwen_image_edit_2509.api.json \
        --input reports/p2/p21/inputs/in-f9bd4904.jpg:qwen_edit_in.jpg

做四件事：上传输入图 → POST /prompt → 轮询 /history → 打印输出文件与大小。
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.p21.remote import inst  # noqa: E402


def sh(cmd: str, timeout: int = 120) -> tuple[int, str]:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ssh(host: str, pwd: str, script: str, timeout: int = 300) -> str:
    b64 = base64.b64encode(script.encode()).decode()
    inner = f"echo {b64} | base64 -d | bash"
    cmd = (f"sshpass -p {pwd!r} ssh -p 23 -o StrictHostKeyChecking=no "
           f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
           f"-c aes128-gcm@openssh.com root@{host} " + repr(inner))
    rc, out = sh(cmd, timeout=timeout)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", required=True)
    ap.add_argument("--graph", required=True)
    ap.add_argument("--input", action="append", default=[], help="本地路径:远端文件名")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    graph = json.load(open(a.graph))
    host, pwd = inst(a.uid)

    for pair in a.input:
        local, remote = pair.rsplit(":", 1)
        cmd = (f"sshpass -p {pwd!r} scp -q -P 23 -o StrictHostKeyChecking=no "
               f"-o UserKnownHostsFile=/dev/null {local!r} root@{host}:/root/ComfyUI/input/{remote}")
        rc, out = sh(cmd, timeout=180)
        print(f"upload {remote}: rc={rc} {out[-200:]}", flush=True)
        if rc != 0:
            return 4

    payload = json.dumps({"prompt": graph, "client_id": "p22-regress"}).encode().decode()
    remote = f'''
source /root/miniconda3/bin/activate
cat > /root/graph_payload.json <<'JSON'
{payload}
JSON
python3 - <<'PY'
import json, time, urllib.request, urllib.error
payload = json.load(open('/root/graph_payload.json'))
req = urllib.request.Request('http://127.0.0.1:8188/prompt',
                             data=json.dumps(payload).encode(),
                             headers={{'Content-Type': 'application/json'}})
t0 = time.time()
try:
    r = json.load(urllib.request.urlopen(req, timeout=30))
except urllib.error.HTTPError as e:
    print('SUBMIT_FAIL', e.code, e.read().decode()[:3000]); raise SystemExit(1)
pid = r['prompt_id']
print('PROMPT', pid, flush=True)
while time.time() - t0 < {a.timeout}:
    try:
        h = json.load(urllib.request.urlopen('http://127.0.0.1:8188/history/' + pid, timeout=10))
    except Exception:
        h = {{}}
    if pid in h:
        entry = h[pid]
        st = entry.get('status', {{}})
        print('STATUS', st.get('status_str'), st.get('completed'))
        for m in st.get('messages', []):
            if m[0] in ('execution_error', 'execution_interrupted'):
                print('MSG', m[0], json.dumps(m[1])[:2500])
        print('OUTPUTS', json.dumps(entry.get('outputs', {{}}), ensure_ascii=False)[:2500])
        break
    time.sleep(5)
else:
    print('TIMEOUT')
PY
'''
    out = ssh(host, pwd, remote, timeout=a.timeout + 180)
    print(out)
    ok = "STATUS success True" in out or ("STATUS" in out and "success" in out and "execution_error" not in out)
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
