#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 产物展示：扫 jobs/*/artifacts/，生成缩略图与画廊页。

    python3 tools/p1/gallery.py            # 生成 thumbs + reports/p1/gallery.html
    python3 tools/p1/gallery.py --no-thumb # 只重建 HTML

范围：**目前只处理图片**（png/jpg/webp）。视频产物会列出但标注"暂不展示"，
      后续接 ffmpeg 抽 poster 帧即可补齐。

缩略图：ffmpeg scale 到 480px 宽（本机无 PIL，用 ffmpeg 最省事）。
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.p1 import store  # noqa: E402

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".webm", ".gif"}
THUMB_W = 480
OUT_HTML = store.P1_DIR / "gallery.html"


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def make_poster(src: Path, dst: Path) -> bool:
    """抽视频第 1 帧当封面。ffmpeg 不可用或抽帧失败都返回 False（调用方降级）。"""
    if not has_ffmpeg():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-frames:v", "1", "-q:v", "3", str(dst)],
            capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return p.returncode == 0 and dst.exists()


def make_thumb(src: Path, dst: Path, width: int = THUMB_W) -> bool:
    """ffmpeg 缩放；成功返回 True。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-vf", f"scale={width}:-1", "-frames:v", "1", "-q:v", "3", str(dst)],
        capture_output=True, text=True, timeout=120)
    return p.returncode == 0 and dst.exists()


def scan() -> list[dict]:
    """遍历全部 job，收集产物与元信息。"""
    items = []
    for jid in store.list_jobs():
        jdir = store.job_dir(jid)
        job = store.read_json(jdir / "job.json") or {}
        man = store.read_json(jdir / "manifest.json") or {}
        adir = jdir / "artifacts"
        if not adir.is_dir():
            continue
        arts = []
        for f in sorted(adir.iterdir()):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext in IMAGE_EXT:
                kind = "image"
            elif ext in VIDEO_EXT:
                kind = "video"
            else:
                continue
            arts.append({"path": f, "kind": kind, "bytes": f.stat().st_size})
        if not arts:
            continue
        items.append({
            "job_id": jid,
            "state": job.get("state", "?"),
            "model": (job.get("model") or {}).get("key", "?"),
            "created": job.get("created_at", ""),
            "verdict": man.get("verdict"),
            "checks": man.get("checks") or {},
            "cost": (man.get("cost") or {}).get("amount"),
            "minutes": (man.get("instance") or {}).get("minutes"),
            "uhost": (man.get("instance") or {}).get("uhost_id"),
            "artifacts": arts,
            "jdir": jdir,
        })
    return sorted(items, key=lambda x: x["job_id"], reverse=True)


def build_thumbs(items: list[dict], force: bool = False) -> int:
    n = 0
    for it in items:
        for a in it["artifacts"]:
            if a["kind"] == "image":
                dst = it["jdir"] / "thumbs" / f"{a['path'].stem}.jpg"
                if dst.exists() and not force:
                    a["thumb"] = dst
                    continue
                if make_thumb(a["path"], dst):
                    a["thumb"] = dst
                    n += 1
            elif a["kind"] == "video":
                dst = it["jdir"] / "thumbs" / f"{a['path'].stem}-poster.jpg"
                if dst.exists() and not force:
                    a["poster"] = dst
                    continue
                if make_poster(a["path"], dst):
                    a["poster"] = dst
                    n += 1
    return n


def _rel(p: Path) -> str:
    """相对 gallery.html 所在目录（reports/p1/）——浏览器按页面位置解析相对路径。"""
    try:
        return str(p.resolve().relative_to(store.P1_DIR))
    except ValueError:
        return str(p)


def build_html(items: list[dict]) -> str:
    cards = []
    for it in items:
        vd = it.get("verdict") or "—"
        vcls = "pass" if vd == "PASS" else ("fail" if vd == "FAIL" else "na")
        checks = " ".join(f"<span class='ck {'on' if v else 'off'}'>{k}</span>"
                          for k, v in (it.get("checks") or {}).items())
        media = []
        for a in it["artifacts"]:
            if a["kind"] == "image":
                thumb = a.get("thumb")
                src = _rel(a["path"])
                img = (f"<a href='{html.escape(src)}' target='_blank'>"
                       f"<img loading='lazy' src='{html.escape(_rel(thumb) if thumb else src)}' "
                       f"alt='{html.escape(it['job_id'])}'></a>")
                media.append(f"<div class='m'>{img}<div class='cap'>{a['bytes']//1024} KB</div></div>")
            else:
                poster = a.get("poster")
                vsrc = html.escape(_rel(a["path"]))
                kb = a["bytes"] // 1024
                if poster:
                    media.append(
                        f"<div class='m'><video controls preload='metadata' "
                        f"poster='{html.escape(_rel(poster))}' src='{vsrc}'></video>"
                        f"<div class='cap'>{kb} KB · 视频</div></div>")
                else:
                    media.append(
                        f"<div class='m vid'><div class='ph'>视频不可预览"
                        f"<br><span>{kb} KB · ffmpeg 不可用</span></div></div>")
        cards.append(f"""
    <section class='card'>
      <header>
        <code>{html.escape(it['job_id'])}</code>
        <span class='badge {vcls}'>{vd}</span>
        <span class='meta'>{html.escape(str(it.get('model')))} ·
          {it.get('minutes') or '—'} 分钟 · ¥{it.get('cost') if it.get('cost') is not None else '—'}</span>
      </header>
      <div class='checks'>{checks}</div>
      <div class='media'>{''.join(media) or "<span class='hint'>无产物</span>"}</div>
    </section>""")

    total = len(items)
    passed = sum(1 for i in items if i.get("verdict") == "PASS")
    cost = sum(float(i.get("cost") or 0) for i in items)
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>P1 产物画廊</title><style>
:root{{--bg:#0b0e12;--card:#131a24;--line:#25313f;--txt:#e8edf5;--dim:#8b96a8;
--ok:#7fe3a1;--bad:#f08a8a;--acc:#7cc0ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.6 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}}
header.top{{padding:16px 20px;border-bottom:1px solid var(--line);position:sticky;top:0;
background:rgba(11,14,18,.92);backdrop-filter:blur(8px);z-index:9;display:flex;gap:14px;
align-items:center;flex-wrap:wrap}}
header.top h1{{font-size:17px;margin:0}}
.stat{{color:var(--dim);font-size:13px}}
main{{padding:16px 20px 60px;max-width:1400px;margin:0 auto}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:14px 16px;margin:0 0 14px}}
.card header{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}}
.card code{{color:var(--acc);font-size:13px}}
.badge{{padding:2px 9px;border-radius:999px;font-size:12px;font-weight:700}}
.badge.pass{{background:rgba(127,227,161,.14);color:var(--ok);border:1px solid rgba(127,227,161,.4)}}
.badge.fail{{background:rgba(240,138,138,.14);color:var(--bad);border:1px solid rgba(240,138,138,.4)}}
.badge.na{{background:#1b2330;color:var(--dim);border:1px solid var(--line)}}
.meta{{color:var(--dim);font-size:12.5px}}
.checks{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}}
.ck{{font-size:11.5px;padding:1px 7px;border-radius:6px;background:#1b2330;color:#63708a}}
.ck.on{{color:var(--ok);background:rgba(127,227,161,.1)}}
.ck.off{{color:var(--bad);background:rgba(240,138,138,.1)}}
.media{{display:flex;gap:12px;flex-wrap:wrap}}
.m img{{max-width:320px;border-radius:10px;border:1px solid var(--line);display:block}}
.m video{{max-width:320px;border-radius:10px;border:1px solid var(--line);display:block;
background:#0f151d}}
.m .cap{{color:var(--dim);font-size:12px;margin-top:4px}}
.m.vid .ph{{width:320px;height:200px;border:1px dashed var(--line);border-radius:10px;
display:flex;flex-direction:column;align-items:center;justify-content:center;
color:var(--dim);font-size:13px;background:#0f151d}}
.hint{{color:var(--dim)}}
footer{{color:#5c6779;font-size:12px;text-align:center;padding:20px}}
</style></head><body>
<header class='top'><h1>P1 产物画廊</h1>
<span class='stat'>作业 {total} 个 · PASS {passed} · 实扣 ¥{cost:.3f}</span>
<a class='stat' style='color:var(--acc);text-decoration:none' href='tasks.md'>任务清单</a>
</header><main>{''.join(cards) or "<p class='hint'>还没有作业产物</p>"}</main>
<footer>由 tools/p1/gallery.py 生成 · 图片缩放 {THUMB_W}px · 视频展示待接入</footer>
</body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gallery", description="P1 产物画廊")
    ap.add_argument("--no-thumb", action="store_true", help="不生成缩略图，只重建 HTML")
    a = ap.parse_args(argv)

    items = scan()
    if not items:
        print("没有可展示的产物")
        OUT_HTML.write_text(build_html([]), encoding="utf-8")
        return 0

    n = 0 if a.no_thumb else build_thumbs(items)
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(build_html(items), encoding="utf-8")
    n_img = sum(1 for i in items for x in i["artifacts"] if x["kind"] == "image")
    n_vid = sum(1 for i in items for x in i["artifacts"] if x["kind"] == "video")
    print(f"✓ {store.rel(OUT_HTML)} · {len(items)} 个作业 · 图片 {n_img} · 视频 {n_vid}"
          f" · 新缩略图 {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
