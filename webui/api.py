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

from . import budget as budget_mod
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
MAX_UPLOAD = 20 * 1048576                    # 图片
MAX_UPLOAD_VIDEO = int(os.environ.get("P21_MAX_UPLOAD_VIDEO_MB", "128")) * 1048576


def max_upload_bytes() -> int:
    """单次上传请求体上限（视频比图片大很多；fileserver 也用这个数）。"""
    return max(MAX_UPLOAD, MAX_UPLOAD_VIDEO)
# 控制台提交时排队等锁的上限（秒）：单任务串行，前面还有作业就等着，别扔在 CREATED
QUEUE_WAIT_S = int(os.environ.get("P21_QUEUE_WAIT_S", "14400"))
# 卡型没现货时最多等多久（秒）；全都没货才失败
STOCK_WAIT_S = int(os.environ.get("P21_STOCK_WAIT_S", "300"))


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


IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)


def sniff_upload(data: bytes, filename: str = "") -> str:
    """按内容判类型，返回扩展名。图片 + 视频都认（动作迁移要传驱动视频）。"""
    for magic, ext in IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return ".avi"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand.startswith(b"qt"):
            return ".mov"
        if brand in (b"M4V ", b"M4VH", b"M4VP"):
            return ".m4v"
        return ".mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":          # EBML / webm-mkv
        return ".webm"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    ext = Path(filename or "").suffix.lower()
    raise ApiError(f"不支持的文件类型（{ext or '未知'}）；图片收 PNG/JPEG/WebP，"
                   f"视频收 MP4/MOV/WebM", 415)


def save_upload(filename: str, data: bytes) -> dict:
    """上传输入素材：校验格式/大小 → 内容寻址命名 → 两份副本（作业用 + 静态可看）。"""
    if not data:
        raise ApiError("空文件", 400)
    ext = sniff_upload(data, filename)
    is_video = ext in (".mp4", ".mov", ".m4v", ".webm", ".avi", ".gif")
    cap = MAX_UPLOAD_VIDEO if is_video else MAX_UPLOAD
    if len(data) > cap:
        raise ApiError(f"文件太大（{cap // 1048576} MB 上限；当前 "
                       f"{len(data) / 1048576:.1f} MB）", 413)
    import hashlib
    h = hashlib.sha256(data).hexdigest()[:8]
    name = f"in-{h}{ext}"
    for d in (INPUTS_DIR, SHARE_INPUTS):
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        (d / name).chmod(0o644)
    return {"name": name, "bytes": len(data), "url": f"/f/inputs/{name}"}


def list_jobs(limit: int = 20, *, offset: int = 0, state_prefix: str = "", workflow: str = "",
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
        cost = None
        mf = d / "manifest.json"
        if mf.exists():
            try:
                cost = (json.loads(mf.read_text(encoding="utf-8")).get("cost") or {})
            except (OSError, json.JSONDecodeError):
                cost = {}
        rows.append({
            "job_id": job.get("job_id"),
            "state": st,
            "cost": (cost or {}).get("amount"),
            "minutes": (cost or {}).get("minutes"),
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
    lim = max(1, min(int(limit or 20), 200))
    off = max(0, int(offset or 0))
    return rows[off:off + lim]


def rerun_jobs(ids: list[str], *, run: bool = False, jobs_dir: Path | None = None) -> list[dict]:
    """按原参数重跑一批作业（逐个新建作业，可选立即执行）。"""
    out = []
    for jid in ids[:20]:
        try:
            job = _load_job(jid, jobs_dir)
        except ApiError as e:
            out.append({"job_id": jid, "error": e.msg})
            continue
        reg = (job.get("model") or {}).get("registry") or {}
        wk = reg.get("workflow_key")
        if not wk:
            out.append({"job_id": jid, "error": "旧作业没有 registry 工作流信息"})
            continue
        try:
            res = create_job(wk, job.get("params") or {}, run=run, jobs_dir=jobs_dir)
            out.append({"job_id": jid, "new_job_id": res["job_id"], "state": res["state"]})
        except ApiError as e:
            out.append({"job_id": jid, "error": e.msg})
    return out


def delete_jobs(ids: list[str], *, purge: bool = False) -> dict:
    """删除作业（默认进 .trash，可恢复；purge=True 彻底删）。"""
    clean = [str(x) for x in ids if x]
    if not clean:
        raise ApiError("ids 为空", 400)
    cmd = [sys.executable, str(JOBCTL), "rm", *clean]
    if purge:
        cmd.append("--purge")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    return {"ok": p.returncode == 0, "output": ((p.stdout or "") + (p.stderr or ""))[-1500:]}


def run_jobs(ids: list[str], *, jobs_dir: Path | None = None,
             spawn=subprocess.Popen) -> list[dict]:
    """把还停在 CREATED 的作业真正执行起来（控制台「执行」按钮用）。

    以前提交时若没勾「立即执行」，作业只会躺在 CREATED，没有任何入口能启动它。
    """
    out = []
    for jid in [str(x) for x in ids if x][:10]:
        try:
            job = _load_job(jid, jobs_dir)
        except ApiError as e:
            out.append({"job_id": jid, "error": e.msg})
            continue
        st = job.get("state")
        if st != jobstate.CREATED:
            out.append({"job_id": jid, "error": f"状态 {st}，只有 CREATED 能执行"})
            continue
        log = _jobs_dir(jobs_dir) / jid / "run.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as fh:
            fh.write(f"\n--- 控制台手动执行 {dt.datetime.now().astimezone():%Y-%m-%dT%H:%M:%S} ---\n".encode())
            spawn([sys.executable, str(RUN_PY), jid, "--no-keep",
                   "--wait-lock", str(QUEUE_WAIT_S), "--wait-stock", str(STOCK_WAIT_S)],
                  stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, cwd=str(ROOT))
        out.append({"job_id": jid, "state": "QUEUED"})
    return out


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
    cost = None
    mf = _jobs_dir(jobs_dir) / job_id / "manifest.json"
    if mf.exists():
        try:
            cost = (json.loads(mf.read_text(encoding="utf-8")).get("cost") or {})
        except (OSError, json.JSONDecodeError):
            cost = None
    return {
        "job": job,
        "cost": cost,
        "artifacts": _artifacts(job_id, jobs_dir),
        "workflow": _workflow_key(job),
        "terminal": job.get("state") in jobstate.TERMINAL,
    }


def create_job(workflow: str, params: dict, *, run: bool = False, budget: float | None = None,
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

    # 预算闸门：超上限直接拒绝，避免误提交烧钱
    try:
        est = budget_mod.check_budget(res["workflow"], res.get("params") or {}, budget)
    except budget_mod.BudgetError as e:
        raise ApiError(str(e), 402) from e

    spec = p20cli._make_spec(res, job_key=workflow, task_ref="P2.1")
    if budget is not None and budget > 0:
        spec.setdefault("budget", {})
        spec["budget"]["max_cost"] = min(float(budget), float(spec["budget"].get("max_cost") or budget))
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
            # --wait-lock：前面还有作业在跑时排队等锁，而不是直接退出把作业扔在 CREATED
            spawn([sys.executable, str(RUN_PY), job_id, "--no-keep",
                   "--wait-lock", str(QUEUE_WAIT_S), "--wait-stock", str(STOCK_WAIT_S)],
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


def _console_workflows(kind: str) -> list[dict]:
    """控制台下拉数据源：按类型取未废弃工作流，可用的排前面。"""
    out = []
    for w in registry.list_workflows():
        if w.get("kind") != kind or w.get("deprecated"):
            continue
        status = (w.get("compat") or {}).get("status")
        out.append({
            "key": w["key"], "name": w["name"], "description": w.get("description") or "",
            "version": w["version"],
            "model": w.get("model_key") or (w.get("model") or {}).get("key"),
            "compat": status,
            "params": w.get("params") or {},
            "_rank": 0 if status in ("verified", "equivalent") else 1,
        })
    out.sort(key=lambda x: x["_rank"])  # 稳定排序：可用的在前，组内保持 registry 顺序
    for x in out:
        x.pop("_rank", None)
    return out


def image_workflows() -> list[dict]:
    """图片工作流（控制台「文生图/图生图」页）。"""
    return _console_workflows("image")


def video_workflows() -> list[dict]:
    """视频工作流（控制台「视频」页）。"""
    return _console_workflows("video")
