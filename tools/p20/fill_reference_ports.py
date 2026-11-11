#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 MiniMax H3 的参考输入接口补满：9 图 / 3 视频 / 3 音频 / 3 视频音频。

## 为什么要补满（2026-11-11 你的要求）

> 他能上传 9 张参考图，那你就得把上传 9 张参考图的窗口摆出来，
> 但是对平民用户来说不需要这么多参考图，但是窗口一定要有，而且还得能跑。

所以策略是：

  **图里把全部位置都接上，registry 里全部声明成 `required: false`。**

- 位置全接 → 用户想传 9 张就有 9 个框；以后加功能不用再改图
- 全部可选 → 平民用户不填也能跑（`min` 是 0，官方节点默认就不要求）

不做成"按需动态加位置"的原因：ComfyUI 的 AUTOGROW 槽位是**图的静态结构**，
`ref_images.ref_image_0..8` 必须在提交前全部存在于 graph 里。
提交时按 `ref_image_size` 组合，空槽位传空字符串即可 —— 节点会忽略。

## 空槽位怎么填

`LoadImage.image` 是 COMBO（候选=已上传文件名）。空串 `""` 在 ComfyUI 里
表示"未选择"，节点会跳过。实测 `LoadAudio.audio` 同理。

但**不能给一个不存在的文件名** —— 那会让整张图校验失败（ComfyUI 会去打开它）。
所以空槽位统一填空串，不用占位符文件名。

## 用法

    python3 tools/p20/fill_reference_ports.py --dry-run
    python3 tools/p20/fill_reference_ports.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WF_DIR = ROOT / "workflows" / "pack"
REG_DIR = ROOT / "registry" / "workflows"

# 四类参考的位置上限（来自 objectinfo 的 AUTOGROW max）
SLOTS = {
    "ref_images":       {"max": 9, "loader": "LoadImage",  "field": "image",
                         "input": "image",  "type": "image", "prefix": "ref_image"},
    "ref_videos":       {"max": 3, "loader": "LoadVideo",  "field": "file",
                         "input": "file",   "type": "video", "prefix": "ref_video"},
    "ref_audios":       {"max": 3, "loader": "LoadAudio",  "field": "audio",
                         "input": "audio",  "type": "audio", "prefix": "ref_audio"},
    "ref_video_audios": {"max": 3, "loader": "LoadAudio",  "field": "audio",
                         "input": "audio",  "type": "audio", "prefix": "ref_video_audio"},
}

# 参数名 → 现有绑定。这三个槽位在旧 registry 里已经占用了 image / image_2，
# 补位置时不能重名。
EXISTING = {
    "ref_images": ["image", "image_2"],     # ref_image_0 / ref_image_1 已有
}

DESC = {
    "image": "参考图。**prompt 里用 <Picture N> 引用第 N 张才会生效**。"
             "不填也能跑；填几张就用几张。",
    "video": "参考视频。prompt 里用 <Video N> 引用。可选。",
    "audio": "参考音频。**prompt 里用 <Audio N> 引用才会生效**。可选。"
             "支持 WAV/MP3/FLAC，上限 64MB。",
}


def _next_node_id(graph: dict) -> int:
    ids = [int(k) for k in graph if str(k).isdigit()]
    return (max(ids) + 1) if ids else 1


def fill_workflow(wf_path: Path, reg_path: Path, *, dry_run: bool) -> dict:
    """给一个工作流补满参考位置。返回统计。"""
    graph = json.loads(wf_path.read_text(encoding="utf-8"))
    reg = json.loads(reg_path.read_text(encoding="utf-8"))

    # 找 ReferenceToVideo 节点
    ref_node = None
    for nid, n in graph.items():
        if isinstance(n, dict) and "ReferenceToVideo" in str(n.get("class_type") or ""):
            ref_node = nid
            break
    if ref_node is None:
        return {"workflow": wf_path.stem, "skip": "没有 ReferenceToVideo 节点"}

    ins = graph[ref_node].setdefault("inputs", {})
    params = reg.setdefault("params", {})
    next_id = _next_node_id(graph)
    added_nodes = added_params = 0

    for group, spec in SLOTS.items():
        prefix = spec["prefix"]
        used_names = set(EXISTING.get(group, []))
        for i in range(spec["max"]):
            slot = f"{group}.{prefix}_{i}"
            if slot in ins:
                continue                      # 已经接了
            # 生成一个空的加载节点
            nid = str(next_id); next_id += 1
            graph[nid] = {"class_type": spec["loader"],
                          "inputs": {spec["field"]: ""}}
            ins[slot] = [nid, 0]
            added_nodes += 1

            # registry 参数名：跳过已有的 image / image_2
            pname = f"{prefix}_{i + 1}"
            if pname in used_names or pname in params:
                continue
            params[pname] = {
                "type": spec["type"],
                "required": False,            # 全部可选 → 平民用户不填也能跑
                "description": DESC[spec["type"]],
                "bindings": [{"node": nid, "input": spec["field"]}],
            }
            added_params += 1

    if not dry_run and (added_nodes or added_params):
        wf_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
        reg_path.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    return {"workflow": wf_path.stem, "ref_node": ref_node,
            "nodes": added_nodes, "params": added_params}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="补满 MiniMax H3 参考输入接口")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="只处理一个 workflow stem")
    a = ap.parse_args(argv)

    rows = []
    for wf_path in sorted(WF_DIR.glob("*.api.json")):
        stem = wf_path.name[:-len(".api.json")]
        if a.only and stem != a.only:
            continue
        # registry 文件名带 `pack_` 前缀，而 graph 文件名不带 ——
        # 两处命名不一致是历史原因（pack 是导入时的来源标记）。
        # 别按 stem 硬拼，直接问 registry 这个工作流用的是哪个文件。
        reg_path = None
        for cand in (REG_DIR / f"{stem}.json", REG_DIR / f"pack_{stem}.json"):
            if cand.exists():
                reg_path = cand
                break
        if reg_path is None:
            continue
        r = fill_workflow(wf_path, reg_path, dry_run=a.dry_run)
        if r.get("skip"):
            continue
        rows.append(r)

    if not rows:
        print("没有需要补的工作流（或者都没有 ReferenceToVideo 节点）")
        return 0

    print(f"{'工作流':<48}{'ref节点':>8}{'新增加载节点':>12}{'新增参数':>10}")
    print("=" * 80)
    for r in rows:
        print(f"{r['workflow'][:46]:<48}{r['ref_node']:>8}{r['nodes']:>12}{r['params']:>10}")
    print(f"\n{len(rows)} 个工作流"
          + ("（演练，未写入）" if a.dry_run else " 已写入"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
