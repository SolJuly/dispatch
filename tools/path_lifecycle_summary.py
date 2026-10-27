#!/usr/bin/env python3
"""任务3：路径生命周期速查表生成器。

读取 ``tools/checkpoint.py --json`` 输出里的 ``paths`` 字段，生成 Markdown 表格：

    reports/路径生命周期速查表.md

用法::

    python3 tools/path_lifecycle_summary.py                  # 读 reports/checkpoint/ 下最新一份 JSON
    python3 tools/path_lifecycle_summary.py --json-file x.json
    python3 tools/path_lifecycle_summary.py --dry-run        # 用内置样例，完全不联网
    python3 tools/path_lifecycle_summary.py --instance <id>  # 现场采集（需实例可连、会产生 SSH 连接）

设计取舍：默认**离线**读取已有 JSON 产物，不主动连接实例；只有显式给
``--instance`` 才会调用 checkpoint.py 去采集。这样避免误触实例。
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT = ROOT / "tools" / "checkpoint.py"
REPORT_DIR = ROOT / "reports" / "checkpoint"
OUT = ROOT / "reports" / "路径生命周期速查表.md"

COLUMNS = ["路径名", "介质类型", "stop-start", "reboot", "save-image新建", "terminate"]

# JSON 键 → 表格列取值键（优先英文键，回退中文键）
FIELD_MAP = {
    "stop-start": ("stop_start", "stop保留"),
    "reboot": ("reboot", "reboot保留"),
    "save-image新建": ("save_image_new", "saveimage保留"),
    "terminate": ("terminate", "terminate保留"),
}


def _pick(item: dict, keys: tuple[str, ...], default: str = "-") -> str:
    for k in keys:
        v = item.get(k)
        if v not in (None, ""):
            return str(v)
    return default


def _fmt(v: str) -> str:
    """把风险点加粗：丢失要醒目，唯一永久保留也要醒目。"""
    if v == "丢失":
        return "**丢失**"
    if v == "保留":
        return "保留"
    return v


def load_paths(args: list[str]) -> tuple[list[dict], str]:
    """按优先级获取 paths：--json-file > --instance > --dry-run > 最新报告。"""
    if "--json-file" in args:
        p = Path(args[args.index("--json-file") + 1])
        if not p.is_absolute():
            p = ROOT / p
        data = json.loads(p.read_text(encoding="utf-8"))
        return list(data.get("paths") or []), f"指定文件 `{p.relative_to(ROOT) if p.is_relative_to(ROOT) else p}`"

    if "--instance" in args:
        ident = args[args.index("--instance") + 1]
        cmd = [sys.executable, str(CHECKPOINT), "--instance", ident, "--json"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode not in (0, 1):
            raise SystemExit(f"checkpoint.py 采集失败 rc={r.returncode}: {(r.stderr or r.stdout)[:300]}")
        data = json.loads(r.stdout)
        return list(data.get("paths") or []), f"现场采集 `{ident}`"

    if "--dry-run" in args:
        cmd = [sys.executable, str(CHECKPOINT), "--dry-run", "--json"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode not in (0, 1):
            raise SystemExit(f"checkpoint.py --dry-run 失败 rc={r.returncode}: {(r.stderr or r.stdout)[:300]}")
        data = json.loads(r.stdout)
        return list(data.get("paths") or []), "内置样例（--dry-run）"

    files = sorted(REPORT_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        raise SystemExit(
            "找不到 JSON 来源。请任选一种：\n"
            "  python3 tools/checkpoint.py --dry-run --json > /tmp/cp.json && "
            "python3 tools/path_lifecycle_summary.py --json-file /tmp/cp.json\n"
            "  python3 tools/path_lifecycle_summary.py --dry-run\n"
            "  python3 tools/path_lifecycle_summary.py --instance <id>"
        )
    # 跳过改动前生成的旧报告（它们没有 paths 字段）
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("paths"):
            return list(data["paths"]), f"最新报告 `{f.relative_to(ROOT)}`"
    raise SystemExit(
        f"{REPORT_DIR.relative_to(ROOT)}/ 下的 {len(files)} 份 JSON 都没有 paths 字段"
        "（是 checkpoint.py 改动前生成的）。请重新采集，或用 --dry-run / --json-file 指定。"
    )


def render(paths: list[dict], source: str) -> str:
    if not paths:
        raise SystemExit("paths 字段为空，无法生成速查表")

    keep_terminate = [p for p in paths if _pick(p, FIELD_MAP["terminate"]) == "保留"]
    lose_save_image = [p for p in paths if _pick(p, FIELD_MAP["save-image新建"]) == "丢失"]
    media_kinds = sorted({_pick(p, ("media", "介质")) for p in paths})

    lines: list[str] = []
    lines.append("# 路径生命周期速查表")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ")
    lines.append(f"> 数据来源：{source}  ")
    lines.append(f"> 路径总数：**{len(paths)}**  ")
    lines.append("> 探针定义：`lab/probe_storage.sh` · 实测依据：`reports/audit-storage-2026-10-26.md`")
    lines.append("")
    lines.append("## 速查表")
    lines.append("")
    lines.append("| " + " | ".join(COLUMNS) + " |")
    lines.append("|" + "|".join(["---"] * len(COLUMNS)) + "|")
    for p in paths:
        name = _pick(p, ("path",))
        desc = _pick(p, ("desc",), "")
        label = f"`{name}`" + (f"（{desc}）" if desc else "")
        row = [
            label,
            _pick(p, ("media", "介质")),
            _fmt(_pick(p, FIELD_MAP["stop-start"])),
            _fmt(_pick(p, FIELD_MAP["reboot"])),
            _fmt(_pick(p, FIELD_MAP["save-image新建"])),
            _fmt(_pick(p, FIELD_MAP["terminate"])),
        ]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append("**一句话：stop / reboot / 存镜像都不丢系统盘，只有 terminate 会全删；"
                 "数据盘只跟着这台实例活；唯一跨实例永久存在的只有 `/model`。**")
    lines.append("")
    lines.append("### 三类介质")
    lines.append("")
    for m in media_kinds:
        n = sum(1 for p in paths if _pick(p, ("media", "介质")) == m)
        lines.append(f"- **{m}**：{n} 条路径")
    lines.append("")
    lines.append("### 两条例外（最容易踩坑）")
    lines.append("")
    if lose_save_image:
        names = "、".join(f"`{_pick(p, ('path',))}`" for p in lose_save_image)
        lines.append(f"- **存镜像会丢**：{names} —— 新建实例后只剩 lost+found，"
                     "数据盘内容必须 `csctl pull` 或转存到 `/model` 才能带走。")
    else:
        lines.append("- 无“存镜像丢失”的路径。")
    if keep_terminate:
        names = "、".join(f"`{_pick(p, ('path',))}`" for p in keep_terminate)
        lines.append(f"- **terminate 后仍存活**：{names} —— 唯一跨实例的持久层，"
                     "但整盘只读，只能读不能写。")
    else:
        lines.append("- 无“terminate 后存活”的路径。")
    lines.append("")
    lines.append("### 落盘策略")
    lines.append("")
    lines.append("```")
    lines.append("要跨实例保留的数据  →  先 pull 到本地，或写进 /model（只读，实际不可写）")
    lines.append("单次任务产物        →  /root/ComfyUI/output 够用，terminate 前记得 pull")
    lines.append("20G 数据盘          →  只当临时暂存，不要当归档，存镜像带不走")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    paths, source = load_paths(args)
    md = render(paths, source)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(md, encoding="utf-8")
    print(f"已生成 {OUT}  （{len(paths)} 条路径，来源：{source}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
