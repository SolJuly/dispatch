#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实验态工作流批量试跑：只用 /model 现有权重（不下载、不入镜像）。

流程：
  1. 本地渲染 33 条实验态工作流（默认参数 + 通用占位输入）；
  2. 把「渲染图里引用的模型文件」与实例 object_info 对照，缺的按 basename 到 /model 找，
     在对应模型目录建软链（只建链接，不占存储）；
  3. 重启 ComfyUI，逐条提交，记录 PASS/FAIL/缺文件；
  4. 结果写 reports/p2/p21/evidence/exp-trial.json。

用法：
    python3 tools/p22/trial.py --create            # 起一台 4090，跑完销毁
    python3 tools/p22/trial.py --uid uhost-x --only a,b
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
OUT = ROOT / "reports/p2/p21/evidence/exp-trial.json"

VERIFIED_DONE = {"zimage_turbo_t2i", "qwen_image_edit_2509", "seedvr2_3b_upscale",
                 "pack_video_wan2_2_14b_i2v"}
SUPERSEDED = {"pack_image_z_image_turbo", "pack_image_z_image_turbo_fun_union_controlnet",
              "pack_image_flux2_klein_text_to_image", "pack_image_qwen_image_edit_2509",
              "pack_utility_seedvr2_3b_int8_upscale_image",
              "pack_utility_seedvr2_3b_int8_upscale_video"}

FILL = {"prompt": "a cinematic photo of a young woman by the sea at sunset, 85mm, detailed skin",
        "negative_prompt": "", "neg_prompt": "", "seed": 42,
        "image": "in-a6891d7b.png", "image_2": "in-f9bd4904.jpg",
        "image_3": "in-5935a044.png", "image_4": "in-ff45f4b6.png",
        "audio": "s2v-audio.wav", "video": "in-902baae2.mp4",
        "first_frame": "in-a6891d7b.png", "last_frame": "in-f9bd4904.jpg"}

LOADER_DIR = {
    "UNETLoader": "diffusion_models", "CLIPLoader": "text_encoders",
    "DualCLIPLoader": "text_encoders", "TripleCLIPLoader": "text_encoders",
    "QuadrupleCLIPLoader": "text_encoders", "VAELoader": "vae",
    "LoraLoaderModelOnly": "loras", "LoraLoader": "loras",
    "CheckpointLoaderSimple": "checkpoints", "UpscaleModelLoader": "upscale_models",
    "ControlNetLoader": "controlnet", "CLIPVisionLoader": "clip_vision",
    "SeedVR2LoadDiTModel": "SEEDVR2", "SeedVR2LoadVAEModel": "SEEDVR2",
    "AudioEncoderLoader": "audio_encoders",
}


def sh(cmd: str, timeout: int = 120):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def ssh(host: str, pwd: str, script: str, timeout: int = 300) -> str:
    b64 = base64.b64encode(script.encode()).decode()
    inner = f"echo {b64} | base64 -d | bash"
    cmd = (f"sshpass -p {pwd!r} ssh -p 23 -o StrictHostKeyChecking=no "
           f"-o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 "
           f"-c aes128-gcm@openssh.com root@{host} " + repr(inner))
    _rc, out = sh(cmd, timeout=timeout)
    return out


def upload(host: str, pwd: str, local: Path, remote: str) -> bool:
    cmd = (f"sshpass -p {pwd!r} scp -q -P 23 -o StrictHostKeyChecking=no "
           f"-o UserKnownHostsFile=/dev/null {str(local)!r} root@{host}:/root/ComfyUI/input/{remote}")
    rc, _ = sh(cmd, timeout=300)
    return rc == 0


def build_graphs(keys: list[str]) -> dict[str, dict]:
    from tools.p20 import registry as reg
    from tools.p20 import render as p20render
    out = {}
    for k in keys:
        wf = reg.get_workflow(k)
        mk = wf["model"]["key"]
        params = {n: FILL[n] for n, s in (wf.get("params") or {}).items()
                  if s.get("default") is None and n in FILL}
        res = reg.resolve(k, model_key=mk, params=params, allow_experimental=True)
        g, _ = p20render.render(wf, reg.get_model(mk), res["params"], "trial",
                                graph=wf.get("graph_obj"))
        out[k] = g
    return out


def model_refs(graph: dict) -> list[tuple[str, str, str]]:
    """[(loader, input, value)]"""
    refs = []
    for n in graph.values():
        ct = n.get("class_type") or ""
        for k, v in (n.get("inputs") or {}).items():
            if isinstance(v, str) and v.endswith((".safetensors", ".ckpt", ".gguf")):
                refs.append((ct, k, v))
    return refs



def fetch_object_info(host: str, pwd: str) -> dict:
    out = ssh(host, pwd, "source /root/miniconda3/bin/activate; python3 -c \"import json,urllib.request;print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8188/object_info',timeout=60))))\"", timeout=180)
    line = [l for l in out.splitlines() if l.startswith("{") and '"object_info"' not in l]
    line = line[-1] if line else out.strip()
    return json.loads(line)


def repair(graph: dict, oi: dict) -> list[str]:
    """把节点缺失的必填输入补上默认值（修复 UI 导出丢 widget 的问题）。"""
    added = []
    for nid, node in graph.items():
        ct = node.get("class_type")
        info = (oi.get(ct) or {}).get("input") or {}
        req = info.get("required") or {}
        for key, spec in req.items():
            if key in (node.get("inputs") or {}):
                continue
            if not isinstance(spec, list) or not spec:
                continue
            typ = spec[0]
            opts = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if "default" in opts:
                val = opts["default"]
            elif typ == "COMBO":
                opts_list = opts.get("options") or []
                if not opts_list:
                    continue
                val = opts_list[0]
            elif isinstance(typ, list):
                val = typ[0] if typ else ""
            elif typ == "INT":
                val = 1
            elif typ == "FLOAT":
                val = 1.0
            elif typ == "BOOLEAN":
                val = False
            elif typ == "STRING":
                val = ""
            else:
                continue
            node.setdefault("inputs", {})[key] = val
            added.append(f"{ct}.{key}={val!r}")
    return added

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uid", default="")
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--submit-only", action="store_true", help="只提交并读首个响应，不等待完成")
    a = ap.parse_args()

    import glob
    allk = [json.load(open(f))["key"] for f in glob.glob(str(ROOT / "registry/workflows/*.json"))]
    verified = {e["workflow"] for e in json.load(open(ROOT / "registry/compat.json"))["entries"]
                if e["status"] == "verified"}
    keys = [k for k in allk if k not in verified and k not in SUPERSEDED]
    if a.only:
        keys = [k.strip() for k in a.only.split(",") if k.strip()]

    graphs = build_graphs(keys)
    print(f"rendered {len(graphs)} workflows", flush=True)

    from compshare.cs import CS
    cs = CS()
    uid = a.uid
    if not uid:
        uid = cs.create(image_id=IMAGE_ID, gpu_type="4090", gpu=1, cpu=16, memory_gb=64,
                        name="p22-exp-trial", charge_type="Postpay", disk_gb=100,
                        data_disk_gb=0, remark="exp trial")[0]
        print("created", uid, flush=True)
        inst = cs.wait_state(uid, {"running"}, timeout=900)
    else:
        inst = cs.get(uid)
    host, pwd = cs.public_ip(inst), cs.password_of(inst)
    print("host", host, flush=True)
    time.sleep(20)
    for _ in range(40):
        if "UP" in ssh(host, pwd, "source /root/miniconda3/bin/activate; python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8188/object_info',timeout=5);print('UP')\" 2>/dev/null", timeout=60):
            break
        time.sleep(5)

    # 0) 用实例的 object_info 修图（补必填默认值）
    try:
        oi = fetch_object_info(host, pwd)
        print("object_info classes", len(oi), flush=True)
        for k in list(graphs):
            added = repair(graphs[k], oi)
            if added:
                print(f"[repair] {k} +{len(added)}: {', '.join(added[:6])}", flush=True)
    except Exception as e:  # noqa: BLE001
        print("repair skipped:", e, flush=True)

    for g in graphs.values():
        for node in g.values():
            if node.get("class_type") == "AudioEncoderLoader":
                node.setdefault("inputs", {}).setdefault(
                    "audio_encoder_name", "wav2vec2_large_english_fp16.safetensors")

    # 1) 收集所有模型引用，按目录去重
    refs = {}
    for k, g in graphs.items():
        for loader, inp, val in model_refs(g):
            d = LOADER_DIR.get(loader)
            if not d:
                continue
            refs.setdefault((d, val), set()).add(k)
    print("unique model refs", len(refs), flush=True)

    # 2) 在实例上建链接：已存在则跳过；缺的按 basename 去 /model 找
    manifest = json.dumps([{"dir": d, "name": n} for (d, n) in refs], ensure_ascii=False)
    script = f'''
cat > /root/refs.json <<'JSON'
{manifest}
JSON
python3 - <<'PY'
import json, os, subprocess
refs = json.load(open('/root/refs.json'))
base = '/root/ComfyUI/models'
linked, present, missing = [], [], []
for r in refs:
    p = os.path.join(base, r['dir'], r['name'])
    if os.path.lexists(p):
        present.append(r['name']); continue
    if os.path.exists(p):
        present.append(r['name']); continue
    os.makedirs(os.path.dirname(p), exist_ok=True)
    bn = os.path.basename(r['name'])
    try:
        out = subprocess.run(['find','/model','-maxdepth','8','-name',bn],
                             capture_output=True, text=True, timeout=120).stdout.splitlines()
    except Exception:
        out = []
    src = out[0] if out else ''
    if src and os.path.exists(src):
        try:
            os.symlink(src, p); linked.append((r['dir'], r['name'], src))
        except FileExistsError:
            present.append(r['name'])
    else:
        missing.append(r['name'])
print('LINKED', len(linked)); print('PRESENT', len(present)); print('MISSING', json.dumps(missing, ensure_ascii=False))
for x in linked: print('L', x[0], x[1], '->', x[2])
PY
'''
    out = ssh(host, pwd, script, timeout=900)
    print(out[-2000:], flush=True)
    missing = []
    for line in out.splitlines():
        if line.startswith("MISSING "):
            try:
                missing = json.loads(line[8:])
            except Exception:
                missing = []

    # 3) 重启 ComfyUI 让文件列表刷新
    ssh(host, pwd, "pkill -f '[m]ain.py --listen'; sleep 2; source /root/miniconda3/bin/activate; cd /root/ComfyUI; "
                   "COMFYUI_MANAGER_OFFLINE=1 nohup python -u main.py --listen 0.0.0.0 --port 8188 > /root/comfy_trial.log 2>&1 & echo OK", timeout=90)
    for _ in range(40):
        if "UP" in ssh(host, pwd, "source /root/miniconda3/bin/activate; python3 -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8188/object_info',timeout=5);print('UP')\" 2>/dev/null", timeout=60):
            break
        time.sleep(5)

    results = []
    for i, k in enumerate(keys, 1):
        g = graphs[k]
        rec = {"workflow": k, "nodes": len(g), "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        print(f"\n=== [{i}/{len(keys)}] {k}", flush=True)
        ins = sorted({v for n in g.values() for v in (n.get("inputs") or {}).values()
                      if isinstance(v, str) and v.lower().endswith((".png", ".jpg", ".jpeg", ".mp4", ".wav"))})
        bad_inputs = [x for x in ins if not (INPUTS / x).exists()]
        for x in ins:
            if (INPUTS / x).exists():
                upload(host, pwd, INPUTS / x, x)
        if bad_inputs:
            rec.update({"status": "INPUT_MISSING", "error": ",".join(bad_inputs)})
            results.append(rec); print(rec, flush=True); continue

        payload = json.dumps({"prompt": g, "client_id": "p22-trial"}).encode().decode()
        remote = f'''
source /root/miniconda3/bin/activate
cat > /root/g.json <<'JSON'
{payload}
JSON
python3 - <<'PY'
import json, time, urllib.request, urllib.error
p = json.load(open('/root/g.json'))
req = urllib.request.Request('http://127.0.0.1:8188/prompt', data=json.dumps(p).encode(),
                             headers={{'Content-Type': 'application/json'}})
t0 = time.time()
try:
    r = json.load(urllib.request.urlopen(req, timeout=30))
except urllib.error.HTTPError as e:
    print('SUBMIT_FAIL', e.code, e.read().decode()[:1500]); raise SystemExit(1)
pid = r['prompt_id']; print('PROMPT', pid, flush=True)
if {str(a.submit_only)}:
    print('SUBMITTED'); raise SystemExit(0)
while time.time() - t0 < {a.timeout}:
    try:
        h = json.load(urllib.request.urlopen('http://127.0.0.1:8188/history/' + pid, timeout=10))
    except Exception:
        h = {{}}
    if pid in h:
        e = h[pid]; st = e.get('status', {{}})
        print('STATUS', st.get('status_str'), st.get('completed'))
        for m in st.get('messages', []):
            if m[0] == 'execution_error':
                d = m[1]; print('ERR', d.get('node_type'), '|', str(d.get('exception_message'))[:400])
        print('OUTPUTS', json.dumps(e.get('outputs', {{}}), ensure_ascii=False)[:600])
        break
    time.sleep(5)
else:
    print('TIMEOUT')
PY
'''
        t0 = time.time()
        try:
            out = ssh(host, pwd, remote, timeout=a.timeout + 240)
        except Exception as e:  # noqa: BLE001
            out = f"SSH_ERR {e}"
        rec["seconds"] = round(time.time() - t0, 1)
        if a.submit_only:
            rec["status"] = "SUBMITTED" if "SUBMITTED" in out else "FAIL"
            if rec["status"] == "FAIL":
                rec["error"] = out[-500:]
                rec["detail"] = out[-3000:]
            results.append(rec)
            print(rec["status"], out[-300:].replace("\n", " ")[:200], flush=True)
            continue
        ok = "STATUS success True" in out
        rec["status"] = "PASS" if ok else "FAIL"
        if not ok:
            rec["error"] = out[-500:]
            rec["detail"] = out[-4000:]
        results.append(rec)
        print(rec["status"], rec.get("seconds"), rec.get("error", "")[:160], flush=True)

    OUT.write_text(json.dumps({"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                               "uid": uid, "missing_files": missing, "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== SUMMARY ===")
    for r in results:
        print(f"{r['status']:6} {r['workflow']:52} {r.get('seconds','-')}s {r.get('error','')[:70]}")
    print("saved", OUT.relative_to(ROOT))
    if not a.uid:
        cs.terminate(uid); print("terminated", uid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
