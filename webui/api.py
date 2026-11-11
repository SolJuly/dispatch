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
from tools.p2 import lifecycle

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
# 音频单独一档：WAV 24bit/48kHz 立体声约 17MB/分钟，3 分钟无损就 50MB。
# 不能套用图片的 20MB（剪一段 90 秒的参考音频就超了），
# 也不该套视频的 128MB（音频很少需要那么大，放宽了只是纵容浪费）。
MAX_UPLOAD_AUDIO = int(os.environ.get("P21_MAX_UPLOAD_AUDIO_MB", "64")) * 1048576


def max_upload_bytes() -> int:
    """单次上传请求体上限（视频比图片大很多；fileserver 也用这个数）。"""
    return max(MAX_UPLOAD, MAX_UPLOAD_VIDEO, MAX_UPLOAD_AUDIO)
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
    """按内容判类型，返回扩展名。图片 + 视频 + 音频都认。

    音频三种格式的判据（**顺序很关键**）：

        WAV   RIFF....WAVE     ← 和 WebP(RIFF+WEBP) / AVI(RIFF+AVI ) 共用 RIFF 头
        MP3   ID3 或 FF FB/FF F3/FF F2（帧同步字）
        FLAC  fLaC

    所以 RIFF 的分支必须**先判断 data[8:12]** 再决定是 webp/avi/wav，
    不能把 WAV 的检查放在 WebP 前面 —— 否则 .webp 会被误判成音频。
    """
    for magic, ext in IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF":
        riff = data[8:12]
        if riff == b"WEBP":
            return ".webp"
        if riff == b"AVI ":
            return ".avi"
        if riff == b"WAVE":
            return ".wav"
    if data[:4] == b"fLaC":
        return ".flac"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return ".mp3"
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
                   f"视频收 MP4/MOV/WebM/AVI，音频收 WAV/MP3/FLAC", 415)


VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".gif")
AUDIO_EXTS = (".wav", ".mp3", ".flac")


def _cap_for(ext: str) -> int:
    if ext in VIDEO_EXTS:
        return MAX_UPLOAD_VIDEO
    if ext in AUDIO_EXTS:
        return MAX_UPLOAD_AUDIO
    return MAX_UPLOAD


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

    **并发闸**：先确认还剩几个槽，再挑前 N 个能跑的启动，其余原样报"并发已满"。
    为什么不是逐个判：批量执行 3 个时，前两个都看到"还有空槽"（因为它们还没写
    RUNNING），第三个才发现满了 —— 然后两台机已经在下单路上。必须先算总账。
    """
    from tools.p2 import concurrency as conc

    todo = [str(x) for x in ids if x][:10]
    ok_now, why = conc.check_slot(jobs_dir)
    cap = conc.max_parallel()
    room = max(0, cap - conc.slots_used(jobs_dir)) if ok_now else 0

    out = []
    for i, jid in enumerate(todo):
        try:
            job = _load_job(jid, jobs_dir)
        except ApiError as e:
            out.append({"job_id": jid, "error": e.msg})
            continue
        st = job.get("state")
        if st != jobstate.CREATED:
            out.append({"job_id": jid, "error": f"状态 {st}，只有 CREATED 能执行"})
            continue
        if i >= room:
            # 槽位用完了：明确说"没启动"，不要让前端以为它跑了
            out.append({"job_id": jid, "error": f"并发已满（{why}），未启动。"
                                                f"等前面的作业结束再执行。"})
            continue
        log = _jobs_dir(jobs_dir) / jid / "run.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as fh:
            fh.write(f"\n--- 控制台手动执行 {dt.datetime.now().astimezone():%Y-%m-%dT%H:%M:%S} ---\n".encode())
            spawn([sys.executable, str(RUN_PY), jid, "--no-keep", "--batch",
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


def read_manifest(job_id: str, jobs_dir: Path | None = None) -> dict:
    """读作业目录的 manifest.json；没有/读不出来一律返回 {}。

    为什么吞异常：manifest 是**派生文件**，只有跑到 PERSISTED 之后才存在
    （`tools/p1/run.py:step_persist`）。未完成作业没有它，磁盘上那一份也可能
    写了一半。详情接口的主体是 job.json，cost/quality 都只是附加信息 ——
    为了一份附加文件让整页 500，就是拿附属品当命门。

    调用方约定：`{}` 表示"没有"，不是"有但为空"。
    """
    mf = _jobs_dir(jobs_dir) / job_id / "manifest.json"
    if not mf.exists():
        return {}
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_job(job_id: str, jobs_dir: Path | None = None) -> dict:
    job = _load_job(job_id, jobs_dir)
    maybe_publish(job_id, job)
    man = read_manifest(job_id, jobs_dir)
    # cost 与 quality 都在 manifest 里，都放在**顶层**与 job 并列。
    # 早期把 cost 挂在 job 下面读，拿到的是 None（job.json 里没有这个字段），
    # 详情页费用区因此一直是空的 —— quality 沿同一条口径，别再塞进 job。
    cost = man.get("cost") or None
    quality = man.get("quality")
    if not isinstance(quality, dict) or not quality:
        # 老 manifest 里 quality 是 `res.get("quality") or {}` 写下的空对象，
        # 那份"空"和"没有"对 UI 是同一件事：不渲染质量卡。
        quality = None
    return {
        "job": job,
        "cost": cost,
        "quality": quality,
        "artifacts": _artifacts(job_id, jobs_dir),
        "workflow": _workflow_key(job),
        "terminal": job.get("state") in jobstate.TERMINAL,
    }


def create_job(workflow: str, params: dict, *, run: bool = False, budget: float | None = None,
               jobs_dir: Path | None = None, spec_dir: Path | None = None,
               jobctl: Path | None = None, spawn=subprocess.Popen,
               force: bool = False, task_class: str | None = None,
               idle_tier: int | None = None) -> dict:
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

    # 并发闸门：只对「勾了立即执行」的提交生效。
    # 不勾执行的作业停在 CREATED，不建机、不花钱，没有理由拦它 ——
    # 拦了反而挡住"先排好队、等有空再点执行"这种正常用法。
    #
    # 满了怎么办：**降级成"建作业但不执行"**，而不是 429 拒绝。
    # 直接拒绝会丢掉用户填好的整套参数（prompt / 输入图 / seed…），
    # 那比"作业躺在 CREATED 等人点执行"难受得多 —— 后者零风险、可随时启动。
    # 代价是要明确告诉前端它没跑，否则用户以为在跑、其实在等。
    defer_reason = ""
    if run:
        from tools.p2 import concurrency as conc
        ok, why = conc.check_slot(jobs_dir)
        if not ok:
            run = False
            defer_reason = why

    spec = p20cli._make_spec(res, job_key=workflow, task_ref="P2.1")

    # ---- 生命周期（2026-11-10 定版）----
    # 短任务不空转；长任务三档（60/90/120，默认 90）。
    # 非法值**静默回落**而不是 400：这个字段只影响"实例多留多久"，
    # 为一个展示性选项丢掉用户刚填好的一整套参数不值得。
    # 但要写清楚回落到了哪一档（`lifecycle` 字段回给前端，UI 如实显示）。
    tc, tsrc = lifecycle.normalize_task_class(task_class)
    tier = lifecycle.normalize_tier(idle_tier, task_class=tc)
    spec["task_class"] = tc
    spec["task_class_source"] = tsrc
    if tier is not None:
        spec["idle_tier"] = tier
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
            # --batch：用作业级锁而不是全局 .job.lock。
            # 2026-11-09 审计：以前不传这个参数，所有 UI 提交都走全局串行锁，
            # 第二个人提交会静默排队最多 QUEUE_WAIT_S（4 小时）后超时失败，
            # 而 UI 显示的是"提交成功"。run.py 里早就有 --batch 分支，只是没接。
            # --wait-lock：前面还有作业在跑时排队等锁，而不是直接退出把作业扔在 CREATED
            spawn([sys.executable, str(RUN_PY), job_id, "--no-keep", "--batch",
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
            # 并发满而降级时的原因。前端据此提示"已创建但未执行"，
            # 而不是显示"已提交执行"然后让作业静静躺在 CREATED。
            "deferred": bool(defer_reason),
            "deferred_reason": defer_reason,
            # 生命周期实算结果：UI 直接渲染，别在前端再算一遍（数字会漂移）
            "lifecycle": lifecycle.describe({"task_class": tc,
                                             "task_class_source": tsrc,
                                             "idle_tier": tier}),
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


def is_recent_verified(compat: dict | None, days: int = 1) -> bool:
    """最近 N 天内**通过真机验证**的工作流 = 「新上线」。

    判据是 `compat.checked_at`，三个坑都踩过：

    ① 不能用 workflow 的 `imported_at` —— 那是派生索引的重建时间，
       每跑一次 `p20ctl import` 就变，拿它做"新"会让全部 54 条都变新。
    ② `checked_at` 只对 verified 有意义。experimental 记录在 import 时
       会被补一条、时间戳写的是"导入那一刻"，所以必须先判 status。
    ③ 窗口不能给 7 天 —— 仓库里 11-03~11-07 验证过 26 条老工作流，
       按 7 天算全算"新"，标签就没有意义。
    """
    import datetime as _dt
    import os
    c = compat or {}
    if c.get("status") != "verified":
        return False
    at = str(c.get("checked_at") or c.get("verified_at") or "")
    try:
        d = _dt.date.fromisoformat(at[:10])
    except ValueError:
        return False
    try:
        days = int(os.environ.get("P21_NEW_BADGE_DAYS") or days)
    except ValueError:
        pass
    return (_dt.date.today() - d).days <= days


def _console_workflows(kind: str) -> list[dict]:
    """控制台下拉数据源：按类型取未废弃工作流，**新上线的排最前**，其余可用的在前。"""
    out = []
    for w in registry.list_workflows():
        if w.get("kind") != kind or w.get("deprecated"):
            continue
        compat = w.get("compat") or {}
        status = compat.get("status")
        new = is_recent_verified(compat)
        out.append({
            "key": w["key"], "name": w["name"], "description": w.get("description") or "",
            "version": w["version"],
            "model": w.get("model_key") or (w.get("model") or {}).get("key"),
            "compat": status,
            "is_new": new,
            "params": w.get("params") or {},
            "_rank": (0 if new else 1) if status in ("verified", "equivalent") else 2,
        })
    out.sort(key=lambda x: x["_rank"])  # 稳定排序：新的最前，然后可用的，再未开发
    for x in out:
        x.pop("_rank", None)
    return out


def image_workflows() -> list[dict]:
    """图片工作流（控制台「文生图/图生图」页）。"""
    return _console_workflows("image")


def video_workflows() -> list[dict]:
    """视频工作流（控制台「视频」页）。"""
    return _console_workflows("video")
