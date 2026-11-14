#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次全量实测（sweep）的产物做成可读的结果页。

## 和 tools/gallery.py 的分工

`gallery.py` 读 `lab/state.json`，那是 P2 benchmark 的结构（gid/workload/cold/warm）。
全量实测的产物在 `reports/p1/jobs/<jid>/artifacts/`，字段是 workflow_key / quality /
verdict —— 两套结构不一样，硬塞进旧画廊只会让两组数据互相污染。

所以这里生成**独立一页**：`reports/lab/sweep-20261114.html`，
按「跑通 / 不可用」分区，跑通的按质量分降序，图片点开看原图、视频直接播。

## 关键设计

- **产物走 share 的绝对路径**：页面从 `/files/<token>/f/reports/lab/` 打开，
  图片用同源相对路径，手机/平板上点开就能看，不依赖本地文件系统。
- **视频带 poster**：没有 poster 的 `<video>` 在移动端是一块灰，用户以为坏了。
  poster 用 ffmpeg 抽第 0 帧（没有 ffmpeg 就退化成 preload=metadata）。
- **不可用的也列出来**：只展示能用的是在骗自己；「为什么不能用」才是要回答的问题。

用法：
    python3 tools/sweep_gallery.py                  # 生成 + 打印路径
    python3 tools/sweep_gallery.py --date 20261114
"""
from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / "reports" / "p1" / "jobs"
LAB = ROOT / "reports" / "lab"
THUMB = LAB / ".sweep-thumbs"
TOKEN_FILE = ROOT / ".share_token"

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VID_EXT = {".mp4", ".webm", ".gif"}

# 失败原因分类：job 里没有结构化字段，靠日志末行的特征词判定。
# 顺序有意义 —— 先匹配更具体的。
FAIL_RULES = [
    ("缺权重", ("模型资产不可见", "没有包含", "loader/input 不存在")),
    ("缺节点", ("missing_node_type", "not found. The custom node")),
    ("图结构缺陷", ("required_input_missing", "custom_validation_failed",
                    "prompt_outputs_failed_validation")),
    ("张量尺寸错配", ("must match the size of tensor",)),
    ("平台配额", ("226608", "quota for the current tier")),
    ("并发冲突", ("云端已有", "上限")),
    ("预算闸", ("超过作业预算上限",)),
    ("校验未过", ("校验未通过",)),
    ("建机失败", ("建机失败", "8433")),
]


def base_url() -> str:
    try:
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    # 注意是 /f/ 而不是 /f/reports/lab/：页面引用的产物在 reports/p1/jobs/ 下，
    # 不在 lab/ 下，前缀到 lab 会让每个链接都得写 ../。
    return f"/files/{tok}/f/" if tok else ""


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def make_poster(video: Path) -> Path | None:
    """抽第 0 帧当 poster。没有 ffmpeg 就返回 None（页面退化成 preload=metadata）。"""
    if not _has_ffmpeg():
        return None
    THUMB.mkdir(parents=True, exist_ok=True)
    out = THUMB / (video.stem + ".jpg")
    if out.is_file() and out.stat().st_size > 0:
        return out
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
             "-frames:v", "1", "-vf", "scale=480:-1", str(out)],
            check=True, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out if out.is_file() and out.stat().st_size > 0 else None
    except Exception:
        return None


def make_thumb(img: Path) -> Path | None:
    if not _has_ffmpeg():
        return None
    THUMB.mkdir(parents=True, exist_ok=True)
    out = THUMB / (img.stem + ".jpg")
    if out.is_file() and out.stat().st_size > 0:
        return out
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(img),
             "-vf", "scale=600:-1", "-q:v", "4", str(out)],
            check=True, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out if out.is_file() and out.stat().st_size > 0 else None
    except Exception:
        return None


def classify_fail(log: Path) -> str:
    """从 run.log / retry*.log 的末几行判定失败类别。"""
    if not log.is_file():
        return "无日志"
    tail = ""
    for p in sorted(log.parent.glob("retry*.log")) + [log]:
        if p.is_file():
            try:
                tail += p.read_text(encoding="utf-8", errors="replace")[-4000:]
            except OSError:
                pass
    for label, marks in FAIL_RULES:
        if any(m in tail for m in marks):
            return label
    return "其他"


def collect(date_prefix: str) -> tuple[list[dict], list[dict]]:
    """返回 (跑通的, 不可用的)，各自已排序。"""
    ok, bad = [], []
    if not JOBS.is_dir():
        return ok, bad
    for d in sorted(JOBS.iterdir()):
        jf = d / "job.json"
        if not jf.is_file():
            continue
        try:
            job = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        created = str(job.get("created_at") or "")[:19].replace("-", "")
        if not created.startswith(date_prefix):
            continue
        reg = (job.get("model") or {}).get("registry") or {}
        wf = reg.get("workflow_key")
        if not wf:
            continue
        man = {}
        if (d / "manifest.json").is_file():
            try:
                man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                man = {}
        q = man.get("quality") or {}
        arts = [p for p in sorted((d / "artifacts").glob("*"))
                if p.suffix.lower() in IMG_EXT | VID_EXT] \
            if (d / "artifacts").is_dir() else []
        rec = {
            "wf": wf, "kind": job.get("kind"), "jid": d.name,
            "state": job.get("state"), "verdict": man.get("verdict"),
            "q": q.get("overall"), "grade": q.get("grade"),
            "bytes": sum(p.stat().st_size for p in arts),
            "raw": d,
        }
        if job.get("state") == "DONE" and arts:
            rec["art"] = arts[0]
            ok.append(rec)
        else:
            rec["why"] = classify_fail(d / "run.log")
            bad.append(rec)
    # 同一工作流只留最新一条（重跑会留下多条）
    def _dedup(rows):
        best = {}
        for r in rows:
            cur = best.get(r["wf"])
            if cur is None or r["jid"] > cur["jid"]:
                best[r["wf"]] = r
        return list(best.values())

    ok = _dedup(ok)
    bad = _dedup(bad)
    # 跑通的：有产物的才进 ok；bad 里排除已在 ok 的
    okw = {r["wf"] for r in ok}
    bad = [r for r in bad if r["wf"] not in okw]
    ok.sort(key=lambda r: (-(r["q"] or 0), r["wf"]))
    bad.sort(key=lambda r: (r["kind"] or "", r["why"], r["wf"]))
    return ok, bad


CSS = """
:root{color-scheme:dark;--bg:#0f1115;--card:#171a21;--line:#252a34;
--fg:#e6e8ee;--dim:#8b94a7;--ok:#3ddc97;--bad:#ff6b6b;--a:#ffd166;--b:#7cc4ff;--c:#c39bff;--d:#ff9f68}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);padding:16px 14px 60px;
font:16px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:17px;margin:30px 0 12px;padding-top:14px;border-top:1px solid var(--line)}
.sub{color:var(--dim);font-size:13px;margin-bottom:16px}
.stats{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 6px}
stat,.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:9px 13px;font-size:13px;color:var(--dim)}
.stat b{display:block;font-size:21px;color:var(--fg);font-weight:600}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(168px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;
display:flex;flex-direction:column}
.card img,.card video{width:100%;aspect-ratio:1/1;object-fit:cover;display:block;background:#0b0d11}
.card video{aspect-ratio:9/16}
.meta{padding:8px 10px;font-size:12px;line-height:1.45}
.name{font-size:12px;color:var(--fg);word-break:break-all;margin-bottom:5px}
.tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;font-weight:600}
.gA{background:rgba(255,209,102,.16);color:var(--a)}
.gB{background:rgba(124,196,255,.16);color:var(--b)}
.gC{background:rgba(195,155,255,.16);color:var(--c)}
.gD{background:rgba(255,159,104,.16);color:var(--d)}
.dim{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:500;font-size:12px}
code{font-size:11px;color:var(--b);word-break:break-all}
.badrow code{color:var(--dim)}
button{background:#1e2430;border:1px solid var(--line);color:var(--fg);
padding:7px 14px;border-radius:9px;font-size:13px;cursor:pointer}
.viewer{display:none;position:fixed;inset:0;background:rgba(0,0,0,.94);z-index:99;
padding:20px;overflow:auto}
.viewer.on{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px}
.viewer img,.viewer video{max-width:100%;max-height:82vh;object-fit:contain}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--b);
border-radius:8px;padding:11px 14px;font-size:13px;color:var(--dim);margin:14px 0}
"""

JS = """
function zoom(src,isVid){
 const v=document.getElementById('viewer');
 v.innerHTML = isVid
   ? '<video src="'+src+'" controls autoplay playsinline></video>'
   : '<img src="'+src+'">';
 v.classList.add('on');
}
function unzoom(){const v=document.getElementById('viewer');
 v.classList.remove('on');v.innerHTML='';}
"""


def render(ok: list[dict], bad: list[dict], date: str) -> str:
    base = base_url()
    n_a = sum(1 for r in ok if r["grade"] == "A")
    n_b = sum(1 for r in ok if r["grade"] == "B")
    n_img = sum(1 for r in ok if r["kind"] == "image")
    n_vid = sum(1 for r in ok if r["kind"] == "video")
    total = len(ok) + len(bad)

    L: list[str] = []
    A = L.append
    A("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>")
    A("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    A(f"<title>全量实测 · {date}</title>")
    A(f"<style>{CSS}</style></head><body>")
    A(f"<div id='viewer' class='viewer' onclick='unzoom()'></div>")
    A(f"<h1>全量工作流实测 · {date}</h1>")
    A("<div class='sub'>54 个工作流全部真机跑一遍，点图看原图、点视频直接播。</div>")

    A("<div class='stats'>")
    A(f"<div class='stat'><b>{len(ok)}/{total}</b>跑通</div>")
    A(f"<div class='stat'><b>{n_img}</b>图像</div>")
    A(f"<div class='stat'><b>{n_vid}</b>视频</div>")
    A(f"<div class='stat'><b>{n_a}</b>A 级</div>")
    A(f"<div class='stat'><b>{n_b}</b>B 级</div>")
    A(f"<div class='stat'><b>{len(bad)}</b>不可用</div>")
    A("</div>")

    A("<div class='note'>产物直接来自 <code>reports/p1/jobs/</code>，"
      "质量分是按边缘能量 + 运动量 + 黑边比例自动算的（不是人工打分）。"
      "C/D 级不等于坏图 —— 静态镜头在运动量上天然吃亏。</div>")

    A("<h2>跑通的（按质量分降序）</h2>")
    A("<div class='grid'>")
    for r in ok:
        art = r["art"]
        # 产物在 reports/p1/jobs/ 下，页面在 reports/lab/ 下。
        # **不用 ../**：虽然实测服务器会规范化并返回 200，但「页面里带路径穿越」
        # 会让任何一层静态校验（nginx、CDN、安全扫描）把它当成可疑请求拒掉，
        # 而且从 URL 上完全看不出真实位置。直接用站点根绝对路径。
        rel = f"reports/p1/jobs/{r['jid']}/artifacts/{art.name}"
        is_vid = art.suffix.lower() in VID_EXT
        if is_vid:
            poster = make_poster(art)
            ptxt = ""
            if poster:
                ptxt = f" poster='{base}reports/lab/{poster.relative_to(LAB).as_posix()}'"
            media = (f"<video src='{base}{rel}' controls preload='metadata'"
                     f"{ptxt} playsinline></video>")
        else:
            thumb = make_thumb(art)
            src = (f"{base}reports/lab/{thumb.relative_to(LAB).as_posix()}"
                   if thumb else f"{base}{rel}")
            media = (f"<img src='{src}' loading='lazy' "
                     f"onclick=\"zoom('{base}{rel}', false)\">")
        g = r["grade"] or "?"
        A("<div class='card'>")
        A(media)
        A("<div class='meta'>")
        A(f"<div class='name'>{html.escape(r['wf'])}</div>")
        A(f"<span class='tag g{g}'>{g} · {r['q']}</span> ")
        A(f"<span class='dim'>{r['bytes']/1024:.0f} KB</span>")
        A("</div></div>")
    A("</div>")

    A(f"<h2>还不可用的（{len(bad)} 条）</h2>")
    A("<div class='sub'>只列能用的等于骗自己 —— 下面每条都注明了卡在哪一步。</div>")
    A("<table><tr><th>工作流</th><th>类型</th><th>卡在</th></tr>")
    for r in bad:
        A(f"<tr class='badrow'><td><code>{html.escape(r['wf'])}</code></td>"
          f"<td class='dim'>{r['kind']}</td>"
          f"<td><span class='tag gD'>{html.escape(r['why'])}</span></td></tr>")
    A("</table>")

    A(f"<script>{JS}</script></body></html>")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="全量实测结果页")
    ap.add_argument("--date", default=None, help="作业日期前缀 YYYYMMDD，默认今天")
    ap.add_argument("--out", default=None, help="输出文件名")
    a = ap.parse_args(argv)

    import datetime as dt
    date = a.date or dt.datetime.now().astimezone().strftime("%Y%m%d")
    ok, bad = collect(date)
    if not ok and not bad:
        print(f"✗ {date} 没有作业")
        return 1

    LAB.mkdir(parents=True, exist_ok=True)
    out = LAB / (a.out or f"sweep-{date}.html")
    page = render(ok, bad, date)
    out.write_text(page, encoding="utf-8")

    tok = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.is_file() else ""
    print(f"✓ {out.relative_to(ROOT)}")
    print(f"  跑通 {len(ok)} · 不可用 {len(bad)} · {len(page)/1024:.0f} KB")
    if tok:
        print(f"  https://123.207.74.155/files/{tok}/f/reports/lab/{out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
