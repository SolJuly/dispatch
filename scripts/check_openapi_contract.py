#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约一致性检查：docs/openapi.json 声明的端点，实现里是否真的有。

为什要做这个：2026-11-08 检查发现 5 个端点照契约写必然 404——
  /api/p2/status        实现只有 /api/status（少了 p2 段）
  /api/p2/presets       代码里连字样都没有
  /api/p2/share/{id}    P2.2 计划，未实现
  /api/p2/queue/jobs/{id}  旧契约残留，与 /api/p2/jobs/{id} 重复
  /api/p2/jobs/{id}/artifacts/{name}  P2.2 计划，未实现

照契约写的前端会踩坑。处理方式：
  · 能补的补（status / presets 已补）
  · 标注 x-status: planned 的视为「计划中」，本脚本跳过
  · 其余必须真实可达

用法：
  python3 scripts/check_openapi_contract.py            # 对着本机 8081 检查
  python3 scripts/check_openapi_contract.py --base URL
退出码：0 全部一致；1 有端点在契约里但没有实现
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
SPEC = PROJ / "docs" / "openapi.json"

# 探测时用的假值。真路由会因为参数不存在返回 404，那是「实现存在但对象不存在」，
# 与「路由根本不存在」是两件事，必须区分——所以不能只看状态码。
SAMPLE = {
    "job_id": "p1-20991231T2359-aaaaaa",
    "name": "x.png",
    "key": "zimage_turbo",
}


def probe(base: str, token: str, path: str, method: str) -> tuple[bool, str]:
    """返回 (是否可达, 说明)。可达 = 响应体不是「路径不存在」类的通用 404。"""
    url = f"{base.rstrip('/')}/{token}{path}"
    for k, v in SAMPLE.items():
        url = url.replace("{" + k + "}", v)
    req = urllib.request.Request(url, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return True, f"{r.status}"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(1200).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        # 路由存在时，框架返回的是结构化的业务错误；路由缺失时是通用 404 页面
        if e.code == 404 and ("路径不存在" in body or "<title>404</title>" in body):
            return False, "404 路由缺失"
        return True, f"{e.code}（路由存在，对象/参数问题）"
    except Exception as e:  # noqa: BLE001
        return False, f"连接失败 {type(e).__name__}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8081")
    ap.add_argument("--token", default="")
    args = ap.parse_args()

    if not args.token:
        tf = PROJ / ".share_token"
        args.token = tf.read_text(encoding="utf-8").strip() if tf.exists() else ""

    spec = json.loads(SPEC.read_text(encoding="utf-8"))["paths"]
    bad, planned, ok = [], [], 0
    for path in sorted(spec):
        for method, op in spec[path].items():
            if method not in ("get", "post", "put", "delete"):
                continue
            label = f"{method.upper():6s} {path}"
            if op.get("x-status") == "planned":
                planned.append(label)
                continue
            reachable, why = probe(args.base, args.token, path, method)
            if reachable:
                ok += 1
            else:
                bad.append((label, why))

    print(f"契约端点：{len(spec)} 个路径")
    print(f"  已实现且可达：{ok}")
    print(f"  计划中（x-status: planned）：{len(planned)}")
    for l in planned:
        print(f"    ○ {l}")
    if bad:
        print(f"\n✗ 契约有、实现无：{len(bad)}")
        for l, why in bad:
            print(f"    ✗ {l}  — {why}")
        return 1
    print("\n✓ 契约与实现一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
