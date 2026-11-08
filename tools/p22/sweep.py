#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4 回归补测：在一台实例上串行跑多条 verified 工作流（不走 job 框架）。

    python3 tools/p22/sweep.py --uid uhost-x            # 在已有实例上跑
    python3 tools/p22/sweep.py --create                 # 起一台 4090（v4）跑完销毁

每条工作流的参数取自它最近一次成功的 registry job（legacy 工作流用固定占位符替换）。
结果写到 reports/p2/p21/evidence/v4-sweep.json，并打印汇总表。
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

IMAGE_ID = "compshareImage-1xdh3lcu5dvu"
INPUTS = ROOT / "reports/p2/p21/inputs"
OUT = ROOT / "reports/p2/p21/evidence/v4-sweep.json"

DONE = {"zimage_turbo_t2i", "qwen_image_edit_2509", "seedvr2_3b_upscale",
        "pack_video_wan2_2_14b_i2v"}
LEGACY = {"h3_t2v_121f", "ltxv_2b_t2v", "wan21_i2v_480p"}
# legacy 工作流不经过 registry 的 setup.links，这里补它依赖的软链
LEGACY_LINKS = {
    "h3_t2v_121f": [
        ("/root/ComfyUI/models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
         "/model/ModelScope/Comfy-Org/MiniMax-H3/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors"),
    ],
}
PROMPT = "a cinematic photo of a young woman by the sea at sunset, 85mm, detailed skin"


def sh(cmd: str, timeout: int = 120):
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


def upload(host: str, pwd: str, local: Path, remote: str) -> bool:
    cmd = (f"sshpass -p {pwd!r} scp -q -P 23 -o StrictHostKeyChecking=no "
           f"-o UserKnownHostsFile=/dev/null {str(local)!r} root@{host}:/root/ComfyUI/input/{remote}")
    rc, _ = sh(cmd, timeout=300)
    return rc == 0


def legacy_graph(key: str) -> dict:
    g = json.load(open(ROOT / f"workflows/{key}.api.json"))
    sub = {"{{PROMPT}}": PROMPT, "{{NEG_PROMPT}}": "", "{{SEED}}": "42",
           "{{OUTPUT_PREFIX}}": key, "{{FIRST_FRAME}}": "in-a6891d7b.png"}
    s = json.dumps(g, ensure_ascii=False)
    for k, v in sub.items():
        s = s.replace(k, v)
    return json.loads(s)


def render(key: str) -> tuple[dict, dict]:
    if key in LEGACY:
        return legacy_graph(key), {"legacy": True}
    import glob
    from tools.p20 import render as p20render
    best = None
    for p in glob.glob(str(ROOT / "reports/p1/jobs/*/job.json")):
        d = json.load(open(p))
        reg = (d.get("model") or {}).get("registry") or {}
        if reg.get("workflow_key") == key:
            if best is None or d.get("created_at", "") > best[0]:
                best = (d.get("created_at", ""), d)
    if not best:
        raise RuntimeError(f"没有 {key} 的 registry job")
    graph, meta = p20render.render_job(best[1])
    return graph, meta


def inputs_of(graph: dict) -> list[str]:
    names = set()
    for n in graph.values():
        for v in (n.get("inputs") or {}).values():
            if isinstance(v, str) and v.lower().endswith((".png", ".jpg", ".jpeg", ".mp4", ".wav")):
                names.add(v)
    return sorted(names)


def run_one(host: str, pwd: str, key: str, timeout: int) -> dict:
    t0 = time.time()
    rec: dict = {"workflow": key, "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    try:
        graph, meta = render(key)
    except Exception as e:  # noqa: BLE001
        rec.update({"status": "RENDER_FAIL", "error": str(e)[:300]})
        return rec
    rec["nodes"] = len(graph)
    rec["meta"] = {k: meta.get(k) for k in ("workflow_key", "model_key", "legacy") if meta.get(k)}

    for link, target in LEGACY_LINKS.get(key, []):
        ssh(host, pwd, f"mkdir -p {link.rsplit('/',1)[0]} && ln -sf {target} {link} && echo LINK", timeout=60)

    missing = []
    for name in inputs_of(graph):
        local = INPUTS / name
        if local.exists():
            if not upload(host, pwd, local, name):
                missing.append(f"upload:{name}")
        else:
            missing.append(name)
    if missing:
        rec.update({"status": "INPUT_MISSING", "error": ",".join(missing)[:300]})
        return rec

    payload = json.dumps({"prompt": graph, "client_id": "p22-sweep"}).encode().decode()
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
    print('SUBMIT_FAIL', e.code, e.read().decode()[:2500]); raise SystemExit(1)
pid = r['prompt_id']
print('PROMPT', pid, flush=True)
while time.time() - t0 < {timeout}:
    try:
        h = json.load(urllib.request.urlopen('http://127.0.0.1:8188/history/' + pid, timeout=10))
    except Exception:
        h = {{}}
    if pid in h:
        e = h[pid]; st = e.get('status', {{}})
        print('STATUS', st.get('status_str'), st.get('completed'))
        for m in st.get('messages', []):
            if m[0] == 'execution_error':
                d = m[1]
                print('ERR', d.get('node_type'), '|', str(d.get('exception_message'))[:500])
        print('OUTPUTS', json.dumps(e.get('outputs', {{}}), ensure_ascii=False)[:1200])
        break
    time.sleep(5)
else:
    print('TIMEOUT')
PY
'''
    out = ssh(host, pwd, remote, timeout=timeout + 240)
    rec["seconds"] = round(time.time() - t0, 1)
    ok = "STATUS success True" in out
    rec["status"] = "PASS" if ok else "FAIL"
    if not ok:
        rec["error"] = out[-600:]
    m = [ln for ln in out.splitlines() if ln.startswith("OUTPUTS ")]
    rec["outputs"] = m[-1][8:][:400] if m else ""
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--only", default="", help="逗号分隔的工作流 key")
    ap.add_argument("--timeout", type=int, default=1200)
    a = ap.parse_args()

    from tools.p20 import registry as reg
    verified = [e["workflow"] for e in json.load(open(ROOT / "registry/compat.json"))["entries"]
                if e["status"] == "verified"]
    todo = [w for w in verified if w not in DONE]
    if a.only:
        todo = [w.strip() for w in a.only.split(",") if w.strip()]

    uid = a.uid
    cs = None
    if not uid:
        from compshare.cs import CS  # noqa: PLC0415
        cs = CS()
        uid = cs.create(image_id=IMAGE_ID, gpu_type="4090", gpu=1, cpu=16, memory_gb=64,
                        name="p22-v4-sweep", charge_type="Postpay", disk_gb=100,
                        data_disk_gb=0, remark="v4 sweep")[0]
        print("created", uid, flush=True)
        inst = cs.wait_state(uid, {"running"}, timeout=900)
    else:
        from compshare.cs import CS  # noqa: PLC0415
        cs = CS()
        inst = cs.get(uid)

    host, pwd = cs.public_ip(inst), cs.password_of(inst)
    print("host", host, flush=True)

    # 等 ComfyUI 就绪
    for _ in range(40):
        if "UP" in ssh(host, pwd, "source /root/miniconda3/bin/activate; python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8188/object_info',timeout=5);print('UP')\" 2>/dev/null", timeout=60):
            break
        time.sleep(5)

    from tools.p20 import assets
    for key in todo:
        if key in LEGACY:
            continue
        try:
            job = None
            import glob
            for p in glob.glob(str(ROOT / "reports/p1/jobs/*/job.json")):
                d = json.load(open(p))
                r = (d.get("model") or {}).get("registry") or {}
                if r.get("workflow_key") == key and (job is None or d.get("created_at", "") > job.get("created_at", "")):
                    job = d
            if job:
                mk = job["model"]["registry"].get("model_key")
                m = reg.get_model(mk)
                links = assets.apply_links(m, lambda cmd, timeout=60: (0, ssh(host, pwd, cmd, timeout=timeout)))
                ok = sum(1 for x in links if x.get("ok"))
                if links:
                    print(f"[link] {key} {ok}/{len(links)}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[link] {key} err {e}", flush=True)

    results = []
    for i, key in enumerate(todo, 1):
        print(f"\n=== [{i}/{len(todo)}] {key}", flush=True)
        rec = run_one(host, pwd, key, a.timeout)
        print(json.dumps(rec, ensure_ascii=False)[:800], flush=True)
        results.append(rec)

    OUT.write_text(json.dumps({"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                               "uid": uid, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== SUMMARY ===")
    for r in results:
        print(f"{r['status']:6} {r['workflow']:38} {r.get('seconds','-')}s {r.get('error','')[:80]}")
    print("saved", OUT.relative_to(ROOT))

    if cs and not a.uid:
        try:
            cs.terminate(uid)
            print("terminated", uid)
        except Exception as e:  # noqa: BLE001
            print("terminate failed", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
