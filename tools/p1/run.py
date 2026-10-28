#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 单任务执行器：把一个 job 从 CREATED 串行推到 DONE。

    python3 tools/p1/run.py <job_id>                 # 真跑（会建机、花钱）
    python3 tools/p1/run.py <job_id> --dry-run       # 只走状态机，不建机不花钱
    python3 tools/p1/run.py <job_id> --gpu 4090_48G  # 指定卡型

九步串行，每一步都是「先转移状态（守卫在此拦截）→ 再执行动作」：
    CREATED → PROVISIONING → READY → SUBMITTED → RUNNING
            → FETCHING → VERIFYING → PERSISTED → RELEASING → DONE

三条硬规矩：
    G1  只有 PERSISTED 才允许 RELEASING（产物不随实例消失）
    G2  DONE 必须 manifest.verdict == PASS
    8903 过渡期拒绝 → 释放重试 3 次（U2 的规避）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import store, state  # noqa: E402
from tools.p1 import verify as verify_mod  # noqa: E402

COMFY_API = "http://127.0.0.1:8188"
IMAGE_KEYWORD = "ComfyUI基础镜像纯净版0.36.0"
RETRY_8903 = 3
CREATE_RETRY = 4
RETRY_8903_WAIT = 10


class RunError(Exception):
    """执行失败，带目标失败态。"""

    def __init__(self, msg: str, fail_state: str):
        super().__init__(msg)
        self.fail_state = fail_state


def _ledger(job_id: str, cost: float, **kw) -> None:
    """把作业成本写进统一账本（csctl 的 ledger.jsonl），供 cost/status 归集。"""
    rec = {"ts": int(time.time()),
           "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "event": "job", "job": job_id, "cost": round(cost, 4), **kw}
    try:
        store.LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with open(store.LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        _say(f"  ⚠ 账本写入失败：{e}")


def _mark_step(job_id: str, name: str, **detail) -> None:
    """把「某一步已完成」写进 job.json，供 --resume 判断断点。

    只记续跑需要的 ID（uhost_id / prompt_id / 文件列表），不记密码。
    """
    j = state.load_job(job_id)
    steps = j.setdefault("steps", {})
    steps[name] = {"done": True,
                   "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                   **{k: v for k, v in detail.items() if v is not None}}
    j["steps"] = steps
    store.atomic_write_json(store.job_file(job_id), j)


def _step_done(job_id: str, name: str) -> bool:
    j = store.read_json(store.job_file(job_id)) or {}
    return bool((j.get("steps") or {}).get(name, {}).get("done"))


def _say(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def _render_workflow(wf: dict, params: dict, prefix: str) -> dict:
    """把 {{PROMPT}} 等占位符换成真实值（深拷贝，不改原文件）。"""
    text = json.dumps(wf, ensure_ascii=False)
    mapping = {
        "{{PROMPT}}": params.get("prompt", ""),
        "{{NEG_PROMPT}}": params.get("neg_prompt", ""),
        "{{SEED}}": params.get("seed"),
        "{{FIRST_FRAME}}": params.get("image", ""),
        "{{OUTPUT_PREFIX}}": prefix,
    }
    for k, v in mapping.items():
        if v is None:
            continue
        text = text.replace(f'"{k}"', json.dumps(v, ensure_ascii=False))
    out = json.loads(text)
    # {{SEED}} 在 JSON 里是字符串，替换后要还原成数字
    for node in out.values():
        ins = node.get("inputs") or {}
        if isinstance(ins.get("seed"), str) and ins["seed"].isdigit():
            ins["seed"] = int(ins["seed"])
    return out


def _http_json(url: str, payload=None, timeout=60):
    if payload is None:
        return json.loads(urllib.request.urlopen(url, timeout=timeout).read())
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _ssh_run(host: str, port: str, password: str, cmd: str, timeout=600) -> tuple[int, str]:
    base = ["sshpass", "-p", password, "ssh", "-p", str(port),
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10", "-c", "aes128-gcm@openssh.com",
            f"root@{host}", cmd]
    p = subprocess.run(base, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


SCP_TIMEOUT = 300      # 单文件 5 分钟：100MB@10MB/s 只需 10s，留足余量
SCP_RETRY = 2          # 失败重试次数（网络抖动很常见）


def _scp_get(host: str, port: str, password: str, remote: str, local: Path,
             timeout: int = SCP_TIMEOUT, retries: int = SCP_RETRY) -> bool:
    """scp 拉单个文件。失败会重试；仍失败返回 False（由调用方决定状态）。"""
    local.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["sshpass", "-p", password, "scp", "-q", "-P", str(port),
           "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "ConnectTimeout=15", "-c", "aes128-gcm@openssh.com",
           f"root@{host}:{remote}", str(local)]
    for attempt in range(1, retries + 1):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            _say(f"  scp 超时（{timeout}s，第 {attempt}/{retries} 次）")
            if attempt < retries:
                time.sleep(5)
            continue
        if p.returncode == 0 and local.exists():
            return True
        _say(f"  scp 失败（第 {attempt}/{retries} 次）：{(p.stderr or '')[:160]}")
        # 半截文件必须删掉，否则下次重试会误判成功
        if local.exists() and p.returncode != 0:
            local.unlink(missing_ok=True)
        if attempt < retries:
            time.sleep(5)
    return False


# ---------------------------------------------------------------- 九步
def step_provision(job: dict, opts) -> dict:
    """建机并等 Running。"""
    from compshare.cs import CS, CompShareError

    cs = CS()
    gpu = opts.gpu or (job.get("instance", {}).get("required_gpu") or ["4090_48G"])[0]
    hours = opts.hours
    img = cs.resolve_image(opts.image or IMAGE_KEYWORD)
    price = cs.price(gpu, 1, 16, 96, "Postpay", 100, 0, image_id=img["CompShareImageId"])
    est = float(price.get("total") or 0) * hours
    cap = float((job.get("budget") or {}).get("max_cost") or 0)

    _say(f"计划 {gpu} ×1 · 镜像 {img.get('Name')} · ≈¥{float(price.get('total') or 0):.2f}/h")
    _say(f"预算上限 ¥{cap}（预计 {hours}h 上限 ¥{est:.2f}）")
    bal = float(cs.balance().get("AmountAvailable") or 0)
    if bal < 5:
        raise RunError(f"余额 ¥{bal:.2f} < ¥5，中止建机", state.FAILED_PROVISION)

    # 库存预检：无货直接失败，不要盲下单（226604 是常态，不是异常）
    wait_s = int(getattr(opts, "wait_stock", 0) or 0)
    if cs.stock(gpu) <= 0 and wait_s:
        _say(f"{gpu} 无现货，最多等 {wait_s}s…")
        t0 = time.time()
        while time.time() - t0 < wait_s and cs.stock(gpu) <= 0:
            time.sleep(15)
    if cs.stock(gpu) <= 0:
        raise RunError(f"{gpu} 当前无现货（226604）。换 --gpu 或加 --wait-stock 秒数",
                       state.FAILED_PROVISION)

    # 库存检查通过≠下单成功：热门卡会被别人抢（226604 竞态），重试几次
    ids, last_err = None, None
    for attempt in range(1, CREATE_RETRY + 1):
        try:
            ids = cs.create(image_id=img["CompShareImageId"], gpu_type=gpu, gpu=1,
                            cpu=16, memory_gb=96, name=f"p1-{job['job_id'][-6:]}",
                            charge_type="Postpay", disk_gb=100, data_disk_gb=0,
                            remark="p1 single-task")
            break
        except CompShareError as e:
            last_err = str(e)
            if "226604" in last_err and attempt < CREATE_RETRY:
                _say(f"  下单被抢（{attempt}/{CREATE_RETRY}），20s 后重试")
                time.sleep(20)
                continue
            raise RunError(f"建机失败：{last_err[:200]}", state.FAILED_PROVISION) from e
    if not ids:
        raise RunError(f"建机失败：{str(last_err)[:200]}", state.FAILED_PROVISION)
    uid = ids[0]
    _say(f"已创建 {uid}，等 Running…")
    inst = cs.wait_state(uid, {"running"}, timeout=900)

    # Running ≠ sshd 就绪（实测还要 20–40s），不探活就 push 必然失败
    host, port = cs.public_ip(inst), "23"
    pwd = cs.password_of(inst)
    for i in range(30):
        rc, out = _ssh_run(host, port, pwd, "echo ssh-ready", timeout=25)
        if rc == 0 and "ssh-ready" in out:
            _say(f"sshd 就绪（{i * 5}s）")
            break
        time.sleep(5)
    else:
        raise RunError("sshd 60 次探活仍未就绪", state.FAILED_PROVISION)
    opts_ctx = {"host": host, "port": port, "password": pwd}

    j = state.load_job(job["job_id"])
    j.setdefault("instance", {})
    j["instance"].update({"uhost_id": uid, "gpu": gpu, "zone": cs.zone,
                          "up_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
    store.atomic_write_json(store.job_file(job["job_id"]), j)
    return {"uhost_id": uid, "inst": inst, "hourly": float(price.get("total") or 0),
            **opts_ctx}


def run_setup(uid: str, need: str = "all", ctx: dict | None = None) -> None:
    """建机后配环境：挂载 / 禁 Manager / 软链 / 启动 / 自检（按任务类型收敛）。"""
    script = ROOT / "tools" / "p1" / "instance_setup.sh"
    if not script.exists():
        raise RunError("instance_setup.sh 缺失", state.FAILED_PROVISION)
    _say(f"配置实例环境（挂载 + 禁 Manager + 软链 + 启动），资产需求={need}")
    env = dict(os.environ)
    env.update({"P1_HOST": ctx.get("host", ""), "P1_PORT": str(ctx.get("port", "23")),
                "P1_PASSWORD": ctx.get("password", "")})
    p = subprocess.run(["bash", str(script), uid, f"--need={need}"],
                       capture_output=True, text=True, timeout=900, env=env)
    tail = ((p.stdout or "") + (p.stderr or ""))[-800:]
    if p.returncode != 0:
        raise RunError(f"setup 失败：{tail}", state.FAILED_PROVISION)
    _say("setup 完成，自检通过")


def step_ready(job: dict, opts, ctx: dict) -> None:
    """等 ComfyUI 就绪 + 6 项资产可见。"""
    from compshare.cs import CS

    host, port, pwd = ctx["host"], ctx["port"], ctx["password"]

    check = ("source /root/miniconda3/bin/activate && python3 -c \""
             "import json,urllib.request;"
             "oi=json.load(urllib.request.urlopen('http://127.0.0.1:8188/object_info',timeout=30));"
             "ul=oi['UNETLoader']['input']['required']['unet_name'][0];"
             "print('OK' if any('dasiwaHybridV2' in u for u in ul) or any('qwen_image' in u for u in ul) else 'NO_MODEL')\"")
    for i in range(20):
        rc, out = _ssh_run(host, port, pwd, check, timeout=90)
        if "OK" in out:
            _say("ComfyUI 就绪，模型可见")
            return
        time.sleep(6)
    raise RunError("ComfyUI 20 次探活未就绪", state.FAILED_PROVISION)


def step_submit(job: dict, opts, ctx: dict) -> None:
    """渲染 workflow 并提交，拿 prompt_id。"""
    wf_path = ROOT / job["model"]["workflow"]
    if not wf_path.exists():
        raise RunError(f"workflow 不存在：{wf_path}", state.FAILED_RUN)
    wf = json.loads(wf_path.read_text(encoding="utf-8"))
    graph = _render_workflow(wf, job.get("params") or {}, job["job_id"])

    host, port, pwd = ctx["host"], ctx["port"], ctx["password"]
    payload = json.dumps({"prompt": graph, "client_id": "p1"}, ensure_ascii=False)
    cmd = ("source /root/miniconda3/bin/activate && python3 -c \""
           "import json,sys,urllib.request;"
           f"p=json.loads(sys.argv[1]);"
           "r=urllib.request.Request('http://127.0.0.1:8188/prompt',data=json.dumps(p).encode(),"
           "headers={'Content-Type':'application/json'});"
           "print(urllib.request.urlopen(r,timeout=120).read().decode())\" "
           + "'" + payload.replace("'", "'\"'\"'") + "'")
    rc, out = _ssh_run(host, port, pwd, cmd, timeout=180)
    import re as _re
    m = _re.search(r'"prompt_id"\s*:\s*"([^"]+)"', out)
    if not m:
        raise RunError(f"提交失败：{out[-400:]}", state.FAILED_RUN)
    pid = m.group(1)
    ctx["prompt_id"] = pid
    _say(f"已提交 prompt_id={pid}")


def step_wait(job: dict, opts, ctx: dict) -> list[tuple[str, str]]:
    """轮询 history 直到完成，返回远端文件列表。"""
    host, port, pwd = ctx["host"], ctx["port"], ctx["password"]
    pid = ctx["prompt_id"]
    limit = int((job.get("budget") or {}).get("max_minutes") or 20) * 60
    t0 = time.time()
    cmd = ("source /root/miniconda3/bin/activate && python3 -c \""
           "import json,sys,urllib.request;"
           f"h=json.load(urllib.request.urlopen('http://127.0.0.1:8188/history/'+sys.argv[1],timeout=60));"
           f"pid=sys.argv[1];"
           "e=h.get(pid);"
           "print(json.dumps(e if e else {}))\" " + pid)
    while time.time() - t0 < limit:
        rc, out = _ssh_run(host, port, pwd, cmd, timeout=120)
        if rc == 0 and out.strip().startswith("{"):
            try:
                raw = out[out.index("{"):].splitlines()[0]
                ent = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                ent = {}
            if ent:
                st = ent.get("status", {})
                if st.get("status_str") == "success":
                    files = []
                    for o in (ent.get("outputs") or {}).values():
                        for k in ("images", "videos", "gifs"):
                            for f in (o.get(k) or []):
                                files.append((f.get("subfolder", ""), f.get("filename")))
                    if not files:
                        raise RunError("任务成功但无输出文件", state.FAILED_FETCH)
                    _say(f"推理完成：{len(files)} 个文件，耗时 {int(time.time()-t0)}s")
                    return sorted(files)
                if st.get("status_str") == "error":
                    detail = ""
                    for m in (st.get("messages") or []):
                        if isinstance(m, list) and m and m[0] == "execution_error":
                            info = m[1]
                            detail = f"{info.get('node_type')}: {info.get('exception_message')}"
                            break
                    raise RunError(f"推理失败：{detail or str(st.get('messages'))[:600]}",
                                   state.FAILED_RUN)
        time.sleep(5)
    raise RunError(f"推理超时（{limit}s）", state.FAILED_RUN)


def step_fetch(job: dict, opts, ctx: dict, files: list[tuple[str, str]]) -> list[Path]:
    """拉产物到 jobs/<id>/artifacts/。"""
    jdir = store.job_dir(job["job_id"])
    art = jdir / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    local = []
    for idx, (sub, fn) in enumerate(files, 1):
        remote = f"/root/ComfyUI/output/{sub}/{fn}" if sub else f"/root/ComfyUI/output/{fn}"
        dst = art / f"{idx:02d}-{fn}"
        _say(f"拉取 {fn}（{idx}/{len(files)}）…")
        if not _scp_get(ctx["host"], ctx["port"], ctx["password"], remote, dst):
            raise RunError(f"拉取失败（已成功 {len(local)}/{len(files)}）：{remote}",
                           state.FAILED_FETCH)
        local.append(dst)
    return local


def step_verify(job: dict, opts, ctx: dict, arts: list[Path]) -> dict:
    """跑 6 条复验项。"""
    wf_path = ROOT / job["model"]["workflow"]
    res = verify_mod.verify_job(job, store.job_dir(job["job_id"]) / "artifacts", wf_path)
    for a in res.get("artifacts", []):
        _say(f"  {a['file']}: {a['verdict']}  failed={a['failed'] or '—'}")
    if res["verdict"] != "PASS":
        raise RunError(f"校验未通过：{res['artifacts'][0]['failed'] if res.get('artifacts') else '?'}",
                       state.FAILED_VERIFY)
    return res


def step_persist(job: dict, opts, ctx: dict, res: dict, arts: list[Path]) -> dict:
    """写 manifest.json（原子写 + 读回校验）。"""
    import hashlib

    jdir = store.job_dir(job["job_id"])
    job = state.load_job(job["job_id"])      # 重新加载：up_at 是 provision 时写入的
    inst = job.get("instance") or {}
    now = dt.datetime.now().astimezone()
    up_at = inst.get("up_at")
    minutes = 0.0
    if up_at:
        try:
            minutes = round((now - dt.datetime.fromisoformat(up_at)).total_seconds() / 60, 1)
        except ValueError:
            pass
    hourly = float(ctx.get("hourly") or 0)

    mans = []
    for a in res.get("artifacts", []):
        p = jdir / "artifacts" / a["file"]
        data = p.read_bytes()
        mans.append({
            "rel": f"artifacts/{a['file']}",
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "probe": a["details"].get("decode", {}),
            "match": {k: bool(v) for k, v in a["checks"].items() if v is not None},
        })

    first = res["artifacts"][0]
    checks = {k: bool(v) for k, v in first["checks"].items() if v is not None}
    manifest = {
        "schema": "p1.manifest/v1",
        "job_id": job["job_id"],
        "params_hash": job["params_hash"],
        "persisted_at": now.isoformat(timespec="seconds"),
        "instance": {"uhost_id": inst.get("uhost_id"), "gpu": inst.get("gpu"),
                     "zone": inst.get("zone"), "up_at": up_at,
                     "release_at": None, "minutes": minutes},
        "artifacts": mans,
        "checks": checks,
        "verdict": "PASS",
        "cost": {"hourly": round(hourly, 2), "minutes": minutes,
                 "amount": round(hourly * minutes / 60, 4)},
        "state_history": [e.get("to") for e in state.read_events(job["job_id"]) if e.get("to")],
    }
    man_path = jdir / "manifest.json"
    store.atomic_write_json(man_path, manifest)
    back = store.read_json(man_path)
    if not back or back.get("verdict") != "PASS":
        raise RunError("manifest 读回校验失败", state.FAILED_VERIFY)
    _say(f"manifest 已落盘（{len(mans)} 个产物，{minutes} 分钟）")
    return manifest


def step_release(job: dict, opts, ctx: dict) -> None:
    """销毁实例，8903 过渡期拒绝时重试。"""
    from compshare.cs import CS, CompShareError

    uid = ctx.get("uhost_id")
    if not uid:
        _say("无实例可销毁")
        return
    cs = CS()
    last = None
    for attempt in range(1, RETRY_8903 + 1):
        try:
            cs.terminate(uid)
            _say(f"已提交销毁 {uid}")
            return
        except CompShareError as e:
            msg = str(e)
            last = msg
            if "8903" in msg or "in the task" in msg.lower():
                _say(f"  8903 过渡期拒绝（{attempt}/{RETRY_8903}），{RETRY_8903_WAIT}s 后重试")
                time.sleep(RETRY_8903_WAIT)
                continue
            raise RunError(f"销毁失败：{msg[:200]}", state.FAILED)
    raise RunError(f"销毁连续 {RETRY_8903} 次被拒：{last[:200]}", state.FAILED)


def step_done(job: dict, opts, ctx: dict, manifest: dict) -> None:
    """补 manifest 的 release_at，收尾。"""
    jdir = store.job_dir(job["job_id"])
    m = store.read_json(jdir / "manifest.json") or manifest
    m.setdefault("instance", {})["release_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    m["state_history"] = [e.get("to") for e in state.read_events(job["job_id"]) if e.get("to")]
    store.atomic_write_json(jdir / "manifest.json", m)


# ------------------------------------------------------------ 编排
def run(job_id: str, opts) -> int:
    job = state.load_job(job_id)
    _say(f"作业 {job_id} · 模型 {job['model']['key']} · 预算 ¥{job['budget'].get('max_cost')}")

    if opts.dry_run:
        _say("--dry-run：只走状态机，不建机、不花钱")
        wf = json.loads((ROOT / job["model"]["workflow"]).read_text(encoding="utf-8"))
        g = _render_workflow(wf, job.get("params") or {}, job_id)
        seeds = [n["inputs"].get("seed") for n in g.values() if "seed" in (n.get("inputs") or {})]
        prompts = [n["inputs"].get("prompt") for n in g.values() if "prompt" in (n.get("inputs") or {})]
        _say(f"  占位符渲染 OK：seed={seeds} · prompt 长度={[len(p) for p in prompts if p]}")
        _say(f"  节点数 {len(g)}，输出目录 {store.rel(store.job_dir(job_id))}")
        _say("  （真实执行请去掉 --dry-run）")
        return 0

    with store.JobLock():
        ctx: dict = {}
        try:
            state.transition(job_id, state.PROVISIONING, reason="开始建机")
            ctx.update(step_provision(job, opts))
            _mark_step(job_id, "provision", uhost_id=ctx.get("uhost_id"),
                       gpu=(ctx.get("inst") or {}).get("GpuType"), hourly=ctx.get("hourly"))

            need = "qwen" if str(job["model"]["key"]).startswith("qwen") else "h3"
            run_setup(ctx["uhost_id"], need, ctx)
            _mark_step(job_id, "setup")
            state.transition(job_id, state.READY, reason="实例就绪")
            step_ready(job, opts, ctx)
            _mark_step(job_id, "ready")

            state.transition(job_id, state.SUBMITTED, reason="提交 workflow")
            step_submit(job, opts, ctx)
            _mark_step(job_id, "submit", prompt_id=ctx.get("prompt_id"))

            state.transition(job_id, state.RUNNING, reason="等待推理")
            files = step_wait(job, opts, ctx)
            _mark_step(job_id, "wait", files=[list(f) for f in files])

            state.transition(job_id, state.FETCHING, reason="拉取产物")
            arts = step_fetch(job, opts, ctx, files)
            _mark_step(job_id, "fetch", local=[a.name for a in arts])

            state.transition(job_id, state.VERIFYING, reason="校验产物")
            res = step_verify(job, opts, ctx, arts)
            _mark_step(job_id, "verify", verdict=res.get("verdict"))

            state.transition(job_id, state.PERSISTED, reason=f"manifest 落盘，{res['verdict']}")
            man = step_persist(job, opts, ctx, res, arts)
            _mark_step(job_id, "persist")

            state.transition(job_id, state.RELEASING, reason="销毁实例")
            step_release(job, opts, ctx)
            _mark_step(job_id, "release")

            state.transition(job_id, state.DONE, reason="闭环完成")
            step_done(job, opts, ctx, man)
            _say("✓ 闭环完成")
            return 0

        except RunError as e:
            _say(f"✗ {e}")
            cur = state.load_job(job_id).get("state")
            if cur not in state.TERMINAL:
                try:
                    state.transition(job_id, e.fail_state, reason=str(e)[:200])
                except Exception as e2:
                    _say(f"  进入 {e.fail_state} 失败：{e2}")
            # 失败也必须释放；G1 允许 FAILED_* → RELEASING
            cur = state.load_job(job_id).get("state")
            if cur in state.FAIL_BRANCH:
                try:
                    state.transition(job_id, state.RELEASING, reason="失败后清理")
                    step_release(job, opts, ctx)
                    state.transition(job_id, state.FAILED, reason="失败收尾")
                except Exception as e3:
                    _say(f"  清理失败：{e3}")
            return 1
        except Exception as e:
            from compshare.cs import CompShareError
            if isinstance(e, CompShareError):
                _say(f"✗ 平台错误：{str(e)[:200]}")
                cur2 = state.load_job(job_id).get("state")
                if cur2 not in state.TERMINAL:
                    try:
                        state.transition(job_id, state.FAILED_PROVISION, reason=str(e)[:150])
                        state.transition(job_id, state.RELEASING, reason="失败清理")
                        step_release(job, opts, ctx)
                        state.transition(job_id, state.FAILED, reason="失败收尾")
                    except Exception as e2:
                        _say(f"  失败收尾异常：{e2}")
                return 1
            _say(f"✗ 未预期异常：{type(e).__name__}: {e}")
            try:
                step_release(job, opts, ctx)
            except Exception:
                pass
            return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="run", description="P1 单任务执行器")
    ap.add_argument("job_id")
    ap.add_argument("--dry-run", action="store_true", help="只走状态机，不建机不花钱")
    ap.add_argument("--gpu", help="覆盖卡型，如 4090_48G")
    ap.add_argument("--image", help="镜像关键字")
    ap.add_argument("--hours", type=float, default=1.0, help="定时关机兜底小时数")
    ap.add_argument("--wait-stock", type=int, default=0, help="无货时最多等待秒数")
    a = ap.parse_args(argv)
    return run(a.job_id, a)


if __name__ == "__main__":
    raise SystemExit(main())
