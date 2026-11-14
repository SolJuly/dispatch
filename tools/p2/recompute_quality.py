#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用当前评分表重算历史 manifest 的 quality 字段（不动别的字段）。

## 为什么需要回填而不是重跑

2026-11-14 修了三个标定错误（见 `quality.py` 的注释）：

  ① `every=3` 抽帧 → YDIF 虚高 75%（运动量被放大），且会漏掉坏帧
  ② 清晰度用全局均值 → 主体小、背景空时把"构图失败"记成"稍微糊"
  ③ 缺两条判据：覆盖率（画面多少比例是空的）、崩坏帧（突然变纯色）

这三条都是**评分代码的错**，不是产物的错。产物文件还在，
重跑一次作业要 ¥0.1–3，而重算只要几秒。

## 只改 quality，不碰别的

`manifest.json` 里的 `verdict`（PASS/FAIL）来自 verify.py 的七项技术判据，
与质量分是两套东西（见 quality.py 模块注释）。回填**不得**改它 ——
否则等于用"内容好不好"改写"文件有效没效"的历史结论。

## 用法

    python3 tools/p2/recompute_quality.py --dry-run   # 只看会改什么
    python3 tools/p2/recompute_quality.py             # 真改
    python3 tools/p2/recompute_quality.py --since 20261114
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

JOBS = ROOT / "reports" / "p1" / "jobs"


def _job_date(d: Path) -> str:
    try:
        j = json.loads((d / "job.json").read_text(encoding="utf-8"))
        return str(j.get("created_at") or "")[:10].replace("-", "")
    except (OSError, ValueError):
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重算历史 manifest 的质量分")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--since", default="", help="只处理这个日期之后（YYYYMMDD）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    a = ap.parse_args(argv)

    from tools.p2 import quality

    changed, skipped, failed = [], 0, []
    for d in sorted(JOBS.iterdir()):
        mf = d / "manifest.json"
        if not mf.is_file():
            continue
        if a.since and _job_date(d) < a.since:
            skipped += 1
            continue
        try:
            man = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        old_q = man.get("quality") or {}
        if not old_q:
            skipped += 1
            continue

        arts = [p for p in sorted((d / "artifacts").glob("*")) if p.is_file()]
        if not arts:
            skipped += 1
            continue
        kind = old_q.get("artifacts", [{}])[0].get("kind") if old_q.get("artifacts") else "video"
        try:
            new_q = quality.assess_job(arts, kind=kind, mode=old_q.get("mode", "report"))
        except Exception as e:                        # noqa: BLE001
            failed.append((d.name, f"{type(e).__name__}: {e}"))
            continue

        newd = new_q.as_dict()
        old_score = old_q.get("overall")
        new_score = newd.get("overall")
        if old_score == new_score and old_q.get("grade") == newd.get("grade"):
            skipped += 1
            continue

        changed.append((d.name, old_score, new_score,
                        old_q.get("grade"), newd.get("grade")))
        if not a.dry_run:
            man["quality"] = newd
            mf.write_text(json.dumps(man, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    if a.json:
        print(json.dumps({"changed": changed, "skipped": skipped,
                          "failed": failed}, ensure_ascii=False, indent=1))
        return 0

    tag = "（dry-run，未写盘）" if a.dry_run else ""
    for jid, o, n, og, ng in changed:
        arrow = "↓" if (n or 0) < (o or 0) else "↑"
        print(f"  {jid}  {o} {og} {arrow} {n} {ng}")
    print(f"\n改了 {len(changed)} 条 · 跳过 {skipped} 条 · 失败 {len(failed)} 条 {tag}")
    for jid, err in failed:
        print(f"  ✗ {jid}: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
