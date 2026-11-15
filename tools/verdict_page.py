#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拆解报告页：把「修前 / 修后」两版成片并排放，给用户自己看。

## 为什么单独做页面而不是写 markdown

用户是在平板上看。markdown 里的 <video> 不会播，得点开再返回。
这个页面是：两版并排、自动循环、直接播，手指划过去就能对比。

## token 处理

**不能**把 share token 写进 HTML —— 生成的文件会进仓库，
`test_no_token_in_repo_files` 会拦。所以全部用相对路径：
页面在 `reports/lab/verdict.html`，视频在 `reports/lab/verdict/*.mp4`，
引用写 `verdict/xxx.mp4` 即可（fileserver 按同目录解析）。
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "reports" / "lab"
VID = LAB / "verdict"

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:20px;background:#0e1116;color:#dfe6ee;
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
h1{font-size:23px;margin:0 0 6px}
h2{font-size:17px;margin:34px 0 12px;padding-bottom:7px;border-bottom:1px solid #222b36}
.sub{color:#8595a8;font-size:13px;margin-bottom:20px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:26px}
@media(max-width:760px){.pair{grid-template-columns:1fr}}
.side{background:#151a22;border:1px solid #232c38;border-radius:10px;overflow:hidden}
.side.before{border-color:#5a2b2b}
.side.after{border-color:#1f6b45}
.hd{padding:9px 13px;font-size:13px;font-weight:600;display:flex;
 justify-content:space-between;align-items:center}
.before .hd{background:#2a1717;color:#ff9d9d}
.after .hd{background:#11251b;color:#66d99b}
.badge{font-weight:400;font-size:12px;opacity:.85;font-variant-numeric:tabular-nums}
video{width:100%;display:block;background:#000}
.spec{padding:10px 13px;font-size:12.5px;color:#8595a8;border-top:1px solid #1d2530}
.spec b{color:#c8d4e2;font-weight:600}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:10px 0 22px}
th,td{text-align:left;padding:8px 11px;border-bottom:1px solid #1d2530}
th{color:#8595a8;font-weight:500;font-size:12.5px}
td.num{font-variant-numeric:tabular-nums}
code{background:#1b222c;padding:1px 5px;border-radius:4px;font-size:12.5px;color:#8fb8de}
.box{background:#151a22;border:1px solid #232c38;border-left:3px solid #d29922;
 border-radius:8px;padding:13px 16px;margin:16px 0;font-size:13.5px}
.box.good{border-left-color:#2ea043}
.box .t{font-weight:600;margin-bottom:5px;color:#e6edf3}
ul{margin:7px 0;padding-left:20px}
li{margin:5px 0}
.foot{color:#5d6b7d;font-size:12px;margin-top:36px;padding-top:16px;border-top:1px solid #1d2530}
"""

JS = """
document.querySelectorAll('video').forEach(v=>{
  v.muted=true; v.loop=true; v.play().catch(()=>{});
});
document.addEventListener('visibilitychange',()=>{
  document.querySelectorAll('video').forEach(v=>{
    if(document.hidden) v.pause(); else v.play().catch(()=>{});
  });
});
"""


def probe(p: Path) -> dict:
    """ffprobe 拿宽高/帧数 —— 尺寸是这次修的核心证据，必须写在页面上。"""
    import json
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,nb_frames",
             "-of", "json", str(p)],
            capture_output=True, text=True, timeout=20, check=True).stdout
        s = (json.loads(out).get("streams") or [{}])[0]
        return {"w": s.get("width"), "h": s.get("height"),
                "frames": s.get("nb_frames"), "bytes": p.stat().st_size}
    except Exception:
        return {"w": None, "h": None, "frames": None,
                "bytes": p.stat().st_size if p.exists() else 0}


CASES = [
    {
        "key": "scail2",
        "name": "角色替换 · SCAIL-2",
        "why": "分辨率写死成 896×512（横屏）+ crop=center。"
               "驱动视频是 720×1280 竖屏，被放大到 896×1592 后中心裁成 896×512 —— "
               "垂直方向只留下 32%，人脸直接切在画面外。",
        "score": ("62.0", "92.2"),
        "fields": [
            ("成片尺寸", "896×512 横屏", "480×896 竖屏"),
            ("景别", "只剩躯干，头被裁掉", "全身完整"),
            ("清晰度指标", "0.969", "2.06"),
            ("画面空洞占比", "19.4%", "3.0%"),
            ("推理耗时", "180s", "176s"),
        ],
    },
    {
        "key": "a2",
        "name": "动作迁移 · Wan Animate 2",
        "why": "比例本来就接近（482×854 vs 720×1280），不裁。"
               "但产物里混进两条调试分支：一条输出「原片|成片」左右拼接的对照图，"
               "还被当成正式产物拉回来；另一条从 SaveVideo 反向取 fps/audio。"
               "另外显存缓存用的是 int8（有损）。",
        "score": ("77.0", "80.6"),
        "fields": [
            ("成片尺寸", "480×848", "496×896"),
            ("产物数量", "4 个（含拼接对照图）", "2 个（只有成片）"),
            ("图节点数", "62", "49"),
            ("显存缓存", "gpu / int8（有损）", "cpu / 全精度"),
            ("推理耗时", "172s", "206s"),
        ],
    },
]


def build() -> str:
    L: list[str] = []
    A = L.append
    A("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>")
    A("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    A("<title>两条工作流拆解 · 修前 vs 修后</title>")
    A(f"<style>{CSS}</style></head><body>")

    A("<h1>两条工作流拆解 · 修前 vs 修后</h1>")
    A("<div class='sub'>同一份素材、同一张卡（4090）、同 seed。"
      "左边是修之前跑出来的，右边是刚跑的。</div>")

    A("<div class='box'><div class='t'>先说结论</div>"
      "这两条都不是「模型不行」，是<b>图里把分辨率写死了</b>，配 <code>crop=center</code>，"
      "竖屏素材被腰斩。SCAIL2 尤其严重 —— 输出里根本看不到人脸。"
      "<br><br>有意思的是官方节点的默认值本身就是反的："
      "<code>WanSCAILToVideo</code> 默认 <code>width=512, height=896</code>（竖屏），"
      "图里却写成了横的 896×512。</div>")

    for c in CASES:
        A(f"<h2>{html.escape(c['name'])}</h2>")
        A("<div class='pair'>")
        for side, label, score in (("before", "修之前", c["score"][0]),
                                   ("after", "修之后", c["score"][1])):
            f = VID / f"{c['key']}-{side}.mp4"
            if not f.exists():
                A(f"<div class='side {side}'><div class='hd'>{label}"
                  f"<span class='badge'>缺文件</span></div></div>")
                continue
            pr = probe(f)
            dims = f"{pr['w']}×{pr['h']}" if pr["w"] else "—"
            A(f"<div class='side {side}'>")
            A(f"<div class='hd'>{label}"
              f"<span class='badge'>评分 {score} · {dims} · "
              f"{pr['bytes']/1024:.0f} KB</span></div>")
            A(f"<video src='verdict/{c['key']}-{side}.mp4' controls muted loop "
              f"playsinline preload='metadata'></video>")
            A(f"<div class='spec'><b>{dims}</b> · {pr['frames']} 帧</div>")
            A("</div>")
        A("</div>")

        A(f"<div class='box'><div class='t'>病在哪</div>{html.escape(c['why'])}</div>")
        A("<table><tr><th>　</th><th>修之前</th><th>修之后</th></tr>")
        for k, b, a in c["fields"]:
            A(f"<tr><td>{html.escape(k)}</td>"
              f"<td class='num'>{html.escape(b)}</td>"
              f"<td class='num'>{html.escape(a)}</td></tr>")
        A("</table>")

    A("<h2>改了什么</h2>")
    A("<div class='box good'><div class='t'>两份图</div><ul>"
      "<li>分辨率从写死改成 <code>scale longer dimension</code> —— 原生保持原比例</li>"
      "<li>后面串一个 <code>scale to multiple</code>（SCAIL2 用 32，Animate2 用 16），"
      "把宽高对齐到模型要求的倍数</li>"
      "<li>去掉 <code>crop=center</code>（比例已经保持住了，不需要裁）</li>"
      "</ul></div>")

    A("<div class='box good'><div class='t'>新增用户参数 <code>long_side</code></div>"
      "输出长边像素，范围 384~1024，默认 896。另一边按素材比例自动算。<br>"
      "这是唯一需要你决定的画质开关：<br>"
      "· <b>896</b>（默认）—— 竖屏出 896×1600 左右，720p 竖屏水平<br>"
      "· <b>640</b> —— 快 15% 左右，细节开始软<br>"
      "· <b>1024</b> —— 更清晰，显存吃紧，4090 上约慢 2 倍</div>")

    A("<div class='box good'><div class='t'>Animate2 额外三处</div><ul>"
      "<li>删掉 <code>292</code> 调试分支 —— 它输出的是「原片 | 成片」左右拼接的对照图，"
      "却被当成正式产物拉回来了</li>"
      "<li>删掉 <code>1674</code> —— 它从 <code>SaveVideo</code> 反向取 fps/audio，"
      "等于把成品再解码一遍</li>"
      "<li><code>WanAnimate2Cache</code> 从 <code>gpu/int8</code> 改回 <code>cpu/default</code> —— "
      "官方 tooltip 明写「cpu 才是安全选择」，int8 是有损缓存</li>"
      "</ul></div>")

    A("<div class='box'><div class='t'>顺手修的脚本 bug</div>"
      "<code>sweep_all.py</code> 给 SCAIL2 传的负向词是 "
      "<code>\"A young woman with dark hair\"</code>，正向词是 "
      "<code>\"A young woman with dark hair, white dress\"</code> —— "
      "等于叫模型别画主角。原来那个 62 分里，有一部分是脚本自己造成的。</div>")

    A("<h2>花了多少</h2>")
    A("<table><tr><th>作业</th><th>机器</th><th>耗时</th><th>花费</th></tr>"
      "<tr><td>SCAIL2 修后</td><td>4090</td><td class='num'>4.7 分钟</td>"
      "<td class='num'>¥0.17</td></tr>"
      "<tr><td>Animate2 修后</td><td>4090</td><td class='num'>5.0 分钟</td>"
      "<td class='num'>¥0.18</td></tr>"
      "<tr><td colspan='3'>合计（含一台误开的验证机 ¥0.10）</td>"
      "<td class='num'><b>¥0.45</b></td></tr></table>")

    A("<div class='foot'>"
      "产物来自 "
      "<code>p1-20261115T2338-a4fb97</code> 与 "
      "<code>p1-20261115T2344-ba4714</code>。"
      "评分是按边缘能量 + 运动量 + 覆盖率自动算的，不是人工打分 —— "
      "它只能说明「没严重出错」，好不好看你自己的眼睛。</div>")

    A(f"<script>{JS}</script></body></html>")
    return "\n".join(L)


def main() -> int:
    out = LAB / "verdict.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(), encoding="utf-8")
    print(f"✓ {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
