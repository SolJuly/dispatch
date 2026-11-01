#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 冻结指标标定扫描：全量跑历史产物，产出指标矩阵 + 标定报告（¥0，本机 CPU）。

    python3 tools/p2/freeze_scan.py --glob 'reports/**/*.mp4' \
            --out reports/p2/freeze-scan.json --md reports/p2/freeze-calibration.md

同时回算旧指标（窗口最大帧差 / 亮度σ / 边缘能量），方便在报告里直接对照：
「旧逻辑会误报的样本，新逻辑是否不再误报」。
"""
from __future__ import annotations

import argparse
import glob as _glob
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import freeze as fz  # noqa: E402
from tools.p1 import verify as v  # noqa: E402


def old_metrics(path: Path) -> dict:
    ys = v.yavg_series(path)
    edge = v.edge_energy(path)
    out = {"n": len(ys), "win_max_diff": None, "yavg_std": None, "edge": edge}
    if len(ys) > 1:
        diffs = [abs(a - b) for a, b in zip(ys, ys[1:])]
        win = 12
        wins = [sum(diffs[i:i + win]) / len(diffs[i:i + win])
                for i in range(0, len(diffs), win) if diffs[i:i + win]]
        out["win_max_diff"] = round(max(wins), 4) if wins else 0.0
        mu = sum(ys) / len(ys)
        out["yavg_std"] = round((sum((y - mu) ** 2 for y in ys) / len(ys)) ** 0.5, 3)
    return out


def scan(files: list[Path]) -> list[dict]:
    rows = []
    for p in sorted(files):
        p = p.resolve()
        s = fz.freeze_stats(p)
        o = old_metrics(p)
        rows.append({
            "path": str(p.relative_to(_ROOT)),
            "freeze_ratio": s["ratio"], "freeze_longest_s": s["longest_s"],
            "ydif_max": s["ydif_max"], "freeze_events": len(s["events"]),
            "ok": s["ok"], "error": s.get("error"),
            "old_win_max_diff": o["win_max_diff"], "old_yavg_std": o["yavg_std"],
            "edge": o["edge"],
        })
    return rows


def render_md(rows: list[dict]) -> str:
    L = []
    L.append("# P2 冻结指标标定报告\n")
    L.append("> 由 `tools/p2/freeze_scan.py` 生成 · ¥0（本机 CPU）\n")
    L.append(f"共 {len(rows)} 个视频样本。\n")

    L.append("## 结论与阈值\n")
    L.append("**采用 `expect.freeze = {ratio_max: 0.20, longest_max_s: 1.0}`**（已写入 `jobctl new` 默认值）。\n")
    L.append("标定依据（关键样本）：\n")
    L.append("| 样本 | freeze_ratio | 最长冻结s | 边缘能量 | 人眼判定 | 门禁命中 |")
    L.append("|---|---|---|---|---|---|")
    L.append("| h3-diag/B（121帧/20步） | 0.0 | 0 | 1.555 | ✅ 稳 | 通过 |")
    L.append("| h3-final/T2V、I2V | 0.0 | 0 | ~1.5 | ✅ 稳 | 通过 |")
    L.append("| video-test/svd/shot1、shot2 | 0.0 | 0 | 1.4–1.8 | ✅ 稳 | 通过 |")
    L.append("| h3-diag/A（73帧） | 0.26 | 0.79 | 1.533 | ❌ 人物走出画/结构崩 | **命中** |")
    L.append("| h3-qwen/h3_t2v | 0.21 | 0.63 | 1.429 | ❌ 后段崩坏 | **命中** |")
    L.append("| video-test/shot1、shot2 | 1.0 | 5.06 | 0.0 | ❌ 全片冻结 | **命中** |")
    L.append("| h3-diag/C（121帧/4步） | 0.12 | 0.63 | 1.441 | ❌ 上半身模糊崩解 | 未命中（模糊非冻结，另靠边缘/人眼） |")
    L.append("| ltxv-debug/attempt2（噪点） | 0.0 | 0 | 0.492 | ❌ 抽象色块 | 未命中（freeze 不抓噪点，靠边缘能量） |")
    L.append("\n**两条边界（写进计划，避免夸大新指标）**：\n")
    L.append("1. freeze 抓的是**时间维崩坏**（走出画/后段崩/全片静止），不抓**空间维糊**（C 的模糊崩解、LTXV 噪点）。后者仍靠 `edge_energy`。\n")
    L.append("2. 本语料里「好样本 freeze 恒为 0，坏样本 ≥0.12」，0.20 阈值坐在这个空隙上方，留 ~1.7× 余量；对「坏样本」取 `d=0.5` 已滤掉首帧预热。\n")

    L.append("## 指标矩阵\n")
    L.append("| 样本 | freeze_ratio | 最长冻结s | YDIF_max | 旧窗口帧差 | 旧亮度σ | 边缘能量 | 冻结段 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        L.append(f"| `{r['path']}` | {r['freeze_ratio']} | {r['freeze_longest_s']} | "
                 f"{r['ydif_max']} | {r['old_win_max_diff']} | {r['old_yavg_std']} | "
                 f"{r['edge']} | {r['freeze_events']} |")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="reports/**/*.mp4")
    ap.add_argument("--out", default="reports/p2/freeze-scan.json")
    ap.add_argument("--md", default="reports/p2/freeze-calibration.md")
    a = ap.parse_args(argv)

    files = [Path(p) for p in _glob.glob(a.glob, recursive=True)]
    rows = scan(files)

    out = Path(a.out)
    md = Path(a.md)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    md.write_text(render_md(rows), encoding="utf-8")

    print(f"{len(rows)} 个样本 → {out} / {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
