#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 20 连测的产物与指标发布到 nginx 静态目录 /srv/dsh-pub/bench/。

产物：`/pub/bench/<workflow>/<case>/<png>`、`/pub/bench/<workflow>/metrics.json`、
      `/pub/bench/index.json`（两个工作流的汇总）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "reports/p2/p21/bench"
EVID = ROOT / "reports/p2/p21/evidence"
PUB = Path("/srv/dsh-pub/bench")


def ensure_rw() -> None:
    subprocess.run(["mount", "-o", "remount,rw", "/"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def latest(pattern: str) -> Path | None:
    files = sorted(EVID.glob(pattern))
    return files[-1] if files else None


def main() -> int:
    ensure_rw()
    PUB.mkdir(parents=True, exist_ok=True)
    summary = {}
    for wf_dir in sorted(BENCH.iterdir()):
        if not wf_dir.is_dir():
            continue
        ok = 0
        total = 0
        for case in sorted(wf_dir.iterdir()):
            if not case.is_dir():
                continue
            dst = PUB / wf_dir.name / case.name
            dst.mkdir(parents=True, exist_ok=True)
            for p in case.glob("*.png"):
                shutil.copy2(p, dst / p.name)
                ok += 1
            total += 1
            dst.chmod(0o755)
        m = latest(f"bench-{wf_dir.name}-*.json")
        if m:
            data = json.loads(m.read_text(encoding="utf-8"))
            (PUB / wf_dir.name / "metrics.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            summary[wf_dir.name] = {k: data.get(k) for k in
                                    ("total", "pass", "success_rate", "inference_p50_s",
                                     "inference_p95_s", "vram_peak_mb", "est_cost",
                                     "elapsed_min", "seed_check")}
        print(f"  ✓ {wf_dir.name}: {total} cases / {ok} images")
    (PUB / "index.json").write_text(
        json.dumps({"workflows": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    for p in PUB.rglob("*"):
        p.chmod(0o755 if p.is_dir() else 0o644)
    print(f"[ok] {PUB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
