#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成详细架构图：SVG（矢量）+ PNG（可下载），带编号数据流与图例，并标注实时进度。

用法:
    python3 tools/arch.py            # 生成并同步到平板窗口
    python3 tools/arch.py --no-sync  # 只生成（给高频接口用）
输出:
    reports/architecture.svg
    reports/architecture.png
    reports/architecture.html        # 内联 SVG 的独立页
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_SVG = ROOT / "reports" / "architecture.svg"
OUT_PNG = ROOT / "reports" / "architecture.png"
OUT_HTML = ROOT / "reports" / "architecture.html"
LAB = ROOT / "lab" / "state.json"
LABDIR = ROOT / "reports" / "lab"
P1STATUS = ROOT / "reports" / "p1" / "status.json"   # P1 单任务闭环进展（实时）

W, H = 1680, 4000   # H 先给足，最后按内容裁剪
BG = "#080b0f"
PANEL = "#101722"
PANEL2 = "#0d141d"
BOX = "#151e2a"
BOXF = "#1b2634"
EDGE = "#25313f"
TXT = "#eef2f8"
DIM = "#94a0b4"
BLUE = "#7cc0ff"
YELLOW = "#ffd08a"
GREEN = "#8ce8ac"
PINK = "#f2a9c4"
VIOLET = "#b9a8ff"
LINE = "#2b3a4d"
FONT = ("-apple-system,BlinkMacSystemFont,'PingFang SC','WenQuanYi Micro Hei',"
        "'Microsoft YaHei',sans-serif")

svg: list[str] = []


def esc(t) -> str:
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text(x, y, s, size=12.5, color=DIM, weight="400", anchor="start", family=None):
    svg.append(f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{weight}" '
               f'text-anchor="{anchor}" font-family="{family or FONT}">{esc(s)}</text>')


def panel(x, y, w, h, title, color="#3a4760"):
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{PANEL}" '
               f'stroke="{color}" stroke-width="1.2"/>')
    text(x + 16, y + 26, title, 15, TXT, "700")


def box(x, y, w, h, title, lines, accent=EDGE, tcolor=None, fill=BOX):
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="{fill}" '
               f'stroke="{accent}" stroke-width="1.3"/>')
    text(x + 14, y + 23, title, 13.5, tcolor or TXT, "700")
    for i, ln in enumerate(lines):
        text(x + 14, y + 44 + i * 17.5, ln, 11.6, DIM)


def arrow(pts, color=BLUE, dash=False, width=2.0):
    d = " ".join(f"{'M' if i == 0 else 'L'}{x},{y}" for i, (x, y) in enumerate(pts))
    da = ' stroke-dasharray="8 6"' if dash else ""
    svg.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"{da} '
               f'stroke-linecap="round" stroke-linejoin="round" '
               f'marker-end="url(#arrow-{color.strip("#")})"/>')


def badge(x, y, num, color=BLUE):
    """编号胶囊：深色描边 + 彩色填充 + 白字。"""
    svg.append(f'<circle cx="{x}" cy="{y}" r="12.5" fill="{BG}" stroke="{color}" stroke-width="1.4"/>')
    svg.append(f'<circle cx="{x}" cy="{y}" r="9.2" fill="{color}" opacity="0.92"/>')
    text(x, y + 4.2, str(num), 11.5, "#07101a", "700", "middle")


def label(x, y, s, color=DIM, size=11.5, anchor="start"):
    text(x, y, s, size, color, "500", anchor)


# ---------------------------------------------------------------- 数据
def stats() -> dict:
    d = {"done": 0, "total": 8, "imgs": 0, "cost": 0.0, "groups": [], "balance": None,
         "today": 0.0, "stock": ""}
    try:
        st = json.loads(LAB.read_text(encoding="utf-8"))
        gs = st.get("groups", {})
        d["groups"] = [(k, v.get("status"), round(float(v.get("cost_estimate") or 0), 3))
                       for k, v in gs.items()]
        d["total"] = 8
        d["done"] = sum(1 for v in gs.values() if v.get("status") == "ok")
        d["cost"] = sum(float(v.get("cost_estimate") or 0) for v in gs.values())
    except Exception:
        pass
    d["imgs"] = len(list(LABDIR.rglob("*.png"))) + len(list(LABDIR.rglob("*.webp")))
    try:
        sys.path.insert(0, str(ROOT))
        from compshare.cs import CS
        cs = CS()
        d["balance"] = cs.balance().get("AmountAvailable")
        inv = cs.inventory_raw().get("GpuInventory") or {}
        parts = []
        for scope, cn in (("Exclusive", "独占"), ("Spot", "抢占")):
            tot = {}
            for zid, gpus in (inv.get(scope) or {}).items():
                for g, n in (gpus or {}).items():
                    if n:
                        tot[g] = tot.get(g, 0) + n
            parts.append(f"{cn} " + ("/".join(f"{k}:{v}" for k, v in sorted(tot.items())) or "0"))
        d["stock"] = " · ".join(parts)
    except Exception:
        pass
    try:
        led = ROOT / ".compshare" / "ledger.jsonl"
        today = time.strftime("%Y-%m-%d")
        for line in led.read_text(encoding="utf-8").splitlines()[-2000:]:
            r = json.loads(line)
            if str(r.get("at", ""))[:10] == today and r.get("cost"):
                d["today"] += float(r["cost"])
    except Exception:
        pass
    return d


def box_h(n: int) -> int:
    return 40 + 18 * n


P1_DEFAULT = {
    "updated_at": "—", "branch": "p1-mini-single-task-loop", "mode": "single-task",
    "job": {"job_id": "—", "state": "IDLE", "instance": None, "gpu": None},
    "states": [], "steps": [], "checks": [], "blockers": [],
    "cases": {"total": 0, "defined": 0, "passed": 0},
    "cost": {"spent": 0.0, "budget": 0.0},
}


def p1_status() -> dict:
    """读取 P1 单任务闭环进展；文件缺失/损坏一律回落默认值，不影响整图生成。"""
    d = json.loads(json.dumps(P1_DEFAULT))
    try:
        raw = json.loads(P1STATUS.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            d.update(raw)
    except Exception:
        pass
    d["job"] = {**P1_DEFAULT["job"], **(d.get("job") or {})}
    d["cost"] = {**P1_DEFAULT["cost"], **(d.get("cost") or {})}
    d["cases"] = {**P1_DEFAULT["cases"], **(d.get("cases") or {})}
    for k in ("states", "steps", "checks", "blockers"):
        if not isinstance(d.get(k), list):
            d[k] = []
    return d


def p1_section(sec, y: int, LX: int, LW: int, text, svg) -> int:
    """H · P1 单任务闭环进度（数据源 reports/p1/status.json，每 10 分钟随架构图刷新）。"""
    p = p1_status()
    job = p.get("job") or {}
    states = p.get("states") or []
    steps = (p.get("steps") or [])[:4]
    checks = p.get("checks") or []
    blockers = p.get("blockers") or []
    cases = p.get("cases") or {}
    cost = p.get("cost") or {}
    COL = {"done": GREEN, "doing": YELLOW, "todo": "#4d5768", "fail": "#e08a8a"}
    MARK = {"done": "✓", "doing": "▸", "todo": "·", "fail": "✗"}

    cols = 4 if LW > 1200 else 3
    check_rows = (len(checks) + cols - 1) // cols
    h = (218 + 12 + check_rows * 20 + 12
         + ((22 + len(blockers) * 17) if blockers else 0) + 14)
    sec(y, h, "H · P1 单任务闭环（最近作业 · 实时）")

    # 信息行（两行：任务面 / 作业面）
    jid = job.get("job_id") or "—"
    cur = job.get("state") or "IDLE"
    tk = p.get("tasks") or {}
    text(LX + 20, y + 52,
         f"分支 {p.get('branch', '—')} · 模式 {p.get('mode', 'single-task')}"
         f" · 更新 {p.get('updated_at', '—')}", 11.4, DIM)
    text(LX + 20, y + 70,
         f"任务 {tk.get('done', 0)}/{tk.get('total', 0)}（{tk.get('pct', 0)}%）"
         f" · 进行中 {tk.get('doing', 0)} · 阻塞 {tk.get('blocked', 0)}"
         f" · 当前 {tk.get('current') or '—'} · 下一步 {tk.get('next') or '—'}"
         f" · job {jid}（{cur}）"
         f" · 用例 {cases.get('passed', 0)}/{cases.get('total', 0)}"
         f"（跳过 {cases.get('skipped', 0)}）"
         f" · 本作业 ¥{float(cost.get('spent') or 0):.3f}"
         f" · P1 累计 ¥{float(cost.get('p1_total') or 0):.2f}"
         f" / 预算 ¥{float(cost.get('budget') or 0):.2f}",
         11.4, DIM)

    # 状态链
    n = max(1, len(states))
    gap = 10
    cw = (LW - 40 - (n - 1) * gap) / n
    cy = y + 84
    for i, s in enumerate(states):
        x = LX + 20 + i * (cw + gap)
        k = s.get("k", "?")
        st = s.get("st", "todo")
        is_cur = (k == cur)
        col = YELLOW if is_cur else COL.get(st, "#4d5768")
        svg.append(f'<rect x="{x:.1f}" y="{cy}" width="{cw:.1f}" height="34" rx="8" '
                   f'fill="{BOXF if is_cur else PANEL}" stroke="{col}" stroke-width="{2.0 if is_cur else 1.0}"/>')
        text(x + cw / 2, cy + 15, s.get("label") or k, 10.6, TXT if is_cur else DIM, "700", "middle")
        text(x + cw / 2, cy + 28, f"{MARK.get(st, '·')}{k}", 8.8, col, "600", "middle")
        if i < n - 1:
            text(x + cw + gap / 2, cy + 22, "›", 13, "#3b4657", "700", "middle")

    # 四步推进
    sy = y + 134
    text(LX + 20, sy + 14, "依赖顺序：底座 → 校验&持久化 → 模型 → 异常测试（只做单任务，不做并发批量）",
         11.2, DIM)
    by = sy + 22
    bw = (LW - 40 - 3 * 12) / 4
    for i, s in enumerate(steps):
        x = LX + 20 + i * (bw + 12)
        st = s.get("st", "todo")
        col = COL.get(st, "#4d5768")
        pct = max(0, min(100, int(s.get("pct") or 0)))
        svg.append(f'<rect x="{x:.1f}" y="{by}" width="{bw:.1f}" height="52" rx="9" '
                   f'fill="{PANEL}" stroke="{EDGE}"/>')
        svg.append(f'<rect x="{x:.1f}" y="{by}" width="3" height="52" rx="1.5" fill="{col}"/>')
        text(x + 12, by + 17, f"{MARK.get(st, '·')} {s.get('id', i + 1)}. {s.get('title', '')}",
             11.0, TXT if st != "todo" else DIM, "700")
        text(x + 12, by + 31, (s.get("note") or "")[:30], 10.2, "#6b7385")
        svg.append(f'<rect x="{x + 12}" y="{by + 38}" width="{bw - 24:.1f}" height="5" rx="2.5" '
                   f'fill="#1b2230"/>')
        if pct:
            svg.append(f'<rect x="{x + 12}" y="{by + 38}" width="{(bw - 24) * pct / 100:.1f}" '
                       f'height="5" rx="2.5" fill="{col}"/>')
        text(x + bw - 12, by + 44, f"{pct}%", 9.6, col, "700", "end")

    # 复验项
    ky = y + 218
    text(LX + 20, ky + 12, "复验项（全部 ✓ 才允许进入 PERSISTED → RELEASING）", 11.0, DIM, "700")
    cwid = (LW - 40 - (cols - 1) * 16) / cols
    for i, c in enumerate(checks):
        r, k = divmod(i, cols)
        x = LX + 20 + k * (cwid + 16)
        y2 = ky + 20 + r * 20
        st = c.get("st", "todo")
        col = COL.get(st, "#4d5768")
        text(x, y2 + 10, f"{MARK.get(st, '·')}", 11, col, "700")
        text(x + 13, y2 + 10, (c.get("name") or "")[:34], 10.4, TXT if st == "done" else DIM)

    # 阻塞
    bly = ky + 20 + check_rows * 20 + 12
    if blockers:
        text(LX + 20, bly + 10, "阻塞点", 11.0, "#e08a8a", "700")
        for i, b in enumerate(blockers):
            text(LX + 20, bly + 26 + i * 17, f"· {b}"[:110], 10.3, DIM)
    return y + h


def build() -> None:
    s = stats()
    LX, LW = 40, 1040
    RX, RW = 1140, 500
    GAP = 30
    GAPX = 1094          # ⑤ 走线（左栏外侧）
    GAPX2 = 1124         # ⑥ 走线（再靠右一点，但不压 F 面板）
    rects: dict[str, tuple] = {}

    ACCENTS = {"A": BLUE, "B": VIOLET, "C": GREEN, "D": PINK, "E": YELLOW, "F": BLUE,
               "G": DIM, "H": "#6fe3c4"}

    def sec(y, h, title):
        m = re.match(r"^([A-G])\s*·\s*(.*)$", title)
        letter, body = (m.group(1), m.group(2)) if m else ("", title)
        accent = ACCENTS.get(letter, BLUE)
        svg.append(f'<rect x="{LX}" y="{y}" width="{LW}" height="{h}" rx="16" fill="url(#sec)" '
                   f'stroke="{LINE}" stroke-width="1.1" filter="url(#soft)"/>')
        svg.append(f'<rect x="{LX}" y="{y}" width="4" height="{h}" rx="2" fill="{accent}" '
                   f'opacity="0.9"/>')
        if letter:
            svg.append(f'<circle cx="{LX + 32}" cy="{y + 26}" r="13" fill="{accent}" opacity="0.15"/>')
            text(LX + 32, y + 30.5, letter, 13, accent, "700", "middle")
        text(LX + 55, y + 31, body, 15.5, TXT, "700")

    def bx(key, x, y, w, title, lines, accent=EDGE):
        h = box_h(len(lines))
        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="url(#card)" '
                   f'stroke="{EDGE}" stroke-width="1"/>')
        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="3" rx="1.5" fill="{accent}" '
                   f'opacity="0.9"/>')
        svg.append(f'<circle cx="{x + 15}" cy="{y + 24}" r="3.4" fill="{accent}"/>')
        text(x + 25, y + 28, title, 12.8, TXT, "700")
        for i, ln in enumerate(lines):
            text(x + 15, y + 48 + i * 18, ln, 11.3, DIM)
        rects[key] = (x, y, w, h)
        return y + h + 12

    svg.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
               f'viewBox="0 0 {W} {H}" font-family="{FONT}">')
    svg.append("<defs>")
    for c in (BLUE, YELLOW, GREEN, PINK, "#94a0b4", VIOLET):
        svg.append(f'<marker id="arrow-{c.strip("#")}" markerWidth="12" markerHeight="12" '
                   f'refx="9" refY="3.4" orient="auto"><path d="M0,0 L0,6.8 L9,3.4 z" '
                   f'fill="{c}"/></marker>')
    svg.append('<linearGradient id="hero" x1="0" y1="0" x2="1" y2="0">'
               '<stop offset="0" stop-color="#7cc0ff"/><stop offset="0.5" stop-color="#b9a8ff"/>'
               '<stop offset="1" stop-color="#8ce8ac"/></linearGradient>')
    svg.append('<linearGradient id="sec" x1="0" y1="0" x2="0" y2="1">'
               '<stop offset="0" stop-color="#121a25"/><stop offset="1" stop-color="#0c1219"/>'
               '</linearGradient>')
    svg.append('<linearGradient id="card" x1="0" y1="0" x2="0" y2="1">'
               '<stop offset="0" stop-color="#18222f"/><stop offset="1" stop-color="#131b26"/>'
               '</linearGradient>')
    svg.append('<linearGradient id="bar" x1="0" y1="0" x2="1" y2="0">'
               '<stop offset="0" stop-color="#2f6fed"/><stop offset="1" stop-color="#7cc0ff"/>'
               '</linearGradient>')
    svg.append('<pattern id="dots" width="26" height="26" patternUnits="userSpaceOnUse">'
               '<circle cx="1.4" cy="1.4" r="1.1" fill="#1a2431"/></pattern>')
    svg.append('<filter id="soft" x="-20%" y="-20%" width="140%" height="140%">'
               '<feDropShadow dx="0" dy="6" stdDeviation="10" flood-color="#000" '
               'flood-opacity="0.45"/></filter>')
    svg.append("</defs>")
    svg.append(f'<rect width="{W}" height="{H}" fill="{BG}"/>')
    svg.append(f'<rect width="{W}" height="{H}" fill="url(#dots)" opacity="0.55"/>')
    svg.append(f'<rect x="0" y="0" width="{W}" height="3" fill="url(#hero)"/>')

    svg.append(f'<text x="40" y="44" fill="url(#hero)" font-size="23" font-weight="800" '
               f'font-family="{FONT}">优云智算 GPU 工作流 · 系统架构</text>')
    text(40, 68, f"生成于 {time.strftime('%Y-%m-%d %H:%M:%S')} · 数据流 ①…⑧ 见图中编号 · "
                 f"PNG / SVG 可下载", 12, DIM)
    # 右上角统计胶囊
    px = W - 40
    for label, val, color in reversed([
            ("余额", f"¥{s['balance']}", GREEN),
            ("实验实扣", f"¥{s['cost']:.2f}", YELLOW),
            ("成品", f"{s['imgs']} 张", VIOLET),
            ("完成机器", f"{s['done']}/{s['total']}", BLUE)]):
        w = 24 + len(label) * 12 + len(str(val)) * 8.4
        px -= w
        svg.append(f'<rect x="{px}" y="26" width="{w}" height="30" rx="15" fill="#111a24" '
                   f'stroke="{color}" stroke-width="1" opacity="0.95"/>')
        text(px + 12, 46, label, 10.8, DIM)
        text(px + w - 12, 46, val, 12.2, color, "700", "end")
        px -= 10

    # ---------------- 左栏
    y = 108
    yA = y
    sec(y, 134, "A · 使用者侧（平板 / 手机浏览器）")
    bx("A1", LX + 16, yA + 38, 320, "浏览器标签页",
       ["地址栏 http://123.207.74.155/", "书签：监控 / 成本 / 架构图"])
    bx("A2", LX + 352, yA + 38, 330, "4 个入口页",
       ["/files/<token>/monitor", "/cost · /arch · gallery-inline"])
    bx("A3", LX + 698, yA + 38, 326, "免登录 vs 登录",
       ["/files/ 靠路径 token（nginx 关 auth）", "GUI 仍走 basic auth + ?token"])
    y = yA + 134 + GAP

    yB = y
    sec(y, 172, "B · 入口层 · nginx（:80 / :443，自签 TLS）")
    rows = [("/", "127.0.0.1:3080", "DSH Web GUI · basic auth", BLUE),
            ("/files/", "127.0.0.1:8081", "文件服务 · auth off · 路径 token", BLUE),
            ("/files/<t>/api/*", "127.0.0.1:8081", "status / cost / arch / log", GREEN),
            ("/up/", "127.0.0.1:8090", "其它本地服务（非本工具链）", DIM)]
    ry = yB + 58
    for path, up, note, col in rows:
        text(LX + 20, ry, path, 12.2, col, "700")
        text(LX + 168, ry, "→", 12, DIM)
        text(LX + 190, ry, up, 11.8, TXT)
        text(LX + 345, ry, note, 11.4, DIM)
        ry += 26
    text(LX + 20, yB + 166, "gzip · 长连接 · proxy_buffering off", 11, "#6b7385")
    y = yB + 172 + GAP

    yC = y
    sec(y, 140, "C · 本机进程（VM 4C/8G · Ubuntu 24.04）")
    cy = yC + 38
    bx("C1", LX + 16, cy, 320, "dsh（Node）:3080",
       ["会话 / 工具调用 / 文件卡片", "profile=web · no-remote.yml", "danger-full-access"], BLUE)
    bx("C2", LX + 352, cy, 330, "fileserver.py :8081",
       ["systemd: cs-files（常驻）", "预览/上传/灯箱/缩略图", "服务端渲染 监控·成本·架构"], GREEN)
    bx("C3", LX + 698, cy, 326, "systemd · cron",
       ["cs-lab：基准流水线（等货即跑）", "*/5 空闲守护 · */10 架构图", "*/10 余额快照"], PINK)
    y = yC + 140 + GAP

    yD = y
    sec(y, 248, "D · 工具层（/root/智云视频工作流）")
    dy = yD + 38
    bx("D1", LX + 16, dy, 320, "csctl.py",
       ["up：flock 互斥 + 查重 + 预算闸", "stop/start/rm/stop-at/cardless",
        "ssh --exec --detach（断连不停）", "cost/jobs 账本 · price/stock"], BLUE)
    bx("D2", LX + 352, dy, 330, "lab_runner.py（cs-lab）",
       ["等现货 → 建机 → 探环境", "→ 跑 6–8 负载 → scp 拉产物",
        "→ 销毁 → 出报告", "预算 ¥8 · 单机 35min"], PINK)
    bx("D3", LX + 698, dy, 326, "tools/ · scripts/",
       ["gallery.py：缩略图/长图/画廊", "arch.py：架构图 SVG+PNG", "cost_snapshot.py：余额快照",
        "sync_share.sh · install_cron.sh"], GREEN)
    step_y = dy + box_h(4) + 12
    steps = ["查现货", "下单", "等 Running", "等 sshd", "跑负载", "scp 拉回", "terminate"]
    x = LX + 18
    for i, st in enumerate(steps, 1):
        w = 132
        svg.append(f'<rect x="{x}" y="{step_y}" width="{w}" height="30" rx="8" fill="{BOXF}" '
                   f'stroke="{EDGE}"/>')
        text(x + 8, step_y + 20, f"{i}. {st}", 11.5, TXT)
        if i < len(steps):
            svg.append(f'<path d="M{x+w},{step_y+15} L{x+w+18},{step_y+15}" stroke="{YELLOW}" '
                       f'stroke-width="1.5" marker-end="url(#arrow-ffcf8a)"/>')
        x += w + 20
    text(LX + 18, step_y + 52, "失败策略：抢卡失败/镜像同步中 → 可重试（不计失败次数）；负载失败记录原因继续；异常必销毁",
         11, DIM)
    text(LX + 18, step_y + 70, "预算：余额下限 ¥5 + 当日上限 ¥20（csctl）/ 总额 ¥8（lab）；并发：up.lock",
         11, DIM)
    y = yD + 248 + GAP

    yE = y
    e_h = 52 + box_h(3) + 14 + box_h(2) + 24
    sec(y, e_h, "E · 状态与产物（本机磁盘）")
    ey = yE + 52
    bx("E1", LX + 16, ey, 320, ".compshare/ 状态",
       ["ledger.jsonl 账本 · up.lock 锁", "stop_at.json · watch_state.json", "state.json（历史）"], YELLOW)
    bx("E2", LX + 352, ey, 330, "lab/ 配置",
       ["state.json：每台结果与花费", "bench_remote.py：远端执行体", "STOP：急停开关"], YELLOW)
    bx("E3", LX + 698, ey, 326, "reports/ 产物",
       ["lab/ 原图 + .thumbs/ + compare/", "gallery*.html · REPORT.md",
        "architecture.svg / .png"], GREEN)
    ey2 = ey + box_h(3) + 12
    bx("E4", LX + 16, ey2, 320, "share/ 平板窗口",
       ["rsync 副本（不含凭证）", "静态资源 + 缩略图 + 长图"], GREEN)
    bx("E5", LX + 352, ey2, 330, "logs/",
       ["lab.log 建机与跑分 · watch.log 守护", "arch.log · cost.log"], DIM)
    bx("E6", LX + 698, ey2, 326, "凭证（600，不入库）",
       ["compshare/credentials.env（AK/SK）", ".share_token 访问口令"], "#e08a8a")

    # ---------------- H · P1 单任务闭环（实时读 reports/p1/status.json）
    left_end = ey2 + box_h(2)

    # ---------------- 右栏
    ry0 = 108
    fy = ry0 + 40
    fy = bx("F1", RX + 16, fy, RW - 32, "API 网关 · UCloud v1 签名",
            ["Signature = SHA1(字典序参数拼接 + PrivateKey)", "AK/SK 来自控制台"], BLUE)
    fy = bx("F2", RX + 16, fy, RW - 32, "已实测接口（11 个）", [
        "Create / Describe / Start / Stop / Terminate",
        "UpdateCompShareStopScheduler",
        "GetCompShareInstanceMonitor",
        "GetCompShareInstanceUserPrice",
        "DescribeCompShareGpuInventory / Images",
        "DescribeCompShareSupportZone"], EDGE)
    fy = bx("F3", RX + 16, fy, RW - 32, "计费与配额规则", [
        "按量后付费 · 秒级计费；关机停算力费",
        "系统盘 SSD ≤100G 免费（默认 100G）",
        "抢占式便宜 30–59%，可被回收",
        "配额：未充值 1 / 已充值 5 / L2 20 台"], YELLOW)
    fy = bx("F4", RX + 16, fy, RW - 32, "实例池（4 可用区 × 8 卡型）", [
        "cn-wlcb-01 · cn-sh2-01/02 · cn-bj2-03",
        "4090 / 4090_48G / 5090 / 3090 / V100S / A100…",
        f"实时现货：{s['stock'] or '查询中…'}"], PINK)
    fy = bx("F5", RX + 16, fy, RW - 32, "单台实例内部（实测）", [
        "Ubuntu 24.04.1 · 内核 5.15 · tini 容器",
        "驱动 580.159.04 · CUDA 12.4 · sm_70/89",
        "ComfyUI 0.3.40 :8188（/start.d 自启）",
        "SSH :23（root+密码）· Jupyter :8888",
        "共享挂载 /model = UPFS 40T（模型软链）"], GREEN)
    fy = bx("F6", RX + 16, fy, RW - 32, "产物与回收", [
        "出图落 /root/ComfyUI/output/*.png",
        "scp 拉回 reports/lab/<机器>/<负载>/",
        "随后 Terminate（系统盘随之删除）",
        "未归档数据随实例消失"], "#e08a8a")

    fy = bx("F7", RX + 16, fy, RW - 32, "已知约束（实测踩坑）", [
        "镜像无 ffmpeg / rsync → PyAV 编码、tar 流传输",
        "Running ≠ sshd 就绪，需再等 20–40s 才能上传",
        "关机即释放 GPU，再开要重新抢卡（最长等 5.5 分钟）",
        "无卡模式 2C4G / 8C16G（0.15 / 0.6 元·时）可下载与建镜像"], "#e08a8a")

    # F 面板背景（按内容定高，卡片不贴边；不足则与左栏齐平）
    f_h = max(left_end, fy + 8) - ry0
    svg.insert(len(svg) - 0, f'<rect x="{RX}" y="{ry0}" width="{RW}" height="{f_h}" rx="16" '
               f'fill="url(#sec)" stroke="{LINE}" stroke-width="1.1" filter="url(#soft)"/>')
    svg.append(f'<rect x="{RX}" y="{ry0}" width="4" height="{f_h}" rx="2" fill="{BLUE}" opacity="0.9"/>')
    svg.append(f'<circle cx="{RX + 32}" cy="{ry0 + 26}" r="13" fill="{BLUE}" opacity="0.15"/>')
    text(RX + 32, ry0 + 30.5, "F", 13, BLUE, "700", "middle")
    text(RX + 55, ry0 + 31, "优云智算（api.compshare.cn）", 15.5, TXT, "700")

    # ---------------- 箭头（面板间走线，编号放空隙里）
    def mid_top(k):
        x, y_, w, h = rects[k]
        return x + w / 2, y_

    def mid_bot(k):
        x, y_, w, h = rects[k]
        return x + w / 2, y_ + h

    def cross(x, y_from, y_to, color, num, dash=False):
        arrow([(x, y_from), (x, y_to - 6)], color, dash=dash)
        badge(x + 15, (y_from + y_to) / 2, num, color)

    # ① A → B
    x1, _ = mid_top("A2")
    cross(x1, yA + 134, yB + 4, BLUE, 1)
    # ② B → C（GUI）
    x2, _ = mid_top("C1")
    cross(x2, yB + 172, yC + 4, BLUE, 2)
    # ③ B → C（文件）
    x3, _ = mid_top("C2")
    cross(x3, yB + 172, yC + 4, GREEN, 3)
    # ④ C → D
    x4, _ = mid_top("D1")
    cross(x4, yC + 140, yD + 4, "#94a0b4", 4, dash=True)

    # ⑤ D → F1（走右侧通道）
    d2x, d2y, d2w, d2h = rects["D2"]
    f1y = rects["F1"][1] + 34
    # 从左侧面板右边缘出线，避免横穿 D3 盒子
    arrow([(LX + LW - 6, d2y + 24), (GAPX, d2y + 24), (GAPX, f1y), (RX - 6, f1y)], BLUE)
    badge(GAPX + 13, (d2y + f1y) / 2, 5)
    # ⑥ D → F5（SSH）
    d3x, d3y, d3w, d3h = rects["D3"]
    f5y = rects["F5"][1] + 40
    arrow([(LX + LW - 6, d3y + 70), (GAPX2, d3y + 70), (GAPX2, f5y), (RX - 6, f5y)], YELLOW)
    badge(GAPX2 - 13, (d3y + f5y) / 2, 6)
    # ⑦ ComfyUI 推理回环
    f5yy, f5h = rects["F5"][1], rects["F5"][3]
    svg.append(f'<path d="M{RX + RW - 16},{f5yy + 34} C{RX + RW + 20},{f5yy + 40} '
               f'{RX + RW + 20},{f5yy + f5h - 16} {RX + RW - 16},{f5yy + f5h - 22}" fill="none" '
               f'stroke="{GREEN}" stroke-width="2" stroke-linecap="round" '
               f'marker-end="url(#arrow-8ee6a8)"/>')
    badge(RX + RW + 6, f5yy + f5h / 2, 7)
    # ⑧ 产物回流 F6 → E3
    f6y = rects["F6"][1] + 34
    e3x, e3y, e3w, e3h = rects["E3"]
    x8 = GAPX2 + 8
    arrow([(RX - 4, f6y), (x8, f6y), (x8, e3y + 46), (e3x + e3w + 6, e3y + 46)], GREEN)
    badge(x8 - 16, (f6y + e3y) / 2 + 4, 8)

    # ---------------- H · P1 单任务闭环（通栏，实时读 reports/p1/status.json）
    yH = max(left_end, ry0 + f_h) + GAP
    yH_end = p1_section(sec, yH, LX, W - 80, text, svg)

    # ---------------- 图例（紧跟内容）
    gy = yH_end + 18
    svg.append(f'<rect x="{LX}" y="{gy}" width="{W - 80}" height="108" rx="16" fill="url(#sec)" '
               f'stroke="{LINE}" stroke-width="1.1" filter="url(#soft)"/>')
    text(LX + 16, gy + 24, "G · 图例与实时进度", 14.5, TXT, "700")
    for i, (c, t) in enumerate([(BLUE, "HTTP 请求 / API 调用"), (GREEN, "文件与产物通路"),
                                (YELLOW, "SSH / scp"), ("#8b93a7", "内部调用（虚线）")]):
        svg.append(f'<rect x="{LX + 16 + i*250}" y="{gy + 42}" width="22" height="6" rx="3" '
                   f'fill="{c}" opacity="0.95"/>')
        text(LX + 46 + i * 250, gy + 48.5, t, 11.4, DIM)
    pct = s["done"] / max(1, s["total"])
    text(LX + 16, gy + 78, f"基准进度 {s['done']}/{s['total']}　·　实扣 ¥{s['cost']:.2f} / 预算 ¥8",
         12, TXT, "700")
    svg.append(f'<rect x="{LX + 430}" y="{gy + 68}" width="300" height="12" rx="6" fill="#1b2230"/>')
    svg.append(f'<rect x="{LX + 360}" y="{gy + 68}" width="{300*pct:.0f}" height="12" rx="6" fill="{BLUE}"/>')
    text(LX + 742, gy + 78, f"{pct*100:.0f}%", 12, BLUE, "700")
    names = " · ".join(f"{g}{'✓' if st == 'ok' else ''}" for g, st, _ in s["groups"])
    text(LX + 16, gy + 96, f"机器：{names}", 10.8, "#6b7385")
    text(LX + 800, gy + 78, "本图由 tools/arch.py 生成 · PNG / SVG 可下载", 11.2, "#6b7385")
    final_h = int(gy + 108 + 34)
    svg.append("</svg>")
    globals()["__final_h__"] = final_h


def main() -> int:
    no_sync = "--no-sync" in sys.argv
    build()
    fh = int(globals().get("__final_h__") or H)
    svg[0] = (svg[0].replace(f'height="{H}"', f'height="{fh}"')
              .replace(f'viewBox="0 0 {W} {H}"', f'viewBox="0 0 {W} {fh}"'))
    OUT_SVG.parent.mkdir(parents=True, exist_ok=True)
    OUT_SVG.write_text("\n".join(svg), encoding="utf-8")
    ET.parse(OUT_SVG)  # XML 合法性

    # PNG（2 倍分辨率，便于平板查看/分享）
    png_ok = False
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(OUT_SVG),
                            "-vf", "scale=iw*2:ih*2:flags=lanczos", "-frames:v", "1",
                            str(OUT_PNG)], capture_output=True, timeout=120)
        png_ok = r.returncode == 0 and OUT_PNG.exists()
        if not png_ok:
            print("PNG 生成失败:", r.stderr.decode()[:200])
    except Exception as e:
        print("PNG 生成异常:", e)

    # 独立 HTML（内联 SVG）
    try:
        tok = (ROOT / ".share_token").read_text(encoding="utf-8").strip()
    except Exception:
        tok = ""
    inline = OUT_SVG.read_text(encoding="utf-8")
    inline = inline[inline.index("<svg"):]
    OUT_HTML.write_text(
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>架构图 · 详细版</title><style>"
        "body{margin:0;background:#0b0e12;color:#e8ebf2;font:16px/1.6 -apple-system,sans-serif}"
        "header{display:flex;gap:8px;align-items:center;padding:10px 14px;"
        "border-bottom:1px solid #222833;position:sticky;top:0;background:#0b0e12}"
        "header .t{flex:1;font-weight:700}a{color:#6cb6ff;text-decoration:none;padding:7px 11px;"
        "border:1px solid #2b3550;border-radius:9px;white-space:nowrap}"
        "main{padding:8px;overflow:auto}svg{max-width:100%;height:auto}"
        "</style></head><body><header><span class='t'>架构图 · 详细版</span>"
        f"<a href='/files/{tok}/f/reports/architecture.png?dl=1'>下载 PNG</a>"
        f"<a href='/files/{tok}/f/reports/architecture.svg?dl=1'>下载 SVG</a>"
        f"<a href='/files/{tok}/monitor'>监控</a><a href='/files/{tok}/cost'>成本</a></header>"
        f"<main>{inline}</main></body></html>", encoding="utf-8")

    if not no_sync:
        try:
            subprocess.run(["bash", str(ROOT / "scripts" / "sync_share.sh")],
                           capture_output=True, timeout=180)
        except Exception:
            pass
    st = stats()
    print(f"arch: {OUT_SVG.stat().st_size}B svg · "
          f"{OUT_PNG.stat().st_size // 1024 if OUT_PNG.exists() else 0}KB png · "
          f"{st['done']}/{st['total']} 台 · {st['imgs']} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
