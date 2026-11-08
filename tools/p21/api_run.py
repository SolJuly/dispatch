#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在实例本机跑一条 ComfyUI /prompt 图（不经过 job 框架，用来验节点/权重）。

    python3 tools/p21/api_run.py --uid uhost-x --graph graph.json --timeout 600
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p21.remote import inst  # noqa: E402
import base64
import subprocess


def run(host, pwd, script, timeout=900):
    b64 = base64.b64encode(script.encode()).decode()
    inner = f"echo {b64} | base64 -d | bash"
    cmd = (f"sshpass -p {pwd!r} ssh -p 23 -o StrictHostKeyChecking=no "
           f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
           f"-c aes128-gcm@openssh.com root@{host} " + repr(inner))
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", required=True)
    ap.add_argument("--graph", required=True)
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    graph = json.load(open(a.graph))
    host, pwd = inst(a.uid)
    payload = json.dumps({"prompt": graph, "client_id": "p21-smoke"}).encode().decode()
    script = f"""
source /root/miniconda3/bin/activate
cat > /root/graph_payload.json <<'JSON'
{payload}
JSON
python3 - <<'PY'
import json, time, urllib.request
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
        h = json.load(urllib.request.urlopen(f'http://127.0.0.1:8188/history/{{pid}}', timeout=10))
    except Exception:
        h = {{}}
    if pid in h:
        entry = h[pid]
        st = entry.get('status', {{}})
        print('STATUS', st.get('status_str'), st.get('completed'))
        for m in st.get('messages', []):
            if m[0] in ('execution_error', 'execution_interrupted'):
                print('MSG', m[0], json.dumps(m[1])[:2000])
        outs = entry.get('outputs', {{}})
        print('OUTPUTS', json.dumps(outs, ensure_ascii=False)[:2000])
        break
    time.sleep(5)
else:
    print('TIMEOUT')
PY
echo ===
ls -la /root/ComfyUI/output/seedvr2_test 2>/dev/null | tail -5
grep -iE 'error|out of memory|OOM' /root/ComfyUI/comfy.log | tail -8
"""
    print(run(host, pwd, script, timeout=a.timeout + 240))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
