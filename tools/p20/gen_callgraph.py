#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 P2.0 调用关系图（SVG，无第三方依赖）。

    python3 tools/p20/gen_callgraph.py
    → reports/p2/p20/调用关系图.svg

图是手工排版的（坐标写死），改节点时改 NODES/EDGES 即可；不依赖 graphviz。
"""
from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports" / "p2" / "p20" / "调用关系图.svg"

W, H = 1460, 980
COLORS = {
    "entry": ("#e8f1ff", "#3b74c9"),
    "orch": ("#eef8ee", "#2f8a4a"),
    "exec": ("#fff4e6", "#c06a12"),
    "ext": ("#f4ecff", "#7a45b8"),
    "data": ("#f5f5f5", "#666666"),
}
NODES = {
    # id: (x, y, w, h, layer, title, subtitle)
    "ui": (30, 70, 230, 52, "entry", "浏览器 / UI", "/monitor /cost /risk /arch /f/*"),
    "fs": (30, 160, 230, 60, "entry", "fileserver.py", "服务端页面 + 只读 API"),
    "api": (330, 160, 270, 60, "orch", "/api/p2/*  +  /api/p2/registry/*", "只读 JSON，token 鉴权"),
    "jobctl": (30, 280, 230, 50, "entry", "tools/p1/jobctl.py", "new / show / validate"),
    "p20": (30, 350, 230, 50, "entry", "tools/p20/p20ctl.py", "import / resolve / new / compat"),
    "batch": (30, 420, 230, 50, "entry", "tools/p2/batchctl.py", "submit / run / doctor"),
    "runner": (30, 550, 230, 56, "entry", "tools/p1/run.py", "一实例一 job · 九步串行"),
    "cron": (30, 700, 230, 50, "entry", "crontab", "arch/cost/risk/status2/watchdog"),
    "jobs": (330, 260, 270, 54, "orch", "reports/p1/jobs/<id>/", "job.json · events · manifest · artifacts"),
    "reg": (330, 350, 270, 54, "orch", "registry/registry.db", "派生索引（可删可重建）"),
    "queue": (330, 430, 270, 50, "orch", "reports/p2/queue.db", "batch / item / audit_log"),
    "sched": (330, 505, 270, 50, "orch", "tools/p2/scheduler.py", "claim / heartbeat / requeue"),
    "views": (330, 620, 270, 56, "orch", "status2 / arch / risk", "聚合快照（只读）"),
    "ledger": (330, 710, 270, 44, "data", ".compshare/ledger.jsonl", "成本账本"),
    "steps": (700, 250, 300, 300, "exec", "run.py 九步", ""),
    "render": (700, 120, 300, 80, "exec", "render / assets / comfy", "registry 渲染 · 资产检查 · 协议解析"),
    "verify": (700, 610, 300, 60, "exec", "verify / freeze", "7 项复验 + freezedetect"),
    "cs": (1060, 150, 360, 50, "ext", "CompShare API", "create / terminate / balance / stock"),
    "comfy": (1060, 230, 360, 50, "ext", "实例内 ComfyUI :8188", "/object_info /prompt /history"),
    "ssh": (1060, 310, 360, 50, "ext", "sshpass / scp / rsync", "远端执行 + 产物回传"),
    "ff": (1060, 610, 360, 50, "ext", "本机 ffmpeg / ffprobe", "解码 / 黑帧 / 冻结 / 边缘能量"),
}
EDGES = [
    ("ui", "fs", "GET"),
    ("fs", "api", ""),
    ("fs", "jobs", ""),
    ("fs", "reg", ""),
    ("fs", "views", ""),
    ("fs", "ledger", ""),
    ("jobctl", "jobs", "job.json + events"),
    ("p20", "reg", "resolve / import"),
    ("p20", "jobctl", "生成 spec"),
    ("batch", "queue", "submit（内含 jobctl new）"),
    ("batch", "sched", "启动"),
    ("sched", "queue", "claim / finish"),
    ("sched", "runner", "Popen --batch"),
    ("runner", "render", "registry 分支"),
    ("render", "reg", "读配置"),
    ("steps", "cs", "provision / release"),
    ("steps", "comfy", "ready / submit / wait"),
    ("steps", "ssh", "setup / fetch"),
    ("steps", "verify", ""),
    ("steps", "jobs", "持久化"),
    ("verify", "ff", ""),
    ("cron", "views", "每 5/10 分钟"),
    ("cron", "sched", "watchdog"),
]


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def box(x, y, w, h, layer, title, sub, *, dashed=False):
    fill, stroke = COLORS[layer]
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
           f'fill="{fill}" stroke="{stroke}" stroke-width="1.6"'
           + (' stroke-dasharray="6 4"' if dashed else "") + '/>']
    ty = y + (22 if (sub or h > 100) else h / 2 + 5)
    out.append(f'<text x="{x + w / 2}" y="{ty}" text-anchor="middle" '
               f'font-size="14" font-weight="700" fill="#1b2330">{esc(title)}</text>')
    if sub:
        out.append(f'<text x="{x + w / 2}" y="{y + 42}" text-anchor="middle" '
                   f'font-size="11.5" fill="#4a5568">{esc(sub)}</text>')
    return "".join(out)


def center(n):
    x, y, w, h = NODES[n][:4]
    return x + w / 2, y + h / 2


def anchor(n, tx, ty):
    """从节点边缘出线，简单取上下左右最近边。"""
    x, y, w, h = NODES[n][:4]
    cx, cy = x + w / 2, y + h / 2
    dx, dy = tx - cx, ty - cy
    if abs(dx) * h > abs(dy) * w:
        return (x + w, cy) if dx > 0 else (x, cy)
    return (cx, y + h) if dy > 0 else (cx, y)


def edge(a, b, label=""):
    ax, ay = center(a)
    bx, by = center(b)
    x1, y1 = anchor(a, bx, by)
    x2, y2 = anchor(b, ax, ay)
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    out = [f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
           'stroke="#8794a8" stroke-width="1.4" marker-end="url(#arrow)"/>']
    if label:
        out.append(f'<text x="{mx:.1f}" y="{my - 6:.1f}" text-anchor="middle" '
                   f'font-size="10.5" fill="#5a6577" '
                   f'paint-order="stroke" stroke="#fbfcfe" stroke-width="4">{esc(label)}</text>')
    return "".join(out)


def main() -> int:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#8794a8"/></marker></defs>',
        '<rect width="100%" height="100%" fill="#fbfcfe"/>',
        '<text x="30" y="36" font-size="22" font-weight="800" fill="#101828">'
        'P2.0 调用关系图（基线 ca2a352 + P2.0 新增链路）</text>',
        '<text x="30" y="58" font-size="12.5" fill="#5a6577">'
        '实线=现有主链；蓝色=入口；绿色=编排/事实；橙色=执行；紫色=外部；灰色=数据</text>',
    ]
    # 列标题
    for x, label, color in ((30, "入口层", "#3b74c9"), (330, "编排 / 事实层", "#2f8a4a"),
                            (700, "执行层", "#c06a12"), (1060, "外部依赖", "#7a45b8")):
        parts.append(f'<text x="{x}" y="{118}" font-size="13" font-weight="700" fill="{color}">{label}</text>')
    for nid, (x, y, w, h, layer, title, sub) in NODES.items():
        parts.append(box(x, y, w, h, layer, title, sub))
    for a, b, label in EDGES:
        parts.append(edge(a, b, label))
    # 九步清单（必须在 box 之后画，否则被底色盖住）
    steps = ["1 provision  建机/等 Running", "2 setup  挂载/禁 Manager/软链",
             "3 ready  等 8188 + 资产可见", "4 submit  渲染 + /prompt",
             "5 wait  /history 轮询", "6 fetch  scp 产物",
             "7 verify  7 项复验", "8 persist  manifest 原子写",
             "9 release  销毁/回池"]
    for i, s in enumerate(steps):
        parts.append(f'<text x="722" y="{305 + i * 27}" font-size="12" fill="#7a3f08">{esc(s)}</text>')
    parts.append('<text x="30" y="940" font-size="11.5" fill="#5a6577">'
                 '说明：ComfyUI 没有公网端口，宿主机通过 sshpass 在实例内执行 python/urllib 调 127.0.0.1:8188；'
                 '产物用 scp 从 /root/ComfyUI/output/ 拉回。</text>')
    parts.append('<text x="30" y="960" font-size="11.5" fill="#5a6577">'
                 'P2.0 新增：registry/*.json → registry.db → render_job() → run.py；'
                 'scheduler/queue/state 零改动。</text>')
    parts.append("</svg>")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"✓ {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
