#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把作业产物发布到 nginx 静态目录，保证"图片预览/下载"链接不依赖任何常驻进程。

背景：控制台页面由 8081 的 fileserver 动态渲染，进程一停就 404；
但产物本身是静态文件，复制到 nginx 已公开的 `/pub/` 目录后链接永久有效。

产物路径：`/srv/dsh-pub/jobs/<job_id>/<file>`，附 `job-summary.json` 与全局 `index.json`。

用法：
    python3 scripts/publish_artifacts.py            # 全量发布
    python3 scripts/publish_artifacts.py --job <id> # 只发布一个作业
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOBS = ROOT / "reports/p1/jobs"
PUB = Path("/srv/dsh-pub/jobs")
ART_SUFFIX = (".png", ".jpg", ".jpeg", ".webp", ".mp4")
SKIP_STATES = ("CREATED", "ABORTED")


def ensure_rw() -> None:
    subprocess.run(["mount", "-o", "remount,rw", "/"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def publish_one(job_dir: Path) -> dict | None:
    f = job_dir / "job.json"
    if not f.exists():
        return None
    job = json.loads(f.read_text(encoding="utf-8"))
    jid = job["job_id"]
    arts_dir = job_dir / "artifacts"
    files = sorted(p for p in arts_dir.iterdir() if p.is_file() and p.suffix.lower() in ART_SUFFIX) \
        if arts_dir.is_dir() else []
    if not files or job.get("state") in SKIP_STATES:
        return None
    out = PUB / jid
    out.mkdir(parents=True, exist_ok=True)
    for p in files:
        shutil.copy2(p, out / p.name)
    reg = (job.get("model") or {}).get("registry") or {}
    summary = {
        "job_id": jid, "state": job.get("state"), "kind": job.get("kind"),
        "workflow": reg.get("workflow_key"), "workflow_version": reg.get("workflow_version"),
        "model": reg.get("model_key"), "model_version": reg.get("model_version"),
        "created_at": job.get("created_at"), "params_hash": job.get("params_hash"),
        "params": job.get("params"), "expect": job.get("expect"),
        "artifacts": [{"name": p.name,
                       "bytes": (out / p.name).stat().st_size,
                       "url": f"/pub/jobs/{jid}/{p.name}"} for p in files],
    }
    (out / "job-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    for p in out.iterdir():
        if p.is_file():
            p.chmod(0o644)
    out.chmod(0o755)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", default="", help="只发布指定 job_id")
    a = ap.parse_args()

    ensure_rw()
    PUB.mkdir(parents=True, exist_ok=True)
    targets = [JOBS / a.job] if a.job else sorted(JOBS.iterdir())
    items = []
    for d in targets:
        if not d.is_dir():
            continue
        s = publish_one(d)
        if s:
            items.append({k: s[k] for k in ("job_id", "state", "workflow", "created_at", "artifacts")})
            print(f"  ✓ {s['job_id']}  {len(s['artifacts'])} 个文件")
    # --job 只更新一条：与已有 index 合并，不把别人的记录冲掉
    if a.job and (PUB / "index.json").exists():
        try:
            old = json.loads((PUB / "index.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old = {}
        merged = {x["job_id"]: x for x in old.get("jobs") or []}
        merged.update({x["job_id"]: x for x in items})
        items = sorted(merged.values(), key=lambda x: x.get("created_at") or "", reverse=True)
    index = {"generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
             "jobs": items}
    (PUB / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    (PUB / "index.json").chmod(0o644)
    PUB.chmod(0o755)
    print(f"[ok] {len(items)} 个作业 → {PUB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
