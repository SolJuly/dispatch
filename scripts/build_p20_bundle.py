#!/usr/bin/env python3
"""重建 P2.0 验收包 zip（reports/p2/p20/p20-验收包.zip）。

打包内容：交付报告、设计文档、审查/硬编码清单、调用关系图、验收证据、
registry 配置、spec、三张真实产物，以及 README.md / 下载与预览.md。

用法：python3 scripts/build_p20_bundle.py
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "reports/p2/p20/p20-验收包.zip"
NAME = "p20-验收包"

README = """# P2.0 Workflow / Model Registry 验收包

本包为 P2.0 第一阶段的完整验收材料：报告、设计、审查、证据、配置与真机产物。

## 公网浏览/下载

- 首页：https://123.207.74.155/pub/p20/
- 本包最新版：https://123.207.74.155/pub/p20/bundle.zip

## 目录

| 路径 | 内容 |
|---|---|
| `P2.0-交付报告.md` | 5 项任务 × 10 项交付字段 |
| `P2.0-Workflow-Model-Registry-设计.md` | DB schema / REST API / 模块划分 / JSON 模板 |
| `01-调用关系审查.md` | UI / 脚本 / ComfyUI API 调用关系与输入输出 |
| `02-硬编码清单.md` | 固定模型 / 节点 / 参数逐条定位 |
| `调用关系图.png` / `.svg` | 调用关系图（1460×980） |
| `registry/` | 4 个模型 + 7 个工作流 + 兼容性矩阵配置 |
| `workflows/` | 新增的 portrait 图（任务 5A） |
| `specs/` | 4 个作业 spec（含故障注入用例） |
| `artifacts/` | 3 张真实 GPU 产物 PNG |
| `test-evidence/` | 测试汇总、作业台账、故障注入、API 响应 |
| `task5b-*.sha256` / `task5b-hash-proof.txt` | 只改配置不动核心代码的哈希证明 |
| `下载与预览.md` | 公网链接索引 |

## 结论速览

- P2.0 测试 60/60；P2 42/42；P1 13 例 12 PASS / 1 SKIP / 0 FAIL
- `p20ctl validate`：7 workflows / 4 models，verified 4、equivalent 2、experimental 1、incompatible 0
- 3 个真实作业全部 DONE + PASS，总花费 ¥0.2150
- 故障注入：`SUBMITTED → FAILED_RUN → RELEASING → FAILED`，实例已销毁，无残留
- 任务 5B 只新增配置：37 个核心文件 sha256 前后完全一致
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="p20zip-"))
    stage = tmp / NAME
    stage.mkdir()

    def put(src: str, dst: str) -> None:
        s = ROOT / src
        if not s.exists():
            print(f"  ! 缺失 {src}")
            return
        d = stage / dst
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)

    put("reports/p2/p20/P2.0-交付报告.md", "P2.0-交付报告.md")
    put("reports/p2/p20/P2.0-Workflow-Model-Registry-设计.md", "P2.0-Workflow-Model-Registry-设计.md")
    put("reports/p2/p20/01-调用关系审查.md", "01-调用关系审查.md")
    put("reports/p2/p20/02-硬编码清单.md", "02-硬编码清单.md")
    put("reports/p2/p20/调用关系图.png", "调用关系图.png")
    put("reports/p2/p20/调用关系图.svg", "调用关系图.svg")
    put("reports/p2/p20/task5b-hash-proof.txt", "task5b-hash-proof.txt")
    put("reports/p2/p20/task5b-core-before.sha256", "task5b-core-before.sha256")
    put("reports/p2/p20/task5b-core-after.sha256", "task5b-core-after.sha256")
    put("reports/p2/p20/下载与预览.md", "下载与预览.md")
    put("reports/p2/p20/test-evidence", "test-evidence")
    put("reports/p2/p20/specs", "specs")
    put("registry/models", "registry/models")
    put("registry/workflows", "registry/workflows")
    put("registry/compat.json", "registry/compat.json")
    put("workflows/qwen_image_t2i_portrait.api.json", "workflows/qwen_image_t2i_portrait.api.json")
    put("reports/p1/jobs/p1-20261101T2121-c82708/artifacts/01-p1-20261101T2121-c82708_00001_.png",
        "artifacts/task4-apple-1024x1024.png")
    put("reports/p1/jobs/p1-20261101T2126-9fb4ed/artifacts/01-p1-20261101T2126-9fb4ed_00001_.png",
        "artifacts/task5-portrait-768x1024.png")
    put("reports/p1/jobs/p1-20261101T2131-4d366a/artifacts/01-p1-20261101T2131-4d366a_00001_.png",
        "artifacts/task5b-landscape-1024x768.png")

    (stage / "README.md").write_text(README, encoding="utf-8")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    subprocess.run(["zip", "-r", "-q", "-X", str(OUT), NAME], cwd=tmp, check=True)
    shutil.rmtree(tmp, ignore_errors=True)

    n = len(subprocess.run(["unzip", "-l", str(OUT)], capture_output=True, text=True).stdout)
    size = OUT.stat().st_size / 1048576
    print(f"[ok] {OUT.relative_to(ROOT)}  {size:.1f} MB")
    assert n > 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
