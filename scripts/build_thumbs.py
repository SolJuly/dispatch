#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给历史作业批量补缩略图。

为什么需要：缩略图是「首次被请求时按需生成」的，所以老作业要等列表刷新
到它们才生成。但列表只取最近 100 条、且按 created_at 倒序 —— 更老的
永远轮不到。清理了 .trash 之后要恢复时同理。

用法：
  python3 scripts/build_thumbs.py            # 预览：哪些作业缺缩略图
  python3 scripts/build_thumbs.py --apply    # 真生成
  python3 scripts/build_thumbs.py --apply --trash   # 连 .trash 里的历史作业一起

产出落在 <job>/thumbs/，并通过硬链接镜像到 share/，无需再跑 sync_share。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

from webui import api  # noqa: E402

JOBS = PROJ / "reports" / "p1" / "jobs"


def scan_job_dirs(include_trash: bool) -> list[Path]:
    out = []
    if not JOBS.is_dir():
        return out
    out.extend(d for d in sorted(JOBS.iterdir()) if d.is_dir() and not d.name.startswith("."))
    if include_trash:
        trash = JOBS / ".trash"
        if trash.is_dir():
            out.extend(d for d in sorted(trash.iterdir()) if d.is_dir())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正生成（默认只预览）")
    ap.add_argument("--trash", action="store_true", help="连 .trash 里的作业一起处理")
    ap.add_argument("--width", type=int, default=api.THUMB_W, help="缩略图宽度")
    args = ap.parse_args()

    dirs = scan_job_dirs(args.trash)
    print(f"扫描 {len(dirs)} 个作业目录 ({'含' if args.trash else '不含'} .trash)")

    todo, done, noart = [], 0, 0
    for d in dirs:
        adir = d / "artifacts"
        if not adir.is_dir():
            noart += 1
            continue
        arts = [p for p in sorted(adir.iterdir())
                if p.is_file() and p.suffix.lower() in api.ART_SUFFIX]
        if not arts:
            noart += 1
            continue
        t = api.thumb_for(arts[0], d, width=args.width)
        if t:
            done += 1
        else:
            todo.append((d.name, arts[0].name))

    print(f"\n已生成/已存在：{done}")
    print(f"无产物：{noart}")
    if todo:
        print(f"失败：{len(todo)}")
        for jid, name in todo[:10]:
            print(f"  ✗ {jid}  {name}")
    return 0 if not todo else 1


if __name__ == "__main__":
    sys.exit(main())
