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
import shutil
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

    URL 必须跟着**实际找到文件的位置**走，不能恒定写 /pub/ ——
    /pub/ 由 nginx 直读 /srv/dsh-pub，本地有产物但没跑过 publish 的作业
    在那里不存在，前端拿到的是必 404 的链接（<video> 还会反复重试，
    表现为「与后端失联」的假象）。本地存在的产物改走 /f/，那条路由读的是
    作业目录本体，永远可用。
    """
    local = _jobs_dir(jobs_dir) / job_id / "artifacts"
    pub = PUB_DIR / job_id
    src = local if local.is_dir() else pub
    if not src.is_dir():
        return []
    from_local = src == local
    out = []
    for p in sorted(src.iterdir()):
        if p.is_file() and p.suffix.lower() in ART_SUFFIX:
            if from_local:
                # /f/ 读 share/ 内的文件；sync_share 会把 reports/p1/jobs 同步过去，
                # 所以本地产物在 share 里有一条等价路径，永远可用（不依赖 publish）。
                url = f"/f/reports/p1/jobs/{job_id}/artifacts/{p.name}"
            else:
                url = f"/pub/jobs/{job_id}/{p.name}"
            out.append({"name": p.name, "bytes": p.stat().st_size,
                        "url": url,
                        "published": (pub / p.name).exists(),
                        "source": "local" if from_local else "published"})
    return out


def _first_artifact_url(job_id: str, jobs_dir: Path | None) -> str:
    arts = _artifacts(job_id, jobs_dir)
    return arts[0]["url"] if arts else ""


# ---------------- 缩略图
# 列表页以前直接把原图/原视频塞进 <img>：
#   · 视频 → <img src="x.mp4"> 永远解不出来，显示黑块
#   · 图片 → 13MB 级原图，一屏几十张等于几百 MB，慢到像卡死
# 这里统一生成 480px 宽的 jpg 缓存，命名带源文件内容哈希：
# 内容没变就复用，改了自动换名，不需要失效逻辑。
THUMB_W = 480
THUMB_DIR_NAME = "thumbs"


def _thumb_dir(job_dir: Path) -> Path:
    return job_dir / THUMB_DIR_NAME


def _src_stamp(p: Path) -> str:
    st = p.stat()
    return f"{st.st_size}-{int(st.st_mtime_ns)}"


def thumb_for(src: Path, job_dir: Path, *, width: int = THUMB_W,
              force: bool = False) -> Path | None:
    """返回 src 的缩略图路径；生成失败返回 None（调用方降级到原图/占位）。

    视频用第 1 帧做封面（黑帧问题由 -ss 微偏移缓解）；图片直接缩放。
    失败一律返回 None，绝不抛异常——列表页不能因为一张图挂掉。
    """
    if not src.is_file():
        return None
    stem = src.stem
    # 内容戳进文件名：源文件一变，旧缩略图自然失效（无需清理逻辑）
    dst = _thumb_dir(job_dir) / f"{stem}.{_src_stamp(src)}.jpg"
    if dst.is_file() and not force and dst.stat().st_size > 0:
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".part.jpg")

    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    is_video = src.suffix.lower() in (".mp4", ".mov", ".webm", ".m4v")
    if is_video:
        # 取 0.5s 处：第 0 帧常常是全黑（编码器首帧/淡入），这是「预览图是黑的」的另一半原因
        cmd += ["-ss", "0.5"]
    cmd += ["-i", str(src), "-frames:v", "1",
            "-vf", f"scale={width}:-2:force_original_aspect_ratio=decrease",
            "-q:v", "4", str(tmp)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        tmp.unlink(missing_ok=True)
        return None
    if p.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        # 视频 0.5s 抽取失败（片子比 0.5s 还短）→ 退回第 0 帧
        if is_video and not force:
            return thumb_for(src, job_dir, width=width, force=True)
        return None
    tmp.replace(dst)
    # 同时丢一份到 share/（硬链接，同分区零额外空间）。
    # 为什么必须现在做：/f/ 路由被 _safe() 限制在 share/ 内，看不到作业目录。
    # 若等下一次 sync_share，新作业的首屏缩略图会 404（又是黑块）。
    try:
        share_job = ROOT / "share" / "reports" / "p1" / "jobs" / job_dir.name
        _drop_copy(dst, share_job / THUMB_DIR_NAME / dst.name)
    except OSError:
        pass          # share 不可写不影响缩略图本身；最坏退化成「下次 sync 后可见」
    return dst


def _thumb_url(job_id: str, art_name: str, job_dir: Path, jobs_dir: Path | None) -> str:
    """缩略图的访问 URL。生成不了就返回空串，前端据此降级。"""
    src = (job_dir / "artifacts" / art_name)
    t = thumb_for(src, job_dir)
    if not t:
        return ""
    return f"/f/reports/p1/jobs/{job_id}/{THUMB_DIR_NAME}/{t.name}"


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


VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".gif")


def _cap_for(ext: str) -> int:
    return MAX_UPLOAD_VIDEO if ext in VIDEO_EXTS else MAX_UPLOAD


def _drop_copy(src: Path, dst: Path) -> None:
    """作业目录与 share 目录各留一份：优先硬链接（同一分区零额外空间），跨盘才复制。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    dst.chmod(0o644)


def save_upload_file(filename: str, tmp_path: Path) -> dict:
    """流式上传落地：按内容哈希命名，内存里只有 64KB 缓冲（128MB 视频也不进内存）。"""
    import hashlib

    size = tmp_path.stat().st_size
    if size <= 0:
        raise ApiError("空文件", 400)
    with tmp_path.open("rb") as fh:
        head = fh.read(4096)
    ext = sniff_upload(head, filename)
    cap = _cap_for(ext)
    if size > cap:
        raise ApiError(f"文件太大（{cap // 1048576} MB 上限；当前 "
                       f"{size / 1048576:.1f} MB）", 413)
    h = hashlib.sha256()
    with tmp_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    name = f"in-{h.hexdigest()[:8]}{ext}"
    dst = INPUTS_DIR / name
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.move(str(tmp_path), dst)
    dst.chmod(0o644)
    _drop_copy(dst, SHARE_INPUTS / name)
    return {"name": name, "bytes": size, "url": f"/f/inputs/{name}"}


def save_upload(filename: str, data: bytes) -> dict:
    """小文件（图片）走内存版；大文件走 save_upload_file。"""
    if not data:
        raise ApiError("空文件", 400)
    ext = sniff_upload(data, filename)
    cap = _cap_for(ext)
    if len(data) > cap:
        raise ApiError(f"文件太大（{cap // 1048576} MB 上限；当前 "
                       f"{len(data) / 1048576:.1f} MB）", 413)
    import hashlib
    h = hashlib.sha256(data).hexdigest()[:8]
    name = f"in-{h}{ext}"
    INPUTS_DIR.mkdir(parents=True, exist_ok=True)
    dst = INPUTS_DIR / name
    dst.write_bytes(data)
    dst.chmod(0o644)
    _drop_copy(dst, SHARE_INPUTS / name)
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
        arts = _artifacts(job.get("job_id"), jobs_dir)
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
            "artifacts": len(arts),
            "first_artifact_url": arts[0]["url"] if arts else "",
            # 列表页缩略图：视频给第 1 帧海报，图片给 480px 缩小版。
            # 前端拿到空串就渲染占位块，不再把 .mp4 塞进 <img>（那是黑块的主因）。
            "thumb_url": (_thumb_url(job.get("job_id"), arts[0]["name"],
                                     d, jobs_dir) if arts else ""),
            "thumb_kind": ("video" if arts and arts[0]["name"].lower().endswith(
                (".mp4", ".mov", ".webm", ".m4v")) else "image") if arts else "",
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
            res = create_job(wk, job.get("params") or {}, run=run, jobs_dir=jobs_dir,
                             force=True)
            out.append({"job_id": jid, "new_job_id": res["job_id"], "state": res["state"]})
        except ApiError as e:
            out.append({"job_id": jid, "error": e.msg})
    return out


def delete_jobs(ids: list[str], *, purge: bool = False, force: bool = False) -> dict:
    """删除作业（默认进 .trash，可恢复；purge=True 彻底删）。

    force：允许删掉未终态（CREATED 等）的作业。
    为什么需要：控制台提交时若没勾「立即执行」，作业会永远停在 CREATED
    且没有任何自动过期机制——既没跑过、也不会跑到，纯属残留。
    jobctl 的默认拒绝是对的（保护真在跑的作业），但要给用户一个
    显式清理僵尸的入口，所以这里把 force 透传下去。
    """
    clean = [str(x) for x in ids if x]
    if not clean:
        raise ApiError("ids 为空", 400)
    cmd = [sys.executable, str(JOBCTL), "rm", *clean]
    if purge:
        cmd.append("--purge")
    if force:
        cmd.append("--force")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=180)
    return {"ok": p.returncode == 0,
            "output": ((p.stdout or "") + (p.stderr or ""))[-1500:]}


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
               jobctl: Path | None = None, spawn=subprocess.Popen,
               force: bool = False) -> dict:
    """校验参数 → 生成 spec → jobctl new →（可选）后台执行。

    同一任务的重复提交由 jobctl 的提交锁兜住：进行中的作业幂等复用（返回已有
    job_id，不 spawn 第二个 run.py），已完成的拒绝（--force 可强制重跑，rerun 用）。
    """
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
    # 同一秒两个请求不能互相覆盖 spec：加 PID + 微秒
    spec_path = sd / (f"{workflow}-{time.strftime('%Y%m%dT%H%M%S')}"
                      f"-{os.getpid()}-{time.time_ns() % 1000000:06d}.json")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")

    cmd = [sys.executable, str(jobctl or JOBCTL), "new", "--spec", str(spec_path)]
    if force:
        cmd.append("--force")
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    out = (p.stdout or "") + (p.stderr or "")
    # 取最后一个 id：jobctl 可能先打印「旧作业已结束：<jid>」这类历史 id，
    # 本次真正新建的那个总在最后（与 p20ctl._pick_job_id 同口径）。
    found = JOB_ID_RE.findall(out)
    if p.returncode != 0 or not found:
        msg = out.strip()[:400]
        code = 409 if "已完成" in msg or "白烧钱" in msg else 500
        raise ApiError(f"建作业失败：{msg}", code)
    job_id = found[-1]
    # 复用了进行中的作业：绝不 spawn 第二个 run.py（否则同一个 job 会被两个进程抢着跑）
    reused = store.DEDUP_REUSE_MARK in out

    if run and not reused:
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

    return {"job_id": job_id, "state": state, "run": bool(run and not reused),
            "reused": reused,
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
