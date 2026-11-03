# -*- coding: utf-8 -*-
"""图片控制台数据层：作业的列出/详情/创建/取消。

不放 HTTP 逻辑，方便单测直接调用；`fileserver.py` 只做路由与 JSON 序列化。
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import zipfile
import re
import subprocess
import sys
import time
from pathlib import Path

from tools.p1 import state as jobstate
from tools.p1 import store
from tools.p20 import config_schema as cs
from tools.p20 import p20ctl as p20cli
from tools.p20 import registry

ROOT = Path(__file__).resolve().parent.parent
PUB_DIR = Path(os.environ.get("P21_PUB_DIR", "/srv/dsh-pub/jobs"))
PUBLISHER = ROOT / "scripts/publish_artifacts.py"
JOBS_DIR = ROOT / "reports/p1/jobs"
SPEC_DIR = ROOT / "reports/p2/p21/specs"
RUN_PY = ROOT / "tools/p1/run.py"
JOBCTL = ROOT / "tools/p1/jobctl.py"
JOB_ID_RE = re.compile(r"(p1-\d{8}T\d{4}-[0-9a-f]{6})")
ART_SUFFIX = (".png", ".jpg", ".jpeg", ".webp", ".mp4")
INPUTS_DIR = ROOT / "reports/p2/p21/inputs"
SHARE_INPUTS = Path(os.environ.get("P21_SHARE_INPUTS", ROOT / "share" / "inputs"))
MAX_UPLOAD = 20 * 1048576


class ApiError(Exception):
    """带 HTTP 语义的业务错误。"""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.msg = msg
        self.code = code


def _jobs_dir(jobs_dir: Path | None) -> Path:
    return Path(jobs_dir) if jobs_dir else JOBS_DIR


def _job_path(job_id: str, jobs_dir: Path | None) -> Path:
    if not re.fullmatch(r"p1-\d{8}T\d{4}-[0-9a-f]{6}", job_id or ""):
        raise ApiError(f"job_id 非法：{job_id!r}", 400)
    p = _jobs_dir(jobs_dir) / job_id / "job.json"
    if not p.exists():
        raise ApiError(f"作业不存在：{job_id}", 404)
    return p


def _load_job(job_id: str, jobs_dir: Path | None) -> dict:
    return json.loads(_job_path(job_id, jobs_dir).read_text(encoding="utf-8"))


def _workflow_key(job: dict) -> str:
    reg = ((job.get("model") or {}).get("registry") or {})
    return reg.get("workflow_key") or ""


def _artifacts(job_id: str, jobs_dir: Path | None) -> list[dict]:
    """作业产物列表。

    优先作业目录里的原件；只读部署（台账迁过来了、产物没迁）时回退到发布副本
    `/srv/dsh-pub/jobs/<job_id>`，这样缩略图/下载仍然可用。
    """
    local = _jobs_dir(jobs_dir) / job_id / "artifacts"
    pub = PUB_DIR / job_id
    src = local if local.is_dir() else pub
    if not src.is_dir():
        return []
    out = []
    for p in sorted(src.iterdir()):
        if p.is_file() and p.suffix.lower() in ART_SUFFIX:
            out.append({"name": p.name, "bytes": p.stat().st_size,
                        "url": f"/pub/jobs/{job_id}/{p.name}",
                        "published": (pub / p.name).exists()})
    return out


def _first_artifact_url(job_id: str, jobs_dir: Path | None) -> str:
    arts = _artifacts(job_id, jobs_dir)
    return arts[0]["url"] if arts else ""


def save_upload(filename: str, data: bytes) -> dict:
    """上传输入图：校验格式/大小 → 内容寻址命名 → 两份副本（作业用 + 静态可看）。"""
    if not data:
        raise ApiError("空文件", 400)
    if len(data) > MAX_UPLOAD:
        raise ApiError(f"文件太大（上限 {MAX_UPLOAD // 1048576} MB）", 413)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    else:
        raise ApiError("只支持 PNG / JPEG / WebP", 415)
    import hashlib
    h = hashlib.sha256(data).hexdigest()[:8]
    name = f"in-{h}{ext}"
    for d in (INPUTS_DIR, SHARE_INPUTS):
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        (d / name).chmod(0o644)
    return {"name": name, "bytes": len(data), "url": f"/f/inputs/{name}"}


def list_jobs(limit: int = 20, *, state_prefix: str = "", workflow: str = "",
              q: str = "", jobs_dir: Path | None = None) -> list[dict]:
    root = _jobs_dir(jobs_dir)
    if not root.is_dir():
        return []
    rows = []
    for d in root.iterdir():
        f = d / "job.json"
        if not f.exists():
            continue
        try:
            job = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        st = job.get("state") or ""
        if state_prefix:
            if state_prefix == "FAILED":
                if not st.startswith("FAILED"):
                    continue
            elif st != state_prefix:
                continue
        wk = _workflow_key(job)
        if workflow and wk != workflow:
            continue
        if q:
            hay = f"{job.get('job_id','')} {json.dumps(job.get('params') or {}, ensure_ascii=False)}"
            if q.lower() not in hay.lower():
                continue
        rows.append({
            "job_id": job.get("job_id"),
            "state": st,
            "workflow": wk,
            "workflow_version": ((job.get("model") or {}).get("registry") or {}).get("workflow_version"),
            "model": (job.get("model") or {}).get("key"),
            "created_at": job.get("created_at"),
            "kind": job.get("kind"),
            "prompt": (job.get("params") or {}).get("prompt", ""),
            "artifacts": len(_artifacts(job.get("job_id"), jobs_dir)),
            "first_artifact_url": _first_artifact_url(job.get("job_id"), jobs_dir),
        })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[:max(1, min(int(limit or 20), 200))]


def maybe_publish(job_id: str, job: dict) -> None:
    """作业终态后把产物复制到 nginx 静态目录（幂等；页面轮询会触发）。"""
    if job.get("state") not in ("DONE", "FAILED_VERIFY"):
        return
    marker = PUB_DIR / job_id / "job-summary.json"
    if marker.exists():
        return
    try:
        subprocess.Popen([sys.executable, str(PUBLISHER), "--job", job_id],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, cwd=str(ROOT))
    except OSError:
        pass


def get_job(job_id: str, jobs_dir: Path | None = None) -> dict:
    job = _load_job(job_id, jobs_dir)
    maybe_publish(job_id, job)
    return {
        "job": job,
        "artifacts": _artifacts(job_id, jobs_dir),
        "workflow": _workflow_key(job),
        "terminal": job.get("state") in jobstate.TERMINAL,
    }


def create_job(workflow: str, params: dict, *, run: bool = False,
               jobs_dir: Path | None = None, spec_dir: Path | None = None,
               jobctl: Path | None = None, spawn=subprocess.Popen) -> dict:
    """校验参数 → 生成 spec → jobctl new →（可选）后台执行。"""
    if not isinstance(params, dict) or not params:
        raise ApiError("params 必须是非空对象", 400)
    if not isinstance(workflow, str) or not workflow:
        raise ApiError("workflow 必填", 400)

    try:
        res = registry.resolve(workflow, params=params)
    except (registry.RegistryError, cs.ConfigError) as e:
        msg = str(e)
        if "experimental" in msg:
            raise ApiError("该工作流×模型组合是 experimental，默认拒绝执行", 409) from e
        raise ApiError(f"参数/工作流校验失败：{msg}", 400) from e
    if (res.get("compat") or {}).get("status") == "experimental":
        raise ApiError("该工作流×模型组合是 experimental，默认拒绝执行", 409)

    spec = p20cli._make_spec(res, job_key=workflow, task_ref="P2.1")
    sd = Path(spec_dir) if spec_dir else SPEC_DIR
    sd.mkdir(parents=True, exist_ok=True)
    spec_path = sd / f"{workflow}-{time.strftime('%Y%m%dT%H%M%S')}.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    p = subprocess.run([sys.executable, str(jobctl or JOBCTL), "new", "--spec", str(spec_path)],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    out = (p.stdout or "") + (p.stderr or "")
    m = JOB_ID_RE.search(out)
    if p.returncode != 0 or not m:
        raise ApiError(f"建作业失败：{out.strip()[:400]}", 500)
    job_id = m.group(1)

    if run:
        log = _jobs_dir(jobs_dir) / job_id / "run.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as fh:
            spawn([sys.executable, str(RUN_PY), job_id, "--no-keep"],
                  stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(ROOT))

    # 以 jobctl 落盘的 job.json 为准（params_hash 由它计算）
    state, phash = jobstate.CREATED, spec.get("params_hash")
    try:
        j = json.loads((_jobs_dir(jobs_dir) / job_id / "job.json").read_text(encoding="utf-8"))
        state = j.get("state") or state
        phash = j.get("params_hash") or phash
    except (OSError, json.JSONDecodeError):
        pass

    return {"job_id": job_id, "state": state, "run": bool(run),
            "workflow": res["workflow"]["key"], "workflow_version": res["workflow"]["version"],
            "model": res["model"]["key"], "params_hash": phash,
            "params": res["params"], "expect": spec.get("expect")}


def cancel_job(job_id: str, jobs_dir: Path | None = None) -> dict:
    """只允许取消还没开始的作业；已经在跑的用 run.py --abort。"""
    job = _load_job(job_id, jobs_dir)
    if job.get("state") != jobstate.CREATED:
        raise ApiError(f"作业状态为 {job.get('state')}，不能取消（已开始的请用 run.py --abort）", 409)
    job["state"] = jobstate.ABORTED
    if isinstance(job.get("state_seq"), int):
        job["state_seq"] += 1
    job["aborted_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    job["aborted_by"] = "web"
    store.atomic_write_json(_job_path(job_id, jobs_dir), job)
    return {"job_id": job_id, "state": jobstate.ABORTED}


def zip_artifacts(job_ids: list[str], jobs_dir: Path | None = None,
                  pub_dir: Path | None = None) -> bytes:
    """把多个作业的产物打成一个 zip（内存中，不落临时文件）。

    优先取已发布的静态副本；没有则回落作业目录。
    """
    pub = Path(pub_dir) if pub_dir else PUB_DIR
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for jid in job_ids:
            if not re.fullmatch(r"p1-\d{8}T\d{4}-[0-9a-f]{6}", jid or ""):
                continue
            src = pub / jid
            if not src.is_dir():
                src = _jobs_dir(jobs_dir) / jid / "artifacts"
            if not src.is_dir():
                continue
            for f in sorted(src.iterdir()):
                if f.is_file() and f.suffix.lower() in ART_SUFFIX:
                    zf.write(f, arcname=f"{jid}/{f.name}")
                    n += 1
    if n == 0:
        raise ApiError("选中的作业没有可下载的产物", 404)
    return buf.getvalue()


def image_workflows() -> list[dict]:
    """控制台的"模型"下拉数据源：只取 image 类工作流。"""
    out = []
    for w in registry.list_workflows():
        if w.get("kind") != "image":
            continue
        out.append({
            "key": w["key"], "name": w["name"], "version": w["version"],
            "model": w.get("model_key") or (w.get("model") or {}).get("key"),
            "compat": (w.get("compat") or {}).get("status"),
            "params": w.get("params") or {},
        })
    return out
