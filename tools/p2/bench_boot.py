#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 建机耗时基准（薄封装）：实际逻辑在 tools/p2/image.py 的 verify。

    python3 tools/p2/bench_boot.py --runs 3 --layer all --gpu 4090
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.p2 import image  # noqa: E402


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="bench_boot")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--layer", default="all", choices=["all", "base", "media"])
    ap.add_argument("--gpu", default="4090")
    a = ap.parse_args(argv)
    from compshare.cs import CS
    return image.cmd_verify(CS(), a)


if __name__ == "__main__":
    raise SystemExit(main())
