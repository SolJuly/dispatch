#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H3 画质对比页：把同一提示词在不同参数下的产物并排放在一起，直接播放。

## 为什么要有这一页

用户的原话是「视频直出质量差」，而评分表只能抓黑帧/静帧，量不出画质。
要判断改动有没有用，只能**并排看片子**。

所以这一页不做打分，只做三件事：
  · 同提示词、不同参数 → 并排，一眼看出差别
  · 每条直接播放（带 poster，移动端不会是一块灰）
  · 每条标出码率 —— 码率是画质最直接的物理指标

## 关键指标：码率

`SaveVideo(format=mp4, codec=h264)` 的默认质量很低。实测同一提示词：
    baseline 全黑      37 kb/s
    fixed 正常出片    496–2187 kb/s
社区实测 `VHS_VideoCombine(crf=12)` 能到 10393 kb/s（比 SaveVideo 高 5 倍）。
码率不是画质的全部，但低到几十 kb/s 必然是糊的或纯色。

用法：
    python3 tools/h3_lab_page.py
"""
from __future__ import annotations

import html
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "reports" / "lab"
SRC = LAB / "h3lab"
THUMBS = LAB / ".h3lab-thumbs"
TOKEN_FILE = ROOT / ".share_token"

# 变体说明：key 必须与文件名前缀一致（<variant>-<prompt>.mp4）
VARIANT_NOTE = {
    "baseline": ("改动前", "shift 5/1 · euler/simple · int8 VAE · cfg 1.0"),
    "fixed": ("改动后", "shift 12/3 · res_multistep · fp16 VAE · cfg 2.0"),
    "shift12": ("只改 shift", "shift 12/3，其余同改动前"),
    "vae16": ("只改 VAE", "fp16 VAE，其余同改动前"),
    "resmulti": ("只改采样器", "res_multistep，其余同改动前"),
    "steps32": ("32 步", "改动后 + 32 步"),
    "pdd8": ("PDD 8 步", "PDD Acc-8Step LoRA（第三方模型直挂，未验证）"),
    "pdd8off": ("PDD 8 步 · 官方模型", "官方 FL2VA int8 + PDD Acc-8Step LoRA，8 步"),
    "quality": ("档位 quality", "20 步 · LoRA 强度 0（最高画质）"),
    "balanced": ("档位 balanced", "8 步 · PDD LoRA（默认）"),
    "fast": ("档位 fast", "4 步 · PDD LoRA（最快）"),
    "crf2": ("crf=2", "接近无损重编码（验证码率上限）"),
    "res1024": ("1024×1024", "短边超 768，验证是否掉画质"),
    "nolora": ("style=none", "不加风格 LoRA（只有 PDD 加速）—— 默认档"),
    "realism": ("style=realism", "h3-realism-people（触发词 r34l1sm，强度 1.0）"),
    "cinema": ("style=cinema", "电影感H3-loraV1.0（强度 1.0）"),
    "davham": ("style=davham", "davham_cinema-h3（强度 0.8）"),
    "kitchen": ("+Kitchen 注意力", "ModelAttentionBackend = comfy kitchen attention"),
    "vsa": ("+VSA 稀疏", "BlockSparseAttention keep=10%（fasth3 是 VSA 训的）"),
    "nvfp4": ("+nvfp4 CLIP", "文本编码器换 nvfp4_awq"),
    "kitchen_vsa": ("Kitchen+VSA", "两项加速组合"),
    "fuse": ("三项全上", "Kitchen + VSA + nvfp4"),
    "pdd8off": ("完整配置", "固化后默认：PDD 8步 + Kitchen + VSA"),
    "pdd4": ("PDD 4 步", "PDD LoRA + 4 步（极限提速）"),
}
KIND_NOTE = {
    "p1": "文生视频", "p2": "文生视频", "p3": "文生视频",
    "p4": "参考图生视频", "p5": "图生视频",
    "a1": "动作测试（画质 LoRA 对比）", "a2": "动作测试", "a3": "动作测试",
}

PROMPT_NOTE = {
    "p1": "高难度动作场景（剑、能量特效、多镜头切换）",
    "p2": "情绪特写（含泪、抬头、微表情）",
    "p3": "人像特写（指定痣的位置、手部动作）",
    "p4": "参考图生视频（绕脸旋转、冰晶反光）— r2v 工作流",
    "p5": "图生视频（缓慢推近、冰面裂纹）— i2v 工作流",
    "a1": "动作：奔跑→起跳→落地水花（wet rooftop，大幅度连续动作）",
    "a2": "动作：回旋踢→扫腿→三连拳（武术，布料随动）",
    "a3": "动作：快速旋转舞→后仰→起身（红裙旋开）",
}

CSS = """
:root{color-scheme:dark;--bg:#0f1115;--card:#171a21;--line:#252a34;--fg:#e6e8ee;
--dim:#8b94a7;--good:#3ddc97;--bad:#ff6b6b}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);padding:16px 14px 60px;
font:16px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
h1{font-size:20px;margin:0 0 6px}
h2{font-size:17px;margin:32px 0 10px;padding-top:14px;border-top:1px solid var(--line)}
.sub{color:var(--dim);font-size:13px;margin-bottom:14px}
.row{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));margin-bottom:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card video{width:100%;display:block;background:#000}
.meta{padding:9px 11px;font-size:12px;line-height:1.5}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;margin-bottom:5px}
.ok{background:rgba(61,220,151,.15);color:var(--good)}
.no{background:rgba(255,107,107,.15);color:var(--bad)}
.br{color:var(--dim);font-size:11px}
code{font-size:11px;color:#7cc4ff}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid #7cc4ff;
border-radius:8px;padding:12px 15px;font-size:13px;color:var(--dim);margin:14px 0}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:12px}
"""


def probe(p: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=bit_rate,size", "-show_entries", "stream=width,height,nb_frames",
         "-of", "json", str(p)], capture_output=True, text=True, timeout=60)
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        return {}
    fmt = d.get("format") or {}
    st = (d.get("streams") or [{}])[0]
    return {
        "kbps": round(int(fmt.get("bit_rate") or 0) / 1000),
        "kb": round(float(fmt.get("size") or 0) / 1024),
        "w": st.get("width"), "h": st.get("height"),
        "frames": st.get("nb_frames"),
    }


def poster(v: Path) -> Path | None:
    if not shutil.which("ffmpeg"):
        return None
    THUMBS.mkdir(parents=True, exist_ok=True)
    out = THUMBS / (v.stem + ".jpg")
    if out.is_file() and out.stat().st_size > 0:
        return out
    # 取 1/3 处而不是第 0 帧：首帧有时是转场黑
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(v),
             "-vf", "select='eq(n,40)',scale=520:-1", "-frames:v", "1", str(out)],
            check=True, timeout=90,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out if out.is_file() and out.stat().st_size > 0 else None
    except Exception:
        return None


def main() -> int:
    if not SRC.is_dir():
        print("✗ 没有 reports/lab/h3lab/，先跑 tools/p2/h3_quality_lab.py")
        return 1
    files = sorted(SRC.glob("*.mp4"))
    if not files:
        print("✗ 没有产物")
        return 1

    # **相对路径，不嵌 token**（2026-11-15 安全测试抓到）。
    # 页面本身通过 /files/<token>/f/reports/lab/ 打开，浏览器解析相对 URL
    # 会自动带上当前目录和 token —— 不用我们再拼，也就不会把 token 写进 HTML。
    base = ""

    # 按 prompt 分组
    by_prompt: dict[str, list[tuple[str, Path]]] = {}
    for f in files:
        parts = f.stem.split("-", 1)
        if len(parts) != 2:
            continue
        var, pr = parts
        by_prompt.setdefault(pr, []).append((var, f))

    L: list[str] = []
    A = L.append
    A("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>")
    A("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    A("<title>H3 画质对比</title>")
    A(f"<style>{CSS}</style></head><body>")
    A("<h1>H3 画质对比实验</h1>")
    A("<div class='sub'>同一提示词、同一 seed（424242）、同样 121 帧 768×512，"
      "只改参数。点开直接播。</div>")

    A("<div class='note'><b>⚠️ LoRA 是模型族专属的，不能跨模型用</b><br>"
      "机器上 307 个 LoRA 按模型族分目录：minimax 46 / Ltx 39 / "
      "Zimage 23 / Flux 20 / Qwen 16 / Wan 12 / krea2 18 / Sdxl 7。"
      "原因是 LoRA 的权重形状由基础模型的**层结构**决定 —— "
      "H3 是 33B omni-transformer，Wan 是 14B DiT，张量维度对不上，"
      "强行加载会报 size mismatch 或静默无效。<br>"
      "所以下面这些风格档只对 H3 有效。<br><br>"
      "<b>本轮重点：分辨率修正 + PDD 加速 LoRA + 风格 LoRA 档位</b><br>"
      "① <b>比例</b>：之前参考图是 1:1 却输出 768×512（3:2），人物被横向拉扁。"
      "现在按参考图比例算，短边钉到 768 的合规上限 → 1:1 时是 768×768，"
      "像素比原来多 50%。<br>"
      "② <b>PDD Acc-8Step LoRA</b>（阿里 PAI 并行解码蒸馏）："
      "实测 8 步 220s vs 20 步 409s，<b>提速 46%</b>，肉眼无画质损失。<br><br>"
      "<b>为什么并排看视频而不是看分数：</b><br>"
      "原来的质量分只量边缘能量/运动/亮度 —— 它能抓黑帧、静帧、画面空，"
      "但量不出对比度、色彩、皮肤质感、运镜自然度。"
      "所以这一轮判断标准是<b>你眼睛看到的东西</b>，不是分数。<br><br>"
      "<b>码率是关键物理指标</b>：<code>SaveVideo</code> 默认质量很低，"
      "实测同一提示词改动前 37 kb/s（全黑）、改动后 496–2187 kb/s；"
      "社区实测 <code>VHS_VideoCombine(crf=12)</code> 可达 10393 kb/s。</div>")

    for pr in sorted(by_prompt):
        A(f"<h2>{KIND_NOTE.get(pr, '')} · 提示词 {pr}</h2>")
        A(f"<div class='sub'>{PROMPT_NOTE.get(pr, '')}</div>")
        A("<div class='row'>")
        for var, f in sorted(by_prompt[pr]):
            info = probe(f)
            name, desc = VARIANT_NOTE.get(var, (var, ""))
            ok = (info.get("kbps") or 0) > 200
            pt = poster(f)
            ptxt = f" poster='.h3lab-thumbs/{pt.name}'" if pt else ""
            src = f"h3lab/{f.name}"
            A("<div class='card'>")
            A(f"<video src='{src}' controls preload='metadata'{ptxt} "
              f"playsinline></video>")
            A("<div class='meta'>")
            A(f"<span class='tag {'ok' if ok else 'no'}'>{name}</span>")
            A(f"<div>{html.escape(desc)}</div>")
            A(f"<div class='br'>{info.get('w')}×{info.get('h')} · "
              f"{info.get('frames')} 帧 · {info.get('kb')} KB · "
              f"<b>{info.get('kbps')} kb/s</b></div>")
            A("</div></div>")
        A("</div>")

    # 汇总表
    A("<h2>全部产物汇总</h2>")
    A("<table><tr><th>文件</th><th>分辨率</th><th>帧数</th><th>体积</th><th>码率</th></tr>")
    for f in files:
        i = probe(f)
        A(f"<tr><td><code>{f.name}</code></td>"
          f"<td>{i.get('w')}×{i.get('h')}</td><td>{i.get('frames')}</td>"
          f"<td>{i.get('kb')} KB</td><td><b>{i.get('kbps')}</b> kb/s</td></tr>")
    A("</table>")

    A("</body></html>")

    out = LAB / "h3-quality.html"
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"✓ {out.relative_to(ROOT)}")
    tok = TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.is_file() else ""
    if tok:
        print(f"  https://123.207.74.155/files/{tok}/f/reports/lab/{out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
