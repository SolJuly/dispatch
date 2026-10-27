#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 reports/lab 的产物做成可读的对照页面：缩略图 + 指标表 + 点击看原图。

用法: python3 tools/gallery.py            # 生成 reports/lab/gallery.html（含缩略图）
"""
from __future__ import annotations

import html
import json
import os
import base64
import shutil
import subprocess
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAB = ROOT / "reports" / "lab"
THUMB = LAB / ".thumbs"
STATE = ROOT / "lab" / "state.json"
TOKEN_FILE = ROOT / ".share_token"


def base_url() -> str:
    """图片绝对路径前缀：同源根路径，无论从 /files/ 还是从 GUI 打开都能加载。"""
    tok = ""
    try:
        tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return f"/files/{tok}/f/reports/lab/" if tok else ""


BASE = base_url()

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VID_EXT = {".mp4", ".webm", ".gif"}

MODEL_CN = {"sd15": "SD1.5 · ghostmix", "sdxl": "SDXL · juggernaut",
            "flux": "FLUX.1-dev", "wan_i2v": "Wan2.1 图生视频"}
CONF_CN = {"base": "标准", "fast": "少步数", "big": "高分辨率"}


def absu(rel: str) -> str:
    """相对 lab 的路径 → 同源绝对路径。"""
    return BASE + urllib.parse.quote(rel, safe="/@")


def thumb(src: Path) -> str | None:
    """生成 480 宽缩略图，返回相对路径（失败则返回 None）。"""
    rel = src.relative_to(LAB)
    dst = THUMB / rel.with_suffix(".jpg")
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return os.path.relpath(dst, LAB)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                            "-vf", "scale=520:-1", "-q:v", "4", str(dst)],
                           capture_output=True)
        if r.returncode == 0 and dst.exists():
            return os.path.relpath(dst, LAB)
    return None


def small_thumb(src: Path, width: int = 300) -> bytes | None:
    """更小的缩略图（内嵌用），返回 JPEG 字节。"""
    if not shutil.which("ffmpeg"):
        return None
    out = THUMB / "inline" / (src.relative_to(LAB).as_posix().replace("/", "_") + ".jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                        "-vf", f"scale={width}:-2", "-q:v", "7", str(out)],
                       capture_output=True)
    return out.read_bytes() if out.exists() else None


def contact_sheet(images: list[Path], out: Path, height: int = 420) -> bytes | None:
    """把一行样本拼成一张横向长图，方便并排看。"""
    images = [p for p in images if p.exists()][:6]
    if len(images) < 2 or not shutil.which("ffmpeg"):
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for p in images:
        cmd += ["-i", str(p)]
    filters = "".join(f"[{i}:v]scale=-2:{height},setsar=1[v{i}];" for i in range(len(images)))
    filters += "".join(f"[v{i}]" for i in range(len(images))) + f"hstack=inputs={len(images)}"
    cmd += ["-filter_complex", filters, "-frames:v", "1", str(out)]
    r = subprocess.run(cmd, capture_output=True)
    return out.read_bytes() if r.returncode == 0 and out.exists() else None


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"groups": {}}


def collect() -> tuple[dict, list]:
    """返回 ({model: [item]}, rows)"""
    groups = load_state().get("groups", {})
    by_model: dict[str, list] = {}
    rows = []
    for gid, g in groups.items():
        for wname, w in (g.get("workloads") or {}).items():
            model = wname.split("_")[0] if wname.split("_")[0] in MODEL_CN else (
                w.get("model") or "other")
            conf = wname.split("_", 1)[1] if "_" in wname else ""
            rows.append({
                "gid": gid, "gpu": g.get("gpu"), "zone": g.get("zone"),
                "model": model, "workload": wname, "conf": conf,
                "status": w.get("status"),
                "cold": w.get("t_cold_s"), "warm": w.get("t_warm_s"),
                "vram": (w.get("vram") or {}).get("vram_peak_mib"),
                "reason": (w.get("reason") or "")[:90],
                "cost": g.get("cost_actual"),
            })
            for a in w.get("artifacts") or []:
                p = LAB / gid / wname / a["file"]
                if not p.exists():
                    continue
                by_model.setdefault(model, []).append({
                    "gpu": g.get("gpu"), "zone": g.get("zone"), "gid": gid,
                    "workload": wname, "conf": conf,
                    "cold": w.get("t_cold_s"), "warm": w.get("t_warm_s"),
                    "vram": (w.get("vram") or {}).get("vram_peak_mib"),
                    "src": os.path.relpath(p, LAB),
                    "thumb": thumb(p),
                    "w": a.get("width"), "h": a.get("height"),
                    "kb": round((a.get("size") or 0) / 1024),
                })
    return by_model, rows


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box;-webkit-tap-highlight-color:rgba(80,160,255,.2)}
body{margin:0;background:#0d1013;color:#e8ebf2;font:16px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
a{color:#6cb6ff;text-decoration:none}
header{position:sticky;top:0;z-index:9;padding:12px 14px;background:rgba(13,16,19,.95);
 backdrop-filter:blur(8px);border-bottom:1px solid #222833;display:flex;gap:10px;align-items:center}
header .t{font-weight:700;font-size:17px;flex:1}
.btn{padding:8px 12px;border-radius:10px;background:#1a2331;border:1px solid #2b3550;font-size:14px}
main{max-width:1100px;margin:0 auto;padding:14px 14px 50px}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:8px 0 18px}
.kpi div{background:#151a22;border:1px solid #222833;border-radius:12px;padding:10px 12px}
.kpi b{display:block;font-size:20px;color:#9fd0ff}
.kpi span{color:#8b93a7;font-size:12px}
h2{font-size:18px;margin:26px 0 6px;padding-top:12px;border-top:1px solid #222833}
.hint{color:#8b93a7;font-size:13px;margin:2px 0 10px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.card{background:#151a22;border:1px solid #222833;border-radius:12px;overflow:hidden}
.card a.img{display:block;background:#0a0d10}
.card img{width:100%;display:block;aspect-ratio:1/1;object-fit:cover}
.meta{padding:8px 10px;font-size:13px;color:#9aa3b5}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
 background:#1d2a3f;color:#9fd0ff;border:1px solid #2b3f5c;margin-right:6px}
.badge.g{background:#2a2317;color:#ffcf8a;border-color:#4a3b23}
.tw{overflow:auto;border:1px solid #222833;border-radius:12px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid #222833;text-align:left;white-space:nowrap}
th{background:#161e29;position:sticky;top:0}
td.ok{color:#8ee6a8}td.failed,td.skipped{color:#8b93a7}
.cmp{display:flex;gap:12px;overflow-x:auto;padding:4px 2px 10px}
.cc{flex:0 0 240px;background:#151a22;border:1px solid #222833;border-radius:12px;overflow:hidden}
.cc img{width:100%;display:block;aspect-ratio:1/1;object-fit:cover}
.cc .meta{padding:8px 10px;font-size:12px;color:#9aa3b5;line-height:1.5}
.broken{aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;
 background:#10151b;color:#6b7385;font-size:12px;text-align:center;padding:8px}
.card a.img{position:relative}
.card a.img::after{content:'⤢';position:absolute;right:8px;bottom:8px;width:26px;height:26px;
 border-radius:50%;background:rgba(10,13,16,.75);color:#cfe0ff;font-size:14px;display:flex;
 align-items:center;justify-content:center;border:1px solid #2b3f5c}
.lb{position:fixed;inset:0;z-index:99;background:rgba(6,8,10,.96);display:flex;flex-direction:column}
.lb[hidden]{display:none}
.lbbar{display:flex;gap:8px;align-items:center;padding:10px 12px;color:#cfd6e4;font-size:13px}
.lbbar .t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lbbar .btn{padding:7px 12px;border-radius:9px;background:#1a2331;border:1px solid #2b3550;color:#dfe7f5}
.lbimgwrap{flex:1;display:flex;align-items:center;justify-content:center;overflow:auto;padding:0 8px 12px;
 background:repeating-conic-gradient(#12161c 0% 25%, #0d1013 0% 50%) 50%/24px 24px}
.lbimgwrap img{max-width:100%;max-height:100%;object-fit:contain;border-radius:8px;
 box-shadow:0 8px 40px rgba(0,0,0,.6)}
.lbnav{position:absolute;inset:56px 0 0;display:flex;justify-content:space-between;pointer-events:none}
.lbnav button{pointer-events:auto;align-self:center;margin:0 10px;width:44px;height:64px;border-radius:12px;
 background:rgba(26,35,49,.85);border:1px solid #2b3550;color:#dfe7f5;font-size:26px;line-height:1}
.lbload{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);color:#9aa3b5;font-size:14px}
"""


def main() -> int:
    by_model, rows = collect()
    total_img = sum(len(v) for v in by_model.values())
    groups = load_state().get("groups", {})
    done = [g for g in groups.values() if g.get("status") == "ok"]
    cost = sum((g.get("cost_actual") or 0) or 0 for g in groups.values())
    best = {}
    for r in rows:
        if r["status"] == "ok" and r["warm"]:
            k = r["model"]
            if k not in best or r["warm"] < best[k]["warm"]:
                best[k] = r

    p = ["<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<meta name='color-scheme' content='dark'><title>GPU 基准对照</title>",
         f"<style>{CSS}</style></head><body>",
         "<header><span class='t'>GPU 基准对照</span>",
         "<a class='btn' href='REPORT.md'>原始数据表</a></header><main>"]

    p.append("<div class='kpi'>")
    p.append(f"<div><b>{len(done)}</b><span>已完成机器</span></div>")
    p.append(f"<div><b>{total_img}</b><span>成品图/视频</span></div>")
    p.append(f"<div><b>{len(by_model)}</b><span>模型</span></div>")
    p.append(f"<div><b>¥{cost:.2f}</b><span>实扣总成本</span></div>")
    p.append("</div>")
    p.append("<div class='hint'>点任意图片看原图（缩略图是 520px，原图 1MB 左右）。"
             "同一模型内左右对比即可看出画质/细节差异；时间与显存看下方表格。</div>")

    # 每模型最快记录
    if best:
        p.append("<div class='kpi'>")
        for m, r in best.items():
            p.append(f"<div><b>{r['warm']:.1f}s</b><span>{MODEL_CN.get(m, m)} 最快预热"
                     f" · {r['gpu']}</span></div>")
        p.append("</div>")

    # 同模型跨卡并排对比
    rows_by_key: dict[tuple, list] = {}
    for model, items in by_model.items():
        for it in items:
            rows_by_key.setdefault((model, it["workload"]), []).append(it)
    cmp_rows = dict(rows_by_key)
    if cmp_rows:
        p.append("<h2>同模型 · 不同卡并排对比</h2>")
        p.append("<div class='hint'>同一模型、同一配置，左边是 4090，右边是其它卡；横向滑动即可对照。"
                 "点图可放大到原图。</div>")
        order = {"4090": 0, "3090": 1, "V100S": 2, "5090": 3, "4090_48G": 4}
        for (model, wl), items in sorted(cmp_rows.items(), key=lambda kv: (kv[0][0], kv[0][1])):
            items.sort(key=lambda x: (order.get(x["gpu"], 9), x["zone"]))
            conf = CONF_CN.get(items[0]["conf"], items[0]["conf"])
            gpus = sorted({x["gpu"] for x in items}, key=lambda g: order.get(g, 9))
            p.append(f"<h3 style='font-size:15px;margin:16px 0 6px'>"
                     f"{html.escape(MODEL_CN.get(model, model))} · {html.escape(conf)}"
                     f"<span class='hint'>（{' / '.join(gpus)} · {len(items)} 个样本）</span></h3>")
            p.append("<div class='cmp'>")
            for it in items:
                src = it["thumb"] or it["src"]
                p.append(
                    "<div class='cc'>"
                    f"<a class='img shot' href='{absu(it['src'])}' data-full='{absu(it['src'])}' "
                    f"data-cap='{html.escape(it['gpu']+' · '+it['zone']+' · 冷启 ' + str(it['cold']) + 's / 预热 ' + str(it['warm']) + 's')}'>"
                    f"<img loading='lazy' src='{absu(src)}' alt='{html.escape(it['gpu'])}'></a>"
                    f"<div class='meta'><b>{html.escape(it['gpu'])}</b> · {html.escape(it['zone'])}<br>"
                    f"冷启 {it['cold'] if it['cold'] is not None else '-'}s · "
                    f"预热 {it['warm'] if it['warm'] is not None else '-'}s · 显存 {it['vram'] or '-'}MiB"
                    "</div></div>")
            p.append("</div>")


    # 指标表
    p.append("<h2>全部负载指标</h2><div class='tw'><table><thead><tr>"
             "<th>机器</th><th>负载</th><th>状态</th><th>冷启 s</th><th>预热 s</th>"
             "<th>显存 MiB</th><th>备注</th></tr></thead><tbody>")
    for r in sorted(rows, key=lambda x: (x["model"], x["gid"], x["workload"])):
        st = r["status"] or ""
        p.append(f"<tr><td>{html.escape(r['gid'])}</td><td>{html.escape(r['workload'])}</td>"
                 f"<td class='{st}'>{st}</td><td>{r['cold'] if r['cold'] is not None else '-'}</td>"
                 f"<td>{r['warm'] if r['warm'] is not None else '-'}</td>"
                 f"<td>{r['vram'] or '-'}</td><td style='color:#8b93a7'>{html.escape(r['reason'])}</td></tr>")
    p.append("</tbody></table></div>")
    p.append("<div class='hint'>生成时间：自动 / 每次跑完一台机器刷新。"
             "数据源：<code>lab/state.json</code>；原图在 <code>reports/lab/&lt;机器&gt;/&lt;负载&gt;/</code></div>")
    p.append("""
<div class="lb" id="lb" hidden>
  <div class="lbbar">
    <span class="t" id="lbtitle"></span>
    <a class="btn" id="lbdl" href="#">下载原图</a>
    <button class="btn" id="lbclose">关闭 ✕</button>
  </div>
  <div class="lbimgwrap"><img id="lbimg" alt=""><video id="lbvid" controls loop muted playsinline
   style="max-width:100%;max-height:100%;display:none;border-radius:8px"></video>
   <div class="lbload" id="lbload">加载中…</div></div>
  <div class="lbnav"><button id="lbprev">‹</button><button id="lbnext">›</button></div>
</div>
<script>
(function(){
  var shots=[].slice.call(document.querySelectorAll('a.shot'));
  var lb=document.getElementById('lb'), img=document.getElementById('lbimg'),
      vid=document.getElementById('lbvid'),
      title=document.getElementById('lbtitle'), dl=document.getElementById('lbdl'),
      load=document.getElementById('lbload'), idx=-1;
  function show(i){
    if(i<0||i>=shots.length) return;
    idx=i; var a=shots[i];
    var full=a.getAttribute('data-full');
    title.textContent=(i+1)+'/'+shots.length+'  '+a.getAttribute('data-cap');
    dl.setAttribute('href', full+(full.indexOf('?')<0?'?dl=1':''));
    var isVid=/\.(webp|mp4|webm|gif)$/i.test(full);
    load.style.display='block';
    if(isVid){ img.style.display='none'; vid.style.display='block'; vid.src=full; vid.play().catch(function(){}); }
    else { vid.style.display='none'; vid.removeAttribute('src'); img.style.display='block';
           img.style.opacity=.25; img.src=full; }
    lb.hidden=false; document.body.style.overflow='hidden';
  }
  function hide(){ lb.hidden=true; img.removeAttribute('src'); vid.pause();
    vid.removeAttribute('src'); document.body.style.overflow=''; }
  img.addEventListener('load', function(){ load.style.display='none'; img.style.opacity=1; });
  vid.addEventListener('loadeddata', function(){ load.style.display='none'; });
  img.addEventListener('error', function(){ load.textContent='加载失败，点“下载原图”试试'; });
  shots.forEach(function(a,i){ a.addEventListener('click', function(e){ e.preventDefault(); show(i); }); });
  document.getElementById('lbclose').addEventListener('click', hide);
  document.getElementById('lbprev').addEventListener('click', function(){ show(idx-1); });
  document.getElementById('lbnext').addEventListener('click', function(){ show(idx+1); });
  lb.addEventListener('click', function(e){ if(e.target===lb||e.target.className==='lbimgwrap') hide(); });
  document.addEventListener('keydown', function(e){
    if(lb.hidden) return;
    if(e.key==='Escape') hide(); if(e.key==='ArrowLeft') show(idx-1); if(e.key==='ArrowRight') show(idx+1);
  });
  var x0=null;
  lb.addEventListener('touchstart', function(e){ x0=e.touches[0].clientX; }, {passive:true});
  lb.addEventListener('touchend', function(e){
    if(x0===null) return; var dx=e.changedTouches[0].clientX-x0; x0=null;
    if(Math.abs(dx)>60){ dx>0 ? show(idx-1) : show(idx+1); }
  }, {passive:true});
})();
</script>
</main></body></html>""")
    (LAB / "gallery.html").write_text("".join(p), encoding="utf-8")

    # ---------- 自包含版（图片内嵌 base64，任何环境都能看） ----------
    q = ["<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>GPU 基准（自包含）</title>",
         f"<style>{CSS}"
         ".diag{background:#1b2a1f;border:1px solid #2e6b3f;border-radius:10px;padding:10px 12px;"
         "font-size:13px;color:#a9e6bd;margin:10px 0}</style></head><body>",
         "<header><span class='t'>GPU 基准对照（图片内嵌，无需联网）</span>",
         "<a class='btn' href='gallery.html'>联网版</a></header><main>",
         "<div class='diag'>诊断：如果你能看到这段绿框文字，但下面空白 → HTML 被过滤了；"
         "如果能看到缩略图 → 图片通路正常，之前是链接问题。</div>"]
    q.append("<div class='kpi'>")
    q.append(f"<div><b>{len(done)}</b><span>机器</span></div>")
    q.append(f"<div><b>{total_img}</b><span>成品</span></div>")
    q.append(f"<div><b>¥{cost:.2f}</b><span>实扣</span></div>")
    q.append("</div>")

    order_g = {"4090": 0, "3090": 1, "V100S": 2, "5090": 3, "4090_48G": 4}
    rows2: dict[tuple, list] = {}
    for model, items in by_model.items():
        for it in items:
            rows2.setdefault((model, it["workload"]), []).append(it)
    for (model, wl), items in sorted(rows2.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        items.sort(key=lambda x: (order_g.get(x["gpu"], 9), x["zone"]))
        conf = CONF_CN.get(items[0]["conf"], items[0]["conf"])
        gpus = sorted({x["gpu"] for x in items}, key=lambda g: order_g.get(g, 9))
        q.append(f"<h3 style='font-size:15px;margin:18px 0 8px'>"
                 f"{html.escape(MODEL_CN.get(model, model))} · {html.escape(conf)}"
                 f"<span class='hint'>（{' / '.join(gpus)}）</span></h3>")
        sheet = contact_sheet([LAB / it["gid"] / wl / it["file"] for it in items
                               if "file" in it] if all("file" in it for it in items) else
                              [LAB / it["gid"] / wl / os.path.basename(it["src"]) for it in items],
                              LAB / "compare" / f"{model}_{wl}.jpg")
        if sheet:
            b64 = base64.b64encode(sheet).decode()
            q.append(f"<div class='card' style='margin-bottom:10px'>"
                     f"<img src='data:image/jpeg;base64,{b64}' style='width:100%;aspect-ratio:auto'>"
                     f"<div class='meta'>并排长图：{' / '.join(it['gpu']+'·'+it['zone'] for it in items)}</div></div>")
        q.append("<div class='cmp'>")
        for it in items:
            data = small_thumb(LAB / it["gid"] / wl / os.path.basename(it["src"]))
            if not data:
                continue
            b64 = base64.b64encode(data).decode()
            q.append("<div class='cc'>"
                     f"<img src='data:image/jpeg;base64,{b64}' alt='{html.escape(it['gpu'])}'>"
                     f"<div class='meta'><b>{html.escape(it['gpu'])}</b> · {html.escape(it['zone'])}<br>"
                     f"冷启 {it['cold'] if it['cold'] is not None else '-'}s · "
                     f"预热 {it['warm'] if it['warm'] is not None else '-'}s<br>"
                     f"<a href='{absu(it['src'])}' target='_blank'>看原图</a></div></div>")
        q.append("</div>")
    q.append("</main></body></html>")
    (LAB / "gallery-inline.html").write_text("".join(q), encoding="utf-8")
    # 生成后立刻同步到平板窗口，避免窗口里是旧版本
    try:
        subprocess.run(["bash", str(ROOT / "scripts" / "sync_share.sh")],
                       capture_output=True, timeout=300)
    except Exception as e:
        print(f"同步失败: {e}")
    print(f"gallery: {len(by_model)} 模型 / {total_img} 张 / "
          f"缩略图 {sum(1 for _ in THUMB.rglob('*.jpg')) if THUMB.exists() else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
